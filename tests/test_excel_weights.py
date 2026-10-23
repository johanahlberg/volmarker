import unittest
from datetime import date

from volmarker.calendar import WEEKDAYS
from volmarker.excel_weights import excel_day_events, pair_events, parse_pair_sheet


def serial(d: str) -> str:
    return str((date.fromisoformat(d) - date(1899, 12, 30)).days)


def sheet() -> dict:
    """A pair sheet laid out like Events USD.xlsm (columns G..I, K..M, O..R, U..X)."""
    c = {(2, 2): "EUR/USD", (16, 2): "Weekday", (16, 3): "Weight", (25, 2): "EUR holiday", (25, 3): "0.4",
         (26, 2): "USD holiday", (26, 3): "0.3"}
    for i, (n, w) in enumerate(zip(WEEKDAYS, ["1.05", "1", "1", "1", "1", "0", "0"])):
        c[(17 + i, 2)], c[(17 + i, 3)] = n, w
    for col, ccy in ((7, "EUR"), (11, "USD"), (15, "EUR"), (21, "USD")):
        c[(2, col)], c[(2, col + 1)], c[(2, col + 2)] = ccy, "Event", "Weight"
    c[(2, 18)] = c[(2, 24)] = "Override"
    # full lists: two EUR events on one date
    for r, (col, d, name, w) in enumerate([(7, "2026-10-20", "Old", "0.1"), (7, "2026-10-29", "ECB", "0.6"),
                                           (7, "2026-10-29", "GE CPI", "0.1"), (11, "2026-10-29", "GDP", "0.2"),
                                           (11, "2026-11-06", "NFP", "0.4")], start=3):
        c[(r, col)], c[(r, col + 1)], c[(r, col + 2)] = serial(d), name, w
    # per-date tables: EUR 29 Oct overridden to 0.9; USD dates with their desk weights
    for r, (col, d, w, ov) in enumerate([(15, "2026-10-20", "0.1", None), (15, "2026-10-29", "0.9", "0.9"),
                                         (21, "2026-10-29", "0.2", None), (21, "2026-11-06", "0.4", None)], start=3):
        c[(r, col)], c[(r, col + 1)], c[(r, col + 2)] = serial(d), "x", w
        if ov:
            c[(r, col + 3)] = ov
    c[(7, 15)] = c[(7, 21)] = "0"                       # formula rows past the end of the table
    c[(165, 15)], c[(165, 21)] = "EUR holidays", "USD holidays"
    c[(166, 15)], c[(166, 21)], c[(167, 21)] = serial("2026-12-25"), serial("2026-11-26"), "0"
    return c


class TestExcelPairSheet(unittest.TestCase):
    def test_parse(self):
        s = parse_pair_sheet(sheet())
        self.assertEqual(s["pair"], "EURUSD")
        self.assertEqual(s["weekdays"], [1.05, 1, 1, 1, 1, 0, 0])
        self.assertEqual(s["holiday"], {"EUR": 0.4, "USD": 0.3})
        self.assertEqual(s["holidays"], {"EUR": [date(2026, 12, 25)], "USD": [date(2026, 11, 26)]})
        eur = s["dates"]["EUR"]
        self.assertEqual([e["date"] for e in eur], ["2026-10-20", "2026-10-29"])
        self.assertEqual((eur[1]["desc"], eur[1]["weight"], eur[1]["override"]), ("ECB, GE CPI", 0.9, 0.9))
        self.assertIsNone(parse_pair_sheet({(2, 2): "Event dates"}))

    def test_events(self):
        s = parse_pair_sheet(sheet())
        today = date(2026, 10, 23)
        # overrides only: 29 Oct, with the USD weight of that date too (a pair event replaces the whole date)
        self.assertEqual([(e["date"], e["ccy"], e["weight"]) for e in pair_events(s, today)],
                         [("2026-10-29", "EUR", 0.9), ("2026-10-29", "USD", 0.2)])
        self.assertEqual([e["date"] for e in pair_events(s, today, all_dates=True)],
                         ["2026-10-29", "2026-10-29", "2026-11-06"])
        self.assertEqual(excel_day_events(s, today), {"2026-10-29": 1.1, "2026-11-06": 0.4})


if __name__ == "__main__":
    unittest.main()
