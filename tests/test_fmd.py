import json
import socket
import tempfile
import threading
import time
import unittest

from volmarker.fmd import simulator
from volmarker.fmd.book import QuoteBook
from volmarker.fmd.fix import FixParser, FixSession, encode, md_entries
from volmarker.market import compare, market_view
from volmarker.service import Service
from volmarker.store import Store
from volmarker.surface import resolve
from tests.test_engine import flat_state


class TestCodec(unittest.TestCase):
    def test_spec_examples(self):
        # spec 5.1 and 6.1: body length and checksum must match exactly
        a = encode("A", [(98, 0), (108, 60), (554, "password"), (1137, 9)],
                   "test", "BGCREG", 1, "20230109-14:26:46.284")
        self.assertEqual(a.decode().replace("\x01", "|"),
                         "8=FIXT.1.1|9=85|35=A|49=test|56=BGCREG|34=1|52=20230109-14:26:46.284|"
                         "98=0|108=60|554=password|1137=9|10=086|")
        v = encode("V", [(262, "testMDReqID"), (263, 1), (146, 1), (55, "*")],
                   "<SenderCompId>", "BGCREG", 2, "20230110-09:34:17.269")
        self.assertTrue(v.decode().endswith("|10=215|".replace("|", "\x01")))
        self.assertIn("9=96\x01", v.decode())

    def test_parser_split_and_corrupt(self):
        a = encode("0", [], "S", "T", 1)
        b = encode("0", [], "S", "T", 2)
        bad = bytearray(a)
        bad[-3] = ord("9") if bad[-3] != ord("9") else ord("8")   # break checksum
        p = FixParser()
        msgs = p.feed(bytes(bad) + b[:10]) + p.feed(b[10:])
        self.assertEqual([dict(m)[34] for m in msgs], ["2"])

    def test_md_entries(self):
        msg = encode("X", [(268, 2), (279, 0), (48, "A"), (132, 1.0), (279, 1), (48, "B"), (133, 2.0)],
                     "S", "T", 1)
        e = md_entries(FixParser().feed(msg)[0])
        self.assertEqual(e, [{279: "0", 48: "A", 132: "1.0"}, {279: "1", 48: "B", 133: "2.0"}])


class TestBookAndView(unittest.TestCase):
    def test_static_fields_cached_and_merged(self):
        b = QuoteBook()
        b.apply({279: "0", 48: "X1", 17001: "EURUSD", 6217: "1M", 423: "DNS", 6960: "FMD",
                 132: "6.9", 133: "7.2", 60: "20261018-09:00:00.000"})
        b.apply({279: "1", 48: "X1", 132: "7.0", 133: "7.3"})   # price-only update
        q = b.pair_quotes("EURUSD")[0]
        self.assertEqual((q["tenor"], q["mark"], q["bid"], q["offer"]), ("1M", "atm", 7.0, 7.3))
        self.assertAlmostEqual(q["mid"], 7.15)
        b.apply({279: "2", 48: "X1"})
        self.assertEqual(b.pair_quotes("EURUSD"), [])

    def test_brand_and_atm_choice(self):
        quotes = [
            {"tenor": "1M", "mark": "atm", "ptype": "ATMF", "brand": "FMD", "bid": 7.0, "offer": 7.3, "mid": 7.15},
            {"tenor": "1M", "mark": "atm", "ptype": "DNS", "brand": "GFI", "bid": 6.9, "offer": 7.4, "mid": 7.15},
            {"tenor": "1M", "mark": "atm", "ptype": "DNS", "brand": "BGC", "bid": 6.95, "offer": 7.25, "mid": 7.1},
        ]
        v = market_view(quotes, "DNS", "auto")["1M"]["atm"]
        self.assertEqual((v["brand"], v["ptype"]), ("BGC", "DNS"))      # DNS wanted; BGC before GFI
        best = market_view(quotes, "DNS", "best")["1M"]["atm"]
        self.assertEqual((best["bid"], best["offer"]), (6.95, 7.25))
        v = market_view(quotes, "ATMF", "auto")["1M"]["atm"]
        self.assertEqual(v["brand"], "FMD")
        # only ATMF quoted but DNS wanted -> falls back, flagged
        v = market_view(quotes[:1], "DNS", "auto")["1M"]["atm"]
        self.assertTrue(v["alt_atm"])

    def test_compare(self):
        s = flat_state("EURUSD", 7.0)
        view = {"1M": {"atm": {"bid": 7.1, "offer": 7.4, "mid": 7.25}}}
        out = compare(resolve(s), view)
        self.assertEqual(out[0]["tenor"], "1M")
        self.assertEqual(out[0]["side"], "below bid")


