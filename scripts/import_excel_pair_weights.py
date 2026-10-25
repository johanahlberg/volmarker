"""Load each pair's event weights from the per-pair sheets of the Excel weight
workbook (one sheet per pair, e.g. "EURUSD"): weekday weights, holiday factors
and the event weights per date. Pairs must already exist in VolMarker.

    python scripts/import_excel_pair_weights.py "Events USD.xlsm" --data "S:\\FXO\\VolMarker\\data"
        [--pairs EURUSD,USDCHF] [--all-dates] [--holidays] [--apply]

Without --apply it only reports what would change, and where VolMarker's weights
would still differ from the workbook's (another event calendar or desk weights).

Events: by default only the dates with an Override in Excel become the pair's
own weights; the other dates keep coming from VolMarker's desk defaults, as in
Excel. --all-dates copies every date's Excel weight instead, so the weights
match the workbook exactly but the desk defaults no longer apply to those dates.

--holidays adds the workbook's holidays that VolMarker's calendar lacks to
calendar/holidays.csv.
"""

import argparse
import csv
import io
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from volmarker.calendar import normalise_weights, with_defaults  # noqa: E402
from volmarker.excel_weights import excel_day_events, pair_events, parse_pair_sheet, read_cells  # noqa: E402
from volmarker.service import Service  # noqa: E402
from volmarker.store import Store, atomic_write  # noqa: E402

SHOW = 12   # differences listed per pair


def fmt_w(x) -> str:
    return f"{x:g}"


def missing_holidays(store: Store, holidays: dict[str, list[date]], today: date) -> set[tuple[str, str]]:
    """(ccy, date) from today on that VolMarker's calendar lacks."""
    have, _ = store.load_calendar()
    return {(ccy, d.isoformat()) for ccy, ds in holidays.items()
            for d in ds if d >= today and d not in have.get(ccy, set())}


def write_holidays(store: Store, new: set[tuple[str, str]]) -> None:
    path = store.root / "calendar" / "holidays.csv"
    rows = set()
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = {(r["ccy"].upper(), r["date"]) for r in csv.DictReader(f)}
    if new:
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["ccy", "date"])
        w.writerows(sorted(rows | new, key=lambda r: (r[1], r[0])))
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, out.getvalue())


def sheet_holidays(sheets: list[dict]) -> dict[str, list[date]]:
    out: dict[str, list[date]] = {}
    for s in sheets:
        for ccy, ds in s["holidays"].items():
            out.setdefault(ccy, []).extend(ds)
    return out


def load_sheets(workbook: str, want: set[str] | None) -> list[dict]:
    sheets = []
    for name, cells in read_cells(workbook).items():
        try:
            s = parse_pair_sheet(cells)
        except ValueError as e:
            print(f"Sheet {name}: {e}")
            continue
        if s and (want is None or s["pair"] in want):
            sheets.append(s)
    return sheets


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--data", required=True)
    ap.add_argument("--user", default="excel-import")
    ap.add_argument("--pairs", help="comma-separated, default: every pair sheet")
    ap.add_argument("--all-dates", action="store_true", help="copy every date's Excel weight, not only the overrides")
    ap.add_argument("--holidays", action="store_true", help="add the workbook's missing holidays to the calendar")
    ap.add_argument("--apply", action="store_true", help="publish the weights (default: report only)")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today(), help=argparse.SUPPRESS)
    a = ap.parse_args()
    today = a.today
    want = {p.strip().upper().replace("/", "") for p in a.pairs.split(",")} if a.pairs else None

    sheets = load_sheets(a.workbook, want)
    if not sheets:
        sys.exit("No pair sheets found (a pair sheet has the pair in B2, e.g. EUR/USD)")

    store = Store(a.data)
    svc = Service(store, a.user)
    if a.holidays:
        new = missing_holidays(store, sheet_holidays(sheets), today)
        if a.apply:
            write_holidays(store, new)
        print(f"Holidays: {len(new)} {'added to calendar/holidays.csv' if a.apply else 'to add with --apply'}")
    holidays, cal = store.load_calendar()
    import_pairs(svc, sheets, Path(a.workbook).name, today, a.all_dates, a.apply, a.holidays,
                 holidays, cal, store.load_event_defaults()["rows"])


