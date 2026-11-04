"""Desk conventions: tenors, quote marks, pair names."""

from __future__ import annotations

import re

TENORS = ["ON", "1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y"]

# Approximate calendar days per tenor. Used only for ordering, the calendar
# arbitrage check and charts; the expiry dates are calendar.tenor_dates.
TENOR_DAYS = {
    "ON": 1, "1W": 7, "2W": 14, "1M": 30, "2M": 61, "3M": 91, "6M": 182,
    "9M": 273, "1Y": 365, "2Y": 730, "3Y": 1095, "4Y": 1461, "5Y": 1826,
}

# A pair can carry extra tenors for a while (e.g. 3W over an event between 2W and 1M) and
# extra deltas (e.g. 5D or 1D for steep wings) on top of the standard tenors and 25D/10D.
TENOR_RE = re.compile(r"^([1-9]\d?)([WMY])$")
MAX_EXTRA_TENORS = 6
STANDARD_DELTAS = (25, 10)
EXTRA_DELTA_RANGE = (1, 45)
MAX_EXTRA_DELTAS = 4


def tenor_days(t: str) -> int:
    """Approximate calendar days of any tenor code (ON, nW, nM, nY)."""
    if t in TENOR_DAYS:
        return TENOR_DAYS[t]
    m = TENOR_RE.match(t)
    if not m:
        raise ValueError(f"Unknown tenor {t!r}")
    n, unit = int(m.group(1)), m.group(2)
    return {"W": 7 * n, "M": round(30.4375 * n), "Y": 365 * n}[unit]


def norm_extra_tenor(t: str) -> str:
    """An extra tenor's code: 3W, 4M, 18M ... (12M is 1Y, 24M 2Y, ...); raises ValueError if
    it isn't a week, month or year tenor or is one of the standard tenors."""
    t = str(t or "").strip().upper().replace(" ", "")
    m = TENOR_RE.match(t)
    if not m:
        raise ValueError(f"Extra tenor {t!r}: use weeks, months or years, e.g. 3W, 4M, 18M")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "M" and n % 12 == 0:
        t = f"{n // 12}Y"
    if t in TENORS:
        raise ValueError(f"{t} is a standard tenor")
    return t


def sort_tenors(tenors) -> list[str]:
    return sorted(tenors, key=tenor_days)


def check_extra_delta(d) -> int:
    try:
        x = float(d)
    except (TypeError, ValueError):
        raise ValueError(f"Delta {d!r} must be a whole number, e.g. 5 or 1") from None
    if x != int(x):
        raise ValueError(f"Delta {d!r} must be a whole number, e.g. 5 or 1")
    x = int(x)
    if x in STANDARD_DELTAS:
        raise ValueError(f"{x}D is always marked")
    if not EXTRA_DELTA_RANGE[0] <= x <= EXTRA_DELTA_RANGE[1]:
        raise ValueError(f"Delta must be between {EXTRA_DELTA_RANGE[0]} and {EXTRA_DELTA_RANGE[1]}")
    return x


# Mark names as written to the export files.
MARKS = ["ATM", "25RR", "10RR", "25BF", "10BF"]


def split_pair(pair: str) -> tuple[str, str]:
    pair = normalise_pair(pair)
    return pair[:3], pair[3:]


def normalise_pair(pair: str) -> str:
    p = pair.replace("/", "").replace(" ", "").upper()
    if len(p) != 6 or not p.isalpha():
        raise ValueError(f"Invalid currency pair: {pair!r}")
    return p
