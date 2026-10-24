import csv
import json
import math
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from volmarker.calendar import cumulative_weights, interpolate_atm, tenor_dates
from volmarker.conventions import TENORS
from volmarker.derived import cross_vol, implied_corr, orientation
from volmarker.service import STATE_KEYS, Service
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
    def test_weekday_and_holiday_weights(self):
        # Excel / Fenics model: weekday weight x holiday factors + events
        today = date(2026, 10, 18)  # Sunday
        hol = {"USD": {date(2026, 10, 21)}, "EUR": {date(2026, 10, 21), date(2026, 10, 23)}}
        w = {"weekdays": [1.05, 1, 1, 1, 1, 0, 0], "holiday1": 0.4, "holiday2": 0.5,
             "events": [{"date": "2026-10-23", "desc": "x", "weight": 0.8}]}
        cum = cumulative_weights("EURUSD", today, today + timedelta(days=7), w, hol)
        daily = [b - a for a, b in zip(cum, cum[1:])]
        # Mon 1.05, Tue 1, Wed both holidays 0.4*0.5, Thu 1, Fri EUR holiday + event 0.4+0.8, Sat 0, Sun 0
        for got, exp in zip(daily, [1.05, 1, 0.2, 1, 1.2, 0, 0]):
            self.assertAlmostEqual(got, exp)

    def test_legacy_weekend_factors(self):
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

        lines = (Path(self.tmp.name) / "exports/frontarena/EURCHFvolsurface.txt").read_text().splitlines()
        bid, ask = (float(x) for x in next(l for l in lines if l.startswith("vol,EUR,CHF,1mo,")).split(",")[4:6])
        self.assertAlmostEqual((bid + ask) / 2, ec["tenors"]["1M"]["atm"], places=9)

    def test_derived_held_off_bases(self):
        self._marked(self.a, "EURUSD", 7.0)
        self._marked(self.a, "USDCHF", 7.5)
        self.a.create_pair("EURCHF", "derived", ["EURUSD", "USDCHF"], False)
        cur = self.store.latest("EURCHF")
        self.assertTrue(cur["follow_bases"])                   # default: moves with the bases
        st = calibrate(cur, {b: self.store.latest(b) for b in ("EURUSD", "USDCHF")},
                       {t: 4.5 for t in TENORS})
        st["follow_bases"] = False
        self.a.publish("EURCHF", st, cur["version"], "held")
        rho = self.store.latest("EURCHF")["tenors"]["1M"]["corr"]

        self.a.publish("EURUSD", flat_state("EURUSD", 8.0), expected_version=2)
        ec = self.store.latest("EURCHF")
        self.assertAlmostEqual(ec["tenors"]["1M"]["atm"], 4.5)  # ATM stays
        new_rho = ec["tenors"]["1M"]["corr"]
        self.assertNotAlmostEqual(new_rho, rho)                 # correlation follows the bases
        self.assertAlmostEqual(cross_vol(8.0, 7.5, new_rho, 1, 1), 4.5, places=6)

        ec["follow_bases"] = True                              # back on: runs off the bases from here
        self.a.publish("EURCHF", ec, ec["version"])
        self.a.publish("EURUSD", flat_state("EURUSD", 7.0), expected_version=3)
        ec = self.store.latest("EURCHF")
        self.assertAlmostEqual(ec["tenors"]["1M"]["corr"], new_rho)
        self.assertAlmostEqual(ec["tenors"]["1M"]["atm"], cross_vol(7.0, 7.5, new_rho, 1, 1), places=5)

    def test_favour_puts_is_display_only(self):
        self._marked(self.a, "USDJPY", 9.0)
        root = Path(self.tmp.name) / "exports"
        files = lambda: [(root / d / "USDJPYvolsurface.txt").read_text() for d in ("frontarena", "fenics")]
        st = self.store.latest("USDJPY")
        for t in TENORS:
            st["tenors"][t]["rr25"] = -1.2
        self.a.publish("USDJPY", st, st["version"])
        before = files()
        st = self.store.latest("USDJPY")
        st["rr_put"] = True
        self.a.publish("USDJPY", st, st["version"])
        rec = self.store.latest("USDJPY")
        self.assertTrue(rec["rr_put"])
        self.assertEqual(rec["tenors"]["1M"]["rr25"], -1.2)       # stored call - put
        self.assertEqual(files(), before)
        self.assertTrue(next(p for p in self.a.summaries() if p["pair"] == "USDJPY")["rr_put"])

    def test_skew_goes_to_fenics_only_and_expires(self):
        self._marked(self.a, "EURUSD", 7.0)
        st = self.store.latest("EURUSD")
        st["tenors"]["1M"]["skew_atm"] = 0.25
        st["skew_expiry"] = "2099-01-01T00:00:00+00:00"
        self.a.publish("EURUSD", st, st["version"])
        root = Path(self.tmp.name) / "exports"

        def atm1m(target, col):
            text = (root / target / "EURUSDvolsurface.txt").read_text()
            prefix = "vol,EUR,USD,1mo," if target == "frontarena" else "vol,atm,EUR,USD,1m,"
            bid, ask = (float(x) for x in next(l for l in text.splitlines() if l.startswith(prefix)).split(",")[-2:])
            return {"Mid": (bid + ask) / 2, "Bid": bid}[col]

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


