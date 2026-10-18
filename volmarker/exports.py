"""CSV exports: one file per pair per target, one line per quote.

    Fenics       quoted surface (mid + skew): ATM as bid/offer, wings as mids
    Front Arena  pure mids, never skewed

ADJUST HERE once the exact layouts of the current files are known
(column names, order, number format, file naming).
"""

from __future__ import annotations

from pathlib import Path

from .conventions import TENORS, split_pair
from .store import atomic_write

FENICS_HEADER = ["Ccy1", "Ccy2", "Tenor", "Mark", "Mid", "Bid", "Offer"]
FRONTARENA_HEADER = ["Ccy1", "Ccy2", "Tenor", "Mark", "Mid"]

WING_MARKS = [("25RR", "rr25"), ("10RR", "rr10"), ("25BF", "bf25"), ("10BF", "bf10")]


def _f(x: float | None) -> str:
    return "" if x is None else f"{x:.4f}"


def fenics_rows(pair: str, resolved: dict) -> list[list[str]]:
    c1, c2 = split_pair(pair)
    rows = []
    for t in TENORS:
        q = resolved[t]["quoted"]
        if q["atm"] is None:
            continue
        rows.append([c1, c2, t, "ATM", "", _f(q["atm_bid"]), _f(q["atm_offer"])])
        for mark, key in WING_MARKS:
            rows.append([c1, c2, t, mark, _f(q[key]), "", ""])
    return rows


def frontarena_rows(pair: str, resolved: dict) -> list[list[str]]:
    c1, c2 = split_pair(pair)
    rows = []
    for t in TENORS:
        m = resolved[t]["mid"]
        if m["atm"] is None:
            continue
        rows.append([c1, c2, t, "ATM", _f(m["atm"])])
        for mark, key in WING_MARKS:
            rows.append([c1, c2, t, mark, _f(m[key])])
    return rows


def _csv(header: list[str], rows: list[list[str]]) -> str:
    return "\r\n".join(",".join(r) for r in [header] + rows) + "\r\n"


def write_exports(pair: str, resolved: dict, fenics_dir: Path, frontarena_dir: Path) -> None:
    atomic_write(Path(fenics_dir) / f"{pair}.csv", _csv(FENICS_HEADER, fenics_rows(pair, resolved)))
    atomic_write(Path(frontarena_dir) / f"{pair}.csv",
                 _csv(FRONTARENA_HEADER, frontarena_rows(pair, resolved)))
