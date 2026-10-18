import csv
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from volmarker.calendar import cumulative_weights, interpolate_atm, tenor_dates
from volmarker.conventions import TENORS
from volmarker.derived import cross_vol, implied_corr, orientation
from volmarker.service import Service
from volmarker.store import ConflictError, Store
from volmarker.surface import calibrate, check_surface, clean_state, new_state, resolve


def flat_state(pair, atm, **kw):
    s = new_state(pair)
    for t in TENORS:
        s["tenors"][t]["atm"] = atm
        s["tenors"][t].update(kw)
    return clean_state(s)


class TestTriangle(unittest.TestCase):
    def test_orientation(self):
        self.assertEqual(orientation("EURCHF", "EURUSD", "USDCHF"), (1, 1))
        self.assertEqual(orientation("AUDCHF", "AUDUSD", "USDCHF"), (1, 1))
        self.assertEqual(orientation("NOKSEK", "EURSEK", "EURNOK"), (1, -1))
        self.assertEqual(orientation("CHFJPY", "USDCHF", "USDJPY"), (-1, 1))
        with self.assertRaises(ValueError):
            orientation("EURCHF", "AUDUSD", "USDJPY")

    def test_round_trip(self):
        for s1, s2 in ((1, 1), (1, -1)):
            v = cross_vol(7.0, 6.5, -0.6 * s1 * s2, s1, s2)
            self.assertAlmostEqual(implied_corr(v, 7.0, 6.5, s1, s2), -0.6 * s1 * s2)

    def test_eurchf_low_vol_with_negative_corr(self):
        # EURUSD and USDCHF strongly negatively correlated -> low EURCHF vol
        v = cross_vol(7.0, 7.5, -0.9, 1, 1)
        self.assertAlmostEqual(v, math.sqrt(49 + 56.25 - 2 * 0.9 * 7 * 7.5))


class TestSurface(unittest.TestCase):
    def test_wings_and_skew(self):
        s = flat_state("EURUSD", 7.0, spread=0.4, rr25=-0.5, rr10f=2.0, bf25=0.2, bf10f=3.0)
        s["tenors"]["1M"].update(skew_atm=0.1, skew_rr=-0.2)
        r = resolve(s)["1M"]
        self.assertAlmostEqual(r["mid"]["rr10"], -1.0)
        self.assertAlmostEqual(r["mid"]["bf10"], 0.6)
        self.assertAlmostEqual(r["quoted"]["atm_bid"], 6.9)
        self.assertAlmostEqual(r["quoted"]["atm_offer"], 7.3)
        self.assertAlmostEqual(r["quoted"]["rr10"], -1.4)
        # expired skew is ignored
        s["skew_expiry"] = "2000-01-01T00:00:00+00:00"
        r = resolve(s)["1M"]
        self.assertAlmostEqual(r["quoted"]["atm"], 7.0)

    def test_factor_lock(self):
        s = flat_state("EURUSD", 7.0, rr25=-0.5, rr10f=2.0)
        self.assertAlmostEqual(s["tenors"]["1M"]["rr10"], -1.0)   # locked by default
        # unlocked: type the 10D quote, factor is calibrated
        s["lock_rr10f"] = False
        s["tenors"]["1M"]["rr10"] = -1.2
        s = clean_state(s)
        self.assertAlmostEqual(s["tenors"]["1M"]["rr10f"], 2.4)
        # unlocked: moving the 25D keeps the 10D quote, factor moves
        s["tenors"]["1M"]["rr25"] = -0.6
        s = clean_state(s)
        self.assertAlmostEqual(s["tenors"]["1M"]["rr10"], -1.2)
        self.assertAlmostEqual(s["tenors"]["1M"]["rr10f"], 2.0)
        # 25D of zero: 10D kept, last factor kept
        s["tenors"]["1M"]["rr25"] = 0.0
        s = clean_state(s)
        self.assertAlmostEqual(s["tenors"]["1M"]["rr10"], -1.2)
        self.assertAlmostEqual(s["tenors"]["1M"]["rr10f"], 2.0)
        # locked again: 10D follows 25D x factor
        s["lock_rr10f"] = True
        s["tenors"]["1M"]["rr25"] = -0.5
        s = clean_state(s)
        self.assertAlmostEqual(s["tenors"]["1M"]["rr10"], -1.0)
        # BF lock is independent
        self.assertTrue(s["lock_bf10f"])
        self.assertAlmostEqual(resolve(s)["1M"]["mid"]["rr10"], -1.0)

    def test_calendar_arbitrage_flag(self):
        s = flat_state("EURUSD", 7.0)
        s["tenors"]["2M"]["atm"] = 4.0
        self.assertTrue(any("calendar arbitrage" in w for w in check_surface(s)))

    def test_calibrate(self):
        bases = {"EURUSD": flat_state("EURUSD", 7.0), "USDCHF": flat_state("USDCHF", 7.5)}
        s = new_state("EURCHF", "derived", ["EURUSD", "USDCHF"])
        s = calibrate(clean_state(s), bases, {t: 4.5 for t in TENORS})
        for t in TENORS:
            self.assertAlmostEqual(s["tenors"][t]["atm"], 4.5, places=5)
            self.assertLess(s["tenors"][t]["corr"], 0)


