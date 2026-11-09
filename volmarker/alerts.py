"""Alerts: the market has moved away from what we publish.

Every running app evaluates alerts for its own trader from the published
surfaces and the FMD files. Each candidate is one (pair, tenor, mark) with a
level:

    breach   outside tolerance           -> alert once it has lasted confirm_s
    hold     between clear_ratio*tol and tol (hysteresis: keeps an active alert)
    clear    inside clear_ratio*tol      -> active alert is cleared

Acknowledging an alert mutes it until it clears, or until the deviation has
grown by another full tolerance.

Each trader can narrow their own market alerts (prefs "alerts", see UserAlerts):
switch them off, mute pairs, set their own default tolerances, and limit them
to a tenor range for every pair or a single one. The desk's alert log is kept
on the desk settings (config.json and the pairs' Setup) whatever the leader's
trader has chosen.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .conventions import TENOR_RE, TENOR_DAYS, tenor_days

MARKS = ("atm", "rr25", "rr10", "bf25", "bf10")


def _tenor(t) -> str | None:
    t = str(t).strip().upper() if isinstance(t, str) else ""
    return t if t in TENOR_DAYS or TENOR_RE.match(t) else None


class UserAlerts:
    """One trader's own market-alert settings, read leniently from their prefs:

        {"off": bool,                         # no market alerts at all
         "muted": ["EURSEK", ...],            # pairs without market alerts
         "tolerance": {"atm": 0.3, ...},      # own defaults (a pair's Setup tolerance still wins)
         "range": {"from": "1W", "to": "1Y"}, # tenors alerted on, every pair (missing/null = open)
         "pair_range": {"EURUSD": {"from": null, "to": "2Y"}}}
                                              # per pair: a key that is there replaces that end of
                                              # "range" (null = open), a missing key keeps it

    Excel-file and Follow market alerts are not market alerts and are never filtered."""

    def __init__(self, prefs: dict | None):
        a = (prefs or {}).get("alerts")
        a = a if isinstance(a, dict) else {}
        self.off = bool(a.get("off"))
        self.muted = {str(p).upper() for p in a.get("muted") or [] if isinstance(p, str)}
        tol = a.get("tolerance") if isinstance(a.get("tolerance"), dict) else {}
        self.tolerance = {m: float(v) for m, v in tol.items()
                          if m in MARKS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
        self.range = self._ends(a.get("range"))
        pr = a.get("pair_range") if isinstance(a.get("pair_range"), dict) else {}
        self.pair_range = {str(p).upper(): self._ends(r) for p, r in pr.items() if isinstance(r, dict)}

    @staticmethod
    def _ends(r) -> dict:
        """{"from"/"to": tenor or None} for the ends that are set; a bad tenor is open."""
        if not isinstance(r, dict):
            return {}
        return {k: _tenor(r[k]) for k in ("from", "to") if k in r}

    def bounds(self, pair: str) -> tuple[int, int]:
        """The pair's tenor range in days, both ends inclusive."""
        r = {**self.range, **self.pair_range.get(pair, {})}
        lo, hi = r.get("from"), r.get("to")
        lo_d = tenor_days(lo) if lo else 0
        hi_d = tenor_days(hi) if hi else 10 ** 6
        return (lo_d, hi_d) if lo_d <= hi_d else (hi_d, lo_d)

    def wants(self, pair: str) -> bool:
        return not self.off and pair not in self.muted

    def in_range(self, pair: str, tenor: str) -> bool:
        lo, hi = self.bounds(pair)
        return lo <= tenor_days(tenor) <= hi


def deviation(value: float, q: dict, basis: str = "mid") -> float | None:
    """Signed distance of our mark from the market. basis "mid": mark - mid;
    "range": distance outside the bid/offer (0 inside)."""
    if basis == "range":
        if q.get("offer") is not None and value > q["offer"]:
            return value - q["offer"]
        if q.get("bid") is not None and value < q["bid"]:
            return value - q["bid"]
        if q.get("bid") is None and q.get("offer") is None and q.get("mid") is not None:
            return value - q["mid"]
        return 0.0
    return None if q.get("mid") is None else value - q["mid"]


def level(dev: float, tol: float, clear_ratio: float) -> str:
    a = abs(dev)
    if a > tol:
        return "breach"
    return "clear" if a < clear_ratio * tol else "hold"


def key_str(key: tuple) -> str:
    return "|".join(key)


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


class AlertEngine:
    def __init__(self):
        self.pending: dict[tuple, float] = {}
        self.active: dict[tuple, dict] = {}

    def update(self, cands: dict[tuple, dict], now: float, confirm_s: float) -> tuple[list, list]:
        """cands: key -> {level, kind, [dev, tol, confirm], display fields}.
        Keys that are missing (no fresh market, pair muted, ...) are cleared.
        Returns (new or re-armed alerts, cleared alerts)."""
        new, cleared = [], []
        for key, c in cands.items():
            lv = c["level"]
            al = self.active.get(key)
            if al is not None:
                if lv == "clear":
                    cleared.append(self.active.pop(key))
                    continue
                al.update({k: v for k, v in c.items() if k != "level"}, updated=iso(now))
                if al.get("acked") and c.get("dev") is not None and c.get("tol") and \
                        abs(c["dev"]) >= al["acked_dev"] + c["tol"]:
                    al["acked"] = False            # got worse since it was acknowledged
                    new.append(al)
            elif lv == "breach":
                first = self.pending.setdefault(key, now)
                if now - first >= c.get("confirm", confirm_s):
                    self.pending.pop(key, None)
                    al = {k: v for k, v in c.items() if k != "level"}
                    al.update(key=key_str(key), since=iso(first), updated=iso(now), acked=False)
                    self.active[key] = al
                    new.append(al)
            else:
                self.pending.pop(key, None)
        for key in [k for k in self.active if k not in cands]:
            cleared.append(self.active.pop(key))
        for key in [k for k in self.pending if k not in cands]:
            del self.pending[key]
        return new, cleared

    def ack(self, keys: list[str] | None = None, pair: str | None = None) -> int:
        n = 0
        for key, al in self.active.items():
            if (keys is not None and key_str(key) in keys) or (pair and key[0] == pair):
                if not al["acked"]:
                    al["acked"] = True
                    al["acked_dev"] = abs(al.get("dev") or 0.0)
                    n += 1
        return n

    def list(self) -> list[dict]:
        return [dict(a) for a in self.active.values()]
