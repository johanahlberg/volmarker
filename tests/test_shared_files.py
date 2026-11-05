"""The shared data folder from several PCs: clock skew, half-written lines, damaged lines."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from volmarker import store as store_mod
from volmarker.service import Service
from volmarker.store import LockTimeout, Store
from tests.test_engine import flat_state


class TestLocks(unittest.TestCase):
    def test_held_lock_with_old_file_time_is_not_taken(self):
        """The share's clock 60 s behind this PC: a lock held right now must still hold."""
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(tmp)
            p = st.root / "locks" / "EURUSD.lock"
            p.parent.mkdir(exist_ok=True)
            p.write_text("other@pc2 1 abc")
            old = time.time() - 60
            os.utime(p, (old, old))
            with mock.patch.object(store_mod, "LOCK_TIMEOUT", 0.3):
                with self.assertRaises(LockTimeout):
                    with st.lock("EURUSD"):
                        pass
            self.assertEqual(p.read_text(), "other@pc2 1 abc")

    def test_abandoned_lock_is_taken_after_being_seen_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(tmp)
            p = st.root / "locks" / "EURUSD.lock"
            p.parent.mkdir(exist_ok=True)
            p.write_text("crashed@pc2 1 abc")
            with mock.patch.object(store_mod, "LOCK_STALE", 0.2), mock.patch.object(store_mod, "LOCK_TIMEOUT", 2):
                with st.lock("EURUSD"):
                    self.assertNotIn("crashed", p.read_text())
            self.assertFalse(p.exists())

    def test_very_old_lock_is_taken_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(tmp)
            p = st.root / "locks" / "EURUSD.lock"
            p.parent.mkdir(exist_ok=True)
            p.write_text("crashed@pc2 1 abc")
            old = time.time() - 3600
            os.utime(p, (old, old))
            with mock.patch.object(store_mod, "LOCK_TIMEOUT", 0.1):
                with st.lock("EURUSD"):
                    pass

    def test_release_leaves_a_lock_someone_else_took_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(tmp)
            p = st.root / "locks" / "EURUSD.lock"
            with st.lock("EURUSD"):
                p.write_text("next@pc2 2 def")      # ours was judged stale and taken over meanwhile
            self.assertEqual(p.read_text(), "next@pc2 2 def")


def make_pair(tmp):
    st = Store(tmp)
    svc = Service(st, "trader")
    svc.create_pair("EURUSD", "marked", None, True)
    svc.publish("EURUSD", flat_state("EURUSD", 7.0), 1)
    return st, svc


class TestPartialLines(unittest.TestCase):
    def test_half_appended_version_is_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, svc = make_pair(tmp)
            v = st.latest("EURUSD")["version"]
            with open(st._history("EURUSD"), "ab") as f:
                f.write(b'{"pair":"EURUSD","version":99,"ten')   # another PC's append, halfway
            self.assertEqual(st.latest("EURUSD")["version"], v)

    def test_damaged_journal_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, svc = make_pair(tmp)
            other = Service(st, "other")
            with open(st.journal_path, "ab") as f:
                f.write(b'{"pair":"EURUSD","vers\x00\x00\n')      # garbage on the share
            svc.publish("EURUSD", flat_state("EURUSD", 7.2), st.latest("EURUSD")["version"])
            other.poll_journal()
            self.assertEqual(other.get("EURUSD")["version"], st.latest("EURUSD")["version"])

    def test_background_loop_survives_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, svc = make_pair(tmp)
            calls = []

            def boom():
                calls.append(1)
                raise RuntimeError("boom")
            with mock.patch.object(svc, "poll_journal", boom), mock.patch.object(svc, "start_propagation"):
                svc.start_background()
                time.sleep(1.3)
                svc._stop.set()
            self.assertGreaterEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()


class FakeMono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestLiveness(unittest.TestCase):
    def test_judged_by_change_not_by_clocks(self):
        from volmarker.liveness import Liveness
        mono = FakeMono()
        lv = Liveness(mono)
        wall = 5000.0                                         # this PC's clock: 60 s ahead of the writer's
        self.assertFalse(lv.alive("fmd", wall - 60, 15, wall))   # first sight: only the clocks to go by
        mono.t += 1
        self.assertTrue(lv.alive("fmd", wall - 59, 15, wall + 1))  # it moved: beating, whatever the clocks say
        mono.t += 10
        self.assertTrue(lv.alive("fmd", wall - 59, 15, wall + 11))
        mono.t += 6                                           # unchanged 16 s by our own clock
        self.assertFalse(lv.alive("fmd", wall - 59, 15, wall + 17))

    def test_stopped_heartbeat_with_future_time(self):
        """This PC's clock behind: a stopped feed's heartbeat looks fresh by the clocks."""
        from volmarker.liveness import Liveness
        mono = FakeMono()
        lv = Liveness(mono)
        self.assertTrue(lv.alive("bbg", 6000.0, 15, 5000.0))  # first sight: the clocks say fresh
        mono.t += 16
        self.assertFalse(lv.alive("bbg", 6000.0, 15, 5016.0))  # but it never moved

    def test_service_feed_alive_with_skewed_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, svc = make_pair(tmp)
            skew = -60                                        # the feed PC's clock 60 s behind
            for i in range(3):
                svc.market_status = {"connected": True, "heartbeat": time.time() + skew + i}
                alive = svc.feed_alive()
            self.assertTrue(alive)
            self.assertEqual(svc.feed_status()["state"], "live")

    def test_leader_not_taken_over_because_of_skew(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, svc = make_pair(tmp)
            path = st.root / "locks" / "leader.json"
            path.parent.mkdir(exist_ok=True)
            other = Service(st, "other")
            for i in range(3):                                # the leader's clock 120 s behind, still beating
                path.write_text(json.dumps({"instance": "leader-pc", "heartbeat": time.time() - 120 + i}))
                self.assertFalse(other.is_leader())
            self.assertEqual(json.loads(path.read_text())["instance"], "leader-pc")
            path.write_text(json.dumps({"instance": "gone-pc", "heartbeat": time.time() - 3600}))
            self.assertTrue(Service(st, "third").is_leader())   # yesterday's leader: taken over at once


class TestClockOffset(unittest.TestCase):
    def test_offset_and_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(tmp)
            self.assertLess(abs(st.clock_offset()), 2)
            real = time.time
            with mock.patch("volmarker.store.time.time", lambda: real() + 60), \
                    self.assertLogs("volmarker.store", "WARNING") as logs:
                self.assertAlmostEqual(st.clock_offset(max_age=0), -60, delta=2)
            self.assertIn("ahead of the file server", logs.output[0])
