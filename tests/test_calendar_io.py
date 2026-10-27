import io
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone

from volmarker.bbg import parse_eco
from volmarker.calendar import normalise_weights
from volmarker.tz import _RuleZone, localize
from volmarker import weights_export as wx


def make_xlsx(rows: list[list]) -> bytes:
    """Minimal .xlsx with inline strings and numbers, like a Bloomberg ECO export."""
    def cell(r, c, v):
        ref = chr(65 + c) + str(r)
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        v = str(v).replace("&", "&amp;")
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    sheet = "".join(f'<row r="{i + 1}">' + "".join(cell(i + 1, j, v) for j, v in enumerate(r) if v is not None) + "</row>"
                    for i, r in enumerate(rows))
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="eco" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{sheet}</sheetData></worksheet>")
    return buf.getvalue()


def serial(dt: datetime) -> float:
    return (dt - datetime(1899, 12, 30)).total_seconds() / 86400 - 1e-9     # Bloomberg's 14:29:59.999


HDR = ["Date Time", "Country Code", "Index", None, "Event", "Period", "Survey", "Actual", "Prior", "Revised", "Relevance", "Ticker"]


class TestBloomberg(unittest.TestCase):
    def test_parse(self):
        rows = [HDR,
                [serial(datetime(2026, 10, 28, 19, 0)), "US", "1)", None, "FOMC Rate Decision (Upper Bound)", "", "", "", "", "", 98.0, "FDTR Index"],
                [serial(datetime(2026, 12, 9, 20, 0)), "US", "2)", None, "FOMC Rate Decision (Upper Bound)", "", "", "", "", "", 98.0, "FDTR Index"],
                ["10/27/2026", "US", "3)", None, "Building Permits", "", "", "", "", "", 62.0, "NHSPATOT Index"],
                ["3/08/2027-3/12/2027", "US", "4)", None, "Household Change in Net Worth", "", "", "", "", "", 27.0, "NWORCHNG Index"],
                [serial(datetime(2026, 11, 6, 8, 0)), "GE", "5)", None, "Industrial Production SA MoM", "", "", "", "", "", 86.8, "GRIPIMOM Index"],
                [serial(datetime(2026, 11, 6, 9, 0)), "XX", "6)", None, "Something", "", "", "", "", "", 1.0, ""],
                ["Download time: 21OCT2026_19:16:01.415756"]]
        r = parse_eco(make_xlsx(rows), "Europe/Zurich")
        ev = r["events"]
        self.assertEqual(r["downloaded"], "21OCT2026_19:16:01.415756")
        # 19:00 Zurich (CET since 25 Oct) = 18:00 UTC = 14:00 New York (EDT until 1 Nov)
        self.assertEqual((ev[0]["date"], ev[0]["time"], ev[0]["ts"]), ("2026-10-28", "19:00", "2026-10-28T18:00+00:00"))
        self.assertEqual(ev[1]["ts"], "2026-12-09T19:00+00:00")
        self.assertEqual((ev[2]["date"], ev[2]["ts"]), ("2026-10-27", None))       # date only
        self.assertEqual(len(r["skipped"]), 1)                                       # date range
        self.assertEqual((ev[3]["ccy"], ev[3]["name"]), ("EUR", "GE Industrial Production SA MoM"))
        self.assertEqual(ev[0]["name"], "FOMC Rate Decision (Upper Bound)")          # home country: no prefix
        self.assertEqual(r["assumed"], {"XX": "USD"})                                # unknown -> the file's currency
        with self.assertRaises(ValueError):
            parse_eco(b"not a zip", "Europe/Zurich")


class TestTimeZones(unittest.TestCase):
    def test_fallback_rules(self):
        ny, zh = _RuleZone("America/New_York", -5, "us"), _RuleZone("Europe/Zurich", 1, "eu")
        u = lambda *a: datetime(*a, tzinfo=timezone.utc)
        self.assertEqual(u(2026, 10, 28, 18).astimezone(ny).hour, 14)        # EDT
        self.assertEqual(u(2026, 11, 2, 19).astimezone(ny).hour, 14)         # EST
        self.assertEqual(u(2026, 10, 28, 18).astimezone(zh).hour, 19)        # CET (gap week)
        self.assertEqual(u(2026, 10, 20, 18).astimezone(zh).hour, 20)        # CEST
        self.assertEqual(datetime(2026, 7, 1, 10).replace(tzinfo=ny).astimezone(timezone.utc).hour, 14)
        self.assertEqual(localize(datetime(2026, 10, 28, 19), "Europe/Zurich"), u(2026, 10, 28, 18))


