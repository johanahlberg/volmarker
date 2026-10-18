"""Normalise FMD market-data entries into broker quotes per pair/tenor/mark.

FMD sends incremental updates keyed by SecurityID (48). Static fields
(pair, tenor, price type, brand, cut) may only be present on the first
message for an instrument, so they are cached per SecurityID and later
updates are merged onto them.
"""

from __future__ import annotations

from ..conventions import TENORS

# FIX tags (spec section 6.2)
SECURITY_ID, UPDATE_ACTION = 48, 279
BID, OFFER, MID = 132, 133, 631
PRICE_TYPE, TRANSACT_TIME, TENOR, BRAND = 423, 60, 6217, 6960
CCY1, CCY2, CCY_PAIR, CUTOFF = 16999, 17000, 17001, 17033

# FMD PriceType -> VolMarker mark
PRICE_TYPES = {
    "DNS": "atm", "ATMF": "atm",
    "RR25": "rr25", "RR10": "rr10", "RR5": "rr5",
    "S25": "bf25", "S10": "bf10", "S5": "bf5",
}

# "1D".."14D" are dailies and are kept as they are (not merged into ON)
TENOR_ALIASES = {"O/N": "ON", "ON": "ON", "12M": "1Y", "24M": "2Y", "36M": "3Y",
                 "48M": "4Y", "60M": "5Y", "1WK": "1W", "2WK": "2W"}


def norm_tenor(t: str | None) -> str | None:
    if not t:
        return None
    t = t.strip().upper().replace(" ", "")
    return TENOR_ALIASES.get(t, t)


def _f(x: str | None) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except ValueError:
        return None


class QuoteBook:
    def __init__(self):
        self.by_id: dict[str, dict] = {}

    def apply(self, entry: dict[int, str]) -> str | None:
        """Apply one MD entry; returns the affected pair (or None)."""
        sid = entry.get(SECURITY_ID)
        if not sid:
            return None
        if entry.get(UPDATE_ACTION) == "2":            # delete
            old = self.by_id.pop(sid, None)
            return old.get("pair") if old else None
        q = self.by_id.setdefault(sid, {"id": sid})
        pair = entry.get(CCY_PAIR)
        if not pair and entry.get(CCY1) and entry.get(CCY2):
            pair = entry[CCY1] + entry[CCY2]
        if pair:
            q["pair"] = pair.replace("/", "").upper()
        if entry.get(TENOR):
            q["tenor"] = norm_tenor(entry[TENOR])
        if entry.get(PRICE_TYPE):
            q["ptype"] = entry[PRICE_TYPE].strip().upper()
            q["mark"] = PRICE_TYPES.get(q["ptype"])
        for tag, key in ((BRAND, "brand"), (CUTOFF, "cut"), (TRANSACT_TIME, "ts")):
            if entry.get(tag):
                q[key] = entry[tag]
        for tag, key in ((BID, "bid"), (OFFER, "offer"), (MID, "mid")):
            if tag in entry:
                q[key] = _f(entry[tag])
        if MID not in entry and BID in entry and OFFER in entry:
            if q.get("bid") is not None and q.get("offer") is not None:
                q["mid"] = (q["bid"] + q["offer"]) / 2
        return q.get("pair")

    def pair_quotes(self, pair: str) -> list[dict]:
        """Usable quotes for one pair: known tenor and a mapped mark."""
        return [dict(q) for q in self.by_id.values()
                if q.get("pair") == pair and q.get("mark")
                and (q.get("tenor") in TENORS or daily_n(q.get("tenor")) is not None)]


def daily_n(tenor: str | None) -> int | None:
    """'7D' -> 7 for daily expiries (1D..31D), else None."""
    if tenor and tenor.endswith("D") and tenor[:-1].isdigit() and 1 <= int(tenor[:-1]) <= 31:
        return int(tenor[:-1])
    return None
