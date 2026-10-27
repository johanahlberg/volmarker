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


class TestXmlPoster(unittest.TestCase):
    # shaped as the Excel query fetchFenicsVols reads it: body/data/node/node[field...]
    ANSWER = """<?xml version="1.0"?><gfi_message version="2.0"><header/><body><data name="d" format="NAME_VALUE">
      <node name="EUR/USD">
        <node name="1"><field name="Maturity" value="1 Day"/><field name="VolType" value="ATM"/><field name="Volity" value="0.0610/0.0640"/></node>
        <node name="2"><field name="Maturity" value="3 Months"/><field name="VolType" value="RR"/><field name="PctDelta" value="0.25"/><field name="Volity" value="-0.0030/-0.0010"/></node>
        <node name="3"><field name="Maturity" value="1 Year"/><field name="VolType" value="S"/><field name="PctDelta" value="0.10"/><field name="Volity" value="0.0075"/></node>
        <node name="4"><field name="Maturity" value="7 Years"/><field name="VolType" value="ATM"/><field name="Volity" value="n/a"/></node>
      </node></data></body></gfi_message>"""

    def test_request_parse_and_entries(self):
        from datetime import datetime
        from volmarker.fmd.xmlposter import NoVols, get_vols_xml, norm_maturity, parse_vols, rows_to_entries
        msg = get_vols_xml("EURUSD", "fenicsVols", "u", "p&w", datetime(2026, 10, 27, 9, 0))
        for part in ('function="RATE_FETCH" name="get_vols"', 'name="horDate" value="27 Oct 2026"',
                     'name="Currency" value="eur"', 'name="CtrCcy" value="usd"', "<password>p&amp;w</password>"):
            self.assertIn(part, msg)
        self.assertEqual([norm_maturity(m) for m in ("1 Day", "2 Weeks", "18 Months", "1 Year", "O/N")],
                         ["ON", "2W", "18M", "1Y", None])
        rows = parse_vols(self.ANSWER)          # Fenics answers in decimals; x100 as the workbook
        self.assertEqual([(r["tenor"], r["ptype"]) for r in rows], [("ON", "ATM"), ("3M", "RR25"), ("1Y", "S10")])
        for r, (bid, offer, mid) in zip(rows, [(6.1, 6.4, 6.25), (-0.3, -0.1, -0.2), (None, None, 0.75)]):
            self.assertAlmostEqual(r["mid"], mid)
            if bid is None:
                self.assertIsNone(r["bid"]); self.assertIsNone(r["offer"])     # one value: a mid, no range
            else:
                self.assertAlmostEqual(r["bid"], bid); self.assertAlmostEqual(r["offer"], offer)
        book = QuoteBook()
        for e in rows_to_entries("EURUSD", rows, "FMD1", "DNS", "2026-10-27T08:00:00+00:00"):
            book.apply(e)
        view = market_view(book.pair_quotes("EURUSD"))
        self.assertAlmostEqual(view["ON"]["atm"]["mid"], 6.25)
        self.assertFalse(view["ON"]["atm"]["alt_atm"])
        self.assertAlmostEqual(view["3M"]["rr25"]["mid"], -0.2)
        self.assertEqual(view["1Y"]["bf10"]["brand"], "FMD1")
        self.assertIsNone(view["1Y"]["bf10"]["bid"])
        self.assertAlmostEqual(view["1Y"]["bf10"]["mid"], 0.75)
        with self.assertRaisesRegex(ValueError, "invalid username"):
            parse_vols("<gfi_message><body><error>Error: invalid username or password</error></body></gfi_message>")
        with self.assertRaisesRegex(ValueError, "not XML"):
            parse_vols("<html>Gateway error")
        with self.assertRaises(NoVols):
            parse_vols('<gfi_message><body><data name="d"><node name="EUR/XYZ"/></data></body></gfi_message>')

    def test_poll_against_simulator(self):
        from http.server import ThreadingHTTPServer
        from volmarker.fmd.xmlposter import XmlPosterSession
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            svc = Service(store, "t")
            svc.create_pair("EURUSD", "marked", None, True)
            svc.publish("EURUSD", flat_state("EURUSD", 7.0), 1)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), simulator.make_xml_handler(store, "s3cret"))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                status = []
                bad = XmlPosterSession(url, "u", "wrong", lambda: ["EURUSD"], on_status=status.append)
                self.assertEqual(bad.poll_once()[0], 0)
                got = []
                sess = XmlPosterSession(url, "u", "s3cret", lambda: ["EURUSD", "GBPUSD"], on_entries=got.extend)
                self.assertEqual(sess.poll_once(), (1, []))           # GBPUSD: no vols, not an error
                self.assertEqual(list(sess.missing), ["GBPUSD"])
                fetched = []
                sess.fetch = lambda pair: fetched.append(pair) or []
                sess.poll_once()
                self.assertEqual(fetched, ["EURUSD"])                 # not asked again within the hour
                sess.missing["GBPUSD"] = 0
                sess.poll_once()
                self.assertEqual(fetched, ["EURUSD", "EURUSD", "GBPUSD"])
            finally:
                srv.shutdown()
                srv.server_close()
            book = QuoteBook()
            for e in got:
                book.apply(e)
            view = market_view(book.pair_quotes("EURUSD"))
            self.assertEqual(set(view["1M"]), {"atm", "rr25", "rr10", "bf25", "bf10"})
            self.assertAlmostEqual(view["1M"]["atm"]["mid"], 7.0, delta=1.0)
            self.assertLess(view["1M"]["atm"]["bid"], view["1M"]["atm"]["offer"])
            self.assertIsNone(view["1M"]["rr25"]["bid"])           # RRs and strangles: mids only
            self.assertEqual(view["ON"]["atm"]["brand"], "FMD1")