class TestWeightsExport(unittest.TestCase):
    W = normalise_weights({"weekdays": [1.05, 1, 1, 1, 1, 0, 0], "holiday1": 0.4, "holiday2": 0.5, "events": [
        {"date": "2026-10-29", "desc": "USD FOMC Rate Decision (Upper Bound)", "weight": 1.4, "ccy": "USD"},
        {"date": "2026-11-11", "desc": "USD Veterans day data", "weight": 0.3, "ccy": "USD"},
        {"date": "2026-11-16", "desc": "S&P PMI", "weight": 0.2, "ccy": "EUR"},
        {"date": "2026-11-20", "desc": "zero", "weight": 0, "ccy": "EUR"}]})
    HOL = {"USD": {date(2026, 11, 11)}, "EUR": {date(2026, 11, 13)}}
    TODAY = date(2026, 10, 21)

    def test_both_currencies_on_one_day_are_separate_events(self):
        from volmarker.calendar import with_defaults
        cal = [{"ccy": "USD", "date": "2026-11-04", "desc": "FOMC Rate Decision", "ts": "2026-11-04T12:00+00:00"},
               {"ccy": "EUR", "date": "2026-11-04", "desc": "ECB Main Refinancing Rate", "ts": "2026-11-04T12:15+00:00"}]
        rules = [{"ccy": "USD", "event": "FOMC Rate Decision", "weight": 0.8},
                 {"ccy": "EUR", "event": "ECB Main Refinancing Rate", "weight": 0.6}]
        w = with_defaults("EURUSD", {"events": []}, cal, rules)
        items = wx.fenics_items("EURUSD", w, {}, self.TODAY)
        self.assertEqual([(e["ccy"], e["name"], e["value"]) for e in items],
                         [("EUR", "ECB Main Refinancing Rate", 0.6), ("USD", "FOMC Rate Decision", 0.8)])
        fx, ws = wx.frontarena_lines("EURUSD", w, {}, self.TODAY, "%Y-%m-%d")
        self.assertEqual(fx, ["EUR,2026-11-04,ECB Main Refinancing Rate,0.6", "USD,2026-11-04,FOMC Rate Decision,0.8"])
        self.assertEqual(len(ws), 1)                     # FA_WS: one weight per day, as the macro
        # the ECB set to 0 for this pair (typed as "ECB"): still sent, as an overview, named after the
        # default event; the FOMC keeps its weight; FA_WS gets only the FOMC
        w = with_defaults("EURUSD", {"events": [{"date": "2026-11-04", "ccy": "EUR", "desc": "ECB", "weight": 0}]},
                          cal, rules)
        self.assertEqual(sorted((e["ccy"], e["name"], e["value"]) for e in wx.fenics_items("EURUSD", w, {}, self.TODAY)),
                         [("EUR", "ECB Main Refinancing Rate", 0.0), ("USD", "FOMC Rate Decision", 0.8)])
        fx, ws = wx.frontarena_lines("EURUSD", w, {}, self.TODAY, "%Y-%m-%d")
        self.assertEqual(len(fx), 2)
        self.assertEqual(ws, ["USD,2026-11-03,USD FOMC Rate Decision,1.8"])

    def test_calibrated_weight_split_between_currencies(self):
        from volmarker.calendar import split_by_ccy
        day = [{"date": "2026-11-04", "ccy": "EUR", "desc": "EUR ECB", "weight": 0.6},
               {"date": "2026-11-04", "ccy": "USD", "desc": "USD FOMC", "weight": 0.8},
               {"date": "2026-11-05", "ccy": "USD", "desc": "USD ISM", "weight": 0.3}]
        got = split_by_ccy([{"date": "2026-11-04", "desc": "FMD daily", "weight": 1.0},
                            {"date": "2026-11-05", "desc": "FMD daily", "weight": 0.4},
                            {"date": "2026-11-06", "desc": "FMD daily", "weight": 0.2}], day)
        self.assertEqual([(e["date"], e.get("ccy"), e["desc"], e["weight"]) for e in got],
                         [("2026-11-04", "EUR", "EUR ECB", 0.5), ("2026-11-04", "USD", "USD FOMC", 0.5),
                          ("2026-11-05", "USD", "USD ISM", 0.4), ("2026-11-06", None, "FMD daily", 0.2)])

    def test_fenics_items(self):
        items = wx.fenics_items("EURUSD", self.W, self.HOL, self.TODAY)
        self.assertEqual([(i["date"].isoformat(), i["type"]) for i in items],
                         [("2026-10-29", "Event"), ("2026-11-11", "Holiday"), ("2026-11-16", "Event"),
                          ("2026-11-20", "Event")])                       # weight 0 sent too: Fenics' overview
        self.assertEqual(items[0]["name"], "FOMC Rate Decision (Upper Bound)")
        # event on a USD holiday (Wed): sent as holiday weight h + w / (weekday x other holiday) = 0.5 + 0.3
        self.assertAlmostEqual(items[1]["value"], 0.5 + 0.3 / 1.0)

    def test_xml(self):
        x = wx.fenics_xml("EURUSD", self.W, self.HOL, self.TODAY, "anna", "s3cret&", datetime(2026, 10, 21, 9, 30))
        self.assertIn('<action name = "load_events" function = "WEIGHT_FEED" version = "1.0">', x)
        self.assertIn('<field name = "Value" value = "1.0500000000000000"/>', x)      # Monday
        self.assertIn('<field name = "Country" value = "USD"/>', x)
        self.assertIn('<field name = "Date" value = "10/29/2026"/>', x)
        self.assertIn('<field name = "EventDesc" value = "S&amp;P PMI"/>', x)           # escaped
        self.assertIn('<field name = "Value" value = "1.4000000000000000"/>', x)
        self.assertIn("<password>s3cret&amp;</password>", x)
        import xml.etree.ElementTree as ET
        ET.fromstring(x.encode())                                                   # well-formed
        # FirstCcy = ccy2 (USD, holiday2), SecondCcy = ccy1 (EUR, holiday1)
        self.assertIn('<node name = "EUR/USD Weights">', x)
        self.assertIn('<node name = "Add event weight 2026-10-29 USD FOMC Rate Decision (Upper Bound) to EUR/USD">', x)
        self.assertRegex(x, r'FirstCcy for EUR/USD">\s*<field name = "Country" value = "USD"/>[\s\S]*?0\.5000000000000000')

    def test_frontarena(self):
        fx, ws = wx.frontarena_lines("EURUSD", self.W, self.HOL, self.TODAY, "%d.%m.%Y")
        self.assertEqual(fx[0], "USD,29.10.2026,FOMC Rate Decision (Upper Bound),1.4")
        self.assertEqual(len(fx), 4)                    # the weight-0 event too; not in FA_WS
        # FA_WS: the business day before; 16 Nov (Mon) -> Fri 13 Nov, a EUR holiday: (0.2 + 0.4) / 0.4
        self.assertEqual(ws[0], "USD,28.10.2026,USD FOMC Rate Decision (Upper Bound),2.4")
        self.assertEqual(ws[-1], f"EUR,13.11.2026,EUR S&P PMI,{(0.2 + 0.4) / 0.4:g}")


