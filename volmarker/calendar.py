"""Event-weighted time and total-variance interpolation between vol marks.

Day weights for a pair, as in the desk's Excel weight files and in Fenics:

    day weight = weekday weight x holiday factor(ccy1) x holiday factor(ccy2) + events

  * weekday weights: one per weekday (Mon..Sun), e.g. 1.05, 1, 1, 1, 1, 0, 0;
  * a holiday factor applies on a holiday of that currency (e.g. 0.4), so a
    holiday in both currencies gives 0.4 x 0.4;
  * event weights are additive on top (FOMC 0.8 -> 1.8 on a normal day).

Total variance  w(T) = vol(T)^2 * T_cal  is interpolated linearly in
weighted time between the marked tenors.

Pair weight config (stored on the pair's state under "weights"):

    {"weekdays": [1, 1, 1, 1, 1, 0, 0], "holiday1": 0.4, "holiday2": 0.4,
     "use_defaults": true,
     "events": [{"date": "2026-10-29", "desc": "FOMC", "weight": 1.2, "ccy": "USD"}]}

Older records with weekend1/weekend2 are read as weekday weights (see
normalise_weights).

Events come from the event calendar (Bloomberg imports plus manual events,
see store.load_calendar) and the desk's list of relevant events
(calendar/event_defaults.csv: ccy, event, weight, ticker). A calendar event in
one of the pair's currencies that is on the list (same ticker, or same name)
gets the listed weight on its OPTION EXPIRY date: the first good business day
whose expiry cut (e.g. 10:00 New York) comes after the release. FOMC at 14:00
New York on the 28th therefore weighs on the 29th's expiry. Events without a
release time weigh on their own date.

Several events on one expiry add up; the biggest names the day. An event the
pair has on the same date replaces the default (manual or calibrated to the
dailies); weight 0 switches it off. "use_defaults": false ignores the list.
"""

from __future__ import annotations

import calendar as _cal
import math
from datetime import date, datetime, time, timedelta

from .conventions import TENORS, split_pair
from .tz import to_local

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULT_WEIGHTS = {"weekdays": [1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0],
                   "holiday1": 0.4, "holiday2": 0.4, "events": [], "use_defaults": True}
DEFAULT_CUT = {"tz": "America/New_York", "time": "10:00"}


def normalise_weights(weights: dict | None) -> dict:
    """Fill defaults; convert the old weekend1/weekend2 factors to weekday weights."""
    w = dict(DEFAULT_WEIGHTS)
    w.update(weights or {})
    if not (weights or {}).get("weekdays") and ("weekend1" in w or "weekend2" in w):
        we = min(float(w.get("weekend1", 0)), float(w.get("weekend2", 0)))
        w["weekdays"] = [1.0] * 5 + [we, we]
    w["weekdays"] = [float(x) for x in w["weekdays"]]
    if len(w["weekdays"]) != 7:
        raise ValueError("weekday weights: 7 values, Monday to Sunday")
    for k in ("weekend1", "weekend2"):
        w.pop(k, None)
    return w

WEEKEND = {5, 6}  # Saturday, Sunday


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, _cal.monthrange(y, m)[1]))


def is_good_day(d: date, ccys: tuple[str, str], holidays: dict[str, set]) -> bool:
    return d.weekday() not in WEEKEND and not any(d in holidays.get(c, ()) for c in ccys)


def tenor_dates(pair: str, today: date, holidays: dict[str, set]) -> dict[str, date]:
    """Approximate expiry dates (following business day). Fenics owns the
    exact spot/delivery logic; this is only for weighting and display."""
    ccys = split_pair(pair)
    out = {}
    for t in TENORS:
        if t == "ON":
            d = today + timedelta(days=1)
        elif t.endswith("W"):
            d = today + timedelta(days=7 * int(t[:-1]))
        elif t.endswith("M"):
            d = _add_months(today, int(t[:-1]))
        else:
            d = _add_months(today, 12 * int(t[:-1]))
        while not is_good_day(d, ccys, holidays):
            d += timedelta(days=1)
        out[t] = d
    return out


