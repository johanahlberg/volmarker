import json
import tempfile
import time
import unittest
from datetime import datetime, timezone

from volmarker.conventions import TENORS
from volmarker.derived import implied_corr
from volmarker.follow import Follower
from volmarker.service import Service
from volmarker.store import ConflictError, Store
from volmarker.surface import calibrate, clean_state
from tests.test_engine import flat_state

NOW = 1_800_000_000.0


def write_market(store, pair, atm, rr25=0.1, rr10=0.2, bf25=0.2, bf10=0.6, ts=None, width=0.3):
    ts = ts or datetime.now(timezone.utc).isoformat()
    quotes = []
    for t in TENORS:
        for mark, ptype, mid in (("atm", "DNS", atm), ("rr25", "RR25", rr25), ("rr10", "RR10", rr10),
                                 ("bf25", "S25", bf25), ("bf10", "S10", bf10)):
            m = mid[t] if isinstance(mid, dict) else mid
            quotes.append({"pair": pair, "tenor": t, "mark": mark, "ptype": ptype, "brand": "FMD",
                           "ts": ts, "bid": m - width / 2, "offer": m + width / 2, "mid": m})
    d = store.root / "market"
    d.mkdir(exist_ok=True)
    (d / f"{pair}.json").write_text(json.dumps({"pair": pair, "quotes": quotes}))
    (d / "_status.json").write_text(json.dumps({"connected": True, "heartbeat": time.time()}))


def setup(tmp, follow=True):
    store = Store(tmp)
    trader = Service(store, "trader")
    trader.create_pair("EURUSD", "marked", None, True)
    st = flat_state("EURUSD", 7.0, rr25=0.1, rr10f=2.0, bf25=0.2, bf10f=3.0)
    st["follow_market"] = follow
    trader.publish("EURUSD", st, 1)
    return store, trader, Follower(Service(store, "FMD"))


