"""Bloomberg market data for the pairs whose Setup prefers Bloomberg.

One trader starts it from the app (Bloomberg button, next to Fenics login) on a
PC with a Bloomberg Terminal and Bloomberg's blpapi package. That VolMarker then fetches, as often as the interval field next to
Fenics login says (the same as FMD 1.0, e.g. every 5 min), the ATM,
RR and BF of every tenor and delta of the pairs set to Bloomberg, and writes

    market/bbg/<PAIR>.json   the pair's Bloomberg quotes (brand BBG), as the FMD
                             feed writes market/<PAIR>.json
    market/_status_bbg.json  state + heartbeat, read by every app

so every VolMarker sees them. Pairs that prefer FMD are never fetched; the FMD
feed keeps fetching (and the leader keeps recording) the Bloomberg pairs too.

To stay within the Terminal's download limits a run stops itself at the next of
bbg.stop_at (18:00 and midnight, calendar_tz). Anyone can start it again at once
(someone staying late); it then stops at the next stop time. The first one in the
next morning starts it. Only one runs per data folder. In bbg.quiet_hours (18:00
to 07:00) Bloomberg pairs standing still is expected, so it raises no alert.

Tickers come from bbg.tickers: EURUSDV1M BGN Curncy (ATM), EURUSD25R1M (25D RR),
EURUSD25B1M (25D BF). An inverse pair (NOKCHF) is fetched as the standard pair
(CHFNOK), as FMD quotes it, and its RRs flipped by the market view as FMD's are.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .conventions import split_pair
from .liveness import Liveness
from .market import BBG
from .store import Store, write_shared
from .surface import all_marks, tenor_list
from .tz import localize, to_local

log = logging.getLogger("volmarker.bbg")

FEED_STALE = 15.0
STATUS_FILE = "_status_bbg.json"
from .fenics import DEFAULT_SECONDS, MIN_SECONDS      # the app's one fetch interval, for FMD 1.0 and Bloomberg


def now_iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).isoformat(timespec="seconds")


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class BlpClient:
    """Reference data (like BDP in Excel) through Bloomberg's blpapi package,
    from the Terminal on this PC (the Desktop API at localhost:8194)."""

    HOST, PORT = "localhost", 8194
    SERVICE = "//blp/refdata"
    TIMEOUT = 30.0                       # seconds for one request

    def __init__(self):
        try:
            import blpapi
        except ImportError as e:
            raise ValueError("The blpapi package is not installed on this PC (pip install --index-url="
                             "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi): "
                             f"{e}") from None
        self.blpapi = blpapi
        opts = blpapi.SessionOptions()
        opts.setServerHost(self.HOST)
        opts.setServerPort(self.PORT)
        self.session = blpapi.Session(opts)
        try:
            ok = self.session.start()
        except Exception as e:
            ok, why = False, e
        else:
            why = "is the Terminal running and logged in?"
        if not ok:
            raise ValueError(f"Could not connect to the Bloomberg Terminal on this PC: {why}")
        if not self.session.openService(self.SERVICE):
            self.close()
            raise ValueError(f"Could not open {self.SERVICE} on the Bloomberg Terminal")
        self.service = self.session.getService(self.SERVICE)

    def fetch(self, tickers: list[str], fields: list[str]) -> dict[str, dict[str, float | None]]:
        blpapi = self.blpapi
        req = self.service.createRequest("ReferenceDataRequest")
        for t in tickers:
            req.getElement("securities").appendValue(t)
        for f in fields:
            req.getElement("fields").appendValue(f)
        cid = self.session.sendRequest(req)
        out: dict[str, dict[str, float | None]] = {}
        deadline = time.monotonic() + self.TIMEOUT
        while True:
            if time.monotonic() > deadline:
                raise ValueError("Bloomberg did not answer within "
                                 f"{self.TIMEOUT:.0f} s")
            ev = self.session.nextEvent(500)
            kind = ev.eventType()
            if kind not in (blpapi.Event.PARTIAL_RESPONSE, blpapi.Event.RESPONSE):
                continue
            for msg in ev:
                if cid not in msg.correlationIds():
                    continue
                if msg.hasElement("responseError"):
                    err = msg.getElement("responseError")
                    raise ValueError(f"Bloomberg request failed: {err.getElementAsString('message')}")
                data = msg.getElement("securityData")
                for i in range(data.numValues()):
                    sec = data.getValueAsElement(i)
                    name = sec.getElementAsString("security")
                    vals: dict[str, float | None] = {f: None for f in fields}
                    if not sec.hasElement("securityError") and sec.hasElement("fieldData"):
                        fd = sec.getElement("fieldData")
                        for f in fields:
                            if fd.hasElement(f):
                                try:
                                    vals[f] = _num(fd.getElementAsFloat(f))
                                except Exception:
                                    vals[f] = None
                    out[name] = vals
            if kind == blpapi.Event.RESPONSE:
                return out

    def close(self) -> None:
        try:
            self.session.stop()
        except Exception:
            pass


def ticker_map(rec: dict, cfg: dict) -> dict[str, tuple[str, str, str]]:
    """ticker -> (tenor, mark, ptype) for every tenor and mark of the pair."""
    pair = rec["pair"]
    c1, c2 = split_pair(pair)
    std = c2 + c1 if rec.get("inverse") else pair
    src = (cfg.get("pair_source") or {}).get(pair) or cfg.get("source") or "BGN"
    tpl = cfg["tickers"]
    out = {}
    for t in tenor_list(rec):
        for m in all_marks(rec):
            if m == "atm":
                tick, ptype = tpl["atm"].format(pair=std, tenor=t, delta="", source=src), cfg.get("atm_type") or "DNS"
            else:
                kind, d = m[:2], m[2:]
                tick = tpl[kind].format(pair=std, tenor=t, delta=d, source=src)
                ptype = f"{'RR' if kind == 'rr' else 'S'}{d}"
            out[tick] = (t, m, ptype)
    return out


def _hm(s: str) -> tuple[int, int]:
    h, m = (int(x) for x in s.strip().split(":"))
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError(f"bbg: {s} is not a time (HH:MM)")
    return (0, m) if h == 24 else (h, m)


def stop_times(cfg: dict) -> list[tuple[int, int]]:
    at = cfg.get("stop_at") or []
    return [_hm(x) for x in ([at] if isinstance(at, str) else at) if str(x).strip()]


def next_stop(cfg: dict, tz: str, now: float) -> float | None:
    """When a run started at now stops: the first stop time (local) after now; None = never."""
    times = stop_times(cfg)
    if not times:
        return None
    day = to_local(datetime.fromtimestamp(now, timezone.utc), tz).date()
    cands = [localize(datetime(d.year, d.month, d.day, h, m), tz).timestamp()
             for d in (day, day + timedelta(days=1)) for h, m in times]
    return min(c for c in cands if c > now)


def quiet(cfg: dict, tz: str, now: float) -> bool:
    """In bbg.quiet_hours ([from, to], local, may wrap midnight): Bloomberg not running is expected."""
    q = cfg.get("quiet_hours") or []
    if len(q) != 2:
        return False
    a, b = _hm(q[0]), _hm(q[1])
    loc = to_local(datetime.fromtimestamp(now, timezone.utc), tz)
    x = (loc.hour, loc.minute)
    return a <= x < b if a <= b else (x >= a or x < b)


class BloombergFeed:
    """The Bloomberg button of one VolMarker: start, stop, status."""

    def __init__(self, store: Store, user: str, recs: Callable[[], list[dict]],
                 client_factory: Callable[[], object] = BlpClient, clock: Callable[[], float] = time.time):
        self.store, self.user, self.recs = store, user, recs
        self.client_factory, self.clock = client_factory, clock
        self.dir = store.root / "market" / "bbg"
        self.status_path = store.root / "market" / STATUS_FILE
        self.lock = threading.Lock()
        self.client = None
        self.stop_ev: threading.Event | None = None
        self.stop_due: float | None = None        # when the current run stops itself
        self.seconds: float = DEFAULT_SECONDS     # between rounds: the app's interval field
        self.instance: str | None = None
        self.status: dict = {}
        self._last: dict[str, list] = {}        # pair -> quotes last written (values only)
        self.live = Liveness()

    # ---- control ------------------------------------------------------
    def cfg(self) -> dict:
        return self.store.config()["bbg"]

    def tz(self) -> str:
        return self.store.config().get("calendar_tz") or "Europe/Zurich"

    def running(self) -> bool:
        return self.stop_ev is not None and not self.stop_ev.is_set()

    def _read_status(self) -> dict:
        try:
            return json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            return {}

    def other(self, confirm: bool = False) -> dict | None:
        """The status of a Bloomberg feed running elsewhere, if any. confirm: watch its
        heartbeat for a few seconds if it can't be told yet (before starting)."""
        cur = self._read_status()
        if not cur.get("connected") or cur.get("instance") == self.instance:
            return None
        if confirm:
            alive = self.live.confirm("bbg", lambda: self._read_status().get("heartbeat"), FEED_STALE, self.clock)
        else:
            alive = self.live.alive("bbg", cur.get("heartbeat"), FEED_STALE, self.clock())
        return cur if alive else None

    @staticmethod
    def _check_seconds(seconds) -> float:
        seconds = float(seconds)
        if not seconds >= MIN_SECONDS:
            raise ValueError(f"Fetch at most every {MIN_SECONDS} seconds")
        return seconds

    def set_seconds(self, seconds: float) -> dict:
        """The interval field changed: applies to the current wait at once."""
        self.seconds = self._check_seconds(seconds)
        with self.lock:
            self.status["seconds"] = self.seconds
        return self.state()

    def start(self, seconds: float | None = None, follow: bool = True) -> dict:
        cfg = self.cfg()
        seconds = self._check_seconds(seconds or self.seconds)
        with self.lock:
            if self.running():
                return self.state()
            other = self.other(confirm=True)
            if other:
                raise ValueError(f"Bloomberg is already running for the desk ({other.get('user')} on "
                                 f"{other.get('host')})")
            self.client = self.client_factory()
            self.instance = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
            t = self.clock()
            self.seconds = seconds
            due = next_stop(cfg, self.tz(), t)
            self.status = {"instance": self.instance, "user": self.user, "host": socket.gethostname(),
                           "connected": True, "since": now_iso(t), "started": now_iso(t), "last_msg": None,
                           "last_error": None, "pairs": 0, "tickers": 0, "seconds": seconds,
                           "stop_due": now_iso(due) if due else None, "stopped": None}
            self.stop_due = due
            self.stop_ev = stop = threading.Event()
            self._write_status()
        threading.Thread(target=self._heartbeat, args=(stop,), name="bbg-status", daemon=True).start()
        threading.Thread(target=self._loop, args=(stop,), name="bbg-feed", daemon=True).start()
        if follow:
            self._start_follower(stop)
        log.info("Bloomberg: started by %s", self.user)
        return self.state()

    def stop(self, reason: str = "stopped") -> dict:
        with self.lock:
            if self.running():
                self.stop_ev.set()
                self.status.update(connected=False, stopped=reason)
                self._write_status()
                if self.client is not None:
                    self.client.close()
                self.client = None
                log.info("Bloomberg: %s", reason)
        return self.state()

    def state(self) -> dict:
        """For the button: ours, someone else's, or none."""
        out = {"running": self.running()}
        if self.running():
            out.update({k: self.status.get(k) for k in ("user", "host", "since", "last_msg", "last_error",
                                                        "pairs", "tickers", "stop_due", "seconds")})
        else:
            due = next_stop(self.cfg(), self.tz(), self.clock())
            out["stop_due"] = now_iso(due) if due else None      # if started now
            other = self.other()
            if other:
                out["other"] = {k: other.get(k) for k in ("user", "host", "since", "last_msg", "stop_due", "seconds")}
            elif self.status.get("stopped"):
                out["stopped"] = self.status["stopped"]
        return out

    # ---- work ---------------------------------------------------------
    def _start_follower(self, stop: threading.Event) -> None:
        # market mode for the Bloomberg pairs; only one follower runs at a time (see Follower.step)
        from .follow import Follower
        from .service import Service
        fsvc = Service(self.store, "BBG")
        fsvc.start_propagation()

        def run():
            try:
                Follower(fsvc).run_forever(stop)
            finally:                     # Bloomberg stopped: its propagation thread ends too
                fsvc.propagation_idle(30)
                fsvc._stop.set()
        threading.Thread(target=run, name="bbg-follow", daemon=True).start()

    def _write_status(self) -> None:
        try:
            write_shared(self.status_path, json.dumps(dict(self.status, heartbeat=self.clock())))
        except OSError as e:
            log.warning("could not write the Bloomberg status: %s", e)

    def _heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(1.0):
            if self.stop_due is not None and self.clock() >= self.stop_due:
                at = to_local(datetime.fromtimestamp(self.stop_due, timezone.utc), self.tz()).strftime("%H:%M")
                self.stop(f"stopped at {at} (download limits); start it again if needed")
                return
            with self.lock:
                if not stop.is_set():
                    self._write_status()

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            began = time.monotonic()
            try:
                self.round()
            except Exception as e:           # next round tries again
                log.exception("Bloomberg round failed")
                with self.lock:
                    self.status["last_error"] = str(e)
            while not stop.is_set():          # in short steps, so a new interval applies at once
                left = self.seconds - (time.monotonic() - began)
                if left <= 0:
                    break
                stop.wait(min(left, 1.0))

    def round(self) -> list[str]:
        """Fetch every Bloomberg pair once; returns the pairs whose quotes changed."""
        cfg = self.cfg()
        recs = {r["pair"]: r for r in self.recs() if r.get("market_source") == BBG}
        maps = {p: ticker_map(r, cfg) for p, r in recs.items()}
        tickers = sorted({t for m in maps.values() for t in m})
        fields = [f for f in (cfg["fields"].get("bid"), cfg["fields"].get("offer"), cfg["fields"].get("mid")) if f]
        got, errors = {}, []
        batch = max(1, int(cfg.get("batch") or 200))
        client = self.client
        if client is None:
            return []
        for i in range(0, len(tickers), batch):
            try:
                got.update(client.fetch(tickers[i:i + batch], fields))
            except Exception as e:
                errors.append(str(e))
        ts = now_iso(self.clock())
        changed = []
        self.dir.mkdir(parents=True, exist_ok=True)
        for pair, tmap in maps.items():
            quotes = []
            for tick, (tenor, mark, ptype) in tmap.items():
                v = got.get(tick) or {}
                bid, offer = v.get(cfg["fields"].get("bid")), v.get(cfg["fields"].get("offer"))
                mid = v.get(cfg["fields"].get("mid"))
                if mid is None and bid is not None and offer is not None:
                    mid = (bid + offer) / 2
                if bid is None and offer is None and mid is None:
                    continue
                quotes.append({"id": f"BBG.{tick}", "pair": pair, "tenor": tenor, "mark": mark, "ptype": ptype,
                               "brand": BBG, "ticker": tick, "ts": ts, "bid": bid, "offer": offer, "mid": mid})
            vals = [(q["id"], q["bid"], q["offer"], q["mid"]) for q in quotes]
            if vals != self._last.get(pair):
                changed.append(pair)
            self._last[pair] = vals
            # every round, as the FMD 1.0 poller: a quote's time is when it was last fetched
            write_shared(self.dir / f"{pair}.json", json.dumps({"pair": pair, "updated": ts, "quotes": quotes}))
        for f in self.dir.glob("*.json"):  # pairs switched back to FMD: no Bloomberg quotes any more
            if f.stem not in maps:
                f.unlink(missing_ok=True)
                self._last.pop(f.stem, None)
        with self.lock:
            self.status.update(pairs=len(maps), tickers=len(tickers),
                               last_error=errors[0] if errors else None)
            if got or not tickers:
                self.status["last_msg"] = ts
        return changed
