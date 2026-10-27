"""FMD 1.0 through Fenics: vol surfaces polled from the Fenics XML poster.

The desk's Excel query fetchFenicsVols (VOL_PUBLISHER_RKUSD) posts, per pair,

    xml=<url-encoded gfi_message>   to <server>/xmlposter

with action RATE_FETCH / get_vols, the horizon date and a scenario (the one
FMD 1.0 is fed into, "fenicsVols"). The answer has one node per tenor and
strategy with the fields Maturity ("1 Week", "3 Months", "1 Year"), VolType
(ATM, RR, S), PctDelta (0.25, 0.10) and Volity ("bid/offer", or one value),
in decimals (0.0605 for 6.05 vol): the workbook multiplies by 100 (scale).
A pair Fenics has no vols for is skipped for an hour instead of asked again
every round.

Each row becomes the same entry format as the FIX and WebAPI feeds, so the
QuoteBook, the market view, alerts and Follow FMD work unchanged. There is
no streaming: the whole surface is fetched again every poll interval.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable
from xml.sax.saxutils import escape

from ..conventions import split_pair
from ..weights_export import post

log = logging.getLogger("volmarker.fmd")

UNITS = {"day": "D", "week": "W", "month": "M", "year": "Y"}
STRATEGIES = {"RR": "RR", "S": "S", "BF": "S", "STR": "S"}


def get_vols_xml(pair: str, scenario: str, user: str, password: str, now: datetime | None = None) -> str:
    c1, c2 = split_pair(pair)
    now = (now or datetime.now()).astimezone()
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>', '<gfi_message version="2.0">', " <header>",
        "  <transactionId>0123456789</transactionId>",
        f"  <timestamp>{now.isoformat(timespec='seconds')}</timestamp>",
        f"  <username>{escape(user)}</username>", f"  <password>{escape(password)}</password>",
        " </header>", " <body>",
        '  <action function="RATE_FETCH" name="get_vols" version="1.0">',
        f'   <option name="horDate" value="{now.strftime("%d %b %Y")}"/>',
        f'   <option name="scenario" value="{escape(scenario)}"/>',
        '   <option name="data" ref="ccyPair"/>', "  </action>",
        '  <data name="ccyPair" format="NAME_VALUE">', '   <node name="">',
        f'    <field name="Currency" value="{c1.lower()}"/>', f'    <field name="CtrCcy" value="{c2.lower()}"/>',
        "   </node>", "  </data>", " </body>", "</gfi_message>"])


def norm_maturity(m: str | None) -> str | None:
    """'1 Day' -> ON, '2 Weeks' -> 2W, '18 Months' -> 18M, '1 Year' -> 1Y."""
    x = re.fullmatch(r"\s*(\d+)\s*(day|week|month|year)s?\s*", (m or "").lower())
    if not x:
        return None
    n, unit = int(x.group(1)), UNITS[x.group(2)]
    return "ON" if (n, unit) == (1, "D") else f"{n}{unit}"


def _num(s: str) -> float | None:
    try:
        return float(s.strip())
    except ValueError:
        return None


class NoVols(ValueError):
    """Fenics answered, but has no vols for the pair."""


def parse_vols(text: str, scale: float = 100) -> list[dict]:
    """Rows {tenor, ptype, bid, offer} in vol points from a get_vols answer;
    raises NoVols for an answer without vols, ValueError for an error."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"not XML: {text[:200]!r}") from e
    rows = []
    for node in root.iter("node"):
        f = {x.get("name"): x.get("value") for x in node.findall("field")}
        tenor, vtype, vol = norm_maturity(f.get("Maturity")), (f.get("VolType") or "").strip().upper(), f.get("Volity")
        if not tenor or not vtype or not vol:
            continue
        if vtype == "ATM":
            ptype = "ATM"
        elif vtype in STRATEGIES and f.get("PctDelta"):
            d = _num(f["PctDelta"])
            if d is None:
                continue
            ptype = f"{STRATEGIES[vtype]}{round(d * 100 if d < 1 else d)}"
        else:
            continue
        parts = [_num(p) for p in vol.split("/")]
        bid, offer = parts[0], parts[1] if len(parts) > 1 else parts[0]
        if bid is None or offer is None:
            continue
        rows.append({"tenor": tenor, "ptype": ptype, "bid": bid * scale, "offer": offer * scale})
    if not rows:
        err = " ".join(t.strip() for t in root.itertext() if t.strip())
        if "error" in err.lower():                  # as the workbook checks: e.g. a wrong password
            raise ValueError(err[:200])
        raise NoVols(f"no vols in the answer{f': {err[:200]}' if err else ''}")
    return rows


