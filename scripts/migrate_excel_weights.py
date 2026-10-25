"""Migrate the event weights from the Excel weight workbook (Events USD.xlsm) in one go:

  1. "Event weights"  -> the relevant events and their default weight (calendar/event_defaults.csv)
  2. "Holidays"       -> holidays per currency (calendar/holidays.csv), with the pair sheets' holidays
  3. "Event dates"    -> dated events (calendar/events.csv); the dates are already the expiry
                         each event weighs on, as events.csv expects
  4. pair sheets (EURUSD, USDCHF, ...) -> each pair's weekday weights, holiday factors and
                         event weights (as scripts/import_excel_pair_weights.py)

    python scripts/migrate_excel_weights.py "Events USD.xlsm" --data "S:\\FXO\\VolMarker\\data"
        [--apply] [--pairs EURUSD,USDCHF] [--all-dates] [--replace-events]

Without --apply nothing is written: each step reports what it would add or change, and
each pair is compared with the workbook as the calendar will be after the import.

Nothing in VolMarker is removed. Events already on the list keep their weight
(--replace-events: the workbook's list replaces the whole list). Holidays and dated events
are added only where missing, from today on. A dated event that a Bloomberg import already
has (same currency and name, released up to 5 days before the date) is left out, so it
doesn't count twice. Pairs must already be set up in VolMarker.
"""

import argparse
import csv
import io
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_excel_pair_weights import (import_pairs, load_sheets, missing_holidays, sheet_holidays,  # noqa: E402
                                       write_holidays)
from volmarker.excel_weights import parse_event_dates, parse_event_weights, parse_holidays, read_cells  # noqa: E402
from volmarker.service import Service  # noqa: E402
from volmarker.store import Store, atomic_write  # noqa: E402
from volmarker.surface import now_iso  # noqa: E402

SHOW = 8        # examples listed per step
BBG_DAYS = 5    # a Bloomberg release up to this many days before an Excel date is the same event


def _sample(items: list[str]) -> str:
    return ", ".join(items[:SHOW]) + (f" (+{len(items) - SHOW})" if len(items) > SHOW else "")


def merge_rules(cur: list[dict], new: list[dict], replace: bool) -> tuple[list[dict], list[dict], list[str]]:
    """(the list after the import, the events added, events whose weight differs and is kept)."""
    if replace:
        return new, new, []
    have = {(r["ccy"], r["event"].lower()): r for r in cur}
    added, differ = [], []
    for r in new:
        old = have.get((r["ccy"], r["event"].lower()))
        if old is None:
            added.append(r)
            have[(r["ccy"], r["event"].lower())] = r
        elif abs(old["weight"] - r["weight"]) > 1e-9:
            differ.append(f"{r['ccy']} {r['event']} {old['weight']:g} (Excel {r['weight']:g})")
    return cur + added, added, differ


def new_dated_events(store: Store, events: list[dict], today: date) -> tuple[list[dict], list[str]]:
    """(events to add to events.csv, those left out because a Bloomberg import has them)."""
    _, cal = store.load_calendar()
    manual = {(e["ccy"], e["date"], e["desc"].lower()) for e in cal if e["source"] == "manual"}
    bbg: dict[tuple[str, str], list[date]] = {}
    for e in cal:
        if e["source"] == "bloomberg":
            bbg.setdefault((e["ccy"], e["desc"].lower()), []).append(date.fromisoformat(e["date"]))
    add, dup = [], []
    for e in events:
        d = date.fromisoformat(e["date"])
        if d < today or (e["ccy"], e["date"], e["desc"].lower()) in manual:
            continue
        if any(d - timedelta(days=BBG_DAYS) <= b <= d for b in bbg.get((e["ccy"], e["desc"].lower()), [])):
            dup.append(f"{e['ccy']} {e['date']} {e['desc']}")
            continue
        add.append(e)
        manual.add((e["ccy"], e["date"], e["desc"].lower()))
    return add, dup