class TestSessionAgainstSimulator(unittest.TestCase):
    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            svc = Service(store, "t")
            svc.create_pair("EURUSD", "marked", None, True)
            svc.publish("EURUSD", flat_state("EURUSD", 7.0), 1)

            srv = socket.socket()
            srv.bind(("127.0.0.1", 0))
            srv.listen()
            port = srv.getsockname()[1]

            def accept():
                conn, _ = srv.accept()
                simulator.serve_client(conn, store, "pw", 0.05)

            threading.Thread(target=accept, daemon=True).start()
            got = []
            sess = FixSession("127.0.0.1", port, "ME", "pw", use_tls=False, heartbeat=5,
                              subscription=[(10460, "fenicsMdFxo")], on_entries=got.extend)
            th = threading.Thread(target=sess.run_once, daemon=True)
            th.start()
            deadline = time.time() + 5
            while time.time() < deadline and len(got) < 200:
                time.sleep(0.05)
            sess.stop()
            th.join(3)
            srv.close()
            book = QuoteBook()
            for e in got:
                book.apply(e)
            view = market_view(book.pair_quotes("EURUSD"))
            self.assertEqual(set(view["1M"]), {"atm", "rr25", "rr10", "bf25", "bf10"})

    def test_bad_password_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            srv = socket.socket()
            srv.bind(("127.0.0.1", 0))
            srv.listen()
            port = srv.getsockname()[1]
            threading.Thread(target=lambda: simulator.serve_client(srv.accept()[0], store, "pw", 0.1),
                             daemon=True).start()
            sess = FixSession("127.0.0.1", port, "ME", "wrong", use_tls=False)
            with self.assertRaisesRegex(ConnectionError, "Invalid password"):
                sess.run_once()
            srv.close()


class TestServiceMarket(unittest.TestCase):
    def test_market_files_feed_summary_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            svc = Service(store, "t")
            svc.create_pair("EURUSD", "marked", None, True)
            (store.root / "market").mkdir()
            (store.root / "market" / "EURUSD.json").write_text(json.dumps({"pair": "EURUSD", "quotes": [
                {"pair": "EURUSD", "tenor": "1M", "mark": "atm", "ptype": "DNS", "brand": "FMD",
                 "bid": 7.1, "offer": 7.4, "mid": 7.25}]}))
            (store.root / "market" / "_status.json").write_text(json.dumps(
                {"connected": True, "heartbeat": time.time()}))
            svc.poll_market()
            svc.publish("EURUSD", flat_state("EURUSD", 7.0), 1)
            s = [p for p in svc.summaries() if p["pair"] == "EURUSD"][0]
            self.assertEqual(s["mkt_out"], 1)
            rec = store.latest("EURUSD")
            self.assertEqual(rec["market_at_publish"]["mids"]["1M"]["atm"], 7.25)
            self.assertEqual(svc.feed_status()["state"], "live")


if __name__ == "__main__":
    unittest.main()


class TestDailies(unittest.TestCase):
    def test_recovers_known_event_weight(self):
        from datetime import date
        from volmarker.calendar import cumulative_weights, daily_dates, implied_daily_weights
        from volmarker.fmd.book import daily_n
        self.assertEqual((daily_n("7D"), daily_n("1D"), daily_n("1M")), (7, 1, None))
        today = date(2026, 10, 19)                       # Monday
        hol = {"USD": {date(2026, 10, 26)}}             # a USD holiday inside the window
        truth = {"weekend1": 0.1, "weekend2": 0.1, "holiday1": 0.3, "holiday2": 0.3,
                 "events": [{"date": "2026-10-28", "desc": "FOMC", "weight": 0.8}]}
        cal = daily_dates("EURUSD", today, [1, 4, 5, 6, 7], hol)    # FXO convention: calendar days
        self.assertEqual(cal[1], date(2026, 10, 20))
        self.assertEqual(cal[4], date(2026, 10, 23))                 # Fri
        self.assertEqual((cal[5], cal[6], cal[7]),                   # Sat, Sun, Mon(USD holiday) -> Tue
                         (date(2026, 10, 27),) * 3)
        dd = daily_dates("EURUSD", today, list(range(1, 15)), hol, "business")
        self.assertEqual(dd[5], date(2026, 10, 27))     # 5th good day: weekend and holiday skipped
        cum = cumulative_weights("EURUSD", today, max(dd.values()), truth, hol)
        ref = 0.07 ** 2 / 252                              # variance of a normal day
        vols = {d: (ref * cum[(d - today).days] * 365 / (d - today).days) ** 0.5 for d in dd.values()}
        # calibrate starting from the same factors but without the event
        res = implied_daily_weights("EURUSD", today, vols, dict(truth, events=[]), hol)
        self.assertAlmostEqual(res["ref_var"], ref)
        self.assertEqual(res["events"], [{"date": "2026-10-28", "weight": 0.8}])
        self.assertEqual(res["warnings"], [])


