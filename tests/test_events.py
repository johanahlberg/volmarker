"""Pair events: desk defaults deleted for a pair, new events being typed, the expiry an event lands on."""

import copy
import csv
import gzip
import io
import tempfile
import unittest
from datetime import date, timedelta

from volmarker.analysis import EVENT_COLS, Exporter

from volmarker.calendar import (DEFAULT_CUT, effective_events, event_complete, expiry_for, normalise_weights,
                                with_defaults)
from volmarker.service import STATE_KEYS, Service
from volmarker.store import Store
from pathlib import Path

from volmarker.conventions import TENORS
from volmarker.surface import new_state

DEFS = [{"date": "2026-11-12", "ccy": "EUR", "desc": "EUR CPI", "weight": 0.5},
        {"date": "2026-11-12", "ccy": "USD", "desc": "USD CPI", "weight": 1.0},
        {"date": "2026-11-13", "ccy": "USD", "desc": "USD Retail Sales", "weight": 0.4}]
NYC = {"tz": "America/New_York", "time": "10:00"}
TOK = {"tz": "Asia/Tokyo", "time": "15:00"}


class TestDeletedDefaults(unittest.TestCase):
    def test_deleted_default_is_gone_for_the_pair_only(self):
        own = [{"date": "2026-11-12", "ccy": "USD", "deleted": True}]
        got = [(e["date"], e["ccy"], e["weight"]) for e in effective_events(own, DEFS)]
        self.assertEqual(got, [("2026-11-12", "EUR", 0.5), ("2026-11-13", "USD", 0.4)])
        # without the marker (brought back): there again
        self.assertEqual(len(effective_events([], DEFS)), 3)

    def test_deleted_own_event_keeps_its_weight_and_hides_nothing(self):
        own = [{"date": "2026-11-13", "ccy": "USD", "desc": "Fed speaker", "weight": 0.3, "deleted": True, "of": "own"},
               {"date": "2026-11-12", "ccy": "EUR", "desc": "EUR CPI", "weight": 0.9, "deleted": True, "of": "default"}]
        got = [(e["date"], e["ccy"], e["weight"]) for e in effective_events(own, DEFS)]
        self.assertEqual(got, [("2026-11-12", "USD", 1.0), ("2026-11-13", "USD", 0.4)])

    def test_markers_never_reach_the_curve(self):
        w = {"events": [{"date": "2026-11-12", "ccy": "USD", "deleted": True},
                        {"date": "2026-11-20", "ccy": "EUR", "desc": "x", "weight": 0.3}]}
        self.assertEqual([e["date"] for e in normalise_weights(w)["events"]], ["2026-11-20"])
        w["use_defaults"] = False
        self.assertEqual([e["date"] for e in with_defaults("EURUSD", w, [], [])["events"]], ["2026-11-20"])

    def test_event_being_typed_does_not_count(self):
        self.assertFalse(event_complete({"date": "", "ccy": "", "desc": "", "weight": None}))
        self.assertFalse(event_complete({"date": "2026-11-20", "weight": None}))
        self.assertFalse(event_complete({"date": "2026-13-01", "weight": 0.5}))
        self.assertTrue(event_complete({"date": "2026-11-20", "weight": 0}))
        w = {"events": [{"date": "", "ccy": "", "desc": "", "weight": None}]}
        self.assertEqual(normalise_weights(w)["events"], [])


class TestExpiry(unittest.TestCase):
    EV = {"ccy": "EUR", "date": "2026-11-11", "time": "08:00", "ts": "2026-11-11T07:00+00:00"}   # 08:00 Zurich

    def test_morning_release_before_the_cut_same_day(self):
        why = []
        self.assertEqual(expiry_for(self.EV, ("EUR", "USD"), {}, NYC, why), date(2026, 11, 11))
        self.assertEqual(why, ["Wed 11 Nov 02:00 New York time, before the 10:00 cut", "expiry Wed 11 Nov"])
        self.assertEqual(expiry_for(self.EV, ("EUR", "CHF"), {}, DEFAULT_CUT), date(2026, 11, 11))

    def test_after_the_tokyo_cut_next_day(self):
        why = []
        self.assertEqual(expiry_for(self.EV, ("EUR", "JPY"), {}, TOK, why), date(2026, 11, 12))
        self.assertEqual(why[0], "Wed 11 Nov 16:00 Tokyo time, after the 15:00 cut: next day Thu 12 Nov")

    def test_holiday_does_not_move_it(self):
        # 11 Nov 2026 is a USD holiday (Veterans Day): EURUSD options still expire that day
        hol = {"USD": {date(2026, 11, 11)}, "EUR": {date(2026, 11, 11)}}
        self.assertEqual(expiry_for(self.EV, ("EUR", "USD"), hol, NYC), date(2026, 11, 11))

    def test_weekend_rolls(self):
        why = []
        ev = {"ccy": "EUR", "date": "2026-11-13", "ts": "2026-11-13T20:00+00:00"}    # Fri 15:00 New York
        self.assertEqual(expiry_for(ev, ("EUR", "USD"), {}, NYC, why), date(2026, 11, 16))
        self.assertEqual(why, ["Fri 13 Nov 15:00 New York time, after the 10:00 cut: next day Sat 14 Nov",
                               "Sat 14 Nov: weekend, no expiry", "Sun 15 Nov: weekend, no expiry", "expiry Mon 16 Nov"])

    def test_evening_release_next_day(self):
        ev = dict(self.EV, time="20:00", ts="2026-11-11T19:00+00:00")       # 20:00 Zurich = 14:00 New York
        self.assertEqual(expiry_for(ev, ("EUR", "USD"), {}, NYC), date(2026, 11, 12))


