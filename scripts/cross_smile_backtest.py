"""Backtest: how well do cross RRs and BFs follow from the legs' smiles?

    python scripts/cross_smile_backtest.py --data <data folder> [--from 2025-01-01] [--to ...]
           [--pairs EURCHF,EURJPY] [--via USD,EUR] [--tenors 1M,3M,1Y] [--step 5] [--bf smile]
           [--jobs 4] [--out file.csv]

Reads the end-of-day marks (marks_eod in the analysis folder, see export_history.py) and,
for every cross whose two legs are marked too (EURCHF from EURUSD and USDCHF, NOKSEK from
EURNOK and EURSEK with --via USD,EUR), rebuilds the cross smile from the legs every day and
tenor in three ways (volmarker/crosssmile.py):

  flat     one correlation, stress independent across the legs: the baseline. The
           correlation matches the cross ATM, as for a derived pair today.
  shared   shared stress fitted to the cross's ATM, 25D RR and 25D BF (rho_calm,
           rho_stress, share). Shows whether the model can fit; the 10D is out of sample.
  others   shared stress with share and rho_stress - rho_calm fixed at the median of the
           other crosses' fits for the tenor; only rho_calm matches the cross ATM. This is
           what a cross with no market (CHFZAR) would get.

Writes one row per day, cross and tenor to --out (default cross_smile_backtest.csv in the
current folder) and prints the mean absolute errors against the desk's marks, the legs' fit
error and how stable the fitted parameters are. Only reads the data folder.

Conventions and simplifications (see crosssmile.py): BFs are broker strangles as the desk
marks them (--bf smile for smile BFs); forward deltas, no rates; premium-adjusted deltas except
for EURUSD, GBPUSD, AUDUSD, NZDUSD.

Run time is roughly 0.1 s per cross, tenor and day on one core, spread over all cores
(--jobs); --step 5 (weekly) or --from/--to keep a first run short.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import os
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from volmarker.conventions import sort_tenors, split_pair  # noqa: E402
from volmarker.crosssmile import (Triangle, calibrate_fixed, calibrate_flat, calibrate_shared,  # noqa: E402
                                  fit_leg)
from volmarker.store import Store  # noqa: E402

MARKS = ("atm", "rr25", "bf25", "rr10", "bf10")
METHODS = ("flat", "shared", "others")


def read_marks(folder: Path, start: date | None, end: date | None) -> dict:
    """{(date, pair, tenor): (t in years, {atm, rr25, ...})} from marks_eod/*.csv.gz."""
    out = {}
    for p in sorted((folder / "marks_eod").glob("*.csv.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as g:
            text = g.read()
        for r in csv.DictReader(io.StringIO(text)):
            if r.get("date") == "date":            # a header repeated by an appended gzip member
                continue
            try:
                d = date.fromisoformat(r["date"])
                exp = date.fromisoformat(r["expiry"])
                q = {m: float(r["atm_mid" if m == "atm" else m]) for m in MARKS}
            except (KeyError, ValueError):
                continue
            if (start and d < start) or (end and d > end) or exp <= d:
                continue
            out[(d, r["pair"], r["tenor"])] = ((exp - d).days / 365.0, q)
    return out


def find_legs(pairs: set[str], via: list[str]) -> dict[str, tuple[str, str]]:
    """Crosses whose legs through one of the via currencies are both in the data. Pairs with
    the first via currency (USD) in them are legs, never crosses."""
    def leg(a, c):
        return next((p for p in (a + c, c + a) if p in pairs), None)

    out = {}
    for p in sorted(pairs):
        a, b = split_pair(p)
        if via and via[0] in (a, b):
            continue
        for c in via:
            if c in (a, b):
                continue
            l1, l2 = leg(a, c), leg(b, c)
            if l1 and l2:
                out[p] = (l1, l2)
                break
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", help="VolMarker's shared data folder (its analysis_dir is read)")
    ap.add_argument("--analysis", help="the analysis folder itself, instead of --data")
    ap.add_argument("--from", dest="start", type=date.fromisoformat)
    ap.add_argument("--to", dest="end", type=date.fromisoformat)
    ap.add_argument("--pairs", help="crosses to test, e.g. EURCHF,EURJPY (default: all with both legs)")
    ap.add_argument("--via", default="USD", help="common currencies to try, in order (default USD)")
    ap.add_argument("--tenors", help="tenors to test (default: all but ON)")
    ap.add_argument("--step", type=int, default=1, help="use every n-th day (default 1)")
    ap.add_argument("--bf", choices=("broker", "smile"), default="broker",
                    help="BF convention of the marks (default broker strangles)")
    ap.add_argument("--jobs", type=int, help="processes to run on (default: one per CPU core)")
    ap.add_argument("--out", default="cross_smile_backtest.csv")
    a = ap.parse_args()
    if not (a.data or a.analysis):
        ap.error("give --data or --analysis")

    folder = Path(a.analysis) if a.analysis else Store(a.data).path_from_config_value(
        Store(a.data).config().get("analysis_dir") or "analysis")
    marks = read_marks(folder, a.start, a.end)
    if not marks:
        sys.exit(f"No end-of-day marks in {folder / 'marks_eod'} (run scripts/export_history.py first)")
    pairs = {k[1] for k in marks}
    crosses = find_legs(pairs, [c.strip().upper() for c in a.via.split(",") if c.strip()])
    if a.pairs:
        want = {p.strip().upper() for p in a.pairs.split(",")}
        crosses = {p: v for p, v in crosses.items() if p in want}
    if not crosses:
        sys.exit(f"No crosses with both legs marked among {', '.join(sorted(pairs))}")
    tenors = {t.strip().upper() for t in a.tenors.split(",")} if a.tenors else None
    days = sorted({k[0] for k in marks})[::max(1, a.step)]
    print(f"{len(days)} days from {days[0]} to {days[-1]}; crosses: "
          + ", ".join(f"{p} ({l1}, {l2})" for p, (l1, l2) in crosses.items()))

    by_day = defaultdict(lambda: defaultdict(dict))
    for (d, pair, tenor), v in marks.items():
        by_day[d][pair][tenor] = v
    jobs = max(1, a.jobs or os.cpu_count() or 1)
    print(f"Running on {jobs} processes")
    rows, failed, t0 = [], 0, time.time()
    with _pool(jobs) as pool:
        work = [(d, {p: dict(v) for p, v in by_day[d].items()}, crosses, tenors, a.bf) for d in days]
        for n, (got, errors) in enumerate(pool.map(day_rows, work), 1):
            rows += got
            failed += len(errors)
            for e in errors:
                print("  " + e)
            if n % 20 == 0 or n == len(days):
                print(f"  {n}/{len(days)} days, {len(rows)} rows, {time.time() - t0:.0f}s")

        # others: each cross with share and rho_stress - rho_calm from the other crosses' fits
        fits = defaultdict(list)
        for r in rows:
            fits[r["tenor"]].append((r["cross"], r["shared_share"], r["shared_rho_stress"] - r["shared_rho_calm"]))
        work, which = [], []
        for r in rows:
            other = [(s, dr) for c, s, dr in fits[r["tenor"]] if c != r["cross"]]
            if other:
                work.append((r["_tri"], r["_q"], statistics.median(s for s, _ in other),
                             statistics.median(dr for _, dr in other), a.bf))
                which.append(r)
        for r, got in zip(which, pool.map(others_row, work, chunksize=16)):
            if got is None:
                failed += 1
            else:
                r.update(got)

    cols = [c for c in rows[0] if not c.startswith("_")] if rows else []
    for m in ("others",):
        cols += [f"{m}_{x}" for x in ("rho_calm", "rho_stress", "share", *MARKS)]
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items() if k in cols})
    print(f"\nWrote {len(rows)} rows to {a.out}" + (f" ({failed} failed)" if failed else ""))
    summary(rows)


def day_rows(job) -> tuple[list[dict], list[str]]:
    """One day: every cross and tenor, flat and shared fits."""
    d, day, crosses, tenors, bf = job
    legs, rows, errors = {}, [], []

    def get_leg(pair, tenor):
        if (pair, tenor) not in legs:
            v = day.get(pair, {}).get(tenor)
            legs[(pair, tenor)] = fit_leg(pair, v[0], v[1], bf=bf) if v else None
        return legs[(pair, tenor)]

    for cross, (p1, p2) in crosses.items():
        for tenor, (t, q) in day.get(cross, {}).items():
            if tenors and tenor not in tenors or not tenors and tenor == "ON":
                continue
            try:
                l1, l2 = get_leg(p1, tenor), get_leg(p2, tenor)
                if not (l1 and l2):
                    continue
                tri = Triangle(cross, l1, l2)
                row = {"date": d.isoformat(), "cross": cross, "tenor": tenor, "t": round(t, 5),
                       "leg1": p1, "leg2": p2, "via": tri.common, "leg1_rmse": l1.rmse, "leg2_rmse": l2.rmse,
                       "p1": tri.c1[1][0], "p2": tri.c2[1][0], "_tri": tri, "_q": q}
                row.update({f"mark_{m}": q[m] for m in MARKS})
                for name, par in (("flat", calibrate_flat(tri, q)), ("shared", calibrate_shared(tri, q, bf=bf))):
                    _put(row, name, tri, par, bf)
                rows.append(row)
            except (ValueError, ZeroDivisionError, OverflowError) as e:
                errors.append(f"{d} {cross} {tenor}: {e}")
    return rows, errors


def others_row(job) -> dict | None:
    tri, q, share, d_rho, bf = job
    try:
        row = {}
        _put(row, "others", tri, calibrate_fixed(tri, q, share, d_rho), bf)
        return row
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


class _Serial:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, f, it, chunksize=1):
        return map(f, it)


def _pool(jobs: int):
    return ProcessPoolExecutor(jobs) if jobs > 1 else _Serial()


def _put(row: dict, name: str, tri: Triangle, par: dict, bf: str) -> None:
    row[f"{name}_rho_calm"] = par["rho_calm"]
    row[f"{name}_rho_stress"] = par["rho_stress"]
    row[f"{name}_share"] = "" if par["share"] is None else par["share"]
    for m, v in tri.pricer(par["rho_calm"], par["rho_stress"], par["share"]).quotes(bf=bf).items():
        row[f"{name}_{m}"] = v


def summary(rows: list[dict]) -> None:
    if not rows:
        return
    by = defaultdict(list)
    for r in rows:
        by[r["cross"]].append(r)
        by[f"  {r['tenor']}"].append(r)

    def mae(rs, m, mk):
        v = [abs(r[f"{m}_{mk}"] - r[f"mark_{mk}"]) for r in rs if isinstance(r.get(f"{m}_{mk}"), float)]
        return f"{sum(v) / len(v):5.2f}" if v else "    -"

    print("\nMean absolute error against the marks (vol points); ATM is matched by every method")
    head = "".join(f" | {m:^23}" for m in METHODS)
    print(f"{'':10}{'legs':>6}{head}")
    print(f"{'':10}{'fit':>6}" + " | rr25  bf25  rr10  bf10" * len(METHODS))
    keys = sorted(k for k in by if not k.startswith(" ")) + ["by tenor"] + \
        sort_tenors_keys([k for k in by if k.startswith(" ")])
    for k in keys:
        if k == "by tenor":
            print("by tenor")
            continue
        rs = by[k]
        leg = statistics.mean(max(r["leg1_rmse"], r["leg2_rmse"]) for r in rs)
        line = f"{k:10}{leg:6.2f}"
        for m in METHODS:
            line += " | " + " ".join(mae(rs, m, mk) for mk in ("rr25", "bf25", "rr10", "bf10"))
        print(line)

    print("\nFitted shared-stress parameters (median [10%, 90%])")
    print(f"{'':10}{'share':>22}{'rho_stress - rho_calm':>26}{'rho_calm':>22}")
    for k in sorted(k for k in by if not k.startswith(" ")):
        rs = by[k]
        cells = []
        for f in (lambda r: r["shared_share"], lambda r: r["shared_rho_stress"] - r["shared_rho_calm"],
                  lambda r: r["shared_rho_calm"]):
            v = sorted(f(r) for r in rs)
            q = lambda x: v[min(len(v) - 1, int(x * len(v)))]
            cells.append(f"{statistics.median(v):6.2f} [{q(0.1):5.2f}, {q(0.9):5.2f}]")
        print(f"{k:10}" + "".join(f"{c:>24}" for c in cells))
    print("\nlegs fit: the worse leg's RMSE over its ATM, RRs and BFs. shared fits ATM and 25D, so its 10D is "
          "out of sample;\nothers fixes share and rho_stress - rho_calm at the other crosses' medians (the "
          "CHFZAR case).")


def sort_tenors_keys(keys: list[str]) -> list[str]:
    try:
        return ["  " + t for t in sort_tenors([k.strip() for k in keys])]
    except ValueError:
        return sorted(keys)


if __name__ == "__main__":
    main()
