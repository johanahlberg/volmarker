import json
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timezone

from volmarker.bbgfeed import BloombergFeed, next_stop, quiet, ticker_map
from volmarker.conventions import TENORS
from volmarker.follow import Follower
from volmarker.service import Service
from volmarker.store import Store
from tests.test_engine import flat_state
from tests.test_follow import NOW, write_market

CFG = {"source": "BGN", "pair_source": {}, "atm_type": "DNS",
       "tickers": {"atm": "{pair}V{tenor} {source} Curncy", "rr": "{pair}{delta}R{tenor} {source} Curncy",
                   "bf": "{pair}{delta}B{tenor} {source} Curncy"},
       "fields": {"bid": "PX_BID", "offer": "PX_ASK", "mid": "PX_MID"}, "stop_at": ["18:00", "00:00"],
       "quiet_hours": ["18:00", "07:00"]}


def zurich(h, m=0, day=5):
    """time.time() of h:m on 5 Nov 2026 in Zurich (November: UTC+1)."""
    return datetime(2026, 11, day, 0, m, tzinfo=timezone.utc).timestamp() + (h - 1) * 3600


class FakeBlp:
    """bdp stand-in: a level per kind of ticker (V, 25R, 10R, 25B, 10B), +0.3 wide."""

    def __init__(self, atm=8.0, rr25=-0.4, rr10=-0.8, bf25=0.25, bf10=0.7, skip=()):
        self.lv = {"V": atm, "25R": rr25, "10R": rr10, "25B": bf25, "10B": bf10}
        self.skip, self.asked = set(skip), []

    def fetch(self, tickers, fields):
        self.asked += tickers
        out = {}
        for t in tickers:
            code = t.split()[0][6:]
            kind = next((k for k in ("25R", "10R", "25B", "10B") if code.startswith(k)), "V")
            if kind in self.skip:
                continue
            v = self.lv[kind]
            out[t] = {"PX_BID": v - 0.15, "PX_ASK": v + 0.15, "PX_MID": v}
        return out

    def close(self):
        pass


def setup(tmp, source="BBG", follow=False, inverse=False, pair="USDPLN"):
    store = Store(tmp)
    trader = Service(store, "trader")
    trader.create_pair(pair, "marked", None, True)
    st = flat_state(pair, 7.0, rr25=0.1, rr10f=2.0, bf25=0.2, bf10f=3.0)
    st.update(market_source=source, follow_market=follow, inverse=inverse)
    trader.publish(pair, st, 1)
    return store, trader


def bbg_status(store, connected=True, stopped=None):
    (store.root / "market").mkdir(exist_ok=True)
    (store.root / "market" / "_status_bbg.json").write_text(json.dumps(
        {"instance": "elsewhere", "user": "anna", "connected": connected, "heartbeat": time.time(),
         "stopped": stopped}))


class TestTickers(unittest.TestCase):
    def test_standard_and_inverse_pairs(self):
        rec = flat_state("EURUSD", 7.0)
        tm = ticker_map(rec, CFG)
        self.assertEqual(tm["EURUSDV1M BGN Curncy"], ("1M", "atm", "DNS"))
        self.assertEqual(tm["EURUSD25R1M BGN Curncy"], ("1M", "rr25", "RR25"))
        self.assertEqual(tm["EURUSD10B1Y BGN Curncy"], ("1Y", "bf10", "S10"))
        self.assertEqual(len(tm), 5 * len(TENORS))
        inv = dict(flat_state("NOKCHF", 9.0), inverse=True)        # fetched as the standard CHFNOK, like FMD
        self.assertIn("CHFNOK25R1M BGN Curncy", ticker_map(inv, CFG))
        self.assertIn("NOKCHFV1M BVOL Curncy", ticker_map(flat_state("NOKCHF", 9.0), dict(CFG, pair_source={"NOKCHF": "BVOL"})))

    def test_stop_times_and_quiet_hours(self):
        z = "Europe/Zurich"
        self.assertEqual(next_stop(CFG, z, zurich(8)), zurich(18))
        self.assertEqual(next_stop(CFG, z, zurich(18)), zurich(0, day=6))        # restarted at 18:00: midnight
        self.assertEqual(next_stop(CFG, z, zurich(18, 30)), zurich(0, day=6))
        self.assertEqual(next_stop(CFG, z, zurich(0, 10, day=6)), zurich(18, day=6))
        self.assertEqual(next_stop(dict(CFG, stop_at="18:00"), z, zurich(19)), zurich(18, day=6))
        self.assertIsNone(next_stop(dict(CFG, stop_at=[]), z, zurich(19)))
        self.assertEqual([quiet(CFG, z, zurich(h)) for h in (6, 7, 17, 18, 23)], [True, False, False, True, True])
        self.assertFalse(quiet(dict(CFG, quiet_hours=[]), z, zurich(23)))