def day_weight(d: date, ccys: tuple[str, str], weights: dict, holidays: dict[str, set],
               events_by_date: dict[date, float]) -> float:
    """weights must be normalised (see normalise_weights)."""
    base = weights["weekdays"][d.weekday()]
    for i, c in enumerate(ccys, start=1):
        if d in holidays.get(c, ()):
            base *= weights[f"holiday{i}"]
    return base + events_by_date.get(d, 0.0)


def expiry_for(ev: dict, ccys: tuple[str, str], holidays: dict[str, set], cut: dict | None) -> date:
    """The option expiry an event weighs on: the first good day whose cut is
    after the release (events without a time: their own date, rolled forward)."""
    cut = cut or DEFAULT_CUT
    if ev.get("ts"):
        local = to_local(datetime.fromisoformat(ev["ts"]), cut["tz"])
        h, m = (int(x) for x in cut["time"].split(":"))
        d = local.date() + timedelta(days=1 if local.time() >= time(h, m) else 0)
    else:
        d = date.fromisoformat(ev["date"])
    while not is_good_day(d, ccys, holidays):
        d += timedelta(days=1)
    return d


def rule_weight(ev: dict, rules: list[dict]) -> float | None:
    """Weight of a calendar event on the desk's list of relevant events, or None.
    A rule matches on its ticker, or on the event name (case-insensitive)."""
    name, raw, ticker = ev["desc"].lower(), (ev.get("event") or "").lower(), (ev.get("ticker") or "").lower()
    ws = []
    for r in rules:
        if (r.get("ccy") or "").strip("*") not in ("", ev["ccy"]):
            continue
        rt, rn = (r.get("ticker") or "").strip().lower(), (r.get("event") or "").strip().lower()
        if (rt and rt == ticker) or (rn and rn in (name, raw)):
            ws.append(float(r["weight"]))
    return max(ws) if ws else None


def default_events(pair: str, cal_events: list[dict], defaults: list[dict],
                   start: str | None = None, holidays: dict[str, set] | None = None,
                   cut: dict | None = None, display_tz: str | None = None) -> list[dict]:
    """Desk default weights for this pair, one entry per expiry date: the sum
    of the relevant events weighing on it, labelled with the biggest one.
    "parts" lists them all, with their release time."""
    ccys = split_pair(pair)
    holidays = holidays or {}
    by_date: dict[str, dict[str, dict]] = {}
    for ev in cal_events:
        if ev["ccy"] not in ccys:
            continue
        w = rule_weight(ev, defaults)
        if w is None:
            continue
        d = expiry_for(ev, ccys, holidays, cut).isoformat()
        if start and d < start:
            continue
        release = ev["date"]
        if ev.get("ts") and display_tz:
            release = to_local(datetime.fromisoformat(ev["ts"]), display_tz).strftime("%Y-%m-%d %H:%M")
        elif ev.get("time"):
            release = f"{ev['date']} {ev['time']}"
        # an event listed twice in the calendar counts once
        by_date.setdefault(d, {})[f"{ev['ccy']} {ev['desc']}"] = {"weight": w, "ccy": ev["ccy"], "release": release}
    out = []
    for d, evs in sorted(by_date.items()):
        parts = sorted(evs.items(), key=lambda kv: -kv[1]["weight"])
        out.append({"date": d, "desc": parts[0][0] + (f" +{len(parts) - 1}" if len(parts) > 1 else ""),
                    "weight": round(sum(p["weight"] for _, p in parts), 6), "ccy": parts[0][1]["ccy"],
                    "parts": [{"desc": n, **p} for n, p in parts]})
    return out


