import csv
import gzip
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from volmarker.analysis import Exporter
from volmarker.conventions import TENORS
from volmarker.exports import fenics_lines
from volmarker.service import STATE_KEYS, Service
from volmarker.store import Store
from volmarker.surface import clean_state, new_state, resolve


def rows(path):
    return list(csv.DictReader(io.StringIO(gzip.decompress(path.read_bytes()).decode())))


class TestAnalysisTables(unittest.TestCase):
    """Back-fill into month files, then one finished day each morning."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "data"
        self.store = Store(self.root)
        self.svc = Service(self.store, "anna")
        self.legacy = Path(self.tmp.name) / "legacy"
        self.legacy.mkdir()
        self.out = self.root / "analysis"

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, pair, atm, rr):
        s = new_state(pair)
        for t in TENORS:
            s["tenors"][t].update(atm=atm, spread=0.4, rr25=rr, bf25=0.2)
        return clean_state(s)

    def excel_copy(self, pair, stamp, atm, rr, inverse=False):
        text = "".join(l + "\r\n" for l in fenics_lines(pair, resolve(self.state(pair, atm, rr)), inverse))
        (self.legacy / f"{pair}_{stamp}.csv").write_text(text)

    def version(self, pair, atm, ts):
        """Publish, then date the version (tests can't wait for days to pass)."""
        rec = self.svc.publish(pair, self.state(pair, atm, 0.1), self.store.latest(pair)["version"])
        p = self.store._history(pair)
        lines = p.read_text().splitlines()
        r = json.loads(lines[-1]); r["ts"] = ts
        lines[-1] = json.dumps(r)
        p.write_text("\n".join(lines) + "\n")
        return rec

    def exporter(self):
        return Exporter(self.store, legacy=self.legacy, zone="Europe/Zurich")

    def test_backfill_in_months(self):
        self.excel_copy("EURUSD", "20250310_101500", 7.0, 0.15)
        self.excel_copy("EURUSD", "20250310_163000", 7.2, 0.20)        # the day's last: the EOD
        self.excel_copy("EURUSD", "20250402_090000", 7.3, 0.20)
        self.excel_copy("NOKCHF", "20250310_120000", 9.0, -0.5, inverse=True)
        self.excel_copy("EURUSD", "20990101_090000", 1.0, 0.0)         # after VolMarker's start: not used
        (self.legacy / "notes_20250310.csv").write_text("x")              # not a dated copy: ignored
        self.svc.create_pair("NOKCHF", "marked", None, False)           # inverse: Fenics has its RRs as CHFNOK
        r = self.store.latest("NOKCHF")
        self.svc.save_settings("NOKCHF", dict({k: r.get(k) for k in STATE_KEYS}, inverse=True), r["version"])
        self.svc.create_pair("EURUSD", "marked", None, True)
        c = self.exporter().backfill(date(2099, 1, 1), progress=lambda *_: None)
        self.assertEqual(c["excel_files"], 4)
        mar = rows(self.out / "marks" / "2025-03.csv.gz")
        r = next(r for r in mar if r["pair"] == "EURUSD" and r["tenor"] == "1M" and r["time_local"].endswith("10:15:00"))
        self.assertEqual((r["atm_mid"], r["spread"], r["rr25"], r["bf25"], r["source"]), ("7", "0.4", "0.15", "0.2", "excel-file"))
        self.assertEqual((r["time_utc"], r["expiry"]), ("2025-03-10 09:15:00", "2025-04-10"))
        self.assertEqual(next(r for r in mar if r["pair"] == "NOKCHF" and r["tenor"] == "1M")["rr25"], "-0.5")
        self.assertEqual({r["date"][:7] for r in rows(self.out / "marks" / "2025-04.csv.gz")}, {"2025-04"})
        eod = rows(self.out / "marks_eod" / "2025-03.csv.gz")
        self.assertEqual(next(r for r in eod if r["pair"] == "EURUSD" and r["tenor"] == "1M")["atm_mid"], "7.2")
        wide = rows(self.out / "atm_eod_wide" / "2025-03.csv.gz")
        self.assertEqual(next(r for r in wide if r["date"] == "2025-03-10")["EURUSD 3M"], "7.2")
        self.assertEqual(json.loads((self.out / "_state.json").read_text())["through"], "2099-01-01")

    def test_daily_appends_each_finished_day(self):
        self.svc.create_pair("EURUSD", "marked", None, True)
        p = self.store._history("EURUSD")
        r = json.loads(p.read_text()); r["ts"] = "2026-10-30T08:00:00+00:00"
        p.write_text(json.dumps(r) + "\n")
        ex = self.exporter()
        ex.backfill(date(2026, 10, 31), progress=lambda *_: None)
        self.version("EURUSD", 7.1, "2026-11-02T09:00:00+00:00")
        self.version("EURUSD", 7.2, "2026-11-02T15:00:00+00:00")
        self.version("EURUSD", 7.3, "2026-11-03T09:00:00+00:00")
        self.version("EURUSD", 9.9, "2026-11-04T09:00:00+00:00")      # today: not yet
        days = self.exporter().daily(date(2026, 11, 4))
        self.assertEqual(days, [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)])
        nov = rows(self.out / "marks" / "2026-11.csv.gz")
        self.assertEqual(sorted({(r["date"], r["atm_mid"]) for r in nov if r["tenor"] == "1M"}),
                         [("2026-11-02", "7.1"), ("2026-11-02", "7.2"), ("2026-11-03", "7.3")])
        eod = rows(self.out / "marks_eod" / "2026-11.csv.gz")
        self.assertEqual([r["atm_mid"] for r in eod if r["tenor"] == "1M"], ["7.2", "7.3"])
        self.assertEqual(self.exporter().daily(date(2026, 11, 4)), [])           # done for today
        self.assertEqual(len(rows(self.out / "marks" / "2026-10.csv.gz")), len(TENORS))   # October untouched

    def test_interrupted_run_is_redone_cleanly(self):
        self.svc.create_pair("EURUSD", "marked", None, True)
        self.version("EURUSD", 7.1, "2026-11-02T09:00:00+00:00")
        ex = self.exporter()
        ex.backfill(date(2026, 11, 2), progress=lambda *_: None)
        f = self.out / "marks" / "2026-11.csv.gz"
        good = f.read_bytes()
        f.write_bytes(good + gzip.compress(b"half a day")[:15])                 # a cut-off append
        self.assertEqual(self.exporter().daily(date(2026, 11, 4)), [date(2026, 11, 3)])
        vals = [r["atm_mid"] for r in rows(f) if r["tenor"] == "1M"]
        self.assertEqual(vals[-1], "7.1")
        self.assertNotIn("half a day", gzip.decompress(f.read_bytes()).decode())

    def test_market_day_files(self):
        self.svc.market = {"EURUSD": [{"tenor": "1M", "mark": "atm", "ptype": "DNS", "brand": "FMD",
                                       "bid": 7.0, "offer": 7.3, "mid": 7.15, "ts": "20261103-09:00:00"}]}
        self.svc.record_market(datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc).timestamp())
        self.exporter().daily(date(2026, 11, 4))
        mk = rows(self.out / "market" / "2026-11" / "2026-11-03.csv.gz")
        self.assertEqual([(r["pair"], r["tenor"], r["mid"], r["time_local"], r["full_set"]) for r in mk],
                         [("EURUSD", "1M", "7.15", "2026-11-03 10:00:00", "1")])

    def test_leader_runs_it_once_a_day(self):
        self.svc.create_pair("EURUSD", "marked", None, True)
        self.assertEqual(self.svc.export_analysis(date(2026, 11, 4)), [date(2026, 11, 3)])   # no back-fill: from yesterday
        cfg = json.loads((self.root / "config.json").read_text())
        cfg["analysis_dir"] = ""
        (self.root / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(self.svc.export_analysis(date(2026, 11, 5)), [])


if __name__ == "__main__":
    unittest.main()