class TestConfigFile(unittest.TestCase):
    def test_bad_json_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            cfg = Path(tmp) / "config.json"
            cfg.write_text('{"frontarena_dir": "S:\\Matrix\\IXCR-IHD\\"}')  # as typed in Notepad
            self.assertEqual(store.config()["frontarena_dir"], "exports/frontarena")     # defaults...
            self.assertIn("double backslashes", store.config_error)                      # ...but said so
            cfg.write_text('{"frontarena_dir": "S:/Matrix/IXCR_IHD", "export_dir": "x"}')
            os.utime(cfg, (1, 1))
            self.assertEqual(store.config()["frontarena_dir"], "S:/Matrix/IXCR_IHD")
            self.assertIn("export_dir", store.config_error)


class TestGroupsPrefsSpread(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.svc = Service(self.store, "anna")

    def tearDown(self):
        self.tmp.cleanup()

    def _marked(self, pair, atm, group=None, rr25=0.0):
        self.svc.create_pair(pair, "marked", None, False, group)
        st = flat_state(pair, atm, rr25=rr25)
        st["group"] = group
        return self.svc.publish(pair, st, expected_version=1)

    def test_group_is_a_pair_setting(self):
        self._marked("EURUSD", 7.0, "  EUR   & CHF ")
        self._marked("GBPUSD", 7.5)
        rec = self.store.latest("EURUSD")
        self.assertEqual(rec["group"], "EUR & CHF")
        self.assertIsNone(self.store.latest("GBPUSD")["group"])
        groups = {s["pair"]: s["group"] for s in self.svc.summaries()}
        self.assertEqual(groups, {"EURUSD": "EUR & CHF", "GBPUSD": None})
        # kept by automatic versions (propagation, skew expiry, market mode) via STATE_KEYS
        st = {k: rec.get(k) for k in STATE_KEYS}
        self.assertEqual(clean_state(st)["group"], "EUR & CHF")
        st.update(spread_ref="usdchf")
        self.assertEqual(clean_state(st)["spread_ref"], "USDCHF")
        with self.assertRaises(ValueError):
            clean_state(dict(st, spread_ref="EURUSD"))

    def test_prefs_per_user(self):
        self.assertEqual(self.svc.prefs(), {})
        self.svc.save_prefs({"group_order": ["Scandies", "GBP"], "collapsed": {"GBP": True}})
        other = Service(Store(self.tmp.name), "ben")
        self.assertEqual(other.prefs(), {})
        self.assertEqual(Service(Store(self.tmp.name), "anna").prefs()["group_order"], ["Scandies", "GBP"])
        weird = Service(Store(self.tmp.name), "DOMAIN\\j.doe")
        weird.save_prefs({"x": 1})
        self.assertTrue((Path(self.tmp.name) / "users" / "DOMAIN_j.doe.json").exists())
        with self.assertRaises(ValueError):
            self.svc.save_prefs(["not", "a", "dict"])


class TestEventDefaults(unittest.TestCase):
    # release times in UTC; NY cut 10:00 = 14:00 UTC in EDT (until 1 Nov 2026), 15:00 UTC in EST
    CAL = [{"ccy": "USD", "date": "2026-10-28", "desc": "FOMC Rate Decision (Upper Bound)",
            "ticker": "FDTR Index", "ts": "2026-10-28T18:00+00:00"},
           {"ccy": "USD", "date": "2026-11-06", "desc": "Change in Nonfarm Payrolls",
            "ticker": "NFP TCH Index", "ts": "2026-11-06T13:30+00:00"},
           {"ccy": "USD", "date": "2026-11-06", "desc": "Unemployment Rate",
            "ticker": "USURTOT Index", "ts": "2026-11-06T13:30+00:00"},
           {"ccy": "USD", "date": "2026-11-06", "desc": "Nonfarm Payrolls 3-Mo Avg Chg",
            "ticker": "NFPPTCH3 Index", "ts": "2026-11-06T13:30+00:00"},
           {"ccy": "EUR", "date": "2026-10-29", "desc": "ECB Main Refinancing Rate",
            "ticker": "EURR002W Index", "ts": "2026-10-29T13:15+00:00"},
           {"ccy": "EUR", "date": "2026-11-06", "desc": "GE Industrial Production SA MoM", "ticker": "GRIPIMOM Index",
            "ts": "2026-11-06T07:00+00:00"},
           {"ccy": "JPY", "date": "2026-11-05", "desc": "BoJ", "ts": "2026-11-05T03:00+00:00"}]
    RULES = [{"ccy": "USD", "event": "FOMC Rate Decision (Upper Bound)", "weight": 0.8},
             {"ccy": "USD", "event": "", "ticker": "NFP TCH Index", "weight": 0.5},
             {"ccy": "USD", "event": "unemployment rate", "weight": 0.2},
             {"ccy": "EUR", "event": "ECB Main Refinancing Rate", "weight": 0.6},
             {"ccy": "EUR", "event": "GE Industrial Production SA MoM", "weight": 0.2}]

    def test_matching_and_expiry(self):
        from volmarker.calendar import default_events
        cal = self.CAL + [dict(self.CAL[1])]                   # listed twice in the calendar
        ev = default_events("EURUSD", cal, self.RULES)
        # FOMC 14:00 NY on the 28th is after the 10:00 NY cut -> the 29th, together with the ECB (09:15 NY)
        # NFP (ticker, counted once) + unemployment (name, case-insensitive) + GE IP; the 3-mo avg is not listed
        self.assertEqual([(e["date"], e["weight"]) for e in ev], [("2026-10-29", 1.4), ("2026-11-06", 0.9)])
        self.assertEqual(ev[0]["desc"], "USD FOMC Rate Decision (Upper Bound) +1")   # labelled by the biggest
        self.assertEqual(ev[0]["ccy"], "USD")
        self.assertEqual([p["weight"] for p in ev[1]["parts"]], [0.5, 0.2, 0.2])
        # Tokyo cut 15:00 JST = 06:00 UTC: FOMC (03:00 JST on the 29th) weighs on the 29th, the ECB on the 30th
        tok = default_events("EURUSD", cal, self.RULES, cut={"tz": "Asia/Tokyo", "time": "15:00"})
        self.assertEqual([(e["date"], e["weight"]) for e in tok][:2], [("2026-10-29", 0.8), ("2026-10-30", 0.6)])
        # a holiday on the expiry rolls it to the next good day
        hol = {"USD": {date(2026, 10, 29)}}
        rolled = default_events("USDJPY", cal, self.RULES, holidays=hol)
        self.assertEqual(rolled[0]["date"], "2026-10-30")

    def test_pair_overrides_default(self):
        from volmarker.calendar import with_defaults
        w = with_defaults("EURUSD", {"events": [{"date": "2026-10-29", "desc": "FOMC", "weight": 1.2},
                                                {"date": "2026-11-20", "desc": "x", "weight": 0.3}]},
                          self.CAL, self.RULES)
        got = {e["date"]: (e["weight"], e["source"], e["ccy"]) for e in w["events"]}
        self.assertEqual(got, {"2026-10-29": (1.2, "pair", "USD"), "2026-11-06": (0.9, "default", "USD"),
                               "2026-11-20": (0.3, "pair", None)})
        off = with_defaults("EURUSD", {"use_defaults": False, "events": []}, self.CAL, self.RULES)
        self.assertEqual(off["events"], [])

    def test_store_and_conflict(self):
        from volmarker.store import DefaultsConflict
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(tmp)
            d0 = st.load_event_defaults()
            self.assertEqual(d0["rows"], [])
            d1 = st.save_event_defaults([{"ccy": "usd", "event": " FOMC ", "weight": "0.8"},
                                         {"ccy": "", "event": "", "weight": 1}], d0["version"], "anna", "t1")
            self.assertEqual(d1["rows"], [{"ccy": "USD", "event": "FOMC", "weight": 0.8, "ticker": ""}])
            self.assertEqual(d1["user"], "anna")
            with self.assertRaises(DefaultsConflict):
                st.save_event_defaults([], d0["version"], "ben", "t2")
            with self.assertRaises(ValueError):
                st.save_event_defaults([{"ccy": "US", "event": "x", "weight": 1}], None, "ben", "t2")

    def test_test_dates_follow_the_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            svc = Service(store, "anna")
            svc.create_pair("EURUSD", "marked", None, True)
            st = flat_state("EURUSD", 7.0)
            st["tenors"]["1M"]["atm"] = 8.0
            svc.publish("EURUSD", st, expected_version=1)
            today = date.today()
            d = today + timedelta(days=10)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            pub = svc.test_dates("EURUSD", [d.isoformat(), "2000-01-01"])["rows"]
            self.assertEqual(len(pub), 1)
            draft = svc.get("EURUSD")
            draft["weights"] = {"events": [{"date": d.isoformat(), "desc": "big", "weight": 3.0}]}
            row = svc.test_dates("EURUSD", [d.isoformat()], draft)["rows"][0]
            self.assertGreater(row["atm"], pub[0]["atm"])      # more weight up to the date -> higher vol
            self.assertAlmostEqual(row["published"], pub[0]["atm"])
            self.assertAlmostEqual(row["day_weight"], 4.0)
            self.assertEqual(row["events"], ["big (+3)"])
            c = svc.curve("EURUSD", draft)
            self.assertTrue(any(p[2] != p[1] for p in c["points"]))


class TestMoveWithParent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.svc = Service(self.store, "anna")
        for pair, atm in (("EURUSD", 7.0), ("USDCHF", 7.6)):
            self.svc.create_pair(pair, "marked", None, True)
            self.svc.publish(pair, flat_state(pair, atm), expected_version=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_child_follows_parent(self):
        st = self.svc.get("USDCHF")
        st.update(spread_ref="EURUSD", follow_parent=True)
        rec = self.svc.publish("USDCHF", st, st["version"])
        self.assertAlmostEqual(rec["tenors"]["1M"]["psprd"], 0.6)          # calibrated to the current ATMs
        # EURUSD up 0.2 -> USDCHF follows with the same spread, as an automatic version
        eu = self.svc.get("EURUSD")
        for t in eu["tenors"]:
            eu["tenors"][t]["atm"] = 7.2
        self.svc.publish("EURUSD", eu, eu["version"])
        ch = self.store.latest("USDCHF")
        self.assertEqual((ch["source"], ch["comment"]), ("propagation", "EURUSD moved"))
        self.assertAlmostEqual(ch["tenors"]["1M"]["atm"], 7.8)
        self.assertEqual(ch["marked_ts"], rec["marked_ts"])                 # not a new mark
        # typing a spread, or an ATM (the spread recalibrates)
        st = self.svc.get("USDCHF")
        st["tenors"]["1M"]["psprd"] = 0.5
        st["tenors"]["3M"].update(atm=8.1, psprd=None)
        rec = self.svc.publish("USDCHF", st, st["version"])
        self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], 7.7)
        self.assertAlmostEqual(rec["tenors"]["3M"]["psprd"], 0.9)
        # switched off: the marks stay where they are and no longer move
        st = self.svc.get("USDCHF")
        st["follow_parent"] = False
        rec = self.svc.publish("USDCHF", st, st["version"])
        self.assertIsNone(rec["tenors"]["1M"]["psprd"])
        self.svc.publish("EURUSD", flat_state("EURUSD", 6.0), self.svc.get("EURUSD")["version"])
        self.assertAlmostEqual(self.store.latest("USDCHF")["tenors"]["1M"]["atm"], 7.7)

    def test_rules_and_cycles(self):
        st = self.svc.get("USDCHF")
        with self.assertRaises(ValueError):                                  # no parent
            self.svc.publish("USDCHF", dict(st, follow_parent=True), st["version"])
        self.svc.publish("USDCHF", dict(st, spread_ref="EURUSD", follow_parent=True), st["version"])
        eu = self.svc.get("EURUSD")
        with self.assertRaises(ValueError):                                  # EURUSD would follow itself
            self.svc.publish("EURUSD", dict(eu, spread_ref="USDCHF", follow_parent=True), eu["version"])


