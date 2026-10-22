"""Vol surface files, in the layout of the desk's Excel marking workbook
(VOL_PUBLISHER, macro CreateVolatilityExportSingleCCY):

  Front Arena  <frontarena_dir>/<CCY1><CCY2>volsurface.txt   pure mids, never skewed
      vol,EUR,USD,1d,<ATM bid>,<ATM ask>                     tenors 1d 1wk 2wk 1mo ... 1yr ... 5yr
      vol25,EUR,USD,strangle,1d,<25D BF>      vol25,EUR,USD,riskreversal,1d,<25D RR>
      vol10,EUR,USD,strangle,1d,<10D BF>      vol10,EUR,USD,riskreversal,1d,<10D RR>
  Fenics       <fenics_dir>/<CCY1><CCY2>volsurface.txt       quoted surface (mid + skew)
      vol,atm,EUR,USD,1w,<bid>,<ask>                          tenors 1d 1w 2w 1m ... 1y ... 5y
      vol,s25|rr25|s10|rr10,EUR,USD,1w,<bid>,<ask>            bid = ask = mid: Fenics uses the
                                                              mid for RR and BF
  DC spreads   <dc_spread_dir>/<CCY1><CCY2>spreads.txt        only if the pair's dc_factor >= 0.01
      1d,<ATM spread x 0.5 x dc_factor>
  History      <history_dir>/<CCY1><CCY2>_yyyymmdd_hhmmss.csv  a copy of the Fenics file

Numbers are written like VBA does (up to 15 significant digits, '.' decimal
point); lines end in CRLF. Files are written under a temporary name and then
renamed, so Fenics and Front Arena never read a half-written file.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .conventions import TENORS, split_pair
from .store import atomic_write

FA_TENORS = dict(zip(TENORS, ["1d", "1wk", "2wk", "1mo", "2mo", "3mo", "6mo", "9mo", "1yr", "2yr", "3yr", "4yr", "5yr"]))
FEN_TENORS = dict(zip(TENORS, ["1d", "1w", "2w", "1m", "2m", "3m", "6m", "9m", "1y", "2y", "3y", "4y", "5y"]))


def _n(x: float | None) -> str:
    """A number as VBA's implicit conversion writes it (15 significant digits)."""
    if x is None:
        return ""
    s = f"{x:.15g}"
    return "0" if s == "-0" else s


def _lines(lines: list[str]) -> str:
    return "".join(l + "\r\n" for l in lines)


def frontarena_lines(pair: str, resolved: dict, spreads: dict[str, float]) -> list[str]:
    c1, c2 = split_pair(pair)
    out = []
    for t in TENORS:
        atm = resolved[t]["mid"]["atm"]
        half = spreads[t] / 2
        out.append(f"vol,{c1},{c2},{FA_TENORS[t]},{_n(None if atm is None else atm - half)},"
                   f"{_n(None if atm is None else atm + half)}")
    for prefix, d in (("vol25", "25"), ("vol10", "10")):
        for name, mark in (("strangle", f"bf{d}"), ("riskreversal", f"rr{d}")):
            out += [f"{prefix},{c1},{c2},{name},{FA_TENORS[t]},{_n(resolved[t]['mid'][mark])}" for t in TENORS]
    return out


def fenics_lines(pair: str, resolved: dict) -> list[str]:
    c1, c2 = split_pair(pair)
    out = [f"vol,atm,{c1},{c2},{FEN_TENORS[t]},{_n(resolved[t]['quoted']['atm_bid'])},"
           f"{_n(resolved[t]['quoted']['atm_offer'])}" for t in TENORS]
    for code, mark in (("s25", "bf25"), ("rr25", "rr25"), ("s10", "bf10"), ("rr10", "rr10")):
        out += [f"vol,{code},{c1},{c2},{FEN_TENORS[t]},{_n(resolved[t]['quoted'][mark])},"
                f"{_n(resolved[t]['quoted'][mark])}" for t in TENORS]
    return out


def dc_spread_lines(spreads: dict[str, float], dc_factor: float) -> list[str]:
    return [f"{FEN_TENORS[t]},{_n(spreads[t] * 0.5 * dc_factor)}" for t in TENORS]


def write_exports(rec: dict, resolved: dict, dirs: dict[str, Path], history: bool = True,
                  now: datetime | None = None) -> None:
    """dirs: fenics, frontarena, dc_spread, history."""
    pair = rec["pair"]
    c1, c2 = split_pair(pair)
    spreads = {t: rec["tenors"][t]["spread"] for t in TENORS}
    fen = _lines(fenics_lines(pair, resolved))
    # no flush to disk: the rename keeps readers safe, and the next publish rewrites them anyway
    atomic_write(Path(dirs["frontarena"]) / f"{c1}{c2}volsurface.txt", _lines(frontarena_lines(pair, resolved, spreads)), fsync=False)
    atomic_write(Path(dirs["fenics"]) / f"{c1}{c2}volsurface.txt", fen, fsync=False)
    dc = rec.get("dc_factor") or 0
    if dc >= 0.01:
        atomic_write(Path(dirs["dc_spread"]) / f"{c1}{c2}spreads.txt", _lines(dc_spread_lines(spreads, dc)), fsync=False)
    if history:
        stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
        atomic_write(Path(dirs["history"]) / f"{c1}{c2}_{stamp}.csv", fen, fsync=False)


def read_frontarena(text: str, pair: str, partial: bool = False) -> dict[str, dict]:
    """Parse a Front Arena volsurface file (as the Excel macro writes it) back
    into marks per tenor: atm (mid), spread (ask - bid), rr25, rr10, bf25, bf10.
    Raises ValueError if the file is incomplete, e.g. while Excel is writing it;
    partial=True returns whatever marks the file has instead."""
    c1, c2 = split_pair(pair)
    tenor = {v: k for k, v in FA_TENORS.items()}
    num = lambda x: None if x.strip() == "" else float(x)
    out: dict[str, dict] = {t: {} for t in TENORS}
    for line in text.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 5 or f[1:3] != [c1, c2]:
            continue
        if f[0] == "vol" and len(f) >= 6 and f[3] in tenor:
            bid, ask = num(f[4]), num(f[5])
            if bid is not None and ask is not None:
                out[tenor[f[3]]].update(atm=round((bid + ask) / 2, 10), spread=round(ask - bid, 10))
        elif f[0] in ("vol25", "vol10") and len(f) >= 6 and f[4] in tenor:
            mark = {"strangle": "bf", "riskreversal": "rr"}.get(f[3])
            if mark and num(f[5]) is not None:
                out[tenor[f[4]]][f"{mark}{f[0][3:]}"] = num(f[5])
    missing = [t for t in TENORS if set(out[t]) != {"atm", "spread", "rr25", "rr10", "bf25", "bf10"}]
    if missing and not partial:
        raise ValueError(f"incomplete file: {', '.join(missing)} missing")
    return out
