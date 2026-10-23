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

from volmarker.calendar import normalise_weights  # noqa: E402
from volmarker.excel_weights import excel_day_events, pair_events, parse_pair_sheet, read_cells  # noqa: E402
from volmarker.service import Service  # noqa: E402
from volmarker.store import Store, atomic_write  # noqa: E402

SHOW = 12   # differences listed per pair


def fmt_w(x) -> str:
    return f"{x:g}"


def add_holidays(store: Store, sheets: list[dict], today: date) -> int:
    path = store.root / "calendar" / "holidays.csv"
    have, _ = store.load_calendar()
    rows = set()
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = {(r["ccy"].upper(), r["date"]) for r in csv.DictReader(f)}
    new = {(ccy, d.isoformat()) for s in sheets for ccy, ds in s["holidays"].items()
           for d in ds if d >= today and d not in have.get(ccy, set())}
    if new:
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["ccy", "date"])
        w.writerows(sorted(rows | new, key=lambda r: (r[1], r[0])))
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, out.getvalue())
    return len(new)


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

    sheets = []
    for name, cells in read_cells(a.workbook).items():
        try:
            s = parse_pair_sheet(cells)
        except ValueError as e:
            print(f"Sheet {name}: {e}")
            continue
        if s and (want is None or s["pair"] in want):
            sheets.append(s)
    if not sheets:
        sys.exit("No pair sheets found (a pair sheet has the pair in B2, e.g. EUR/USD)")

    store = Store(a.data)
    svc = Service(store, a.user)
    if a.holidays:
        n = add_holidays(store, sheets, today) if a.apply else None
        print(f"Holidays: {n} added to calendar/holidays.csv" if a.apply else "Holidays: added with --apply")
    holidays, cal = store.load_calendar()

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
        own = pair_events(s, today, a.all_dates)
        new["events"] = [e for e in old.get("events") or [] if e["date"] < today.isoformat()] + own
        old_own = [e for e in old.get("events") or [] if e["date"] >= today.isoformat()]

        if old["weekdays"] != new["weekdays"]:
            print(f"   weekday weights {' '.join(map(fmt_w, old['weekdays']))}  ->  {' '.join(map(fmt_w, new['weekdays']))}")
        for i, ccy in ((1, c1), (2, c2)):
            if old[f"holiday{i}"] != new[f"holiday{i}"]:
                print(f"   {ccy} holiday factor {fmt_w(old[f'holiday{i}'])}  ->  {fmt_w(new[f'holiday{i}'])}")
        print(f"   own event weights from today: {len(old_own)}  ->  {len(own)} "
              f"({'every Excel date' if a.all_dates else 'the dates with an Override in Excel'})")
        for ccy, ov in s["holiday_overrides"].items():
            if ov:
                print(f"   NOT imported: {ccy} holiday overrides {', '.join(f'{d} {fmt_w(v)}' for d, v in ov)}")

        # where VolMarker's weights would still differ from the workbook
        xl = excel_day_events(s, today)
        horizon = max(xl) if xl else today.isoformat()
        eff = svc.effective_weights(pair, new, cal, holidays, rec)
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
                      f"{f' (+{len(missing) - 8})' if len(missing) > 8 else ''}" + ("" if a.holidays else "  (--holidays adds them)"))

        if new == old:
            print("   no change")
        elif a.apply:
            pub = svc.publish(pair, dict(rec, weights=new), rec["version"],
                              f"event weights from {Path(a.workbook).name} ({pair} sheet)")
            print(f"   published v{pub['version']}")
            changed += 1
        else:
            changed += 1
    print(f"\n{changed} pair(s) {'updated' if a.apply else 'to update; run again with --apply to publish'}")


if __name__ == "__main__":
    main()