def effective_events(pair_events: list[dict], defaults: list[dict]) -> list[dict]:
    """The pair's own events plus the defaults on dates the pair doesn't set itself."""
    own: dict[str, list] = {}
    for e in pair_events:
        own.setdefault(e["date"], []).append(e)
    out = [dict(e, source="default") for e in defaults if e["date"] not in own]
    dflt = {e["date"]: e for e in defaults}
    for d, es in own.items():
        out.append({"date": d, "desc": ", ".join(e.get("desc") or "" for e in es),
                    "weight": sum(float(e["weight"]) for e in es), "source": "pair",
                    "ccy": es[0].get("ccy") or dflt.get(d, {}).get("ccy")})
    return sorted(out, key=lambda e: e["date"])


def with_defaults(pair: str, weights: dict | None, cal_events: list[dict], defaults: list[dict],
                  holidays: dict[str, set] | None = None, cut: dict | None = None) -> dict:
    """Pair weights (normalised) with the desk defaults merged into the events."""
    w = normalise_weights(weights)
    if w.get("use_defaults", True):
        w["events"] = effective_events(w.get("events") or [],
                                       default_events(pair, cal_events, defaults, holidays=holidays, cut=cut))
    return w


def cumulative_weights(pair: str, today: date, end: date, weights: dict | None,
                       holidays: dict[str, set]) -> list[float]:
    """cum[n] = weighted days from today to today + n (day 0 excluded)."""
    w = normalise_weights(weights)
    ccys = split_pair(pair)
    events: dict[date, float] = {}
    for ev in w.get("events", []):
        d = date.fromisoformat(ev["date"])
        events[d] = events.get(d, 0.0) + float(ev["weight"])
    cum = [0.0]
    d = today
    for _ in range((end - today).days):
        d += timedelta(days=1)
        cum.append(cum[-1] + day_weight(d, ccys, w, holidays, events))
    return cum


def interpolate_atm(pair: str, atm_by_tenor: dict[str, float], today: date,
                    targets: list[date], weights: dict | None,
                    holidays: dict[str, set]) -> dict[date, float]:
    """ATM vol for arbitrary expiry dates, interpolating total variance in
    weighted time between marked tenors (flat forward weighted vol beyond)."""
    tdates = tenor_dates(pair, today, holidays)
    pillars = [(tdates[t], atm_by_tenor[t]) for t in TENORS
               if atm_by_tenor.get(t) is not None]
    if not pillars:
        return {}
    end = max([p[0] for p in pillars] + targets)
    cum = cumulative_weights(pair, today, end, weights, holidays)

    # knots in (weighted time, total variance), anchored at today
    knots = [(0.0, 0.0)]
    for d, vol in pillars:
        n = (d - today).days
        knots.append((cum[n], vol ** 2 * n / 365.0))

    out = {}
    for d in targets:
        n = (d - today).days
        if n <= 0:
            continue
        tau = cum[n]
        for (t0, w0), (t1, w1) in zip(knots, knots[1:]):
            if tau <= t1:
                frac = 0.0 if t1 == t0 else (tau - t0) / (t1 - t0)
                var = w0 + frac * (w1 - w0)
                break
        else:
            (t0, w0), (t1, w1) = knots[-2], knots[-1]
            var = w1 + (tau - t1) * (w1 - w0) / (t1 - t0)
        out[d] = math.sqrt(max(var, 0.0) * 365.0 / n)
    return out


# ---- market dailies (1D..14D) -> day weights -------------------------------

def daily_dates(pair: str, today: date, days: list[int], holidays: dict[str, set],
                basis: str = "calendar") -> dict[int, date]:
    """Expiry date of an 'ND' daily. 'calendar' (FXO convention): today + N
    calendar days, rolled forward to a good day for both currencies - so
    e.g. 1D-3D from a Friday can share Monday's expiry. 'business': the N-th
    good day."""
    ccys = split_pair(pair)
    out = {}
    if basis == "calendar":
        for n in days:
            d = today + timedelta(days=n)
            while not is_good_day(d, ccys, holidays):
                d += timedelta(days=1)
            out[n] = d
        return out
    d, k = today, 0
    for n in sorted(days):
        while k < n:
            d += timedelta(days=1)
            if is_good_day(d, ccys, holidays):
                k += 1
        out[n] = d
    return out


