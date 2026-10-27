"""Broker market view for a pair: pick a brand per quote and compare marks.

Input is the list of normalised quotes the feed writes to
market/<PAIR>.json. The view is tenor -> mark -> {bid, offer, mid, brand, ...}.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .conventions import TENORS

MARKS = ["atm", "rr25", "rr10", "bf25", "bf10"]
BRAND_ORDER = ["FMD", "FMD1", "BGC", "GFI", "RPM"]   # FMD = aggregated, FMD1 = FMD 1.0 via the Fenics XML poster


def _brand_rank(b: str | None) -> int:
    return BRAND_ORDER.index(b) if b in BRAND_ORDER else len(BRAND_ORDER)


def market_view(quotes: list[dict], atm_type: str = "DNS", brand: str = "auto") -> dict:
    """brand: 'auto' (FMD aggregate first, then FMD1, BGC, GFI, RPM), 'best'
    (highest bid / lowest offer across brands) or a specific brand."""
    view: dict[str, dict] = {}
    groups: dict[tuple[str, str], list[dict]] = {}
    for q in quotes:
        if q.get("mark") not in MARKS:
            continue
        groups.setdefault((q["tenor"], q["mark"]), []).append(q)
    for (tenor, mark), qs in groups.items():
        alt = False
        if mark == "atm":
            same = [q for q in qs if q.get("ptype") == atm_type]
            alt = not same
            qs = same or qs
        if brand == "best":
            bids = [q["bid"] for q in qs if q.get("bid") is not None]
            offers = [q["offer"] for q in qs if q.get("offer") is not None]
            if not bids and not offers:
                mids = [q["mid"] for q in qs if q.get("mid") is not None]
                if not mids:
                    continue
                pick = {"mid": sum(mids) / len(mids), "bid": None, "offer": None}
            else:
                b = max(bids) if bids else None
                o = min(offers) if offers else None
                pick = {"bid": b, "offer": o,
                        "mid": (b + o) / 2 if b is not None and o is not None else (b if o is None else o)}
            pick.update(brand="best", ts=max((q.get("ts") or "" for q in qs), default=""),
                        ptype=qs[0].get("ptype"), n=len(qs))
        else:
            cands = qs if brand == "auto" else [q for q in qs if q.get("brand") == brand]
            if not cands:
                continue
            q = min(cands, key=lambda q: _brand_rank(q.get("brand")))
            pick = {k: q.get(k) for k in ("bid", "offer", "mid", "brand", "ts", "ptype", "cut")}
        if pick.get("mid") is None and pick.get("bid") is not None and pick.get("offer") is not None:
            pick["mid"] = (pick["bid"] + pick["offer"]) / 2
        pick["alt_atm"] = alt
        view.setdefault(tenor, {})[mark] = pick
    return view


def compare(resolved: dict, view: dict, tol: float = 1e-9) -> list[dict]:
    """Marks (mid) outside the broker bid/offer."""
    out = []
    for t in TENORS:
        for m in MARKS:
            q = view.get(t, {}).get(m)
            v = resolved[t]["mid"].get(m)
            if not q or v is None:
                continue
            if q.get("bid") is not None and q.get("offer") is not None and q["offer"] - q["bid"] <= tol:
                continue          # bid = offer is a mid, not a range to be outside of
            if q.get("bid") is not None and v < q["bid"] - tol:
                out.append({"tenor": t, "mark": m, "side": "below bid", "diff": v - q["bid"]})
            elif q.get("offer") is not None and v > q["offer"] + tol:
                out.append({"tenor": t, "mark": m, "side": "above offer", "diff": v - q["offer"]})
    return out


def parse_fix_time(ts: str | None) -> datetime | None:
    """FIX UTCTimestamp (YYYYMMDD-HH:MM:SS[.sss]) or ISO -> aware datetime."""
    if not ts:
        return None
    for fmt in ("%Y%m%d-%H:%M:%S.%f", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