class TestFenicsLogin(unittest.TestCase):
    def test_login_feeds_market_files_until_logout(self):
        from http.server import ThreadingHTTPServer
        from volmarker.fenics import FenicsLogin
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            svc = Service(store, "t")
            svc.create_pair("EURUSD", "marked", None, True)
            svc.publish("EURUSD", flat_state("EURUSD", 7.0), 1)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), simulator.make_xml_handler(store, "s3cret"))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            (store.root / "config.json").write_text(json.dumps(
                {"fmd": {"xml": {"url": f"http://127.0.0.1:{srv.server_address[1]}"}}}))
            fx = FenicsLogin(store)
            try:
                self.assertEqual(fx.status()["seconds"], 300)
                with self.assertRaisesRegex(ValueError, "login failed: .*invalid username"):
                    fx.login("anna", "wrong")
                self.assertFalse(fx.status()["logged_in"])
                st = fx.login("anna", "s3cret", 600)
                self.assertTrue(st["logged_in"])
                self.assertEqual((st["user"], st["seconds"], fx.credentials()), ("anna", 600, ("anna", "s3cret")))
                path = store.root / "market" / "EURUSD.json"
                deadline = time.time() + 5
                while time.time() < deadline and not path.exists():
                    time.sleep(0.1)
                quotes = json.loads(path.read_text())["quotes"]
                self.assertEqual({q["brand"] for q in quotes}, {"FMD1"})
                self.assertEqual(fx.set_seconds(30)["seconds"], 30)
                self.assertEqual(fx.feed.session.interval, 30)
                with self.assertRaisesRegex(ValueError, "at most every 10 seconds"):
                    fx.set_seconds(5)
                with self.assertRaisesRegex(ValueError, "Another market data feed"):
                    FenicsLogin(store).login("ben", "s3cret")      # a second app on the same folder
                self.assertFalse(fx.logout()["logged_in"])
                self.assertIsNone(fx.credentials())
            finally:
                fx.logout()
                srv.shutdown()
                srv.server_close()

class TestWriteShared(unittest.TestCase):
    def test_written_in_place_when_the_share_refuses_a_replace(self):
        from pathlib import Path
        from unittest import mock
        from volmarker.store import write_shared
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "_status.json"
            write_shared(path, '{"a": 1}')
            with mock.patch("volmarker.store.os.replace", side_effect=PermissionError("Access is denied")):
                write_shared(path, '{"a": 2}')
            self.assertEqual(json.loads(path.read_text()), {"a": 2})
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["_status.json"])   # no temp file left

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