class TestFenicsCurrencyCalendar(unittest.TestCase):
    """Fenics lists events per currency: what a TOK pair's cut moves off the NYC expiry goes under its other currency."""
    CAL = [{"ccy": "USD", "desc": "NFP", "date": "2026-11-06", "ts": "2026-11-06T13:30+00:00"},     # Fri 08:30 NY
           {"ccy": "CNH", "desc": "CPI", "date": "2026-11-09", "ts": "2026-11-09T01:30+00:00"},     # Mon 09:30 Beijing
           {"ccy": "USD", "desc": "Claims", "date": "2026-11-12", "ts": "2026-11-12T13:30+00:00"},  # Thu 08:30 NY
           {"ccy": "USD", "desc": "Fed talk", "date": "2026-11-12", "ts": "2026-11-12T17:00+00:00"}]  # Thu 12:00 NY
    RULES = [{"ccy": "USD", "event": "NFP", "weight": 0.6}, {"ccy": "CNH", "event": "CPI", "weight": 0.3},
             {"ccy": "USD", "event": "Claims", "weight": 0.2}, {"ccy": "USD", "event": "Fed talk", "weight": 0.1}]

    def items(self, pair, cut, name, events=None):
        from volmarker.weights_export import fenics_items
        w = with_defaults(pair, {"events": events or []}, self.CAL, self.RULES, {}, cut)
        w["cut"] = name
        return [(e["date"].isoformat(), e["ccy"], e["name"], round(e["value"], 6))
                for e in fenics_items(pair, w, {}, date(2026, 11, 2))]

    def test_tok_pair(self):
        self.assertEqual(self.items("USDCNH", TOK, "TOK"), [
            # NFP: Monday's TOK expiry, listed under CNH with the CNH CPI of that day
            ("2026-11-09", "CNH", "CPI + USD NFP (TOK)", 0.9),
            # Thursday's claims weigh on Friday under TOK (moved: CNH); the 12:00 talk is on Friday under NYC too
            ("2026-11-13", "USD", "Fed talk", 0.1),
            ("2026-11-13", "CNH", "USD Claims (TOK)", 0.2)])

    def test_switched_off(self):
        from volmarker.weights_export import fenics_items
        w = with_defaults("USDCNH", {}, self.CAL, self.RULES, {}, TOK)
        w.update(cut="TOK", cut_events_other_ccy=False)
        self.assertEqual([(e["date"].isoformat(), e["ccy"], e["name"]) for e in fenics_items("USDCNH", w, {}, date(2026, 11, 2))],
                         [("2026-11-09", "USD", "NFP"), ("2026-11-09", "CNH", "CPI"), ("2026-11-13", "USD", "Claims")])

    def test_nyc_pair_unchanged(self):
        self.assertEqual(self.items("EURUSD", NYC, "NYC"), [
            ("2026-11-06", "USD", "NFP", 0.6), ("2026-11-12", "USD", "Claims", 0.2), ("2026-11-13", "USD", "Fed talk", 0.1)])

    def test_override_split_by_default_shares(self):
        # the pair weighs Friday at 0.6 instead of 0.3: 2/3 of it was moved by the cut
        got = self.items("USDCNH", TOK, "TOK", [{"date": "2026-11-13", "ccy": "USD", "desc": "x", "weight": 0.6}])
        self.assertEqual(got[1:], [("2026-11-13", "USD", "Fed talk", 0.2), ("2026-11-13", "CNH", "USD Claims (TOK)", 0.4)])


