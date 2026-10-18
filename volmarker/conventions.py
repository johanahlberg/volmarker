"""Desk conventions: tenors, quote marks, pair names."""

from __future__ import annotations

TENORS = ["ON", "1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y"]

# Approximate calendar days per tenor. Used only for ordering, the calendar
# arbitrage check and charts - Fenics owns the exact expiry date logic.
TENOR_DAYS = {
    "ON": 1, "1W": 7, "2W": 14, "1M": 30, "2M": 61, "3M": 91, "6M": 182,
    "9M": 273, "1Y": 365, "2Y": 730, "3Y": 1095, "4Y": 1461, "5Y": 1826,
}

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
