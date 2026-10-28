"""Correlation triangle for derived (cross) surfaces.

A cross is written as a product of its two base pairs, each raised to +1 or
-1, e.g.

    EURCHF = EURUSD^+1 * USDCHF^+1
    AUDCHF = AUDUSD^+1 * USDCHF^+1
    NOKSEK = EURSEK^+1 * EURNOK^-1

The correlation is the triangle correlation, as in the desk's Excel workbook:

    var(cross) = var1 + var2 - 2 * rho * vol1 * vol2

whatever way the bases are quoted (EURCHF from EURUSD and USDCHF: rho is
typically strongly positive). Versions written before this convention
(no corr_conv) held the correlation of the bases as quoted, which is
-s1 * s2 * rho; upgrade_corr turns them into the triangle correlation.
"""

from __future__ import annotations

import math

from .conventions import split_pair


def orientation(cross: str, base1: str, base2: str) -> tuple[int, int]:
    """Return (s1, s2) with cross = base1^s1 * base2^s2."""
    c1, c2 = split_pair(cross)
    a1, a2 = split_pair(base1)
    b1, b2 = split_pair(base2)
    for s1 in (1, -1):
        for s2 in (1, -1):
            num, den = {}, {}
            for (x, y), s in (((a1, a2), s1), ((b1, b2), s2)):
                top, bottom = (x, y) if s == 1 else (y, x)
                num[top] = num.get(top, 0) + 1
                den[bottom] = den.get(bottom, 0) + 1
            # cancel the common currency
            for ccy in list(num):
                while num.get(ccy, 0) and den.get(ccy, 0):
                    num[ccy] -= 1
                    den[ccy] -= 1
            num = {k: v for k, v in num.items() if v}
            den = {k: v for k, v in den.items() if v}
            if num == {c1: 1} and den == {c2: 1}:
                return s1, s2
    raise ValueError(f"{cross} cannot be built from {base1} and {base2}")


CORR_CONV = "triangle"


def cross_vol(vol1: float, vol2: float, rho: float) -> float:
    var = vol1 ** 2 + vol2 ** 2 - 2 * rho * vol1 * vol2
    if var <= 0:
        raise ValueError("Correlation gives a non-positive cross variance")
    return math.sqrt(var)


def implied_corr(cross: float, vol1: float, vol2: float) -> float:
    """Correlation that reproduces the given cross vol (not clamped)."""
    return (vol1 ** 2 + vol2 ** 2 - cross ** 2) / (2 * vol1 * vol2)


def upgrade_corr(rec: dict) -> dict:
    """A version read from history in the triangle convention: one written
    before corr_conv held the correlation of the bases as quoted."""
    if rec.get("corr_conv") == CORR_CONV:
        return rec
    rec["corr_conv"] = CORR_CONV
    bases = rec.get("bases")
    if not bases or len(bases) != 2:
        return rec
    try:
        s1, s2 = orientation(rec["pair"], *bases)
    except ValueError:
        return rec
    for row in (rec.get("tenors") or {}).values():
        if row.get("corr") is not None:
            row["corr"] = -s1 * s2 * row["corr"]
    return rec