class TestFollower(unittest.TestCase):
    def test_publishes_market_mids_with_threshold_and_throttle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, fol = setup(tmp)
            write_market(store, "EURUSD", 7.03, rr10=0.2, bf10=0.6)
            self.assertEqual(fol.step(NOW), [])                 # 0.03 < min_change
            write_market(store, "EURUSD", 7.3, rr10=0.25, bf10=0.7)
            self.assertEqual(fol.step(NOW + 1), ["EURUSD"])
            rec = store.latest("EURUSD")
            self.assertEqual((rec["source"], rec["user"]), ("market", "FMD"))
            self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], 7.3)
            self.assertAlmostEqual(rec["tenors"]["1M"]["rr10"], 0.25)
            self.assertAlmostEqual(rec["tenors"]["1M"]["rr10f"], 2.5)  # locked factor recalibrated
            self.assertTrue(rec["follow_market"])
            self.assertIn("ATM +0.30", rec["comment"])
            self.assertIn("vol,atm,EUR,USD,1m,7.05,7.55\r\n", (store.root / "exports" / "fenics" / "EURUSDvolsurface.txt").read_bytes().decode())
            # no history copy of the follower's version (manual publishes do get one)
            hist = list((store.root / "exports" / "history").glob("EURUSD_*.csv"))
            self.assertTrue(hist)
            self.assertFalse(any("1m,7.05," in h.read_text() for h in hist))
            write_market(store, "EURUSD", 7.5, rr10=0.25, bf10=0.7)
            self.assertEqual(fol.step(NOW + 5), [])             # throttled
            self.assertTrue(fol.status["EURUSD"]["waiting"])
            self.assertEqual(fol.step(NOW + 20), ["EURUSD"])

    def test_10d_with_other_sign_than_25d(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, fol = setup(tmp)
            write_market(store, "EURUSD", 7.0, rr25=-0.05, rr10=0.1, bf25=-0.02, bf10=0.3)
            self.assertEqual(fol.step(NOW), ["EURUSD"])
            r = store.latest("EURUSD")["tenors"]["1M"]
            self.assertEqual((r["rr10"], r["bf10"]), (0.1, 0.3))
            self.assertAlmostEqual(r["rr10f"], -2.0)

    def test_big_move_is_held_then_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, fol = setup(tmp)
            write_market(store, "EURUSD", {t: 7.0 + (2.0 if t == "1W" else 0.0) for t in TENORS})
            fol.step(NOW)
            self.assertEqual(fol.status["EURUSD"]["state"], "held")
            self.assertEqual(store.latest("EURUSD")["tenors"]["1W"]["atm"], 7.0)
            fol.step(NOW + 61)
            self.assertAlmostEqual(store.latest("EURUSD")["tenors"]["1W"]["atm"], 9.0)
            self.assertEqual(fol.status["EURUSD"]["state"], "ok")

    def test_stale_quotes_and_dead_feed_are_not_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, fol = setup(tmp)
            write_market(store, "EURUSD", 7.5, ts="2020-01-01T00:00:00Z")
            self.assertEqual(fol.step(NOW), [])
            self.assertEqual(fol.status["EURUSD"]["missing"], 5 * len(TENORS))
            write_market(store, "EURUSD", 7.5)
            (store.root / "market" / "_status.json").write_text(json.dumps({"connected": False, "heartbeat": 0}))
            self.assertEqual(fol.step(NOW + 30), [])
            self.assertEqual(fol.status["EURUSD"]["state"], "down")

    def test_manual_pairs_untouched_and_crosses_follow_bases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, fol = setup(tmp)
            trader.create_pair("USDCHF", "marked", None, True)
            trader.publish("USDCHF", flat_state("USDCHF", 8.0), 1)
            trader.create_pair("EURCHF", "derived", ["EURUSD", "USDCHF"], False)
            base = {b: store.latest(b) for b in ("EURUSD", "USDCHF")}
            st = calibrate(store.latest("EURCHF"), base, {t: 5.0 for t in TENORS})
            trader.publish("EURCHF", st, store.latest("EURCHF")["version"])
            write_market(store, "USDCHF", 9.0)
            write_market(store, "EURUSD", 7.5)
            self.assertEqual(fol.step(NOW), ["EURUSD"])
            self.assertEqual(store.latest("USDCHF")["tenors"]["1M"]["atm"], 8.0)
            cross = store.latest("EURCHF")
            self.assertEqual(cross["source"], "propagation")
            self.assertGreater(cross["tenors"]["1M"]["atm"], 5.0)

    def test_cross_edit_survives_propagation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, fol = setup(tmp)
            trader.create_pair("USDCHF", "marked", None, True)
            trader.publish("USDCHF", flat_state("USDCHF", 8.0), 1)
            trader.create_pair("EURCHF", "derived", ["EURUSD", "USDCHF"], False)
            draft = store.latest("EURCHF")
            write_market(store, "EURUSD", 7.5)
            fol.step(NOW)                                        # propagates EURCHF
            self.assertGreater(store.latest("EURCHF")["version"], draft["version"])
            draft["tenors"]["1M"]["rr25"] = -0.7
            rec = trader.publish("EURCHF", draft, draft["version"])
            self.assertEqual(rec["tenors"]["1M"]["rr25"], -0.7)
            eurusd, usdchf = 7.5, 8.0                            # re-derived on the latest bases, corr 0
            self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], (eurusd ** 2 + usdchf ** 2) ** 0.5)

    def test_derived_cross_follows_fmd_with_implied_corr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, fol = setup(tmp, follow=False)
            trader.create_pair("USDCHF", "marked", None, True)
            trader.publish("USDCHF", flat_state("USDCHF", 8.0), 1)
            trader.create_pair("EURCHF", "derived", ["EURUSD", "USDCHF"], False)
            st = store.latest("EURCHF")
            st["follow_market"] = True
            for t in TENORS:                                     # as the Setup tab does: start from the FMD mids
                st["tenors"][t]["atm"] = 4.9
            trader.publish("EURCHF", st, st["version"])
            write_market(store, "EURCHF", 5.0)
            self.assertEqual(fol.step(NOW), ["EURCHF"])
            cross = store.latest("EURCHF")
            self.assertTrue(cross["follow_bases"])               # the setting is kept, but FMD owns the ATMs
            self.assertAlmostEqual(cross["tenors"]["1M"]["atm"], 5.0)
            self.assertAlmostEqual(cross["tenors"]["1M"]["corr"], implied_corr(5.0, 7.0, 8.0, 1, 1))
            # an FMD-driven base move: the ATMs stay with FMD, the correlation follows the bases
            trader.publish("EURUSD", flat_state("EURUSD", 7.6), store.latest("EURUSD")["version"], source="market")
            cross = store.latest("EURCHF")
            self.assertEqual(cross["source"], "propagation")
            self.assertAlmostEqual(cross["tenors"]["1M"]["atm"], 5.0)
            self.assertAlmostEqual(cross["tenors"]["1M"]["corr"], implied_corr(5.0, 7.6, 8.0, 1, 1))
            v = cross["version"]
            trader.publish("EURUSD", flat_state("EURUSD", 7.6001), store.latest("EURUSD")["version"], source="market")
            self.assertEqual(store.latest("EURCHF")["version"], v)      # below min_corr_change

    def test_not_excel_and_fmd_at_once(self):
        st = flat_state("EURCHF", 5.0)
        st.update(mode="derived", bases=["EURUSD", "USDCHF"], follow_market=True, excel=True)
        with self.assertRaisesRegex(ValueError, "Excel and follow FMD"):
            clean_state(st)

    def test_trader_edit_survives_market_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, fol = setup(tmp)
            draft = store.latest("EURUSD")                       # trader starts editing v2
            write_market(store, "EURUSD", 7.4)
            fol.step(NOW)
            self.assertEqual(store.latest("EURUSD")["version"], 3)
            draft["tenors"]["1M"]["spread"] = 0.8
            draft["tenors"]["1M"]["atm"] = 6.0                   # read-only in the UI, ignored
            rec = trader.publish("EURUSD", draft, 2)
            self.assertEqual(rec["version"], 4)
            self.assertEqual(rec["tenors"]["1M"]["spread"], 0.8)
            self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], 7.4)
            # a version by another trader is still a conflict
            other = Service(store, "other")
            other.publish("EURUSD", store.latest("EURUSD"), 4)
            with self.assertRaises(ConflictError):
                trader.publish("EURUSD", draft, 4)


