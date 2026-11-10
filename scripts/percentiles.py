"""Where today's ATM, RRs and BFs sit in their own history, as percentile ranks.

    python scripts/percentiles.py --data <data folder> [--pairs EURUSD,USDJPY] [--lookback 1Y]
                                  [--tenors 1W-1Y] [--csv out.csv] [--extremes 10]

For every pair, tenor and mark: the latest published value and its percentile rank in the
pair's end-of-day history (0 = lowest seen, 100 = highest, 50 = median). History comes
from the analysis tables (analysis_dir/marks_eod, written by export_history.py and then by
the leader every morning); days a pair wasn't republished carry its last mark forward
(weekdays only; --no-fill to use only the days it changed). Only reads.

  --lookback   3M, 6M, 1Y (default), 2Y, 90D ... or all; --from YYYY-MM-DD overrides it
  --tenors     a range such as 1W-1Y, or a list 1M,3M,1Y (default: every tenor)
  --min-obs    fewer history days than this shows no percentile (default 20)
  --extremes   list the marks at or beyond this percentile from either end (default 10)
  --csv        also write every row (with min, 10/25/50/75/90th and max) for Excel

Percentiles are of levels. RRs are call - put in the pair's own terms, as stored.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from volmarker.conventions import normalise_pair  # noqa: E402
from volmarker.percentiles import (MARKS, QUANTILES, analyse, current_marks, parse_lookback,  # noqa: E402
                                   read_eod, tenor_ok)
from volmarker.store import Store  # noqa: E402
from volmarker.tz import to_local  # noqa: E402

LABEL = {"atm": "ATM", "rr25": "25D RR", "rr10": "10D RR", "bf25": "25D BF", "bf10": "10D BF"}


def tenor_filter(text: str | None):
    if not text:
        return lambda t: True
    text = text.upper().replace(" ", "")
    if "-" in text:
        lo, hi = text.split("-", 1)
        return lambda t: tenor_ok(t, lo or None, hi or None)
    keep = set(text.split(","))
    return lambda t: t in keep


def cell(s, min_obs: int, flag: float) -> str:
    if s.current is None:
        return f"{'':>7} {'':>4}"
    if s.pct is None or s.n < min_obs:
        return f"{s.current:7.2f} {'  –':>4}"
    mark = "▲" if s.pct >= 100 - flag else "▼" if s.pct <= flag else " "
    return f"{s.current:7.2f} {s.pct:3.0f}{mark}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="VolMarker's shared data folder")
    ap.add_argument("--analysis", help="analysis folder (default: analysis_dir)")
    ap.add_argument("--pairs", help="comma-separated pairs (default: all)")
    ap.add_argument("--lookback", default="1Y")
    ap.add_argument("--from", dest="first", type=date.fromisoformat)
    ap.add_argument("--tenors")
    ap.add_argument("--min-obs", type=int, default=20)
    ap.add_argument("--extremes", type=float, default=10.0)
    ap.add_argument("--no-fill", action="store_true")
    ap.add_argument("--csv")
    a = ap.parse_args()

    store = Store(a.data)
    cfg = store.config()
    folder = Path(a.analysis) if a.analysis else store.path_from_config_value(cfg.get("analysis_dir") or "analysis")
    today = to_local(datetime.now(timezone.utc), cfg.get("calendar_tz") or "Europe/Zurich").date()
    until = today - timedelta(days=1)                        # the tables hold finished days
    first = a.first or parse_lookback(a.lookback, today)
    wanted = {normalise_pair(p) for p in a.pairs.split(",")} if a.pairs else None
    keep_tenor = tenor_filter(a.tenors)

    current = {}
    for pair in store.list_pairs():
        if wanted and pair not in wanted:
            continue
        rec = store.latest(pair)
        if rec:
            current[pair] = {t: v for t, v in current_marks(rec).items() if keep_tenor(t)}
    if wanted and wanted - set(current):
        print(f"Unknown pair(s): {', '.join(sorted(wanted - set(current)))}")
    history = read_eod(folder, set(current), first, until)
    stats = analyse(current, history, until, fill=not a.no_fill)

    span = f"{first} to {until}" if first else f"all history to {until}"
    print(f"Percentile of the latest published marks vs end-of-day history, {span}"
          f"{'' if a.no_fill else ' (weekdays, last mark carried forward)'}")
    print(f"History: {folder / 'marks_eod'}   ▲/▼ at or beyond {a.extremes:g}/{100 - a.extremes:g}   "
          f"– fewer than {a.min_obs} days\n")
    if not history:
        print("No history found. Run scripts/export_history.py once to build the analysis tables.")
    by_pair: dict[str, list] = {}
    for s in stats:
        by_pair.setdefault(s.pair, []).append(s)
    for pair, rows in by_pair.items():
        n = max((s.n for s in rows), default=0)
        print(f"{pair}   ({n} days)")
        print(f"{'Tenor':<6}" + "".join(f"{LABEL[m]:>13}" for m in MARKS))
        for tenor in dict.fromkeys(s.tenor for s in rows):
            cells = {s.mark: s for s in rows if s.tenor == tenor}
            print(f"{tenor:<6}" + "".join(f" {cell(cells[m], a.min_obs, a.extremes)}" for m in MARKS))
        print()

    ext = sorted((s for s in stats if s.pct is not None and s.n >= a.min_obs and
                  (s.pct <= a.extremes or s.pct >= 100 - a.extremes)), key=lambda s: -abs(s.pct - 50))
    if ext:
        print(f"Extremes ({len(ext)}), most extreme first")
        for s in ext:
            print(f"  {s.pair} {s.tenor:<4} {LABEL[s.mark]:<7} {s.current:7.2f}  pct {s.pct:5.1f}   "
                  f"range {s.lo:.2f} to {s.hi:.2f}, median {s.q[0.5]:.2f}")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["pair", "tenor", "mark", "current", "percentile", "days", "min",
                        *[f"p{int(q * 100)}" for q in QUANTILES], "max", "history_from", "history_to"])
            for s in stats:
                r = lambda v: "" if v is None else round(v, 4)  # noqa: E731
                w.writerow([s.pair, s.tenor, s.mark, r(s.current), r(s.pct), s.n, r(s.lo),
                            *[r(s.q.get(q)) for q in QUANTILES], r(s.hi), s.first or "", s.last or ""])
        print(f"\nWrote {a.csv}")


if __name__ == "__main__":
    main()
