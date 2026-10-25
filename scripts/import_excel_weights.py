"""Load the relevant events and their weights from the Excel weight workbook
(sheet "Event weights": per currency an event-name column and a weight
column, currency names in row 4) into the desk's list of relevant events.

    python scripts/import_excel_weights.py "Events USD.xlsm" --data "S:\\FXO\\VolMarker\\data" [--replace]

Without --replace, events already on the list keep their weight.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from volmarker.excel_weights import parse_event_weights, read_cells  # noqa: E402
from volmarker.store import Store  # noqa: E402


def excel_rules(path: str) -> list[dict]:
    return parse_event_weights(read_cells(path, ["Event weights"])["Event weights"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--data", required=True)
    ap.add_argument("--user", default="excel-import")
    ap.add_argument("--replace", action="store_true", help="replace the whole list")
    a = ap.parse_args()
    store = Store(a.data)
    new = excel_rules(a.workbook)
    cur = store.load_event_defaults()
    if a.replace:
        rows = new
    else:
        have = {(r["ccy"], r["event"].lower()) for r in cur["rows"]}
        rows = cur["rows"] + [r for r in new if (r["ccy"], r["event"].lower()) not in have]
    from volmarker.surface import now_iso
    store.save_event_defaults(rows, cur["version"], a.user, now_iso())
    print(f"{len(new)} events in the workbook; the list now has {len(rows)} events")


if __name__ == "__main__":
    main()