class TestAlerts(unittest.TestCase):
    def test_tolerance_confirm_hysteresis_and_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, _ = setup(tmp, follow=False)
            write_market(store, "EURUSD", 7.1)
            trader.poll_market(notify=False)
            trader.evaluate_alerts(NOW)
            self.assertEqual(trader.alerts.list(), [])           # 0.10 < 0.25
            write_market(store, "EURUSD", 7.4)
            trader.poll_market(notify=False)
            trader.evaluate_alerts(NOW)
            self.assertEqual(trader.alerts.list(), [])           # not confirmed yet
            trader.evaluate_alerts(NOW + 21)
            keys = {a["key"] for a in trader.alerts.list()}
            self.assertIn("EURUSD|1M|atm", keys)
            self.assertNotIn("EURUSD|ON|atm", keys)              # ON tolerance 0.25 x 2
            a = [a for a in trader.alerts.list() if a["key"] == "EURUSD|1M|atm"][0]
            self.assertAlmostEqual(a["dev"], -0.4)
            s = [p for p in trader.summaries() if p["pair"] == "EURUSD"][0]
            self.assertEqual(s["alerts"], s["alerts_new"])
            trader.ack_alerts(pair="EURUSD")
            self.assertTrue(all(a["acked"] for a in trader.alerts.list()))
            write_market(store, "EURUSD", 7.7)                   # worse by another tolerance
            trader.poll_market(notify=False)
            trader.evaluate_alerts(NOW + 22)
            a = [a for a in trader.alerts.list() if a["key"] == "EURUSD|1M|atm"][0]
            self.assertFalse(a["acked"])
            write_market(store, "EURUSD", 7.22)                  # inside tol but above 0.8*tol: kept
            trader.poll_market(notify=False)
            trader.evaluate_alerts(NOW + 23)
            self.assertIn("EURUSD|1M|atm", {a["key"] for a in trader.alerts.list()})
            write_market(store, "EURUSD", 7.1)
            trader.poll_market(notify=False)
            trader.evaluate_alerts(NOW + 24)
            self.assertEqual(trader.alerts.list(), [])

    def test_pair_tolerance_override_and_mute(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, _ = setup(tmp, follow=False)
            st = store.latest("EURUSD")
            st["alert_tol"] = {"atm": 0.5}
            trader.publish("EURUSD", st, st["version"])
            write_market(store, "EURUSD", 7.4)
            trader.poll_market(notify=False)
            trader.evaluate_alerts(NOW)
            trader.evaluate_alerts(NOW + 30)
            self.assertFalse([a for a in trader.alerts.list() if a["mark"] == "atm"])
            st = store.latest("EURUSD")
            st["alerts_smile_off"] = True                       # RR and BF muted, ATM still alerts
            st["alert_tol"] = None
            st["tenors"]["1M"]["rr25"] = 2.0
            trader.publish("EURUSD", st, st["version"])
            trader.evaluate_alerts(NOW + 40)
            trader.evaluate_alerts(NOW + 70)
            self.assertEqual({a["mark"] for a in trader.alerts.list() if a["kind"] == "market"}, {"atm"})
            st = store.latest("EURUSD")
            st["alerts_off"] = True
            st["tenors"]["1M"]["rr25"] = 2.0
            trader.publish("EURUSD", st, st["version"])
            trader.evaluate_alerts(NOW + 100)
            self.assertEqual(trader.alerts.list(), [])

    def test_follow_pair_alerts_when_not_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, fol = setup(tmp)
            write_market(store, "EURUSD", 7.5)
            trader.poll_market(notify=False)
            trader.evaluate_alerts(NOW)
            trader.evaluate_alerts(NOW + 11)
            kinds = [a["kind"] for a in trader.alerts.list()]
            self.assertEqual(kinds, ["follow"])                  # no follower running yet
            fol.step()
            trader.poll_market(notify=False)
            trader.poll_journal()
            trader.evaluate_alerts(NOW + 12)
            self.assertEqual(trader.alerts.list(), [])           # deviations on a follow pair don't alert
            self.assertEqual([p["follow"] for p in trader.summaries()], ["ok"])

    def test_history_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader, _ = setup(tmp, follow=False)
            for _ in range(30):
                st = store.latest("EURUSD")
                trader.publish("EURUSD", st, st["version"])
            h = store.history("EURUSD", 5)
            self.assertEqual([r["version"] for r in h], [32, 31, 30, 29, 28])
            self.assertEqual(len(store.history("EURUSD", 1000)), 32)


if __name__ == "__main__":
    unittest.main()