if __name__ == "__main__":
    unittest.main()


class TestVolExports(unittest.TestCase):
    def test_workbook_layout(self):
        from pathlib import Path
        from volmarker.exports import write_exports
        from volmarker.surface import new_state, clean_state, resolve
        s = new_state("EURUSD")
        for t in s["tenors"]:
            s["tenors"][t].update(atm=6.35, spread=1.2, rr25=-0.36, rr10f=1.9614, bf25=0.15, bf10f=3.6313)
        s["tenors"]["1W"].update(skew_atm=0.1, skew_rr=0.05)
        s["skew_expiry"] = "2099-01-01T00:00:00+00:00"
        s = clean_state(s)
        s["dc_factor"] = 1.25
        with tempfile.TemporaryDirectory() as tmp:
            dirs = {k: Path(tmp) / k for k in ("fenics", "frontarena", "dc_spread", "history")}
            write_exports(s, resolve(s), dirs, now=datetime(2026, 10, 21, 13, 5, 9))
            fa = (dirs["frontarena"] / "EURUSDvolsurface.txt").read_bytes().decode()
            fen = (dirs["fenics"] / "EURUSDvolsurface.txt").read_bytes().decode()
            lines = fa.split("\r\n")
            self.assertEqual(lines[0], "vol,EUR,USD,1d,5.75,6.95")                     # mid +- spread/2
            self.assertEqual(lines[1], "vol,EUR,USD,1wk,5.75,6.95")                    # no skew for Front Arena
            self.assertEqual(lines[13], "vol25,EUR,USD,strangle,1d,0.15")
            self.assertEqual(lines[26], "vol25,EUR,USD,riskreversal,1d,-0.36")
            self.assertEqual(lines[39], "vol10,EUR,USD,strangle,1d,0.544695")
            self.assertEqual(len(lines), 13 * 5 + 1)                                   # CRLF after the last line
            fl = fen.split("\r\n")
            self.assertEqual(fl[0], "vol,atm,EUR,USD,1d,5.75,6.95")
            self.assertEqual(fl[1], "vol,atm,EUR,USD,1w,5.85,7.05")                    # skewed
            self.assertEqual(fl[13], "vol,s25,EUR,USD,1d,0.15,0.15")                   # bid = ask = mid
            self.assertEqual(fl[27], "vol,rr25,EUR,USD,1w,-0.31,-0.31")
            self.assertTrue(fl[39].startswith("vol,s10,EUR,USD,1d,"))
            self.assertTrue(fl[52].startswith("vol,rr10,EUR,USD,1d,"))
            dc = (dirs["dc_spread"] / "EURUSDspreads.txt").read_text().splitlines()
            self.assertEqual(dc[0], "1d,0.75")                                         # 1.2 x 0.5 x 1.25
            self.assertEqual((dirs["history"] / "EURUSD_20261021_130509.csv").read_text(), fen.replace("\r\n", "\n"))
            s["dc_factor"] = 0.005
            write_exports(s, resolve(s), {**dirs, "dc_spread": Path(tmp) / "dc2"}, history=False)
            self.assertFalse((Path(tmp) / "dc2").exists())


