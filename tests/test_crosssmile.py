import csv
import gzip
import io
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from volmarker.crosssmile import (Mixture, Triangle, atm_strike, black, calibrate_fixed, calibrate_flat,
                                  calibrate_shared, fit_leg, implied_sd, ncdf, premium_adjusted, quote_errors,
                                  quote_targets, quotes_from_vols, strike)

ROOT = Path(__file__).resolve().parents[1]
FLAT = dict(rr25=0.0, bf25=0.0, rr10=0.0, bf10=0.0)
ZAR = dict(atm=15, rr25=2.5, bf25=0.5, rr10=4.75, bf10=1.65)
CHF = dict(atm=8, rr25=-1.0, bf25=0.25, rr10=-1.9, bf10=0.8)
EUR = dict(atm=7, rr25=-0.4, bf25=0.2, rr10=-0.76, bf10=0.66)


class TestBasics(unittest.TestCase):
    def test_implied_round_trip(self):
        for k in (0.8, 1.0, 1.3):
            for sd in (0.02, 0.2, 0.8):
                if abs(math.log(k)) > 3 * sd:          # no time value left to invert
                    continue
                for call in (True, False):
                    self.assertAlmostEqual(implied_sd(black(1.0, k, sd * sd, call), k, call), sd, places=7)

    def test_strikes_hit_their_delta(self):
        sd = 0.1
        for pa in (False, True):
            for call in (True, False):
                for delta in (0.25, 0.10):
                    k = strike(delta, call, sd, pa)
                    d1 = (-math.log(k) + 0.5 * sd * sd) / sd
                    if pa:
                        got = k * (ncdf(d1 - sd) if call else ncdf(sd - d1))
                    else:
                        got = ncdf(d1) if call else ncdf(-d1)
                    self.assertAlmostEqual(got, delta, places=8, msg=(pa, call, delta))
        self.assertLess(atm_strike(sd, True), 1.0)
        self.assertGreater(atm_strike(sd, False), 1.0)

    def test_conventions(self):
        self.assertFalse(premium_adjusted("EURUSD"))
        self.assertTrue(premium_adjusted("USDJPY"))
        self.assertTrue(premium_adjusted("EURCHF"))

    def test_broker_and_smile_bfs(self):
        flat = Mixture("EURCHF", 0.5, [(1.0, 1.0, 0.08 * math.sqrt(0.5))])
        self.assertAlmostEqual(flat.broker_bf(8.0, 25, True), 0.0, places=6)
        mix = Mixture("USDJPY", 0.5, [(0.8, 1.01, 0.06 * math.sqrt(0.5)), (0.2, 0.96, 0.15 * math.sqrt(0.5))])
        smile = quotes_from_vols(mix.smile_vols(True))
        broker = mix.quotes(True)
        self.assertEqual(broker["rr25"], smile["rr25"])
        self.assertGreater(abs(broker["bf25"] - smile["bf25"]), 0.01)   # with a skew the two differ
        # a model's own quotes are hit exactly, in either convention
        for bf, q in (("broker", broker), ("smile", smile)):
            tg = quote_targets(q, 0.5, True, bf, {25: smile["bf25"], 10: smile["bf10"]})
            for e in quote_errors(mix.price, tg):
                self.assertLess(abs(e), 1e-4, bf)

    def test_leg_fit(self):
        leg = fit_leg("USDZAR", 0.25, ZAR)
        self.assertLess(leg.rmse, 0.05)
        self.assertAlmostEqual(sum(w * f for w, f, _ in leg.comps), 1.0, places=10)
        self.assertLess(fit_leg("USDZAR", 0.25, ZAR, bf="smile").rmse, 0.05)