class TestFeed(unittest.TestCase):
    def test_fetches_only_bloomberg_pairs_and_drops_switched_back_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader = setup(tmp)
            trader.create_pair("EURUSD", "marked", None, True)          # prefers FMD
            fake = FakeBlp()
            feed = BloombergFeed(store, "trader", trader._recs)
            feed.client = fake
            self.assertEqual(feed.round(), ["USDPLN"])
            self.assertTrue(fake.asked and all(t.startswith("USDPLN") for t in fake.asked))
            data = json.loads((store.root / "market" / "bbg" / "USDPLN.json").read_text())
            q = next(q for q in data["quotes"] if q["tenor"] == "1M" and q["mark"] == "rr25")
            self.assertEqual((q["brand"], q["bid"], q["offer"], q["mid"]), ("BBG", -0.55, -0.25, -0.4))
            self.assertEqual(feed.round(), [])                           # unchanged ...
            self.assertTrue((store.root / "market" / "bbg" / "USDPLN.json").exists())   # ... but stamped again
            rec = store.latest("USDPLN")
            rec["market_source"] = None
            trader.save_settings("USDPLN", rec, rec["version"])
            trader.poll_journal()
            fake.asked = []
            feed.round()
            self.assertEqual(fake.asked, [])
            self.assertFalse((store.root / "market" / "bbg" / "USDPLN.json").exists())

    def test_start_refused_while_another_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader = setup(tmp)
            bbg_status(store)
            feed = BloombergFeed(store, "trader", trader._recs, client_factory=FakeBlp)
            with self.assertRaisesRegex(ValueError, "already running.*anna"):
                feed.start(follow=False)

    def test_stops_itself_at_stop_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader = setup(tmp)
            t = [zurich(17, 59, )]
            feed = BloombergFeed(store, "trader", trader._recs, client_factory=FakeBlp, clock=lambda: t[0])
            feed.start(follow=False)
            self.assertTrue(feed.running())
            t[0] = zurich(18, 0) + 1
            for _ in range(40):
                if not feed.running():
                    break
                time.sleep(0.1)
            self.assertFalse(feed.running())
            st = json.loads((store.root / "market" / "_status_bbg.json").read_text())
            self.assertFalse(st["connected"])
            self.assertIn("18:00", st["stopped"])
            # someone staying late starts it again at once; that run stops at midnight
            t[0] = zurich(18, 5)
            self.assertTrue(feed.start(300, follow=False)["running"])
            self.assertEqual(feed.state()["seconds"], 300)              # the app's interval field (5 min)
            self.assertEqual(feed.set_seconds(30)["seconds"], 30)
            with self.assertRaisesRegex(ValueError, "at most every 10 seconds"):
                feed.set_seconds(5)
            self.assertEqual(feed.stop_due, zurich(0, day=6))
            feed.stop()


