"""Where today's marks sit in their own history: percentile ranks of ATM, RRs and BFs.

History is the analysis folder's end-of-day table (marks_eod/YYYY-MM.csv.gz, see
volmarker.analysis): each pair's last snapshot of every day it had one. A pair that
wasn't republished on a day has no row for it, so each series is carried forward over
weekdays (the mark stood all day), up to the last day of the table. Without that, quiet
spells would count for nothing and busy days twice.

"Current" is each pair's latest published version, read the way the analysis tables
read it (mids, without skew; RRs call - put in the pair's own terms), so the two compare
like for like.

Percentile rank = share of the history below the current value, ties counting half:
0 = below everything seen, 100 = above everything, 50 = the median.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .analysis import _read_csv_gz, version_marks
from .conventions import sort_tenors, tenor_days

MARKS = ("atm", "rr25", "rr10", "bf25", "bf10")
COLUMN = {"atm": "atm_mid", "rr25": "rr25", "rr10": "rr10", "bf25": "bf25", "bf10": "bf10"}
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)


def parse_lookback(text: str, today: date) -> date | None:
    """'3M', '1Y', '2Y', '6W', '90D' -> the first day; 'all' -> None."""
    t = text.strip().upper()
    if t == "ALL":
        return None
    n, unit = int(t[:-1]), t[-1]
    if unit == "D":
        return today - timedelta(days=n)
    if unit == "W":
        return today - timedelta(weeks=n)
    if unit == "M":
        y, m = divmod(today.month - 1 - n, 12)
        return _clip(today.year + y, m + 1, today.day)
    if unit == "Y":
        return _clip(today.year - n, today.month, today.day)
    raise ValueError(f"Lookback like 3M, 1Y, 90D or all, not {text!r}")


def _clip(y: int, m: int, d: int) -> date:
    while True:
        try:
            return date(y, m, d)
        except ValueError:
            d -= 1


def _float(s: str) -> float | None:
    try:
        return float(s) if s != "" else None
    except ValueError:
        return None


def read_eod(folder: Path, pairs: set[str] | None = None, first: date | None = None,
             last: date | None = None) -> dict[tuple[str, str, str], dict[date, float]]:
    """(pair, tenor, mark) -> {day: value} from marks_eod/*.csv.gz."""
    out: dict[tuple[str, str, str], dict[date, float]] = {}
    for path in sorted((folder / "marks_eod").glob("*.csv.gz")):
        month = path.name[:7]                      # YYYY-MM: skip whole files outside the window
        if first and month < f"{first:%Y-%m}" or last and month > f"{last:%Y-%m}":
            continue
        header = None
        for r in _read_csv_gz(path):
            if not r:
                continue
            if r[0] == "time_utc":                 # every appended gzip member may repeat it
                header = {k: i for i, k in enumerate(r)}
                continue
            if header is None or len(r) < len(header):
                continue
            pair = r[header["pair"]]
            if pairs and pair not in pairs:
                continue
            try:
                d = date.fromisoformat(r[header["date"]])
            except ValueError:
                continue
            if first and d < first or last and d > last:
                continue
            tenor = r[header["tenor"]]
            for m in MARKS:
                v = _float(r[header[COLUMN[m]]])
                if v is not None:
                    out.setdefault((pair, tenor, m), {})[d] = v
    return out


def fill_weekdays(series: dict[date, float], until: date) -> list[float]:
    """One value per weekday from the first day on, the last mark carried forward."""
    if not series:
        return []
    days = sorted(series)
    out, d, i, cur = [], days[0], 0, None
    while d <= until:
        while i < len(days) and days[i] <= d:
            cur = series[days[i]]
            i += 1
        if d.weekday() < 5:
            out.append(cur)
        d += timedelta(days=1)
    return out


def quantile(sorted_vals: list[float], q: float) -> float:
    """Linear interpolation, as Excel's PERCENTILE.INC and numpy's default."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def pct_rank(sorted_vals: list[float], v: float) -> float:
    below = bisect.bisect_left(sorted_vals, v)
    equal = bisect.bisect_right(sorted_vals, v) - below
    return 100.0 * (below + 0.5 * equal) / len(sorted_vals)


@dataclass
class Stat:
    pair: str
    tenor: str
    mark: str
    current: float | None
    n: int
    pct: float | None = None
    lo: float | None = None
    hi: float | None = None
    q: dict = field(default_factory=dict)     # 0.10 -> value, ...
    first: date | None = None
    last: date | None = None


def current_marks(rec: dict) -> dict[str, dict[str, float | None]]:
    """tenor -> mark -> value of a published version, as the analysis tables have it."""
    return {t: {m: v.get(COLUMN[m]) for m in MARKS} for t, v in version_marks(rec).items()}


def analyse(current: dict[str, dict], history: dict[tuple[str, str, str], dict[date, float]],
            until: date, fill: bool = True) -> list[Stat]:
    """current: pair -> tenor -> mark -> value. One Stat per pair, tenor and mark."""
    out = []
    for pair in sorted(current):
        for tenor in sort_tenors(current[pair]):
            for m in MARKS:
                v = current[pair][tenor].get(m)
                series = history.get((pair, tenor, m)) or {}
                vals = fill_weekdays(series, until) if fill else [series[d] for d in sorted(series)]
                s = Stat(pair, tenor, m, v, len(vals))
                if vals:
                    sv = sorted(vals)
                    s.lo, s.hi = sv[0], sv[-1]
                    s.q = {q: quantile(sv, q) for q in QUANTILES}
                    s.first, s.last = min(series), max(series)
                    if v is not None:
                        s.pct = pct_rank(sv, v)
                out.append(s)
    return out


def tenor_ok(tenor: str, lo: str | None, hi: str | None) -> bool:
    d = tenor_days(tenor)
    return (not lo or tenor_days(lo) <= d) and (not hi or d <= tenor_days(hi))