def implied_daily_weights(pair: str, today: date, daily_vols: dict[date, float],
                          weights: dict | None, holidays: dict[str, set],
                          min_event: float = 0.05, pillars: list[date] | None = None) -> dict:
    """Day weights implied by market daily ATM vols.

    Forward variance between consecutive daily expiries, divided by the
    variance of a normal day, is the weight of that interval. Weekend and
    holiday days inside an interval keep their factors; the remainder sits
    on the expiry day as an additive event weight (the first expiry that
    includes an event is the one that prices it).

    The interpolation only uses weights *relative to each other between two
    marked tenors*, so the normal-day reference is taken per pillar segment
    (pillars = expiry dates of ON, 1W, 2W, ...). Otherwise differences in vol
    level between segments would show up as fake events.
    """
    w = normalise_weights(weights)
    ccys = split_pair(pair)
    exp = sorted(d for d in daily_vols if d > today)   # dailies sharing an expiry collapse to one
    rows, prev_d, prev_tv = [], today, 0.0
    for d in exp:
        n = (d - today).days
        tv = daily_vols[d] ** 2 * n / 365.0
        days = [prev_d + timedelta(days=i) for i in range(1, (d - prev_d).days + 1)]
        base = [day_weight(x, ccys, w, holidays, {}) for x in days]
        rows.append({"date": d, "vol": daily_vols[d], "fwd_var": tv - prev_tv,
                     "cal_days": len(days), "base_sum": sum(base), "base_expiry": base[-1]})
        prev_d, prev_tv = d, tv
    def reference(rs):
        """forward variance of a single normal business day (median)"""
        singles = sorted(r["fwd_var"] for r in rs if r["cal_days"] == 1 and abs(r["base_sum"] - 1) < 1e-9)
        if singles:
            return singles[len(singles) // 2]
        tot_b = sum(r["base_sum"] for r in rs)
        return sum(r["fwd_var"] for r in rs) / tot_b if tot_b else 0.0

    global_ref = reference(rows)
    bounds = sorted(p for p in (pillars or []) if p > today)
    seg_of = lambda d: next((i for i, p in enumerate(bounds) if d <= p), len(bounds))
    segs: dict[int, list] = {}
    for r in rows:
        r["segment"] = seg_of(r["date"])
        segs.setdefault(r["segment"], []).append(r)
    seg_ref = {}
    for k, rs in segs.items():
        has_normal = any(r["cal_days"] == 1 and abs(r["base_sum"] - 1) < 1e-9 for r in rs)
        seg_ref[k] = reference(rs) if has_normal or len(rs) > 1 else global_ref
    ref = global_ref
    warnings, events = [], []
    for r in rows:
        ref = seg_ref[r["segment"]]
        r["fwd_vol"] = (max(r["fwd_var"], 0) * 365.0 / r["cal_days"]) ** 0.5
        if r["fwd_var"] < 0:
            warnings.append(f"{r['date']}: negative forward variance (calendar arbitrage in dailies)")
        ev = (r["fwd_var"] / ref - r["base_sum"]) if ref > 0 else 0.0
        if ev < -r["base_expiry"]:
            warnings.append(f"{r['date']}: implied weight below zero - clamped")
            ev = -r["base_expiry"]
        r["implied_event"] = ev
        r["implied_weight"] = r["base_expiry"] + ev
        if abs(ev) >= min_event:
            events.append({"date": r["date"].isoformat(), "weight": round(ev, 2)})
    return {"rows": rows, "ref_var": global_ref, "events": events, "warnings": warnings}