class TestBackgroundPropagation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.svc = Service(self.store, "anna")
        for pair, atm in (("EURUSD", 7.0), ("USDCHF", 7.6)):
            self.svc.create_pair(pair, "marked", None, True)
            self.svc.publish(pair, flat_state(pair, atm), expected_version=1)
        st = self.svc.get("USDCHF")
        self.svc.publish("USDCHF", dict(st, spread_ref="EURUSD", follow_parent=True), st["version"])
        self.svc.create_pair("EURCHF", "derived", ["EURUSD", "USDCHF"], False)
        cfg = json.loads((Path(self.tmp.name) / "config.json").read_text())
        cfg["propagation"] = {"min_change": 0.05, "min_interval_s": 5}
        (Path(self.tmp.name) / "config.json").write_text(json.dumps(cfg))

    def tearDown(self):
        self.svc.stop()
        self.tmp.cleanup()

    def _move_eurusd(self, d, source="manual"):
        st = self.svc.get("EURUSD")
        for t in TENORS:
            st["tenors"][t]["atm"] += d
        return self.svc.publish("EURUSD", st, st["version"], source=source)

    def test_publish_returns_and_dependents_follow(self):
        self.svc.start_propagation()
        self._move_eurusd(0.2)
        self.assertTrue(self.svc.propagation_idle())
        self.assertAlmostEqual(self.store.latest("USDCHF")["tenors"]["1M"]["atm"], 7.8)
        ec = self.store.latest("EURCHF")
        self.assertEqual(ec["source"], "propagation")
        self.assertEqual(ec["base_versions"]["USDCHF"], self.store.latest("USDCHF")["version"])   # after USDCHF
        hist = list((Path(self.tmp.name) / "exports" / "history").glob("USDCHF_*.csv"))
        self.assertTrue(hist)                # manual USDCHF publishes get a copy, the follow-up (1M 7.8) doesn't
        self.assertFalse(any("vol,atm,USD,CHF,1m,7.55,8.05" in h.read_text() for h in hist))

    def test_queue_collapses_and_manual_wins(self):
        self.svc._prop_thread = object()      # worker "busy": things only queue up
        self._move_eurusd(0.1, "market")
        self._move_eurusd(0.1)
        self._move_eurusd(0.1, "market")
        self.assertEqual(self.svc._prop_pending, {("deps", "EURUSD"): "manual"})
        self.svc._prop_thread = None
        self.svc.start_propagation()
        self.assertTrue(self.svc.propagation_idle())
        self.assertAlmostEqual(self.store.latest("USDCHF")["tenors"]["1M"]["atm"], 7.9)   # one step to the latest

    def test_fmd_moves_are_throttled(self):
        v = self.store.latest("USDCHF")["version"]
        self._move_eurusd(0.01, "market")                        # below min_change: no new USDCHF version
        self.assertEqual(self.store.latest("USDCHF")["version"], v)
        self._move_eurusd(0.10, "market")                        # 0.11 in total now: republished
        self.assertAlmostEqual(self.store.latest("USDCHF")["tenors"]["1M"]["atm"], 7.71)
        v = self.store.latest("USDCHF")["version"]
        self._move_eurusd(0.10, "market")                        # too soon after the last follow-up: deferred
        self.assertEqual(self.store.latest("USDCHF")["version"], v)
        self.assertIn("USDCHF", self.svc._prop_deferred)
        self._move_eurusd(0.0001)                                # a manual publish is never throttled
        self.assertAlmostEqual(self.store.latest("USDCHF")["tenors"]["1M"]["atm"], 7.8101)

    def test_reconcile_catches_up(self):
        self.svc._prop_thread = object()      # the app "closes" with the follow-ups still queued
        self._move_eurusd(0.3)
        self.svc._prop_thread, self.svc._prop_pending = None, {}
        self.assertAlmostEqual(self.store.latest("USDCHF")["tenors"]["1M"]["atm"], 7.6)
        self.assertEqual(self.svc.reconcile(), 2)                # USDCHF and EURCHF are behind
        self.assertAlmostEqual(self.store.latest("USDCHF")["tenors"]["1M"]["atm"], 7.9)
        self.assertEqual(self.svc.reconcile(), 0)

    def test_deferred_moves_are_applied_later(self):
        cfg = json.loads((Path(self.tmp.name) / "config.json").read_text())
        cfg["propagation"] = {"min_change": 0.05, "min_interval_s": 2.5}      # version times are whole seconds
        (Path(self.tmp.name) / "config.json").write_text(json.dumps(cfg))
        self.svc.start_propagation()
        self._move_eurusd(0.10, "market")
        self.assertTrue(self.svc.propagation_idle())
        self._move_eurusd(0.10, "market")                        # within 2.5 s of the last follow-up
        self.assertTrue(self.svc.propagation_idle())
        self.assertAlmostEqual(self.store.latest("USDCHF")["tenors"]["1M"]["atm"], 7.7)
        import time as _t
        _t.sleep(3.2)
        self.assertTrue(self.svc.propagation_idle())
        self.assertAlmostEqual(self.store.latest("USDCHF")["tenors"]["1M"]["atm"], 7.8)   # taken a little later
        self.assertEqual(self.svc._prop_deferred, {})
