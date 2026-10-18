"""Business logic: publish, propagate to derived pairs, export, housekeeping."""

from __future__ import annotations

import copy
import json
import logging
import queue
import threading
import time
import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from . import exports
from .alerts import AlertEngine, deviation, iso, level
from .conventions import TENORS, normalise_pair
from .calendar import daily_dates, implied_daily_weights, interpolate_atm, tenor_dates
from .conventions import split_pair
from .fmd.book import daily_n
from .market import compare, market_view, parse_fix_time
from .store import LockTimeout, Store, atomic_write
from .surface import (ALERT_MARKS, FOLLOW_FIELDS, apply_derivation, check_surface, clean_state,
                      new_state, now_iso, resolve, skew_active)

log = logging.getLogger("volmarker")

STATE_KEYS = ["pair", "mode", "bases", "key", "skew_expiry", "weights", "atm_type",
              "lock_rr10f", "lock_bf10f", "follow_market", "alert_tol", "alerts_off", "tenors"]
LEADER_STALE = 45.0
FEED_STALE = 15.0
FOLLOW_STALE = 30.0


class Service:
    def __init__(self, store: Store, user: str):
        self.store = store
        self.user = user
        self.instance = f"{user}-{uuid.uuid4().hex[:6]}"
        self._lock = threading.RLock()
        self.cache: dict[str, dict] = {}
        self._listeners: list[queue.Queue] = []
        self._journal_offset = store.journal_size()
        for p in store.list_pairs():
            self.cache[p] = store.latest(p)
        self._stop = threading.Event()
        self.market: dict[str, list[dict]] = {}
        self.market_status: dict = {}
        self.follow_status: dict = {}
        self._market_mtimes: dict[str, float] = {}
        self.alerts = AlertEngine()
        self._leader = False
        self.poll_market(notify=False)

    # ---- read side ----------------------------------------------------
    def get(self, pair: str) -> dict | None:
        with self._lock:
            return copy.deepcopy(self.cache.get(pair))

    def summaries(self) -> list[dict]:
        cfg = self.store.config()
        with self._lock:
            recs = list(self.cache.values())
        out = []
        now = datetime.now(timezone.utc)
        brand = cfg.get("fmd_brand", "auto")
        active = self.alerts.list()
        for r in recs:
            quotes = self.market.get(r["pair"])
            pair_has_market = bool(quotes) and self.feed_alive()
            outside = (compare(resolve(r), market_view(quotes, r.get("atm_type") or "DNS", brand))
                       if pair_has_market else None)
            age_min = (now - datetime.fromisoformat(r["marked_ts"])).total_seconds() / 60
            limit = cfg["stale_minutes_key"] if r.get("key") else cfg["stale_minutes_other"]
            follow = self.follow_state(r["pair"]) if r.get("follow_market") else None
            alerts = [a for a in active if a["pair"] == r["pair"]]
            out.append({
                "pair": r["pair"], "mode": r["mode"], "bases": r.get("bases"),
                "key": r.get("key", False), "version": r["version"],
                "ts": r["ts"], "user": r["user"], "source": r.get("source"),
                "marked_ts": r["marked_ts"], "age_minutes": round(age_min, 1),
                "stale": follow == "down" if follow else r["mode"] == "marked" and age_min > limit,
                "follow": follow,
                "alerts": len(alerts), "alerts_new": sum(not a["acked"] for a in alerts),
                "skew": skew_active(r),
                "warnings": len(r.get("warnings", [])),
                "atm_1m": r["tenors"]["1M"]["atm"],
                "mkt": pair_has_market,
                "mkt_out": len(outside) if outside is not None else None,
            })
        return sorted(out, key=lambda x: x["pair"])

    def dependents(self, pair: str) -> list[str]:
        with self._lock:
            return sorted(p for p, r in self.cache.items()
                          if r["mode"] == "derived" and pair in (r.get("bases") or []))

    def view(self, pair: str) -> dict | None:
        """Everything the UI needs for one pair."""
        rec = self.get(pair)
        if rec is None:
            return None
        bases = {b: self._resolved_mid_atm(b) for b in (rec.get("bases") or [])}
        return {"record": rec, "resolved": resolve(rec), "bases": bases,
                "dependents": self.dependents(pair)}

    # ---- market data (written by the FMD feed process) -----------------
    def feed_alive(self) -> bool:
        return bool(self.market_status.get("connected")) and \
            time.time() - self.market_status.get("heartbeat", 0) < FEED_STALE

    def feed_status(self) -> dict:
        st = dict(self.market_status)
        if not st:
            return {"state": "off"}
        age = time.time() - st.get("heartbeat", 0)
        st["state"] = "live" if self.feed_alive() else ("down" if age < FEED_STALE else "off")
        st["heartbeat_age"] = round(age, 1)
        return st

    def follow_state(self, pair: str) -> str:
        """Market-mode health of one pair: ok, held (big move waiting), down."""
        fs = self.follow_status
        if not self.feed_alive() or time.time() - fs.get("heartbeat", 0) > FOLLOW_STALE:
            return "down"
        st = (fs.get("pairs") or {}).get(pair)
        if st is None:
            return "starting"
        return {"ok": "ok", "held": "held"}.get(st.get("state"), "down")

    def fresh_view(self, pair: str, rec: dict, cfg: dict, brand: str | None = None) -> dict:
        """Market view with the age of every quote and a stale flag."""
        brand = brand or cfg.get("fmd_brand", "auto")
        view = market_view(self.market.get(pair, []), rec.get("atm_type") or "DNS", brand)
        stale_min = cfg.get("market_stale_minutes", 30)
        now = datetime.now(timezone.utc)
        for marks in view.values():
            for q in marks.values():
                ts = parse_fix_time(q.get("ts"))
                q["age_min"] = round((now - ts).total_seconds() / 60, 1) if ts else None
                q["stale"] = q["age_min"] is not None and q["age_min"] > stale_min
        return view

    def market_for(self, pair: str, brand: str | None = None, draft: dict | None = None) -> dict:
        rec = draft or self.get(pair) or {}
        quotes = self.market.get(pair, [])
        cfg = self.store.config()
        brand = brand or cfg.get("fmd_brand", "auto")
        view = self.fresh_view(pair, rec, cfg, brand)
        follow = None
        if rec.get("follow_market"):
            follow = dict((self.follow_status.get("pairs") or {}).get(pair) or {},
                          health=self.follow_state(pair))
        return {"feed": self.feed_status(), "brand": brand, "follow": follow,
                "brands": sorted({q.get("brand") for q in quotes if q.get("brand")}),
                "atm_type": rec.get("atm_type") or "DNS", "view": view}

    def dailies(self, pair: str, draft: dict | None = None, brand: str | None = None) -> dict:
        """Market dailies (1D..14D) vs the draft's interpolation, plus the event
        weights that would make the weights reproduce the market dailies."""
        rec = self._prepare(pair, draft) if draft else self.get(pair)
        m = self.market_for(pair, brand, draft=rec)
        holidays, cal_events = self.store.load_calendar()
        basis = self.store.config().get("fmd_daily_basis", "calendar")
        daily = {daily_n(t): marks for t, marks in m["view"].items() if daily_n(t)}
        today = date.today()
        if not daily:
            return {"rows": [], "events": [], "warnings": [], "feed": m["feed"], "basis": basis}
        dd = daily_dates(pair, today, sorted(daily), holidays, basis)
        vols = {dd[n]: q["atm"]["mid"] for n, q in daily.items() if q.get("atm") and q["atm"].get("mid")}
        pillar_dates = tenor_dates(pair, today, holidays)
        res = implied_daily_weights(pair, today, vols, rec.get("weights"), holidays,
                                    pillars=[pillar_dates[t] for t in TENORS])
        atm = {t: rec["tenors"][t]["atm"] for t in TENORS}
        model = interpolate_atm(pair, atm, today, sorted(vols), rec.get("weights"), holidays)
        ccys = split_pair(pair)
        cur_events: dict[str, list] = {}
        for ev in (rec.get("weights") or {}).get("events", []):
            cur_events.setdefault(ev["date"], []).append(ev)
        cal = {}
        for ev in cal_events:
            if ev["ccy"] in ccys:
                cal.setdefault(ev["date"], []).append(ev["desc"])
        n_by_date: dict = {}
        for n, d in sorted(dd.items()):
            n_by_date.setdefault(d, []).append(n)       # e.g. 6D-8D over a weekend share Monday's expiry
        rows, prev_d, prev_tv = [], today, 0.0
        for r in res["rows"]:
            d, iso = r["date"], r["date"].isoformat()
            mv = model.get(d)
            tv = (mv or 0) ** 2 * (d - today).days / 365
            ns = n_by_date[d]
            q = daily[ns[-1]]
            rows.append({
                "n": str(ns[0]) if len(ns) == 1 else f"{ns[0]}–{ns[-1]}", "date": iso, "weekday": d.strftime("%a"),
                "mkt_atm": r["vol"], "mkt_fwd": r["fwd_vol"],
                "model_atm": mv,
                "model_fwd": (max(tv - prev_tv, 0) * 365 / (d - prev_d).days) ** 0.5 if mv else None,
                "rr25": (q.get("rr25") or {}).get("mid"), "bf25": (q.get("bf25") or {}).get("mid"),
                "implied_event": r["implied_event"], "implied_weight": r["implied_weight"],
                "current_event": sum(float(e["weight"]) for e in cur_events.get(iso, [])),
                "labels": list(dict.fromkeys([e.get("desc", "") for e in cur_events.get(iso, [])] + cal.get(iso, []))),
            })
            prev_d, prev_tv = d, tv
        events = []
        for ev in res["events"]:
            names = [x for x in [e.get("desc") for e in cur_events.get(ev["date"], [])] + cal.get(ev["date"], []) if x]
            events.append({"date": ev["date"], "desc": names[0] if names else "FMD daily", "weight": ev["weight"]})
        return {"rows": rows, "events": events, "warnings": res["warnings"], "basis": basis,
                "window_end": max(vols).isoformat() if vols else None, "feed": m["feed"]}

    def poll_market(self, notify: bool = True) -> None:
        d = self.store.root / "market"
        if not d.is_dir():
            return
        changed = []
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            if not e.name.endswith(".json"):
                continue
            try:
                mt = e.stat().st_mtime
            except OSError:
                continue
            if self._market_mtimes.get(e.name) == mt:
                continue
            try:
                data = json.loads(Path(e.path).read_text())
            except (OSError, ValueError):
                continue          # being replaced - next poll
            self._market_mtimes[e.name] = mt
            if e.name == "_status.json":
                self.market_status = data
            elif e.name == "_follow.json":
                self.follow_status = data
                changed.append("_follow")
            else:
                with self._lock:
                    self.market[data["pair"]] = data["quotes"]
                changed.append(data["pair"])
        follow = "_follow" in changed
        changed = [p for p in changed if p != "_follow"]
        if notify and (changed or follow):
            self._notify({"type": "market", "pairs": sorted(changed), "follow": follow})

    def _market_snapshot(self, rec: dict) -> dict | None:
        """Broker mids at publish time, for the audit trail."""
        if not self.market.get(rec["pair"]) or not self.feed_alive():
            return None
        m = self.market_for(rec["pair"], draft=rec)
        snap = {t: {k: q.get("mid") for k, q in marks.items()} for t, marks in m["view"].items()}
        return {"brand": m["brand"], "mids": snap} if snap else None

    def _resolved_mid_atm(self, pair: str) -> dict | None:
        r = self.get(pair)
        if r is None:
            return None
        return {"version": r["version"], "ts": r["ts"],
                "atm": {t: r["tenors"][t]["atm"] for t in TENORS}}

    def preview(self, pair: str, draft: dict) -> dict:
        state = self._prepare(pair, draft)
        state, warnings = self._derive(state)
        return {"state": state, "resolved": resolve(state),
                "warnings": warnings + check_surface(state)}

    # ---- write side ---------------------------------------------------
    def create_pair(self, pair: str, mode: str, bases: list[str] | None, key: bool) -> dict:
        pair = normalise_pair(pair)
        if pair in self.cache:
            raise ValueError(f"{pair} already exists")
        bases = [normalise_pair(b) for b in bases] if bases else None
        if mode == "derived":
            for b in bases:
                if b not in self.cache:
                    raise ValueError(f"Base pair {b} does not exist")
        state = new_state(pair, mode, bases)
        state["key"] = bool(key)
        if mode == "derived":
            # start from the base-implied ATM at zero correlation
            for t in TENORS:
                state["tenors"][t]["corr"] = 0.0
        return self.publish(pair, state, expected_version=0, comment="created")

    def publish(self, pair: str, draft: dict, expected_version: int | None,
                comment: str = "", source: str = "manual") -> dict:
        pair = normalise_pair(pair)
        state = self._prepare(pair, draft)
        self._check_cycle(state)
        with self.store.lock(pair, self.user):
            if source == "manual" and expected_version is not None:
                state, expected_version = self._rebase_over_market(state, expected_version)
            state, warnings = self._derive(state)
            rec = self._append(state, warnings, expected_version, comment, source,
                               marked=source == "manual")
        self._propagate(pair, set())
        return rec

    def _rebase_over_market(self, state: dict, expected: int) -> tuple[dict, int]:
        """Automatic versions since the trader started editing are not a
        conflict: after market versions the trader keeps their edits and the
        market-owned fields come from the latest version; after propagation
        the derivation is simply redone on the latest bases."""
        latest = self.store.latest(state["pair"])
        if latest is None or latest["version"] <= expected:
            return state, expected
        newer = self.store.history(state["pair"], latest["version"] - expected)
        if any(r.get("source") not in ("market", "propagation") for r in newer):
            return state, expected                    # a real conflict - reported by append
        if state.get("follow_market"):
            for t in TENORS:
                for f in FOLLOW_FIELDS:
                    state["tenors"][t][f] = latest["tenors"][t][f]
        return state, latest["version"]

    def _prepare(self, pair: str, draft: dict) -> dict:
        state = {k: draft.get(k) for k in STATE_KEYS}
        state["pair"] = pair
        state = clean_state(state)
        state["key"] = bool(draft.get("key"))
        state["weights"] = draft.get("weights") or None
        return state

    # ---- alerts -------------------------------------------------------
    def evaluate_alerts(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cfg = self.store.config()
        acfg = cfg["alerts"]
        feed_ok = self.feed_alive()
        with self._lock:
            recs = list(self.cache.values())
        cands: dict[tuple, dict] = {}
        for rec in recs:
            pair = rec["pair"]
            if rec.get("follow_market"):
                self._follow_candidates(rec, cands)
                continue
            if not acfg.get("enabled", True) or rec.get("alerts_off") or not feed_ok \
                    or not self.market.get(pair):
                continue
            view = self.fresh_view(pair, rec, cfg)
            res = resolve(rec)
            own = rec.get("alert_tol") or {}
            for t in TENORS:
                for m in ALERT_MARKS:
                    q = view.get(t, {}).get(m)
                    v = res[t]["mid"][m]
                    if not q or q.get("stale") or q.get("alt_atm") or v is None:
                        continue
                    dev = deviation(v, q, acfg.get("basis", "mid"))
                    if dev is None:
                        continue
                    tol = (own.get(m) or acfg["tolerance"][m]) * acfg.get("tenor_scale", {}).get(t, 1.0)
                    cands[(pair, t, m)] = {
                        "level": level(dev, tol, acfg.get("clear_ratio", 0.8)), "kind": "market",
                        "pair": pair, "tenor": t, "mark": m, "value": v, "mkt": q.get("mid"),
                        "bid": q.get("bid"), "offer": q.get("offer"), "brand": q.get("brand"),
                        "dev": round(dev, 4), "tol": round(tol, 4), "source": rec.get("source"),
                        "msg": f"{pair} {t} {m.upper()}: ours {v:.2f}, FMD {q.get('mid'):.2f} "
                               f"({dev:+.2f}, tol {tol:.2f})"}
        new, cleared = self.alerts.update(cands, now, acfg.get("confirm_s", 20))
        if new or cleared:
            self._log_alerts(new, cleared, now)
            self._notify({"type": "alerts", "new": new, "cleared": [a["key"] for a in cleared],
                          "count": len(self.alerts.active)})

    def _follow_candidates(self, rec: dict, cands: dict) -> None:
        """A pair in market mode: alert when it is not being updated, or a big
        move is being held back."""
        pair = rec["pair"]
        health = self.follow_state(pair)
        st = (self.follow_status.get("pairs") or {}).get(pair) or {}
        why = ("the FMD feed is down" if not self.feed_alive() else
               f"the follower failed ({st.get('error')})" if st.get("state") == "error" else
               "the follower is not running")
        cands[(pair, "", "follow")] = {
            "level": "breach" if health == "down" else "clear", "kind": "follow", "confirm": 10,
            "pair": pair, "tenor": "", "mark": "follow",
            "msg": f"{pair} follows FMD but {why}: Fenics keeps v{rec['version']} "
                   f"from {rec['ts'][11:16]} UTC"}
        if health == "down":
            return
        for h in st.get("held") or []:
            d = h["to"] - (h["from"] or 0)
            cands[(pair, h["tenor"], h["mark"] + "-jump")] = {
                "level": "breach", "kind": "jump", "confirm": 0, "pair": pair, "tenor": h["tenor"],
                "mark": h["mark"], "value": h["from"], "mkt": h["to"], "dev": round(-d, 4),
                "msg": f"{pair} {h['tenor']} {h['mark'].upper()}: FMD moved {d:+.2f}, held back; "
                       f"taken in {h['accept_in']}s unless it reverts"}

    def ack_alerts(self, keys: list[str] | None = None, pair: str | None = None) -> int:
        n = self.alerts.ack(keys, pair)
        if n:
            self._notify({"type": "alerts", "new": [], "cleared": [], "count": len(self.alerts.active)})
        return n

    def _log_alerts(self, new: list, cleared: list, now: float) -> None:
        """One app (the leader) keeps the desk's alert log on the shared drive."""
        if not self._leader:
            return
        path = self.store.root / "alerts" / f"{iso(now)[:10]}.jsonl"
        try:
            path.parent.mkdir(exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                for ev, items in (("raised", new), ("cleared", cleared)):
                    for a in items:
                        f.write(json.dumps({"ts": iso(now), "event": ev, **{
                            k: a.get(k) for k in ("key", "kind", "value", "mkt", "dev", "tol", "msg")}}) + "\n")
        except OSError as e:
            log.warning("alert log: %s", e)

    def _derive(self, state: dict) -> tuple[dict, list[str]]:
        if state["mode"] != "derived":
            return state, []
        bases = {b: self.store.latest(b) for b in state["bases"]}
        return apply_derivation(state, bases)

    def _append(self, state: dict, warnings: list[str], expected_version: int | None,
                comment: str, source: str, marked: bool) -> dict:
        prev = self.store.latest(state["pair"])
        record = {k: state.get(k) for k in STATE_KEYS}
        record.update({
            "ts": now_iso(), "user": self.user, "comment": comment, "source": source,
            "warnings": warnings + check_surface(state),
        })
        # marked_ts = last time a person touched it (staleness); propagation keeps it
        record["marked_ts"] = record["ts"] if marked or prev is None else prev["marked_ts"]
        if marked:
            snap = self._market_snapshot(record)
            if snap:
                record["market_at_publish"] = snap
        if state["mode"] == "derived":
            record["base_versions"] = {b: (self.store.latest(b) or {}).get("version")
                                       for b in state["bases"]}
        rec = self.store.append(record, expected_version)
        self._export(rec)
        with self._lock:
            self.cache[rec["pair"]] = rec
        self.store.journal({"ts": rec["ts"], "pair": rec["pair"], "version": rec["version"],
                            "user": rec["user"], "source": source,
                            "instance": self.instance})
        self._notify({"type": "update", "pair": rec["pair"], "version": rec["version"],
                      "user": rec["user"], "source": source})
        return rec

    def _export(self, rec: dict) -> None:
        exports.write_exports(rec["pair"], resolve(rec),
                              self.store.path_from_config("fenics_dir"),
                              self.store.path_from_config("frontarena_dir"))

    def _propagate(self, pair: str, seen: set) -> None:
        """Recompute every derived pair that uses `pair`, recursively."""
        for dep in self.dependents(pair):
            if dep in seen:
                continue
            seen.add(dep)
            with self.store.lock(dep, self.user):
                latest = self.store.latest(dep)
                state = {k: latest.get(k) for k in STATE_KEYS}
                new, warnings = apply_derivation(state, {b: self.store.latest(b)
                                                         for b in state["bases"]})
                if new["tenors"] == latest["tenors"]:
                    continue
                self._append(new, warnings, latest["version"],
                             f"{pair} moved", "propagation", marked=False)
            self._propagate(dep, seen)

    def _check_cycle(self, state: dict) -> None:
        if state["mode"] != "derived":
            return
        with self._lock:
            graph = {p: (r.get("bases") or []) for p, r in self.cache.items()
                     if r["mode"] == "derived"}
        graph[state["pair"]] = state["bases"]
        stack, seen = list(state["bases"]), set()
        while stack:
            p = stack.pop()
            if p == state["pair"]:
                raise ValueError(f"{state['pair']} would depend on itself")
            if p not in seen:
                seen.add(p)
                stack.extend(graph.get(p, []))

    # ---- change feed --------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def _notify(self, event: dict) -> None:
        with self._lock:
            for q in self._listeners:
                q.put(event)

    def poll_journal(self) -> None:
        """Pick up versions published by other traders' apps."""
        try:
            if self.store.journal_size() <= self._journal_offset:
                return
            entries, self._journal_offset = self.store.read_journal_from(self._journal_offset)
        except OSError as e:
            log.warning("journal read failed: %s", e)
            return
        for e in entries:
            if e.get("instance") == self.instance:
                continue
            rec = self.store.latest(e["pair"])
            if rec is None:
                continue
            with self._lock:
                cur = self.cache.get(e["pair"])
                if cur is None or cur["version"] < rec["version"]:
                    self.cache[e["pair"]] = rec
            self._notify({"type": "update", "pair": e["pair"], "version": e["version"],
                          "user": e.get("user"), "source": e.get("source")})

    # ---- housekeeping (one app at a time) -----------------------------
    def is_leader(self) -> bool:
        path = self.store.root / "locks" / "leader.json"
        try:
            with self.store.lock("leader-election", self.user):
                try:
                    cur = json.loads(path.read_text())
                except (OSError, ValueError):
                    cur = None
                if cur is None or cur["instance"] == self.instance or \
                        time.time() - cur["heartbeat"] > LEADER_STALE:
                    atomic_write(path, json.dumps({"instance": self.instance,
                                                   "heartbeat": time.time()}))
                    return True
                return False
        except LockTimeout:
            return False

    def housekeeping(self) -> None:
        self._leader = self.is_leader()
        if not self._leader:
            return
        now = now_iso()
        for pair, rec in list(self.cache.items()):
            exp = rec.get("skew_expiry")
            if exp and now >= exp:
                state = {k: copy.deepcopy(rec.get(k)) for k in STATE_KEYS}
                for row in state["tenors"].values():
                    row["skew_atm"] = row["skew_rr"] = row["skew_bf"] = 0.0
                state["skew_expiry"] = None
                try:
                    self.publish(pair, state, rec["version"], "skew expired", "skew-expiry")
                except Exception as e:  # conflict etc. - retried next round
                    log.info("skew expiry for %s deferred: %s", pair, e)

    def start_background(self) -> None:
        def journal_loop():
            n = 0
            while not self._stop.wait(0.5):
                self.poll_journal()
                n += 1
                if n % 2 == 0:
                    self.poll_market()
                    try:
                        self.evaluate_alerts()
                    except Exception:
                        log.exception("alert evaluation failed")

        def housekeeping_loop():
            while not self._stop.wait(15):
                try:
                    self.housekeeping()
                except Exception:
                    log.exception("housekeeping failed")

        threading.Thread(target=journal_loop, daemon=True).start()
        threading.Thread(target=housekeeping_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
