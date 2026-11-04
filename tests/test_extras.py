"""Extra tenors (e.g. 3W over an event, for a while) and extra deltas (e.g. 5D, 1D for steep wings) per pair."""

import copy
import csv
import gzip
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from volmarker.analysis import MARK_COLS, Exporter, version_marks
from volmarker.calendar import interpolate_atm, tenor_dates
from volmarker.conventions import TENORS, norm_extra_tenor, tenor_days
from volmarker.exports import fenics_lines, frontarena_lines
from volmarker.fmd.book import QuoteBook, price_mark
from volmarker.market import compare, market_view
from volmarker.service import STATE_KEYS, Service
from volmarker.store import Store
from volmarker.surface import all_marks, apply_derivation, check_surface, clean_state, new_state, resolve, tenor_list


def surface(pair, atm=7.0, **kw):
    s = new_state(pair)
    for t in TENORS:
        s["tenors"][t].update(atm=atm, spread=0.4, rr25=-0.5, bf25=0.2, **kw)
    return s


class TestConventions(unittest.TestCase):
    def test_extra_tenor_codes(self):
        self.assertEqual(norm_extra_tenor(" 3w"), "3W")
        self.assertEqual(norm_extra_tenor("18M"), "18M")
        for bad in ("12M", "1M", "ON", "3D", "W", "0W", "IMM"):
            with self.assertRaises(ValueError):
                norm_extra_tenor(bad)
        self.assertEqual(tenor_days("3W"), 21)
        self.assertTrue(tenor_days("2W") < tenor_days("3W") < tenor_days("1M") < tenor_days("4M") < tenor_days("6M"))


