"""Is a heartbeat another PC writes still beating, independent of the clocks?

The feeds, the follower and the leader write a heartbeat (their time.time())
into a shared file. Comparing it with this PC's time.time() breaks when the PCs'
clocks differ by more than the limit (15 s for the feeds). Instead, a Liveness
remembers when it saw each heartbeat change, by this PC's own monotonic clock:

    it changed recently          -> beating
    unchanged for `stale` s      -> stopped
    no change seen yet           -> unknown: the clocks decide (wall_alive)

"Unknown" lasts only until the first change is seen (about a second for the
feeds), or `stale` seconds for a heartbeat that has stopped.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class Liveness:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._seen: dict[str, tuple[object, float, bool]] = {}   # key -> (value, since, seen to change)
        self._lock = threading.Lock()

    def beating(self, key: str, hb, stale: float, value=None) -> bool | None:
        """True / False, or None while it cannot tell yet. value: what to watch for
        change, if more than hb (e.g. the leader's instance and heartbeat)."""
        if not hb:
            return False
        value = hb if value is None else value
        now = self.clock()
        with self._lock:
            prev = self._seen.get(key)
            if prev is None or prev[0] != value:
                self._seen[key] = (value, now, prev is not None)
                return True if prev is not None else None
            if now - prev[1] >= stale:
                return False
            return True if prev[2] else None

    def alive(self, key: str, hb, stale: float, wall_now: float, value=None) -> bool:
        """beating(), with the clocks deciding while it cannot tell yet."""
        b = self.beating(key, hb, stale, value)
        return b if b is not None else wall_alive(hb, stale, wall_now)

    def age(self, key: str, hb, wall_now: float) -> float:
        """Seconds since the heartbeat last moved (by this PC's clock once seen to move)."""
        with self._lock:
            prev = self._seen.get(key)
        if prev is not None and prev[0] == hb and prev[2]:
            return self.clock() - prev[1]
        return wall_now - (hb or 0)

    def confirm(self, key: str, read: Callable[[], object], stale: float,
                wall: Callable[[], float] = time.time, wait: float = 3.0) -> bool:
        """A one-off check (before starting a feed): watch the heartbeat for up to
        `wait` seconds; the feeds write it every second."""
        hb = read()
        b = self.beating(key, hb, stale)
        end = self.clock() + wait
        while b is None and self.clock() < end:
            time.sleep(0.5)
            hb = read()
            b = self.beating(key, hb, stale)
        return b if b is not None else wall_alive(hb, stale, wall())


def wall_alive(hb, stale: float, wall_now: float) -> bool:
    return bool(hb) and wall_now - hb < stale