class TestWebApi(unittest.TestCase):
    def test_sse_parser_guide_format(self):
        from volmarker.fmd.webapi import SSEParser, json_to_entry
        # verbatim shape of the guide's example: pretty-printed JSON after "data:"
        text = '''event:DataMessage
data:{
    "ask":"3.718", "bid":"3.708", "mid":"3.713",
    "assetClass":"RATES", "subAssetClass":"OIS",
    "ccy1":"USD", "convention":"SOFR", "tenor":"1Y",
    "brand":"FMD",
    "sym":"FMD.RATES.OIS.USD.SOFR.1Y",
    "processTime":"2026-04-24T09:11:55.890Z"}

: keep-alive

event:DataMessage
data:{"ask":"3.719","mid":"3.714","bid":"3.709","sym":"FMD.RATES.OIS.USD.SOFR.1Y"}

'''
        p, got = SSEParser(), []
        for line in text.splitlines(keepends=True):
            ev = p.line(line)
            if ev:
                got.append(ev)
        self.assertEqual([e[0] for e in got], ["DataMessage", "DataMessage"])
        e = json_to_entry(json.loads(got[0][1]))
        self.assertEqual((e[48], e[132], e[133], e[6217], e[6960]),
                         ("FMD.RATES.OIS.USD.SOFR.1Y", "3.708", "3.718", "1Y", "FMD"))

    def _sim(self, store, secret="s3cret"):
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), simulator.make_webapi_handler(store, secret, 0.05))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def test_end_to_end_and_bad_secret(self):
        from volmarker.fmd.webapi import WebApiSession
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            svc = Service(store, "t")
            svc.create_pair("EURUSD", "marked", None, True)
            svc.publish("EURUSD", flat_state("EURUSD", 7.0), 1)
            srv, base = self._sim(store)
            try:
                bad = WebApiSession(base, base + "/token", "c", "wrong")
                with self.assertRaisesRegex(ConnectionError, "HTTP 401"):
                    bad.run_once()
                got = []
                sess = WebApiSession(base, base + "/token", "c", "s3cret", idle_timeout=5,
                                     on_entries=got.extend)
                th = threading.Thread(target=sess.run_once, daemon=True)
                th.start()
                deadline = time.time() + 5
                while time.time() < deadline and len(got) < 300:
                    time.sleep(0.05)
                sess.stop()
            finally:
                srv.shutdown()
            book = QuoteBook()
            for e in got:
                book.apply(e)
            view = market_view(book.pair_quotes("EURUSD"))
            self.assertEqual(set(view["1M"]), {"atm", "rr25", "rr10", "bf25", "bf10"})
            self.assertIn("14D", view)                      # dailies captured
            self.assertIn("_snapshot_age=2", sess.url())


class TestDailySegments(unittest.TestCase):
    def test_level_differences_between_pillars_are_not_events(self):
        from datetime import date, timedelta
        from volmarker.calendar import cumulative_weights, daily_dates, implied_daily_weights
        today = date(2026, 10, 19)
        w = {"weekend1": 0.1, "weekend2": 0.1, "holiday1": 0.3, "holiday2": 0.3,
             "events": [{"date": "2026-10-28", "desc": "FOMC", "weight": 0.8}]}
        dd = daily_dates("EURUSD", today, list(range(1, 15)), {})
        cum = cumulative_weights("EURUSD", today, max(dd.values()), w, {})
        pillar = dd[5]                                   # e.g. the 1W expiry
        # normal-day variance 30% higher after the pillar (term structure, not an event)
        def tv(d):
            n = (d - today).days
            np_ = (pillar - today).days
            return 1e-4 * (cum[min(n, np_)] + 1.3 * max(0, cum[n] - cum[np_]))
        vols = {d: (tv(d) * 365 / (d - today).days) ** 0.5 for d in dd.values()}
        flat = dict(w, events=[])
        naive = implied_daily_weights("EURUSD", today, vols, flat, {})
        seg = implied_daily_weights("EURUSD", today, vols, flat, {}, pillars=[pillar])
        self.assertGreater(len(naive["events"]), 1)       # global reference invents events
        self.assertEqual(seg["events"], [{"date": "2026-10-28", "weight": 0.8}])