class TestCalendar(unittest.TestCase):
    def test_weights(self):
        today = date(2026, 10, 19)  # Monday
        hol = {"USD": {date(2026, 10, 21)}}
        w = {"weekend1": 0.1, "weekend2": 0.1, "holiday1": 0.3, "holiday2": 0.3,
             "events": [{"date": "2026-10-22", "desc": "FOMC", "weight": 0.8}]}
        cum = cumulative_weights("EURUSD", today, today + timedelta(days=7), w, hol)
        daily = [b - a for a, b in zip(cum, cum[1:])]
        # Tue 1, Wed USD holiday 0.3, Thu FOMC 1.8, Fri 1, Sat 0.1, Sun 0.1, Mon 1
        for got, exp in zip(daily, [1, 0.3, 1.8, 1, 0.1, 0.1, 1]):
            self.assertAlmostEqual(got, exp)

    def test_interpolation_hits_pillars(self):
        today = date(2026, 10, 19)
        atm = {t: 7.0 + i * 0.1 for i, t in enumerate(TENORS)}
        td = tenor_dates("EURUSD", today, {})
        out = interpolate_atm("EURUSD", atm, today, list(td.values()), None, {})
        for t, d in td.items():
            self.assertAlmostEqual(out[d], atm[t], places=6)


class TestServiceAndStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.a = Service(self.store, "anna")
        self.b = Service(Store(self.tmp.name), "ben")  # second trader, same folder

    def tearDown(self):
        self.tmp.cleanup()

    def _marked(self, svc, pair, atm):
        svc.create_pair(pair, "marked", None, True)
        st = flat_state(pair, atm)
        return svc.publish(pair, st, expected_version=1)

    def test_conflict(self):
        self._marked(self.a, "EURUSD", 7.0)
        st = flat_state("EURUSD", 7.1)
        with self.assertRaises(ConflictError):
            self.b.publish("EURUSD", st, expected_version=1)
        self.b.publish("EURUSD", st, expected_version=2)
        self.assertEqual(self.store.latest("EURUSD")["version"], 3)

    def test_propagation_and_exports(self):
        self._marked(self.a, "EURUSD", 7.0)
        self._marked(self.a, "USDCHF", 7.5)
        self.a.create_pair("EURCHF", "derived", ["EURUSD", "USDCHF"], False)
        cur = self.store.latest("EURCHF")
        st = calibrate(cur, {b: self.store.latest(b) for b in ("EURUSD", "USDCHF")},
                       {t: 4.5 for t in TENORS})
        self.a.publish("EURCHF", st, cur["version"], "calibrated")
        rho = self.store.latest("EURCHF")["tenors"]["1M"]["corr"]

        # Ben's app moves EURUSD; EURCHF must follow with the same correlation
        self.b.poll_journal()
        self.b.publish("EURUSD", flat_state("EURUSD", 8.0), expected_version=2)
        ec = self.store.latest("EURCHF")
        self.assertEqual(ec["source"], "propagation")
        self.assertAlmostEqual(ec["tenors"]["1M"]["corr"], rho)
        self.assertAlmostEqual(ec["tenors"]["1M"]["atm"], cross_vol(8.0, 7.5, rho, 1, 1), places=5)

        # Anna's app sees it via the journal
        self.a.poll_journal()
        self.assertEqual(self.a.get("EURCHF")["version"], ec["version"])

        with open(Path(self.tmp.name) / "exports/frontarena/EURCHF.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        atm1m = [r for r in rows if r["Tenor"] == "1M" and r["Mark"] == "ATM"][0]
        self.assertAlmostEqual(float(atm1m["Mid"]), ec["tenors"]["1M"]["atm"], places=3)

    def test_skew_goes_to_fenics_only_and_expires(self):
        self._marked(self.a, "EURUSD", 7.0)
        st = self.store.latest("EURUSD")
        st["tenors"]["1M"]["skew_atm"] = 0.25
        st["skew_expiry"] = "2099-01-01T00:00:00+00:00"
        self.a.publish("EURUSD", st, st["version"])
        root = Path(self.tmp.name) / "exports"

        def atm1m(target, col):
            with open(root / target / "EURUSD.csv", newline="") as f:
                row = [r for r in csv.DictReader(f) if r["Tenor"] == "1M" and r["Mark"] == "ATM"][0]
            return float(row[col])

        self.assertAlmostEqual(atm1m("frontarena", "Mid"), 7.0)
        self.assertAlmostEqual(atm1m("fenics", "Bid"), 7.0 + 0.25 - 0.25)  # spread 0.5
        # expiry passes -> the housekeeping app publishes a clean version
        rec = self.store.latest("EURUSD")
        rec["skew_expiry"] = "2000-01-01T00:00:00+00:00"
        self.a.publish("EURUSD", rec, rec["version"])
        self.a.housekeeping()
        latest = self.store.latest("EURUSD")
        self.assertEqual(latest["source"], "skew-expiry")
        self.assertEqual(latest["tenors"]["1M"]["skew_atm"], 0.0)
        self.assertAlmostEqual(atm1m("fenics", "Bid"), 7.0 - 0.25)

    def test_cycle_rejected(self):
        self._marked(self.a, "EURUSD", 7.0)
        self._marked(self.a, "USDCHF", 7.5)
        self.a.create_pair("EURCHF", "derived", ["EURUSD", "USDCHF"], False)
        st = self.store.latest("EURUSD")
        st.update(mode="derived", bases=["EURCHF", "USDCHF"])
        with self.assertRaises(ValueError):
            self.a.publish("EURUSD", st, st["version"])


if __name__ == "__main__":
    unittest.main()