class TestPublish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.svc = Service(self.store, "anna")
        self.svc.create_pair("EURUSD", "marked", None, True)
        s = new_state("EURUSD")
        for i, t in enumerate(TENORS):
            s["tenors"][t].update(atm=6.0 + 0.1 * i, spread=0.4, rr25=-0.5, bf25=0.2)
        self.svc.publish("EURUSD", s, 1)

    def tearDown(self):
        self.tmp.cleanup()

    def draft(self):
        rec = self.store.latest("EURUSD")
        return {k: copy.deepcopy(rec.get(k)) for k in STATE_KEYS}

    def test_incomplete_new_event_is_refused_marker_is_kept(self):
        d = self.draft()
        d["weights"] = {"events": [{"date": "", "ccy": "", "desc": "", "weight": None}]}
        with self.assertRaisesRegex(ValueError, "needs a date and a weight"):
            self.svc.publish("EURUSD", d, 2)
        self.svc.preview("EURUSD", d)                          # the preview just leaves it out
        d["weights"] = {"events": [{"date": "2026-11-12", "ccy": "USD", "deleted": True}]}
        rec = self.svc.publish("EURUSD", d, 2)
        self.assertEqual(rec["weights"]["events"], [{"date": "2026-11-12", "ccy": "USD", "deleted": True}])

    def test_desk_default_needs_a_weight(self):
        with self.assertRaisesRegex(ValueError, "type a weight"):
            self.store.save_event_defaults([{"ccy": "EUR", "event": "CPI", "weight": None}], None, "anna", "2026-11-04T10:00+00:00")


    def test_events_history_and_morning_prune(self):
        today = date.today()
        monday = today + timedelta(days=35 - today.weekday())            # expiries Monday to Thursday, weeks ahead
        iso = lambda n: (today + timedelta(days=n) if n < 0 else monday + timedelta(days=n - 30)).isoformat()
        self.store.save_event_defaults([{"ccy": "USD", "event": "CPI", "weight": 0.5},
                                        {"ccy": "EUR", "event": "IFO", "weight": 0.2},
                                        {"ccy": "EUR", "event": "ZEW", "weight": 0.3}], None, "anna", "2026-01-01T00:00+00:00")
        d = self.draft()
        d["weights"] = {"events": [
            {"date": iso(-3), "ccy": "EUR", "desc": "old one", "weight": 0.4},                       # past
            {"date": iso(30), "ccy": "USD", "desc": "USD CPI", "weight": 0.9},                       # override
            {"date": iso(31), "ccy": "EUR", "desc": "EUR IFO", "weight": 0.25, "deleted": True, "of": "default"},
            {"date": iso(32), "ccy": "EUR", "desc": "ECB speaker", "weight": 0.3}]}                  # own
        self.svc.publish("EURUSD", d, 2)
        ex = Exporter(self.store)
        ex.calendar = [{"ccy": "USD", "desc": "CPI", "date": iso(30)}, {"ccy": "EUR", "desc": "IFO", "date": iso(31)},
                       {"ccy": "EUR", "desc": "ZEW", "date": iso(33)}]
        rows = ex.event_rows(today)
        self.assertEqual([(r[2], r[3], r[6], r[7], r[8]) for r in rows],
                         [(iso(30), "USD", "0.5", "0.9", "override"), (iso(31), "EUR", "0.2", "0.25", "deleted"),
                          (iso(32), "EUR", "", "0.3", "own"), (iso(33), "EUR", "0.3", "0.3", "default")])
        self.assertEqual({r[9] for r in rows}, {3})
        ex.events_day(today)
        with gzip.open(Path(self.tmp.name) / "analysis" / "events" / f"{today:%Y-%m}" / f"{today}.csv.gz", "rt") as f:
            got = list(csv.reader(f))
        self.assertEqual((got[0], len(got)), (EVENT_COLS, 5))
        # the morning clean-up: only the past event goes, with a version of its own
        self.assertEqual(self.svc.prune_past_events(today), ["EURUSD"])
        rec = self.store.latest("EURUSD")
        self.assertEqual([e["date"] for e in rec["weights"]["events"]], [iso(30), iso(31), iso(32)])
        self.assertEqual((rec["source"], rec["comment"]), ("event-prune", "1 past event(s) removed"))
        self.assertEqual(self.svc.prune_past_events(today), [])
        # a trader who started before the clean-up publishes without bringing the old event back
        d["tenors"]["1M"]["atm"] = 6.5
        rec = self.svc.publish("EURUSD", d, 3)
        self.assertNotIn(iso(-3), [e["date"] for e in rec["weights"]["events"]])


if __name__ == "__main__":
    unittest.main()
