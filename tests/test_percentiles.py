import gzip
import tempfile
import unittest
from datetime import date
from pathlib import Path

from volmarker.analysis import MARK_COLS
from volmarker.percentiles import (analyse, fill_weekdays, parse_lookback, pct_rank, quantile, read_eod,
                                   tenor_ok)


def eod_row(d, pair, tenor, atm, rr25=-0.5, bf25=0.2):
    r = dict.fromkeys(MARK_COLS, "")
    r.update(date=d, pair=pair, tenor=tenor, atm_mid=atm, rr25=rr25, rr10=rr25 * 2, bf25=bf25, bf10=bf25 * 3)
    return ",".join(str(r[k]) for k in MARK_COLS) + "\n"


class TestPercentiles(unittest.TestCase):
    def test_rank_and_quantiles(self):
        v = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(pct_rank(v, 0.5), 0)
        self.assertEqual(pct_rank(v, 5.0), 100)
        self.assertEqual(pct_rank(v, 3.0), 62.5)           # 2 below, 1 tie counting half
        self.assertEqual(pct_rank(v, 2.5), 50)
        self.assertEqual(quantile(v, 0.5), 2.5)
        self.assertAlmostEqual(quantile(v, 0.9), 3.7)      # as Excel PERCENTILE.INC
        self.assertEqual(quantile([7.0], 0.1), 7.0)

    def test_weekdays_carried_forward(self):
        s = {date(2026, 11, 2): 7.0, date(2026, 11, 5): 7.5}   # Mon, Thu
        self.assertEqual(fill_weekdays(s, date(2026, 11, 10)),     # to the next Tue, no weekend
                         [7.0, 7.0, 7.0, 7.5, 7.5, 7.5, 7.5])
        self.assertEqual(fill_weekdays({}, date(2026, 11, 10)), [])

    def test_lookback_and_tenors(self):
        today = date(2026, 3, 31)
        self.assertEqual(parse_lookback("1M", today), date(2026, 2, 28))
        self.assertEqual(parse_lookback("1y", today), date(2025, 3, 31))
        self.assertEqual(parse_lookback("90D", today), date(2025, 12, 31))
        self.assertIsNone(parse_lookback("all", today))
        with self.assertRaises(ValueError):
            parse_lookback("3Q", today)
        self.assertTrue(tenor_ok("3W", "1W", "1M"))
        self.assertFalse(tenor_ok("ON", "1W", None))
        self.assertFalse(tenor_ok("2Y", None, "1Y"))

    def test_read_eod_and_analyse(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "marks_eod"
            d.mkdir()
            header = ",".join(MARK_COLS) + "\n"
            # two appended members, the second repeating the header; a cut-off tail
            data = gzip.compress((header + eod_row("2026-10-01", "EURUSD", "1M", 6.0)).encode())
            data += gzip.compress((header + "".join(eod_row(f"2026-10-{d:02d}", "EURUSD", "1M", 6.0 + d / 10)
                                                    for d in range(2, 31))).encode())
            (d / "2026-10.csv.gz").write_bytes(data + gzip.compress(eod_row("2026-10-31", "EURUSD", "1M", 99).encode())[:-6])
            (d / "2026-09.csv.gz").write_bytes(gzip.compress((header + eod_row("2026-09-30", "USDJPY", "1M", 9)).encode()))
            h = read_eod(Path(tmp), {"EURUSD"}, date(2026, 10, 1), date(2026, 10, 30))
            self.assertEqual(set(h), {("EURUSD", "1M", m) for m in ("atm", "rr25", "rr10", "bf25", "bf10")})
            self.assertEqual(len(h[("EURUSD", "1M", "atm")]), 30)        # the cut-off row is dropped
            cur = {"EURUSD": {"1M": {"atm": 9.5, "rr25": -0.5, "rr10": None, "bf25": 0.2, "bf10": 0.6},
                              "1Y": {"atm": 7.0}}}
            st = {(s.tenor, s.mark): s for s in analyse(cur, h, date(2026, 10, 30), fill=False)}
            atm = st[("1M", "atm")]
            self.assertEqual((atm.n, atm.pct, atm.lo, atm.hi), (30, 100.0, 6.0, 9.0))
            self.assertEqual(st[("1M", "rr25")].pct, 50)                  # equal to every day
            self.assertIsNone(st[("1M", "rr10")].pct)                     # no current value
            self.assertEqual((st[("1Y", "atm")].n, st[("1Y", "atm")].pct), (0, None))   # no history
            filled = {(s.tenor, s.mark): s for s in analyse(cur, h, date(2026, 10, 30))}
            self.assertEqual(filled[("1M", "atm")].n, 22)                 # weekdays of October to the 30th


if __name__ == "__main__":
    unittest.main()
