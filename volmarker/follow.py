"""Market mode: publish FMD mids for pairs set to "Follow FMD".

Runs inside the feed process, which is already the single writer of market
data for the folder, so exactly one follower publishes. Each round, for each
following pair:

  * takes the fresh FMD mid (brand per fmd_brand) for ATM, 25D/10D RR and BF;
    tenors without a fresh quote keep their last value
  * publishes when any mark has moved at least min_change, at most once per
    min_publish_interval_s - through the normal publish, so the Fenics and
    Front Arena files, derived crosses, history and journal all follow
  * holds a move bigger than max_jump until it has lasted jump_confirm_s
  * does nothing while the feed is down (Fenics keeps the last levels; the
    apps raise an alert)

Spread, skew and event weights stay with the traders.

Writes market/_follow.json (per-pair state + heartbeat) for the apps.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time

from .alerts import iso
from .conventions import TENORS
from .service import STATE_KEYS, Service
from .store import ConflictError, LockTimeout, atomic_write
from .surface import ALERT_MARKS, resolve

log = logging.getLogger("volmarker.follow")


class Follower:
    def __init__(self, svc: Service):
        self.svc = svc
        self.pending: dict[tuple, float] = {}     # (pair, tenor, mark) -> first seen of a big move
        self.last_pub: dict[str, float] = {}
        self.status: dict[str, dict] = {}
        self.path = svc.store.root / "market" / "_follow.json"

    def step(self, now: float | None = None) -> list[str]:
        """One round; returns the pairs published."""
        now = time.time() if now is None else now
        svc = self.svc
        svc.poll_journal()
        svc.poll_market(notify=False)
        cfg = svc.store.config()
        with svc._lock:
            pairs = sorted(p for p, r in svc.cache.items() if r.get("follow_market"))
        published, status = [], {}
        for pair in pairs:
            try:
                st = self._pair(pair, cfg, now)
            except Exception as e:           # one bad pair must not stop the others
                log.exception("follow %s failed", pair)
                st = {"state": "error", "error": str(e)}
            prev = self.status.get(pair, {})
            st.setdefault("last_publish", prev.get("last_publish"))
            if st.pop("published", False):
                published.append(pair)
            status[pair] = st
        self.pending = {k: v for k, v in self.pending.items() if k[0] in status}
        self.status = status
        atomic_write(self.path, json.dumps({"heartbeat": now, "instance": svc.instance,
                                            "pairs": status}))
        return published

    def _pair(self, pair: str, cfg: dict, now: float) -> dict:
        svc, fc = self.svc, cfg["follow"]
        rec = svc.get(pair)
        st = {"last_check": iso(now), "version": rec["version"]}
        if not svc.feed_alive():
            return dict(st, state="down")
        view = svc.fresh_view(pair, rec, cfg)
        res = resolve(rec)
        take, held, missing, moved = {}, [], [], []
        for t in TENORS:
            for m in ALERT_MARKS:
                key = (pair, t, m)
                q = view.get(t, {}).get(m)
                if not q or q.get("mid") is None or q.get("stale") or q.get("alt_atm"):
                    missing.append(f"{t} {m}")
                    self.pending.pop(key, None)
                    continue
                mid = round(q["mid"], 4)
                cur = res[t]["mid"][m]
                d = None if cur is None else mid - cur
                if d is not None and abs(d) > fc["max_jump"].get(m, float("inf")):
                    first = self.pending.setdefault(key, now)
                    if now - first < fc["jump_confirm_s"]:
                        held.append({"tenor": t, "mark": m, "from": cur, "to": mid, "since": iso(first),
                                     "accept_in": round(fc["jump_confirm_s"] - (now - first))})
                        continue
                else:
                    self.pending.pop(key, None)
                take[(t, m)] = mid
                if d is None or abs(d) >= fc["min_change"] - 1e-9:
                    moved.append((abs(d) if d is not None else float("inf"), t, m, d))
        st.update(state="held" if held else "ok", held=held, missing=len(missing),
                  missing_list=missing[:30])
        if not moved:
            return st
        if now - self.last_pub.get(pair, 0) < fc["min_publish_interval_s"]:
            return dict(st, waiting=True)
        state = {k: copy.deepcopy(rec.get(k)) for k in STATE_KEYS}
        for (t, m), v in take.items():          # ALERT_MARKS order: rr25 before rr10, bf25 before bf10
            row = state["tenors"][t]
            row[m] = v
            if m in ("rr10", "bf10"):
                w = m[:2]
                if abs(row[f"{w}25"]) > 1e-9:
                    row[f"{w}10f"] = v / row[f"{w}25"]
        _, t, m, d = max(moved, key=lambda x: x[0])
        comment = f"FMD: {t} {m.upper()} " + (f"{d:+.2f}" if d is not None else "new")
        if len(moved) > 1:
            comment += f" (+{len(moved) - 1} more)"
        try:
            new = svc.publish(pair, state, rec["version"], comment, source="market")
        except (ConflictError, LockTimeout) as e:      # a trader just published - next round
            log.info("follow %s deferred: %s", pair, e)
            return st
        self.last_pub[pair] = now
        return dict(st, version=new["version"], last_publish=iso(now), published=True)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.wait(self.svc.store.config()["follow"]["interval_s"]):
            try:
                pubs = self.step()
                if pubs:
                    log.info("published from FMD: %s", ", ".join(pubs))
            except Exception:
                log.exception("follower round failed")