class TestServiceView(unittest.TestCase):
    def test_bloomberg_first_fmd_where_it_has_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader = setup(tmp)
            write_market(store, "USDPLN", 7.5, rr25=0.3, bf25=0.2)   # FMD
            feed = BloombergFeed(store, "trader", trader._recs)
            feed.client = FakeBlp(skip={"25B"})
            feed.round()
            bbg_status(store)
            trader.poll_market(notify=False)
            m = trader.market_for("USDPLN")
            self.assertEqual(m["source"], "BBG")
            v = m["view"]["1M"]
            self.assertEqual((v["atm"]["brand"], v["atm"]["mid"]), ("BBG", 8.0))
            self.assertEqual((v["bf25"]["brand"], v["bf25"]["mid"]), ("FMD", 0.2))     # Bloomberg has none
            self.assertIn("BBG", m["brands"])
            # the draft's source before it is saved; an FMD pair never shows Bloomberg
            self.assertEqual(trader.market_for("USDPLN", source="FMD")["view"]["1M"]["atm"]["brand"], "FMD")
            # Bloomberg stopped: FMD again
            bbg_status(store, connected=False, stopped="stopped at 18:00 (daily download limit)")
            trader.poll_market(notify=False)
            self.assertEqual(trader.market_for("USDPLN")["view"]["1M"]["atm"]["brand"], "FMD")

    def test_bloomberg_alone_is_a_live_market_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader = setup(tmp)
            feed = BloombergFeed(store, "trader", trader._recs)
            feed.client = FakeBlp()
            feed.round()
            bbg_status(store)
            trader.poll_market(notify=False)
            self.assertFalse(trader.feed_alive())                      # no FMD
            rec = trader.get("USDPLN")
            self.assertTrue(trader.market_alive(rec))
            summ = next(s for s in trader.summaries() if s["pair"] == "USDPLN")
            self.assertTrue(summ["mkt_out"])                             # 7.0 marked vs Bloomberg 8.0
            self.assertGreater(trader.record_market(), 0)


class TestFollowBloomberg(unittest.TestCase):
    def test_follows_bloomberg_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader = setup(tmp, follow=True)
            write_market(store, "USDPLN", 7.5, bf25=0.4)                # FMD, also live
            feed = BloombergFeed(store, "trader", trader._recs)
            feed.client = FakeBlp(skip={"25B"})
            feed.round()
            bbg_status(store)
            fol = Follower(Service(store, "BBG"))
            self.assertEqual(fol.step(NOW), ["USDPLN"])
            rec = store.latest("USDPLN")
            self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], 8.0)
            self.assertAlmostEqual(rec["tenors"]["1M"]["bf25"], 0.2)    # not FMD's 0.4: no mix
            self.assertTrue(rec["comment"].startswith("Bloomberg:"))

    def test_daily_stop_holds_the_levels_without_an_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader = setup(tmp, follow=True)
            write_market(store, "USDPLN", 7.5)
            bbg_status(store, connected=False, stopped="stopped at 18:00 (daily download limit)")
            fol = Follower(Service(store, "FMD"))
            self.assertEqual(fol.step(NOW), [])                         # FMD is live, but not the pair's source
            self.assertEqual(fol.status["USDPLN"]["state"], "down")
            trader.poll_market(notify=False)
            with mock.patch("volmarker.service.quiet", return_value=True):            # evening: expected
                self.assertEqual(trader.follow_state("USDPLN"), "stopped")
                trader.evaluate_alerts()
                self.assertFalse([a for a in trader.alerts.list() if a["pair"] == "USDPLN"])
            with mock.patch("volmarker.service.quiet", return_value=False):           # morning, not started yet
                self.assertEqual(trader.follow_state("USDPLN"), "down")
                trader.evaluate_alerts(time.time())
                trader.evaluate_alerts(time.time() + 11)
                al = [a for a in trader.alerts.list() if a["pair"] == "USDPLN"]
                self.assertTrue(al and "follows Bloomberg but the Bloomberg feed is down" in al[0]["msg"])

    def test_one_follower_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, trader = setup(tmp, source=None, follow=True)
            write_market(store, "USDPLN", 7.5)
            (store.root / "market" / "_follow.json").write_text(json.dumps({"instance": "other", "heartbeat": NOW}))
            fol = Follower(Service(store, "BBG"))
            self.assertEqual(fol.step(NOW + 1), [])                     # the other one publishes
            self.assertEqual(store.latest("USDPLN")["tenors"]["1M"]["atm"], 7.0)
            self.assertEqual(fol.step(NOW + 60), ["USDPLN"])            # its heartbeat went stale: takes over


if __name__ == "__main__":
    unittest.main()