class TestModel(unittest.TestCase):
    def test_extra_tenor_sorted_and_marked(self):
        s = surface("EURUSD")
        s["extra_tenors"] = {"4m": "2026-12-04", "3w": None}
        s["tenors"]["3W"] = {"atm": 7.4, "rr25": -0.6}
        s = clean_state(s)
        self.assertEqual(s["extra_tenors"], {"3W": None, "4M": "2026-12-04"})
        self.assertEqual(tenor_list(s)[:6], ["ON", "1W", "2W", "3W", "1M", "2M"])
        self.assertEqual(list(s["tenors"])[3], "3W")
        self.assertEqual(s["tenors"]["3W"]["atm"], 7.4)
        self.assertIsNone(s["tenors"]["4M"]["atm"])          # added without marks: flagged
        self.assertIn("4M: no ATM", check_surface(s))
        self.assertEqual(list(resolve(s))[3], "3W")

    def test_extra_delta_factor_and_typed(self):
        s = surface("EURUSD")
        s["deltas"] = [5, "1"]
        s = clean_state(s)
        self.assertEqual(s["deltas"], [5, 1])
        self.assertEqual(s["wing_locks"], {"rr5": True, "bf5": True, "rr1": True, "bf1": True})
        r = s["tenors"]["1M"]
        self.assertAlmostEqual(r["rr5"], -0.5 * r["rr5f"])            # locked: 25D x starting factor
        self.assertTrue(r["rr10f"] < r["rr5f"] < r["rr1f"])
        # unlocked: the 5D is typed and its factor calibrated
        s["wing_locks"]["rr5"] = False
        s["tenors"]["1M"]["rr5"] = -2.0
        s = clean_state(s)
        self.assertAlmostEqual(s["tenors"]["1M"]["rr5f"], 4.0)
        res = resolve(s)["1M"]
        self.assertAlmostEqual(res["mid"]["rr5"], -2.0)
        self.assertEqual(all_marks(s), ["atm", "rr25", "rr10", "rr5", "rr1", "bf25", "bf10", "bf5", "bf1"])
        # a skew on the 25D moves the 5D by skew x factor, as the 10D
        s["tenors"]["1M"]["skew_rr"] = 0.1
        self.assertAlmostEqual(resolve(s)["1M"]["quoted"]["rr5"], -2.0 + 0.1 * 4.0)
        # removing the delta drops its fields
        s["deltas"] = None
        s = clean_state(s)
        self.assertNotIn("rr5", s["tenors"]["1M"])
        self.assertIsNone(s["wing_locks"])

    def test_inner_delta(self):
        s = surface("EURUSD")
        s["deltas"] = [30]
        s = clean_state(s)
        self.assertTrue(0 < s["tenors"]["1M"]["rr30f"] < 1)
        self.assertEqual(all_marks(s)[:4], ["atm", "rr30", "rr25", "rr10"])

    def test_bad_extras(self):
        for extra in ({"deltas": [10]}, {"deltas": [25]}, {"deltas": [50]}, {"deltas": [2.5]},
                      {"deltas": [1, 2, 3, 4, 5]}, {"extra_tenors": {"1M": None}},
                      {"extra_tenors": {"3W": "not a date"}}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                clean_state(dict(surface("EURUSD"), **extra))
        with self.assertRaisesRegex(ValueError, "Excel"):
            clean_state(dict(surface("EURUSD"), excel=True, deltas=[5]))

    def test_old_records_unchanged(self):
        s = clean_state(surface("EURUSD"))
        self.assertEqual(list(s["tenors"]), TENORS)
        self.assertEqual(set(s["tenors"]["1M"]), {"atm", "spread", "rr25", "rr10f", "rr10", "bf25", "bf10f", "bf10",
                                                 "corr", "psprd", "skew_atm", "skew_rr", "skew_bf"})
        self.assertIsNone(s["deltas"])
        self.assertIsNone(s["extra_tenors"])

    def test_derived_extra_tenor_not_in_bases_is_typed(self):
        bases = {b: clean_state(surface(b, atm)) for b, atm in (("EURUSD", 7.0), ("USDCHF", 8.0))}
        s = new_state("EURCHF", "derived", ["EURUSD", "USDCHF"])
        for t in TENORS:
            s["tenors"][t]["corr"] = 0.5
        s["extra_tenors"] = {"3W": None}
        s["tenors"]["3W"] = {"atm": 6.0}
        s, warnings = apply_derivation(clean_state(s), bases)
        self.assertEqual(s["tenors"]["3W"]["atm"], 6.0)
        self.assertNotEqual(s["tenors"]["1M"]["atm"], None)
        self.assertFalse([w for w in warnings if "3W" in w])
        # the bases carry it too: derived like the others
        for b in bases.values():
            b["extra_tenors"] = {"3W": None}
            b["tenors"]["3W"] = dict(b["tenors"]["1M"])
        s["tenors"]["3W"]["corr"] = 0.5
        s, _ = apply_derivation(s, bases)
        self.assertAlmostEqual(s["tenors"]["3W"]["atm"], s["tenors"]["1M"]["atm"])


class TestExports(unittest.TestCase):
    def test_lines(self):
        s = surface("EURUSD")
        s["extra_tenors"] = {"3W": None, "18M": None}
        s["tenors"]["3W"] = {"atm": 7.1}
        s["tenors"]["18M"] = {"atm": 7.3}
        s["deltas"] = [5]
        s = clean_state(s)
        res = resolve(s)
        fa = frontarena_lines("EURUSD", res, {t: r["spread"] for t, r in s["tenors"].items()})
        self.assertIn("vol,EUR,USD,3wk,6.85,7.35", fa)          # default spread 0.5
        self.assertIn("vol,EUR,USD,18mo,7.05,7.55", fa)
        self.assertTrue(any(l.startswith("vol5,EUR,USD,riskreversal,3wk,") for l in fa))
        fen = fenics_lines("EURUSD", res)
        self.assertTrue(any(l.startswith("vol,atm,EUR,USD,3w,") for l in fen))
        self.assertTrue(any(l.startswith("vol,rr5,EUR,USD,18m,") for l in fen))
        self.assertTrue(any(l.startswith("vol,s5,EUR,USD,1m,") for l in fen))
        # 25D and 10D blocks come first, as before
        codes = [l.split(",")[1] for l in fen]
        self.assertEqual(sorted(set(codes), key=codes.index), ["atm", "s25", "rr25", "s10", "rr10", "s5", "rr5"])

    def test_standard_file_unchanged(self):
        res = resolve(clean_state(surface("EURUSD")))
        fen = fenics_lines("EURUSD", res)
        self.assertEqual(len(fen), 13 * 5)
        self.assertEqual(fen[0], "vol,atm,EUR,USD,1d,6.8,7.2")


class TestMarket(unittest.TestCase):
    def test_fmd_codes(self):
        self.assertEqual(price_mark("RR5"), "rr5")
        self.assertEqual(price_mark("S1"), "bf1")
        self.assertEqual(price_mark("RR30"), "rr30")
        self.assertEqual(price_mark("DNS"), "atm")
        self.assertIsNone(price_mark("XYZ"))

    def test_book_keeps_extra_tenors(self):
        book = QuoteBook()
        for sid, tenor in (("1", "3W"), ("2", "1M"), ("3", "IMM1")):
            book.apply({48: sid, 17001: "EURUSD", 6217: tenor, 423: "RR5", 132: "-1.1", 133: "-0.9"})
        self.assertEqual(sorted(q["tenor"] for q in book.pair_quotes("EURUSD")), ["1M", "3W"])

    def test_view_and_compare(self):
        q = {"tenor": "3W", "mark": "rr5", "ptype": "RR5", "brand": "FMD", "bid": -1.1, "offer": -0.9}
        view = market_view([q], inverse=True)
        self.assertAlmostEqual(view["3W"]["rr5"]["mid"], 1.0)            # an inverse pair's RR: flipped
        s = surface("EURUSD")
        s.update(deltas=[5], extra_tenors={"3W": None})
        s["tenors"]["3W"] = {"atm": 7.0, "rr25": -0.5}
        res = resolve(clean_state(s))
        out = compare(res, market_view([q]))
        self.assertEqual([(o["tenor"], o["mark"]) for o in out], [("3W", "rr5")])     # -1.3 below the -1.1 bid


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.svc = Service(self.store, "anna")
        self.svc.create_pair("EURUSD", "marked", None, True)
        s = surface("EURUSD")
        for i, t in enumerate(TENORS):
            s["tenors"][t]["atm"] = 6.0 + 0.1 * i
        self.svc.publish("EURUSD", s, 1)

    def tearDown(self):
        self.tmp.cleanup()

    def draft(self):
        rec = self.store.latest("EURUSD")
        return {k: copy.deepcopy(rec.get(k)) for k in STATE_KEYS}

    def test_new_tenor_row_keeps_the_curve(self):
        r = self.svc.new_tenor_row("EURUSD", self.draft(), "3w")
        self.assertEqual(r["tenor"], "3W")
        rec = self.store.latest("EURUSD")
        holidays, cal = self.store.load_calendar()
        d = date.fromisoformat(r["expiry"])
        w = self.svc.effective_weights("EURUSD", rec.get("weights"), cal, holidays, rec)
        want = interpolate_atm("EURUSD", {t: rec["tenors"][t]["atm"] for t in TENORS}, date.today(), [d], w, holidays)[d]
        self.assertAlmostEqual(r["row"]["atm"], round(want, 4))
        self.assertTrue(6.2 <= r["row"]["atm"] <= 6.3)                  # between 2W and 1M
        self.assertAlmostEqual(r["row"]["rr25"], -0.5)
        with self.assertRaises(ValueError):
            self.svc.new_tenor_row("EURUSD", self.draft(), "1M")

    def test_publish_and_expire(self):
        d = self.draft()
        row = self.svc.new_tenor_row("EURUSD", d, "3W")["row"]
        today = date.today()
        d["extra_tenors"] = {"3W": (today + timedelta(days=30)).isoformat()}
        d["tenors"]["3W"] = row
        d["deltas"] = [5]
        rec = self.svc.publish("EURUSD", d, 2, "3W for the event")
        self.assertIn("3W", rec["tenors"])
        self.assertIn("rr5", rec["tenors"]["1M"])
        self.assertEqual(self.svc.view("EURUSD")["expiries"]["3W"],
                         tenor_dates("EURUSD", today, self.store.load_calendar()[0], ["3W"])["3W"].isoformat())
        fen = (Path(self.tmp.name) / "exports" / "fenics" / "EURUSDvolsurface.txt").read_text()
        self.assertIn("vol,atm,EUR,USD,3w,", fen)
        self.assertIn("vol,rr5,EUR,USD,1m,", fen)
        # not yet
        self.assertEqual(self.svc.expire_tenors(today + timedelta(days=29)), [])
        # on its date the leader removes it with a new version; the delta stays
        self.assertEqual(self.svc.expire_tenors(today + timedelta(days=30)), ["EURUSD"])
        rec = self.store.latest("EURUSD")
        self.assertNotIn("3W", rec["tenors"])
        self.assertIsNone(rec["extra_tenors"])
        self.assertEqual(rec["deltas"], [5])
        self.assertEqual((rec["source"], rec["comment"]), ("tenor-expiry", "extra tenor 3W removed (end date)"))
        fen = (Path(self.tmp.name) / "exports" / "fenics" / "EURUSDvolsurface.txt").read_text()
        self.assertNotIn(",3w,", fen)

    def test_alerts_on_extra_delta_use_10d_tolerance(self):
        d = self.draft()
        d["deltas"] = [5]
        self.svc.publish("EURUSD", d, 2)
        now = datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S")
        rr5 = self.store.latest("EURUSD")["tenors"]["1M"]["rr5"]
        self.svc.market = {"EURUSD": [{"tenor": "1M", "mark": "rr5", "ptype": "RR5", "brand": "FMD",
                                       "bid": rr5 + 2.9, "offer": rr5 + 3.1, "mid": rr5 + 3.0, "ts": now}]}
        self.svc.market_status = {"connected": True, "heartbeat": datetime.now().timestamp()}
        self.svc.evaluate_alerts(0)
        self.svc.evaluate_alerts(100)
        al = self.svc.alerts.list()
        self.assertEqual([(a["tenor"], a["mark"]) for a in al], [("1M", "rr5")])
        tol = self.store.config()["alerts"]["tolerance"]["rr10"]
        self.assertAlmostEqual(al[0]["tol"], tol)


class TestRemovedTenors(unittest.TestCase):
    """Standard tenors a pair doesn't mark, e.g. beyond 2Y."""
    def test_model_and_exports(self):
        s = surface("EURUSD")
        s["removed_tenors"] = ["5y", "3Y", "4Y"]
        s = clean_state(s)
        self.assertEqual(s["removed_tenors"], ["3Y", "4Y", "5Y"])
        self.assertEqual(tenor_list(s)[-1], "2Y")
        self.assertNotIn("3Y", s["tenors"])
        res = resolve(s)
        self.assertEqual(list(res)[-1], "2Y")
        fen = fenics_lines("EURUSD", res)
        self.assertEqual(len(fen), 10 * 5)
        self.assertFalse(any(",3y," in l for l in fen))
        self.assertFalse([w for w in check_surface(s) if "3Y" in w])
        for bad in (["7Y"], TENORS[:-1]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_state(dict(surface("EURUSD"), removed_tenors=bad))
        with self.assertRaisesRegex(ValueError, "Excel"):
            clean_state(dict(surface("EURUSD"), excel=True, removed_tenors=["5Y"]))

    def test_derived_warns_when_a_base_removed_it(self):
        bases = {b: clean_state(surface(b, atm)) for b, atm in (("EURUSD", 7.0), ("USDCHF", 8.0))}
        bases["USDCHF"] = clean_state(dict(bases["USDCHF"], removed_tenors=["5Y"]))
        s = new_state("EURCHF", "derived", ["EURUSD", "USDCHF"])
        for t in TENORS:
            s["tenors"][t].update(corr=0.5, atm=6.0)
        s, warnings = apply_derivation(clean_state(s), bases)
        self.assertEqual(s["tenors"]["5Y"]["atm"], 6.0)
        self.assertIn("5Y: not marked on USDCHF; ATM kept as typed", warnings)

    def test_service_remove_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            svc = Service(store, "anna")
            svc.create_pair("EURUSD", "marked", None, True)
            s = surface("EURUSD")
            for i, t in enumerate(TENORS):
                s["tenors"][t]["atm"] = 6.0 + 0.1 * i
            svc.publish("EURUSD", s, 1)
            d = {k: copy.deepcopy(store.latest("EURUSD").get(k)) for k in STATE_KEYS}
            d["removed_tenors"] = ["1M", "4Y", "5Y"]
            rec = svc.publish("EURUSD", d, 2, "no long end")
            self.assertNotIn("5Y", rec["tenors"])
            self.assertIsNone(svc.summaries()[0]["atm_1m"])            # the sidebar copes without 1M
            self.assertNotIn("5Y", svc.view("EURUSD")["expiries"])
            fa = (Path(tmp) / "exports" / "frontarena" / "EURUSDvolsurface.txt").read_text()
            self.assertNotIn(",5yr,", fa)
            # bring 4Y back: it starts on the curve, extrapolated beyond 3Y
            d = {k: copy.deepcopy(rec.get(k)) for k in STATE_KEYS}
            r = svc.new_tenor_row("EURUSD", d, "4Y")
            self.assertEqual(r["tenor"], "4Y")
            self.assertGreater(r["row"]["atm"], rec["tenors"]["3Y"]["atm"] - 0.5)
            with self.assertRaises(ValueError):
                svc.new_tenor_row("EURUSD", d, "3Y")                   # still marked
            svc.load_export_file("EURUSD", d)                           # a file with every tenor


class TestAnalysis(unittest.TestCase):
    def test_version_marks_and_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            s = surface("EURUSD")
            s.update(deltas=[5], extra_tenors={"3W": None})
            s["tenors"]["3W"] = {"atm": 7.1}
            rec = clean_state(s)
            m = version_marks(rec)
            self.assertEqual(list(m)[:4], ["ON", "1W", "2W", "3W"])
            self.assertAlmostEqual(m["1M"]["wings"]["rr5"], rec["tenors"]["1M"]["rr5"])
            ex = Exporter(store)
            _, rows = ex.rows("EURUSD", datetime(2026, 11, 4, 9, tzinfo=timezone.utc), m, "volmarker:manual", "anna", "3")
            r = dict(zip(MARK_COLS, rows[3]))
            self.assertEqual((r["tenor"], r["atm_mid"]), ("3W", "7.1"))
            self.assertTrue(r["expiry"])
            self.assertTrue(dict(zip(MARK_COLS, rows[4]))["wings"].startswith("rr5="))

    def test_month_begun_with_old_columns_keeps_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            svc = Service(store, "anna")
            svc.create_pair("EURUSD", "marked", None, True)
            ex = Exporter(store)
            month = ex.path("marks", "2026-11")
            month.parent.mkdir(parents=True)
            old = MARK_COLS[:-1]
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerow(old)
            month.write_bytes(gzip.compress(buf.getvalue().encode()))
            st = {"through": "2026-11-02", "month": "2026-11", "sizes": {"marks": month.stat().st_size}}
            today = datetime.fromisoformat(store.latest("EURUSD")["ts"]).date()
            ex.export_day(today, st)
            got = list(csv.reader(io.StringIO(gzip.decompress(month.read_bytes()).decode())))
            self.assertEqual(got[0], old)
            self.assertTrue(len(got) > 1 and all(len(r) == len(old) for r in got))


if __name__ == "__main__":
    unittest.main()
