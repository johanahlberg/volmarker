"""Alerts: the market has moved away from what we publish.

Every running app evaluates alerts for its own trader from the published
surfaces and the FMD files. Each candidate is one (pair, tenor, mark) with a
level:

    breach   outside tolerance           -> alert once it has lasted confirm_s
    hold     between clear_ratio*tol and tol (hysteresis: keeps an active alert)
    clear    inside clear_ratio*tol      -> active alert is cleared

Acknowledging an alert mutes it until it clears, or until the deviation has
grown by another full tolerance.
"""

from __future__ import annotations

from datetime import datetime, timezone


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
