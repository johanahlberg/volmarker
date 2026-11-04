"""Back-fill the analysis tables: the whole vol history up to yesterday, in month files.

    python scripts/export_history.py --data <data folder> [--legacy <folder>] [--to 2026-11-03]

Run it once, by hand. From then on the leader app adds each finished day by itself every
morning (config analysis_dir). Running it again rewrites the months it covers, so it is
also the way to rebuild the tables, e.g. after adding old files. Only reads the history;
nothing else in the data folder changes.

Writes to the analysis folder (analysis_dir, default <data folder>/analysis):
  marks/YYYY-MM.csv.gz              every snapshot, one row per snapshot and tenor
  marks_eod/YYYY-MM.csv.gz          each pair's last snapshot of every day
  atm_eod_wide/YYYY-MM.csv.gz       end-of-day ATM mids, one column per pair and tenor
  market/YYYY-MM/YYYY-MM-DD.csv.gz  the recorded FMD quotes of the day

Sources: every VolMarker version (archive included); the dated Fenics copies
<PAIR>_yyyymmdd_hhmmss.csv from before VolMarker, in --legacy (default history_dir), read
in calendar_tz; and the recorded FMD market. See volmarker/analysis.py.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from volmarker.analysis import Exporter  # noqa: E402
from volmarker.store import Store  # noqa: E402
from volmarker.tz import to_local  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="VolMarker's shared data folder")
    ap.add_argument("--legacy", help="folder with the dated Fenics copies (default: history_dir)")
    ap.add_argument("--out", help="analysis folder (default: analysis_dir)")
    ap.add_argument("--tz", help="local time zone of days and of the copies' time stamps (default: calendar_tz)")
    ap.add_argument("--to", dest="end", type=date.fromisoformat, help="last day (default: yesterday)")
    a = ap.parse_args()

    store = Store(a.data)
    ex = Exporter(store, Path(a.out) if a.out else None, a.tz, Path(a.legacy) if a.legacy else None)
    end = a.end or to_local(datetime.now(timezone.utc), ex.zone).date() - timedelta(days=1)
    print(f"Back-filling up to {end} into {ex.out}")
    print(f"Dated copies: {ex.legacy or 'none'} ({sum(len(v) for v in ex.legacy_files().values())} files)")
    c = ex.backfill(end)
    print(f"Done: {c['pairs']} pairs, {c['snapshots']} snapshots ({c['excel_files']} from dated copies), "
          f"{c['months']} months, {c['market_quotes']} market quotes")
    if c["unreadable"]:
        print(f"{c['unreadable']} dated copies could not be read and were left out (see the log)")


if __name__ == "__main__":
    main()