class TestCross(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zar = fit_leg("USDZAR", 0.25, ZAR)
        cls.chf = fit_leg("USDCHF", 0.25, CHF)
        cls.eur = fit_leg("EURUSD", 0.25, EUR)

    def test_flat_legs_give_the_triangle(self):
        tri = Triangle("CHFZAR", fit_leg("USDZAR", 0.5, dict(atm=15, **FLAT)), fit_leg("USDCHF", 0.5, dict(atm=8, **FLAT)))
        q = tri.pricer(-0.3, -0.3, None).quotes()
        self.assertAlmostEqual(q["atm"], math.sqrt(15 ** 2 + 8 ** 2 + 2 * 0.3 * 15 * 8), delta=0.05)
        self.assertLess(abs(q["rr25"]), 0.02)

    def test_forward_is_one_whatever_the_orientation(self):
        for cross, l1, l2 in (("CHFZAR", self.zar, self.chf), ("ZARCHF", self.zar, self.chf),
                              ("EURCHF", self.eur, self.chf), ("CHFEUR", self.chf, self.eur)):
            for share in (None, 0.0, 1.0):
                self.assertAlmostEqual(Triangle(cross, l1, l2).pricer(0.2, 0.5, share).forward, 1.0, places=10)

    def test_inversion_symmetry(self):
        """A CHFZAR call struck at K is K ZARCHF puts struck at 1/K (forwards 1)."""
        a = Triangle("CHFZAR", self.zar, self.chf).pricer(-0.3, 0.1, 0.7)
        b = Triangle("ZARCHF", self.zar, self.chf).pricer(-0.3, 0.1, 0.7)
        for k in (0.85, 1.0, 1.2):
            self.assertAlmostEqual(a.price(k, True), k * b.price(1 / k, False), places=10)

    def test_common_stress_steepens_chfzar(self):
        tri = Triangle("CHFZAR", self.zar, self.chf)
        rr = [tri.pricer(-0.3, -0.3, s).quotes()["rr25"] for s in (0.0, 0.5, 1.0)]
        self.assertLess(rr[0], rr[1])
        self.assertLess(rr[1], rr[2])

    def test_calibrations(self):
        tri = Triangle("EURCHF", self.eur, self.chf)
        truth = tri.pricer(0.6, 0.8, 0.6).quotes()
        flat = calibrate_flat(tri, truth)
        self.assertAlmostEqual(tri.pricer(**flat).quotes()["atm"], truth["atm"], delta=0.01)
        fit = calibrate_shared(tri, truth)
        self.assertAlmostEqual(fit["share"], 0.6, delta=0.05)
        self.assertAlmostEqual(fit["rho_stress"] - fit["rho_calm"], 0.2, delta=0.03)
        fixed = calibrate_fixed(tri, truth, 0.6, 0.2)
        got = tri.pricer(fixed["rho_calm"], fixed["rho_stress"], 0.6).quotes()
        for m in ("atm", "rr25", "bf25", "rr10", "bf10"):
            self.assertAlmostEqual(got[m], truth[m], delta=0.02, msg=m)


class TestScript(unittest.TestCase):
    def test_runs_on_a_small_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "marks_eod").mkdir()
            legs = {"EURUSD": EUR, "USDCHF": CHF}
            tri = Triangle("EURCHF", fit_leg("EURUSD", 91 / 365, EUR), fit_leg("USDCHF", 91 / 365, CHF))
            rows = [*legs.items(), ("EURCHF", tri.pricer(0.6, 0.8, 0.6).quotes())]
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["date", "pair", "tenor", "expiry", "atm_mid", "rr25", "rr10", "bf25", "bf10"])
            for p, q in rows:
                w.writerow(["2026-06-01", p, "3M", "2026-08-31", q["atm"], q["rr25"], q["rr10"], q["bf25"], q["bf10"]])
            (tmp / "marks_eod" / "2026-06.csv.gz").write_bytes(gzip.compress(buf.getvalue().encode()))
            out = tmp / "out.csv"
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "cross_smile_backtest.py"),
                                "--analysis", str(tmp), "--out", str(out)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("EURCHF (EURUSD, USDCHF)", r.stdout)
            with out.open() as f:
                (row,) = list(csv.DictReader(f))
            self.assertAlmostEqual(float(row["shared_share"]), 0.6, delta=0.05)


if __name__ == "__main__":
    unittest.main()