def import_pairs(svc: Service, sheets: list[dict], workbook: str, today: date, all_dates: bool, apply: bool,
                 holidays_flag: bool, holidays: dict, cal: list[dict], rules: list[dict]) -> int:
    """Each pair sheet into its pair's weights. The comparison with the workbook uses the
    calendar and relevant events given (in a dry run: as they will be after the import)."""
    changed = 0
    for s in sheets:
        pair, (c1, c2) = s["pair"], s["ccys"]
        rec = svc.get(pair)
        print(f"\n== {pair}")
        if rec is None:
            print("   not in VolMarker: skipped (set the pair up first)")
            continue
        old = normalise_weights(rec.get("weights"))
        new = dict(old, weekdays=s["weekdays"])
        for i, ccy in ((1, c1), (2, c2)):
            if ccy in s["holiday"]:
                new[f"holiday{i}"] = s["holiday"][ccy]
        own = pair_events(s, today, all_dates)
        new["events"] = [e for e in old.get("events") or [] if e["date"] < today.isoformat()] + own
        old_own = [e for e in old.get("events") or [] if e["date"] >= today.isoformat()]

        if old["weekdays"] != new["weekdays"]:
            print(f"   weekday weights {' '.join(map(fmt_w, old['weekdays']))}  ->  {' '.join(map(fmt_w, new['weekdays']))}")
        for i, ccy in ((1, c1), (2, c2)):
            if old[f"holiday{i}"] != new[f"holiday{i}"]:
                print(f"   {ccy} holiday factor {fmt_w(old[f'holiday{i}'])}  ->  {fmt_w(new[f'holiday{i}'])}")
        print(f"   own event weights from today: {len(old_own)}  ->  {len(own)} "
              f"({'every Excel date' if all_dates else 'the dates with an Override in Excel'})")
        for ccy, ov in s["holiday_overrides"].items():
            if ov:
                print(f"   NOT imported: {ccy} holiday overrides {', '.join(f'{d} {fmt_w(v)}' for d, v in ov)}")

        # where VolMarker's weights would still differ from the workbook
        xl = excel_day_events(s, today)
        horizon = max(xl) if xl else today.isoformat()
        eff = with_defaults(pair, new, cal, rules, holidays, svc.cut_of(rec))
        vm: dict[str, float] = {}
        for e in eff["events"]:
            if today.isoformat() <= e["date"] <= horizon:
                vm[e["date"]] = round(vm.get(e["date"], 0.0) + float(e["weight"]), 6)
        diffs = [(d, xl.get(d, 0.0), vm.get(d, 0.0)) for d in sorted(set(xl) | set(vm))
                 if abs(xl.get(d, 0.0) - vm.get(d, 0.0)) > 1e-6]
        if diffs:
            print(f"   {len(diffs)} date(s) up to {horizon} where VolMarker's event weight differs (Excel / VolMarker):")
            for d, x, v in diffs[:SHOW]:
                print(f"     {d}  {fmt_w(x):>6} / {fmt_w(v):<6}")
            if len(diffs) > SHOW:
                print(f"     ... {len(diffs) - SHOW} more")
        else:
            print(f"   event weights match the workbook up to {horizon}")
        for ccy, ds in s["holidays"].items():
            ds = [d for d in ds if d >= today]
            missing = [d.isoformat() for d in ds if d not in holidays.get(ccy, set())]
            if missing:
                print(f"   {ccy} holidays not in VolMarker's calendar: {', '.join(missing[:8])}"
                      f"{f' (+{len(missing) - 8})' if len(missing) > 8 else ''}" + ("" if holidays_flag else "  (--holidays adds them)"))

        if new == old:
            print("   no change")
        elif apply:
            pub = svc.publish(pair, dict(rec, weights=new), rec["version"],
                              f"event weights from {workbook} ({pair} sheet)")
            print(f"   published v{pub['version']}")
            changed += 1
        else:
            changed += 1
    print(f"\n{changed} pair(s) {'updated' if apply else 'to update; run again with --apply to publish'}")
    return changed


if __name__ == "__main__":
    main()