def rows_to_entries(pair: str, rows: list[dict], brand: str, atm_ptype: str, ts: str) -> list[dict[int, str]]:
    """In the FIX tags the QuoteBook reads; the ATM gets the configured convention (Fenics' ATM is DNS)."""
    out = []
    for r in rows:
        ptype = atm_ptype if r["ptype"] == "ATM" else r["ptype"]
        out.append({279: "1", 48: f"XML.{pair}.{r['tenor']}.{ptype}.{brand}", 17001: pair, 6217: r["tenor"],
                    423: ptype, 6960: brand, 60: ts, 132: f"{r['bid']}", 133: f"{r['offer']}",
                    631: f"{(r['bid'] + r['offer']) / 2}"})
    return out


class XmlPosterSession:
    """Polls every pair's surface; 'connected' while the last round had answers."""

    def __init__(self, url: str, user: str, password: str, pairs: Callable[[], list[str]],
                 scenario: str = "fenicsVols", interval: float = 60, brand: str = "FMD1",
                 atm_ptype: str = "DNS", timeout: float = 20, scale: float = 100, retry_missing: float = 3600,
                 on_entries: Callable[[list[dict[int, str]]], None] = lambda e: None,
                 on_status: Callable[[dict], None] = lambda s: None):
        self.url, self.user, self.password, self.pairs = url, user, password, pairs
        self.scenario, self.interval, self.brand, self.atm_ptype, self.timeout = scenario, interval, brand, atm_ptype, timeout
        self.scale, self.retry_missing = scale, retry_missing
        self.missing: dict[str, float] = {}           # pair -> when to ask again (monotonic)
        self.on_entries, self.on_status = on_entries, on_status
        self._stop, self._wake = threading.Event(), threading.Event()

    def fetch(self, pair: str) -> list[dict[int, str]]:
        text = post(self.url, get_vols_xml(pair, self.scenario, self.user, self.password), self.timeout)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return rows_to_entries(pair, parse_vols(text, self.scale), self.brand, self.atm_ptype, ts)

    def poll_once(self) -> tuple[int, list[str]]:
        """One round over all pairs: (pairs answered, errors). Pairs without vols
        are left out until retry_missing has passed."""
        ok, errors = 0, []
        for pair in self.pairs():
            if self._stop.is_set():
                break
            if time.monotonic() < self.missing.get(pair, 0):
                continue
            try:
                entries = self.fetch(pair)
            except NoVols:
                if pair not in self.missing:
                    log.info("Fenics has no vols for %s: asking again every %g min", pair, self.retry_missing / 60)
                self.missing[pair] = time.monotonic() + self.retry_missing
                continue
            except (OSError, ValueError, urllib.error.HTTPError) as e:
                errors.append(f"{pair}: {e}")
                continue
            ok += 1
            self.missing.pop(pair, None)
            self.on_entries(entries)
        return ok, errors

    def run_forever(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            ok, errors = self.poll_once()
            for e in errors[:3]:
                log.warning("Fenics get_vols %s", e)
            missing = sorted(self.missing)
            if ok:
                self.on_status({"connected": True, "no_vols": missing,
                                "last_error": f"{len(errors)} pairs failed, e.g. {errors[0]}" if errors else None})
                log.info("Fenics XML poll: %d pairs%s", ok, f", {len(errors)} failed" if errors else "")
            else:
                self.on_status({"connected": False, "no_vols": missing,
                                "last_error": errors[0] if errors else "no pairs with vols to poll"})
            # the next round is due interval after this one started; a new interval counts from there too
            while not self._stop.is_set():
                left = self.interval - (time.monotonic() - started)
                if left <= 0:
                    break
                self._wake.wait(left)
                self._wake.clear()

    def set_interval(self, seconds: float) -> None:
        self.interval = seconds
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
