"""Vol surface files, in the layout of the desk's Excel marking workbook
(VOL_PUBLISHER, macro CreateVolatilityExportSingleCCY):

  Front Arena  <frontarena_dir>/<CCY1><CCY2>volsurface.txt   pure mids, never skewed
      vol,EUR,USD,1d,<ATM bid>,<ATM ask>                     tenors 1d 1wk 2wk 1mo ... 1yr ... 5yr
      vol25,EUR,USD,strangle,1d,<25D BF>      vol25,EUR,USD,riskreversal,1d,<25D RR>
      vol10,EUR,USD,strangle,1d,<10D BF>      vol10,EUR,USD,riskreversal,1d,<10D RR>
  Fenics       <fenics_dir>/<CCY1><CCY2>volsurface.txt       quoted surface (mid + skew)
      vol,atm,EUR,USD,1w,<bid>,<ask>                          tenors 1d 1w 2w 1m ... 1y ... 5y
      vol,s25|rr25|s10|rr10,EUR,USD,1w,<bid>,<ask>            bid = ask = mid: Fenics uses the
                                                              mid for RR and BF; an inverse pair's
                                                              RRs with the sign flipped (Excel's
                                                              Quotation = Inverse)
  DC spreads   <dc_spread_dir>/<CCY1><CCY2>spreads.txt        only if the pair's dc_factor >= 0.01
      1d,<ATM spread x 0.5 x dc_factor>
  History      <history_dir>/<CCY1><CCY2>_yyyymmdd_hhmmss.csv  a copy of the Fenics file

A pair's extra tenors go out in the same notation (3W: 3wk / 3w, 18M: 18mo / 18m) and its
extra deltas like the 10D (5D: vol5,...,strangle / vol,s5,... and rr5).

Front Arena knows some currencies under another code (config frontarena_ccy,
e.g. CNH as CNY): its file name and lines use that code, Fenics keeps the pair's.

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


def fa_tenor(t: str) -> str:
    """Front Arena's tenor code: 1d, 3wk, 4mo, 1yr ..."""
    return FA_TENORS.get(t) or t[:-1] + {"W": "wk", "M": "mo", "Y": "yr"}[t[-1]]


def fen_tenor(t: str) -> str:
    """Fenics' tenor code: 1d, 3w, 4m, 1y ..."""
    return FEN_TENORS.get(t) or t.lower()


def fa_pair(pair: str, fa_ccy: dict | None = None) -> tuple[str, str]:
    """The pair's two currencies as Front Arena knows them (USDCNH -> USD, CNY)."""
    return tuple((fa_ccy or {}).get(c, c) for c in split_pair(pair))


def _n(x: float | None) -> str:
    """A number as VBA's implicit conversion writes it (15 significant digits)."""
    if x is None:
        return ""
    s = f"{x:.15g}"
    return "0" if s == "-0" else s


def _lines(lines: list[str]) -> str:
    return "".join(l + "\r\n" for l in lines)


def frontarena_lines(pair: str, resolved: dict, spreads: dict[str, float],
                     fa_ccy: dict | None = None) -> list[str]:
    c1, c2 = fa_pair(pair, fa_ccy)
    tenors = list(resolved)
    out = []
    for t in tenors:
        atm = resolved[t]["mid"]["atm"]
        half = spreads[t] / 2
        out.append(f"vol,{c1},{c2},{fa_tenor(t)},{_n(None if atm is None else atm - half)},"
                   f"{_n(None if atm is None else atm + half)}")
    for d in _deltas(resolved):
        for name, mark in (("strangle", f"bf{d}"), ("riskreversal", f"rr{d}")):
            out += [f"vol{d},{c1},{c2},{name},{fa_tenor(t)},{_n(resolved[t]['mid'][mark])}" for t in tenors]
    return out


def _deltas(resolved: dict) -> list[int]:
    """25, 10, then the pair's extra deltas from the inside out (the files' order so far)."""
    marks = next(iter(resolved.values()))["mid"] if resolved else {}
    extra = sorted((int(m[2:]) for m in marks if m.startswith("rr") and m not in ("rr25", "rr10")), reverse=True)
    return [25, 10] + extra


def fenics_lines(pair: str, resolved: dict, inverse: bool = False) -> list[str]:
    """inverse: Fenics reads the pair's RRs in the inverted pair's terms (e.g. the
    desk marks NOKCHF RRs as CHFNOK), so they go out with the sign flipped."""
    c1, c2 = split_pair(pair)
    tenors = list(resolved)
    out = [f"vol,atm,{c1},{c2},{fen_tenor(t)},{_n(resolved[t]['quoted']['atm_bid'])},"
           f"{_n(resolved[t]['quoted']['atm_offer'])}" for t in tenors]
    for d in _deltas(resolved):
        for code, mark in ((f"s{d}", f"bf{d}"), (f"rr{d}", f"rr{d}")):
            sign = -1 if inverse and mark.startswith("rr") else 1
            vals = [resolved[t]["quoted"][mark] for t in tenors]
            vals = [None if v is None else sign * v for v in vals]
            out += [f"vol,{code},{c1},{c2},{fen_tenor(t)},{_n(v)},{_n(v)}" for t, v in zip(tenors, vals)]
    return out


def dc_spread_lines(spreads: dict[str, float], dc_factor: float) -> list[str]:
    return [f"{fen_tenor(t)},{_n(v * 0.5 * dc_factor)}" for t, v in spreads.items()]


def write_exports(rec: dict, resolved: dict, dirs: dict[str, Path], history: bool = True,
                  now: datetime | None = None, fa_ccy: dict | None = None) -> None:
    """dirs: fenics, frontarena, dc_spread, history. fa_ccy: Front Arena's own currency codes."""
    pair = rec["pair"]
    c1, c2 = split_pair(pair)
    f1, f2 = fa_pair(pair, fa_ccy)
    spreads = {t: rec["tenors"][t]["spread"] for t in resolved}
    fen = _lines(fenics_lines(pair, resolved, bool(rec.get("inverse"))))
    # no flush to disk: the rename keeps readers safe, and the next publish rewrites them anyway
    atomic_write(Path(dirs["frontarena"]) / f"{f1}{f2}volsurface.txt", _lines(frontarena_lines(pair, resolved, spreads, fa_ccy)), fsync=False)
    atomic_write(Path(dirs["fenics"]) / f"{c1}{c2}volsurface.txt", fen, fsync=False)
    dc = rec.get("dc_factor") or 0
    if dc >= 0.01:
        atomic_write(Path(dirs["dc_spread"]) / f"{c1}{c2}spreads.txt", _lines(dc_spread_lines(spreads, dc)), fsync=False)
    if history:
        stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
        atomic_write(Path(dirs["history"]) / f"{c1}{c2}_{stamp}.csv", fen, fsync=False)


def read_frontarena(text: str, pair: str, partial: bool = False, fa_ccy: dict | None = None) -> dict[str, dict]:
    """Parse a Front Arena volsurface file (as the Excel macro writes it) back
    into marks per tenor: atm (mid), spread (ask - bid), rr25, rr10, bf25, bf10.
    Raises ValueError if the file is incomplete, e.g. while Excel is writing it;
    partial=True returns whatever marks the file has instead."""
    c1, c2 = fa_pair(pair, fa_ccy)
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