def write_events(store: Store, add: list[dict]) -> None:
    path = store.root / "calendar" / "events.csv"
    rows = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = [(r["ccy"], r["date"], r["description"]) for r in csv.DictReader(f)]
    rows += [(e["ccy"], e["date"], e["desc"]) for e in add]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["ccy", "date", "description"])
    w.writerows(sorted(rows, key=lambda r: (r[1], r[0], r[2])))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, out.getvalue())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workbook")
    ap.add_argument("--data", required=True)
    ap.add_argument("--user", default="excel-import")
    ap.add_argument("--pairs", help="pair sheets to import, comma-separated (default: every pair sheet)")
    ap.add_argument("--all-dates", action="store_true", help="copy every date's Excel weight, not only the overrides")
    ap.add_argument("--replace-events", action="store_true", help="the workbook's relevant events replace the list")
    ap.add_argument("--apply", action="store_true", help="write and publish (default: report only)")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today(), help=argparse.SUPPRESS)
    a = ap.parse_args()
    today = a.today
    want = {p.strip().upper().replace("/", "") for p in a.pairs.split(",")} if a.pairs else None

    sheets_needed = ["Event weights", "Holidays", "Event dates"]
    cells = read_cells(a.workbook, sheets_needed)
    missing = [s for s in sheets_needed if s not in cells]
    if missing:
        sys.exit(f"Not in the workbook: {', '.join(missing)}")
    store = Store(a.data)
    svc = Service(store, a.user)
    verb = "added" if a.apply else "to add"

    # 1. relevant events
    cur = store.load_event_defaults()
    rules, added, differ = merge_rules(cur["rows"], parse_event_weights(cells["Event weights"]), a.replace_events)
    print("== 1. Relevant events (Event weights)")
    if a.replace_events:
        print(f"   the list of {len(cur['rows'])} events {'is' if a.apply else 'would be'} replaced by the "
              f"workbook's {len(rules)}")
    else:
        print(f"   {len(added)} {verb}; {len(cur['rows'])} already on the list")
        if differ:
            print(f"   {len(differ)} on the list with another weight, kept (--replace-events takes Excel's): "
                  f"{_sample(differ)}")

    # 2. holidays: the Holidays sheet and the pair sheets
    sheets = load_sheets(a.workbook, want)
    hols = parse_holidays(cells["Holidays"])
    for ccy, ds in sheet_holidays(sheets).items():
        hols.setdefault(ccy, []).extend(ds)
    new_hols = missing_holidays(store, hols, today)
    print("\n== 2. Holidays (Holidays sheet and pair sheets)")
    per_ccy: dict[str, int] = {}
    for ccy, _ in new_hols:
        per_ccy[ccy] = per_ccy.get(ccy, 0) + 1
    print(f"   {len(new_hols)} {verb}" + (f": {', '.join(f'{c} {n}' for c, n in sorted(per_ccy.items()))}"
                                          if per_ccy else ""))

    # 3. dated events
    add_ev, dup = new_dated_events(store, parse_event_dates(cells["Event dates"]), today)
    print("\n== 3. Event dates")
    print(f"   {len(add_ev)} {verb} to calendar/events.csv")
    if dup:
        print(f"   {len(dup)} left out, already in a Bloomberg import: {_sample(dup)}")
    unlisted = sorted({f"{e['ccy']} {e['desc']}" for e in add_ev}
                      - {f"{r['ccy']} {r['event']}" for r in rules}, key=str.lower)
    if unlisted:
        print(f"   {len(unlisted)} of them not on the relevant events list, so they get no weight: "
              f"{_sample(unlisted)}")

    if a.apply:
        store.save_event_defaults(rules, cur["version"], a.user, now_iso())
        write_holidays(store, new_hols)
        write_events(store, add_ev)

    # 4. the pairs, compared with the workbook as the calendar is after steps 1-3
    print("\n== 4. Pair sheets")
    if not sheets:
        print("   no pair sheets found (a pair sheet has the pair in B2, e.g. EUR/USD)")
        return
    holidays, cal = store.load_calendar()
    if not a.apply:
        for ccy, d in new_hols:
            holidays.setdefault(ccy, set()).add(date.fromisoformat(d))
        cal = cal + [dict(e, source="manual") for e in add_ev]
    import_pairs(svc, sheets, Path(a.workbook).name, today, a.all_dates, a.apply, True, holidays, cal, rules)


if __name__ == "__main__":
    main()