class TestExcelPairs(unittest.TestCase):
    def setUp(self):
        import os
        from pathlib import Path
        from volmarker.service import Service
        from volmarker.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.svc = Service(self.store, "anna")
        self.svc._leader = True
        self.fa = Path(self.tmp.name) / "exports" / "frontarena"
        self.os = os

    def tearDown(self):
        self.tmp.cleanup()

    def _excel_file(self, atm, mtime, pair="USDCHF"):
        from volmarker.exports import frontarena_lines
        from volmarker.surface import clean_state, new_state, resolve
        s = new_state(pair)
        for t in s["tenors"]:
            s["tenors"][t].update(atm=atm, spread=0.6, rr25=-0.45, bf25=0.2)
        s = clean_state(s)
        s["tenors"]["1M"]["rr10"] = -0.95                   # a 10D quote that isn't 25D x factor
        p = self.fa / f"{pair}volsurface.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\r\n".join(frontarena_lines(pair, resolve(s), {t: 0.6 for t in s["tenors"]})) + "\r\n")
        self.os.utime(p, (mtime, mtime))
        return p

    def test_import_and_no_overwrite(self):
        import time as _t
        self.svc.create_pair("USDCHF", "marked", None, True)           # VolMarker writes the file once
        own = (self.fa / "USDCHFvolsurface.txt").stat().st_mtime
        st = self.store.latest("USDCHF")
        st["excel"] = True
        for t in st["tenors"]:
            st["tenors"][t]["atm"] = 7.0
        self.svc.publish("USDCHF", st, st["version"])
        self.assertEqual((self.fa / "USDCHFvolsurface.txt").stat().st_mtime, own)   # not written again
        v0 = self.store.latest("USDCHF")["version"]
        self.svc.poll_excel(now=_t.time() + 5)
        self.assertEqual(self.store.latest("USDCHF")["version"], v0)    # its own old file is not an Excel export
        base = _t.time() + 100
        p = self._excel_file(7.8, base)
        self.svc.poll_excel(now=base + 1)
        self.assertEqual(self.store.latest("USDCHF")["version"], v0)    # maybe still being written: wait 2 s
        self.svc.poll_excel(now=base + 5)
        rec = self.store.latest("USDCHF")
        self.assertEqual((rec["source"], rec["user"]), ("excel", "Excel"))
        self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], 7.8)
        self.assertAlmostEqual(rec["tenors"]["1M"]["spread"], 0.6)
        self.assertAlmostEqual(rec["tenors"]["1M"]["rr10"], -0.95)
        self.assertAlmostEqual(rec["tenors"]["1M"]["rr10f"], -0.95 / -0.45)
        self.assertEqual(rec["marked_ts"][:16], datetime.fromtimestamp(base, timezone.utc).isoformat()[:16])
        v = rec["version"]
        self.svc.poll_excel(now=base + 10)
        self.assertEqual(self.store.latest("USDCHF")["version"], v)            # same file: read once
        # a trader edits the weights while Excel exports again: no conflict, Excel's marks win
        draft = self.svc.get("USDCHF")
        draft["weights"] = {"weekdays": [1, 1, 1, 1, 1, 0, 0], "holiday1": 0.5, "holiday2": 0.5, "events": []}
        self._excel_file(7.9, base + 60)
        self.svc.poll_excel(now=base + 65)
        rec = self.svc.publish("USDCHF", draft, v)
        self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], 7.9)
        self.assertEqual(rec["weights"]["holiday1"], 0.5)
        self.assertEqual(rec["excel_mtime"], base + 60)                        # kept, so it isn't read again
        self.assertEqual(p.stat().st_mtime, base + 60)                         # and the file is untouched
        # a half-written file is skipped and reported
        p.write_text("vol,USD,CHF,1d,7,8\r\n"); self.os.utime(p, (base + 120, base + 120))
        self.svc.poll_excel(now=base + 125)
        self.assertIn("incomplete", self.svc.excel_errors["USDCHF"])
        self.assertEqual(self.store.latest("USDCHF")["version"], rec["version"])

    def test_derived_cross_marked_in_excel(self):
        import time as _t
        from tests.test_engine import flat_state
        from volmarker.derived import implied_corr
        for pair, atm in (("EURUSD", 7.0), ("USDCHF", 8.0)):
            self.svc.create_pair(pair, "marked", None, True)
            self.svc.publish(pair, flat_state(pair, atm), 1)
        self.svc.create_pair("EURCHF", "derived", ["EURUSD", "USDCHF"], False)
        st = self.store.latest("EURCHF")
        st["excel"] = True
        self.svc.publish("EURCHF", st, st["version"])
        base = _t.time() + 100
        p = self._excel_file(5.0, base, "EURCHF")
        self.svc.poll_excel(now=base + 5)
        rec = self.store.latest("EURCHF")
        self.assertEqual(rec["source"], "excel")
        self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], 5.0)
        self.assertAlmostEqual(rec["tenors"]["1M"]["corr"], implied_corr(5.0, 7.0, 8.0, 1, 1))
        # a base publish keeps Excel's ATMs and recalculates the correlation; the file stays Excel's
        self.svc.publish("EURUSD", flat_state("EURUSD", 7.5), self.store.latest("EURUSD")["version"])
        rec = self.store.latest("EURCHF")
        self.assertEqual(rec["source"], "propagation")
        self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], 5.0)
        self.assertAlmostEqual(rec["tenors"]["1M"]["corr"], implied_corr(5.0, 7.5, 8.0, 1, 1))
        self.assertEqual(p.stat().st_mtime, base)
        self.assertTrue(self.svc.summaries()[[s["pair"] for s in self.svc.summaries()].index("EURCHF")]["excel"])

    def test_create_reads_existing_file(self):
        from volmarker.exports import read_frontarena
        import time as _t
        p = self._excel_file(7.8, _t.time() - 3600)                            # copied in before setup
        before = read_frontarena(p.read_text(), "USDCHF")
        rec = self.svc.create_pair("USDCHF", "marked", None, True)
        self.assertEqual(rec["comment"], "created from USDCHFvolsurface.txt")
        self.assertAlmostEqual(rec["tenors"]["1M"]["atm"], 7.8)
        self.assertAlmostEqual(rec["tenors"]["1M"]["rr10"], -0.95)
        after = read_frontarena(p.read_text(), "USDCHF")                       # rewritten with the same marks
        for t in before:
            for m, v in before[t].items():
                self.assertAlmostEqual(after[t][m], v, places=9)

    def test_create_derived_reads_existing_file(self):
        from volmarker.exports import frontarena_lines
        from volmarker.surface import clean_state, new_state, resolve
        for pair, atm in (("EURUSD", 7.0), ("USDCHF", 7.5)):
            st = clean_state(new_state(pair))
            for t in st["tenors"]:
                st["tenors"][t]["atm"] = atm
            self.svc.create_pair(pair, "marked", None, True)
            self.svc.publish(pair, st, 1)
        s = new_state("EURCHF")
        for t in s["tenors"]:
            s["tenors"][t].update(atm=5.2, rr25=-0.3)
        s = clean_state(s)
        (self.fa / "EURCHFvolsurface.txt").write_text(
            "\r\n".join(frontarena_lines("EURCHF", resolve(s), {t: 0.5 for t in s["tenors"]})))
        rec = self.svc.create_pair("EURCHF", "derived", ["EURUSD", "USDCHF"], False)
        self.assertAlmostEqual(rec["tenors"]["3M"]["atm"], 5.2)
        self.assertAlmostEqual(rec["tenors"]["3M"]["rr25"], -0.3)
        self.assertIsNotNone(rec["tenors"]["3M"]["corr"])                     # calibrated to the file's ATM

    def test_create_keeps_10d_of_other_sign(self):
        p = self._excel_file(7.8, 0)
        p.write_text(p.read_text().replace("riskreversal,1mo,-0.95", "riskreversal,1mo,0.1"))
        rec = self.svc.create_pair("USDCHF", "marked", None, True)
        self.assertFalse(rec["lock_rr10f"])
        self.assertAlmostEqual(rec["tenors"]["1M"]["rr10"], 0.1)

    def test_load_export_file_into_draft(self):
        import time as _t
        self.svc.create_pair("USDCHF", "marked", None, True)
        rec = self.store.latest("USDCHF")
        self.assertTrue(self.svc.load_export_file("USDCHF")["ours"])            # VolMarker's own export
        self._excel_file(7.8, _t.time() + 120)                                 # someone marks it in Excel
        r = self.svc.load_export_file("USDCHF", rec)
        self.assertFalse(r["ours"])
        self.assertEqual((r["file"], r["missing"]), ("USDCHFvolsurface.txt", []))
        self.assertAlmostEqual(r["state"]["tenors"]["1M"]["atm"], 7.8)
        self.assertAlmostEqual(r["state"]["tenors"]["1M"]["rr10"], -0.95)
        self.assertEqual(self.store.latest("USDCHF")["version"], rec["version"])   # nothing saved yet
        pub = self.svc.publish("USDCHF", r["state"], rec["version"], "from file")
        self.assertAlmostEqual(pub["tenors"]["1M"]["spread"], 0.6)
        st = dict(pub, excel=True)
        self.svc.publish("USDCHF", st, pub["version"])
        with self.assertRaises(ValueError):                                    # Excel pairs import by themselves
            self.svc.load_export_file("USDCHF")

    def test_foreign_write_alert(self):
        from volmarker.surface import clean_state, new_state
        import time as _t
        self.svc.create_pair("USDCHF", "marked", None, True)
        st = clean_state(dict(new_state("USDCHF")))
        for t in st["tenors"]:
            st["tenors"][t]["atm"] = 7.0
        self.svc.publish("USDCHF", st, 1)
        self.svc.poll_excel()
        self.assertEqual(self.svc.excel_foreign, {})                           # our own export
        self._excel_file(7.8, _t.time() + 120)                                 # Excel writes it later
        self.svc.poll_excel()
        self.assertIn("USDCHF", self.svc.excel_foreign)
        self.svc.evaluate_alerts()
        self.assertTrue(any(k[2] == "excel" for k in self.svc.alerts.active))
