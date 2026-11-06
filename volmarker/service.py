"""Business logic: publish, propagate to derived pairs, export, housekeeping."""

from __future__ import annotations

import copy
import gzip
import json
import logging
import queue
import threading
import time
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import exports
from .alerts import AlertEngine, deviation, iso, level
from .analysis import Exporter
from .tz import to_local
from .conventions import TENORS, norm_extra_tenor, normalise_pair
from . import bbg, weights_export
from .calendar import (DEFAULT_CUT, calibrate_to_quotes, cumulative_weights, daily_dates, default_events, expiry_for,
                       event_complete, implied_daily_weights, interpolate_atm, is_expiry_day, normalise_weights,
                       rule_weight, split_by_ccy, tenor_dates, with_defaults)
from .conventions import split_pair
from .derived import CORR_CONV
from .liveness import Liveness, wall_alive
from .bbgfeed import STATUS_FILE as BBG_STATUS, BloombergFeed, quiet
from .fenics import FenicsLogin
from .fmd.book import daily_n
from .market import BBG, compare, market_view, parse_fix_time
from .store import ConflictError, LockTimeout, Store, _reverse_lines, atomic_write, write_shared
from .surface import (all_marks, apply_derivation, apply_parent, check_surface, clean_state, follow_fields,
                      new_state, now_iso, resolve, skew_active, tenor_list, tol_mark)

log = logging.getLogger("volmarker")


def _atms(rec: dict) -> dict[str, float | None]:
    """ATM per tenor, the pair's extra tenors included (pillars of the interpolation)."""
    return {t: rec["tenors"][t]["atm"] for t in tenor_list(rec) if t in rec["tenors"]}

STATE_KEYS = ["pair", "mode", "bases", "key", "skew_expiry", "weights", "atm_type",
              "lock_rr10f", "lock_bf10f", "follow_market", "alert_tol", "alerts_off", "alerts_smile_off", "group", "spread_ref", "follow_parent", "cut", "dc_factor", "excel", "rr_put", "inverse", "market_source", "follow_bases",
              "deltas", "extra_tenors", "removed_tenors", "wing_locks", "tenors"]
LEADER_STALE = 45.0
LEADER_FIRST_SIGHT_S = 300.0   # a leader heartbeat this old by the clocks is gone at once
FEED_STALE = 15.0
FOLLOW_STALE = 30.0
MAX_TEST_DATES = 120
# pair settings saved by the Setup tab's Save (the rest of a version comes from the latest one)
SETTING_KEYS = ["mode", "bases", "key", "group", "spread_ref", "follow_parent", "follow_bases", "cut", "dc_factor",
                "atm_type", "follow_market", "excel", "alert_tol", "alerts_off", "alerts_smile_off", "inverse",
                "market_source", "deltas", "extra_tenors", "removed_tenors"]


def _labels(own: list, cal: list) -> list[str]:
    """Event names for a date: the pair's (or default) ones, then calendar ones not already named."""
    out = [x for x in dict.fromkeys(own) if x]
    return out + [c for c in dict.fromkeys(cal) if c and not any(c in x for x in out)]


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
        self.market_bbg: dict[str, list[dict]] = {}   # Bloomberg quotes of the pairs that prefer it (market/bbg/)
        self.market_status: dict = {}
        self.bbg_status: dict = {}
        self.follow_status: dict = {}
        self._market_mtimes: dict[str, float] = {}
        self.alerts = AlertEngine()
        self.fenics = FenicsLogin(store)          # FMD 1.0 feed while a trader is logged in to Fenics
        self.bbg = BloombergFeed(store, user, self._recs)   # Bloomberg while a trader runs it from this app
        self._leader = False
        self._archived_on: date | None = None         # the day this app last ran the history archive
        self._analysed_on: date | None = None         # ... and the daily export for analysis
        self._rec_at = 0.0                            # market recorder: last round (time.time())
        self._rec_day = ""                            # ... its UTC day; a new day starts with every quote
        self._rec_last: dict[str, dict] = {}          # ... pair -> {(tenor, mark, ptype, brand): (bid, offer, mid)}
        self.excel_foreign: dict[str, float] = {}     # pair -> mtime of a file changed outside VolMarker
        self.excel_errors: dict[str, str] = {}
        self.export_errors: dict[str, str] = {}
        self.live = Liveness()                          # the feeds', follower's and leader's heartbeats
        self._own_checked: dict[tuple, bool] = {}     # (export file, mtime) -> it is VolMarker's own        # pair -> why its export files could not be written
        self._prop_cv = threading.Condition()
        self._prop_pending: dict[tuple, str] = {}               # (kind, pair) -> source, coalesced
        self._prop_deferred: dict[str, tuple[float, str]] = {}  # dependent -> (due, source)
        self._prop_thread = None
        self._prop_busy = False
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
            quotes = self.quotes_for(r)
            pair_has_market = bool(quotes) and self.market_alive(r)
            outside = (compare(resolve(r), market_view(quotes, r.get("atm_type") or "DNS", brand, bool(r.get("inverse")),
                                                       prefer=self.source_of(r)))
                       if pair_has_market else None)
            age_min = (now - datetime.fromisoformat(r["marked_ts"])).total_seconds() / 60
            limit = cfg["stale_minutes_key"] if r.get("key") else cfg["stale_minutes_other"]
            follow = self.follow_state(r["pair"]) if r.get("follow_market") else None
            alerts = [a for a in active if a["pair"] == r["pair"]]
            out.append({
                "pair": r["pair"], "mode": r["mode"], "bases": r.get("bases"),
                "key": r.get("key", False), "group": r.get("group"), "spread_ref": r.get("spread_ref"),
                "follow_parent": bool(r.get("follow_parent")), "rr_put": bool(r.get("rr_put")),
                "excel": bool(r.get("excel")), "excel_foreign": r["pair"] in self.excel_foreign,
                "excel_error": self.excel_errors.get(r["pair"]), "version": r["version"],
                "ts": r["ts"], "user": r["user"], "source": r.get("source"),
                "marked_ts": r["marked_ts"], "age_minutes": round(age_min, 1),
                "stale": follow == "down" if follow else (r["mode"] == "marked" or r.get("excel")) and age_min > limit,
                "follow": follow, "market_source": self.source_of(r),
                "alerts": len(alerts), "alerts_new": sum(not a["acked"] for a in alerts),
                "skew": skew_active(r),
                "warnings": len(r.get("warnings", [])),
                "atm_1m": (r["tenors"].get("1M") or {}).get("atm"),
                "mkt": pair_has_market,
                "mkt_out": len(outside) if outside is not None else None,
            })
        return sorted(out, key=lambda x: x["pair"])

    def dependents(self, pair: str) -> list[str]:
        with self._lock:
            return sorted(p for p, r in self.cache.items()
                          if (r["mode"] == "derived" and pair in (r.get("bases") or []))
                          or (r.get("follow_parent") and r.get("spread_ref") == pair))

    def view(self, pair: str) -> dict | None:
        """Everything the UI needs for one pair."""
        rec = self.get(pair)
        if rec is None:
            return None
        bases = {b: self._resolved_mid_atm(b) for b in (rec.get("bases") or [])}
        holidays, _ = self.store.load_calendar()
        expiries = {t: d.isoformat() for t, d in tenor_dates(pair, date.today(), holidays, tenor_list(rec)).items()}
        return {"record": rec, "resolved": resolve(rec), "bases": bases,
                "dependents": self.dependents(pair), "expiries": expiries}

    def as_of(self, pair: str, before: datetime) -> dict | None:
        """The pair as last published before a moment, to compare today's marks with."""
        rec = self.store.as_of(pair, before)
        return {"record": rec, "resolved": resolve(rec)} if rec else None

    # ---- market data (written by the FMD feed process and the Bloomberg feed) ---------
    def _recs(self) -> list[dict]:
        with self._lock:
            return list(self.cache.values())

    def _beat(self, key: str, st: dict, stale: float = FEED_STALE) -> bool:
        """The heartbeat in st still moves (judged by this PC's clock, see liveness)."""
        return self.live.alive(key, st.get("heartbeat"), stale, time.time())

    def feed_alive(self) -> bool:
        """The FMD feed."""
        return bool(self.market_status.get("connected")) and self._beat("fmd", self.market_status)

    def bbg_alive(self) -> bool:
        # live False: running, but the last round got nothing from the Terminal
        return bool(self.bbg_status.get("connected")) and self.bbg_status.get("live") is not False and \
            self._beat("bbg", self.bbg_status)

    @staticmethod
    def source_of(rec: dict | None) -> str:
        """The pair's preferred market source: BBG or FMD."""
        return BBG if (rec or {}).get("market_source") == BBG else "FMD"

    def source_alive(self, rec: dict | None) -> bool:
        """The pair's preferred source is live (what Follow market needs)."""
        return self.bbg_alive() if self.source_of(rec) == BBG else self.feed_alive()

    def market_alive(self, rec: dict | None) -> bool:
        """Some live market for the pair: FMD, or Bloomberg on a pair that prefers it."""
        return self.feed_alive() or (self.source_of(rec) == BBG and self.bbg_alive())

    def quotes_for(self, rec: dict | None) -> list[dict]:
        """The pair's quotes: FMD's, and Bloomberg's while it runs if the pair prefers it
        (the market view takes Bloomberg first and FMD where Bloomberg has nothing)."""
        pair = (rec or {}).get("pair")
        with self._lock:
            q = list(self.market.get(pair) or [])
            if self.source_of(rec) == BBG and self.bbg_alive():
                q += self.market_bbg.get(pair) or []
        return q

    def bbg_quiet(self, now: float | None = None) -> bool:
        """In Bloomberg's quiet hours (18:00-07:00): Bloomberg pairs standing still is expected."""
        cfg = self.store.config()
        return quiet(cfg["bbg"], cfg.get("calendar_tz") or "Europe/Zurich", time.time() if now is None else now)

    def bbg_feed_status(self) -> dict:
        st = dict(self.bbg_status)
        if not st:
            return {"state": "off"}
        age = self.live.age("bbg", st.get("heartbeat"), time.time())
        st["state"] = ("live" if self.bbg_alive() else "stopped" if st.get("stopped")
                       else "down" if self._beat("bbg", st) else "off")
        st["heartbeat_age"] = round(age, 1)
        return st

    def feed_status(self) -> dict:
        st = dict(self.market_status)
        if not st:
            return {"state": "off"}
        age = self.live.age("fmd", st.get("heartbeat"), time.time())
        st["state"] = "live" if self.feed_alive() else ("down" if self._beat("fmd", st) else "off")
        st["heartbeat_age"] = round(age, 1)
        return st

    def follow_state(self, pair: str) -> str:
        """Market-mode health of one pair: ok, held (big move waiting), down."""
        fs, rec = self.follow_status, self.get(pair)
        if self.source_of(rec) == BBG and not self.bbg_alive() and self.bbg_quiet():
            return "stopped"              # Bloomberg's quiet hours: the pair keeps its levels, no alert
        if not self.source_alive(rec) or not self._beat("follow", fs, FOLLOW_STALE):
            return "down"
        st = (fs.get("pairs") or {}).get(pair)
        if st is None:
            return "starting"
        return {"ok": "ok", "held": "held"}.get(st.get("state"), "down")

    def fresh_view(self, pair: str, rec: dict, cfg: dict, brand: str | None = None, follow: bool = False) -> dict:
        """Market view with the age of every quote and a stale flag. follow: the pair's
        preferred source only, as Follow market publishes it (no mix of Bloomberg and FMD)."""
        src = self.source_of(rec)
        quotes = self.quotes_for(dict(rec, pair=pair))
        if follow:
            brand = BBG if src == BBG else cfg.get("fmd_brand", "auto")
            quotes = [q for q in quotes if (q.get("brand") == BBG) == (src == BBG)]
        brand = brand or cfg.get("fmd_brand", "auto")
        view = market_view(quotes, rec.get("atm_type") or "DNS", brand, bool(rec.get("inverse")), prefer=src)
        stale_min = cfg.get("market_stale_minutes", 30)
        now = datetime.now(timezone.utc)
        for marks in view.values():
            for q in marks.values():
                ts = parse_fix_time(q.get("ts"))
                q["age_min"] = round((now - ts).total_seconds() / 60, 1) if ts else None
                q["stale"] = q["age_min"] is not None and q["age_min"] > stale_min
        return view

    def market_for(self, pair: str, brand: str | None = None, draft: dict | None = None,
                   source: str | None = None) -> dict:
        """source: the draft's market source (FMD or BBG) before it is saved."""
        rec = draft or self.get(pair) or {}
        if source in ("FMD", BBG):
            rec = dict(rec, market_source=BBG if source == BBG else None)
        quotes = self.quotes_for(dict(rec, pair=pair))
        cfg = self.store.config()
        brand = brand or cfg.get("fmd_brand", "auto")
        view = self.fresh_view(pair, rec, cfg, brand)
        follow = None
        if rec.get("follow_market"):
            follow = dict((self.follow_status.get("pairs") or {}).get(pair) or {},
                          health=self.follow_state(pair))
        return {"feed": self.feed_status(), "bbg": self.bbg_feed_status(), "source": self.source_of(rec),
                "live": self.market_alive(rec), "brand": brand, "follow": follow,
                "brands": sorted({q.get("brand") for q in quotes if q.get("brand")}),
                "atm_type": rec.get("atm_type") or "DNS", "view": view}

    def dailies(self, pair: str, draft: dict | None = None, brand: str | None = None) -> dict:
        """Market dailies (1D..14D) vs the draft's interpolation, plus the event
        weights that would make the weights reproduce the market dailies."""
        rec = self._prepare(pair, draft) if draft else self.get(pair)
        m = self.market_for(pair, brand, draft=rec)
        holidays, cal_events = self.store.load_calendar()
        eff = self.effective_weights(pair, rec.get("weights"), cal_events, holidays, rec)
        basis = self.store.config().get("fmd_daily_basis", "calendar")
        daily = {daily_n(t): marks for t, marks in m["view"].items() if daily_n(t)}
        today = date.today()
        if not daily:
            return {"rows": [], "events": [], "warnings": [], "feed": m["feed"], "basis": basis}
        dd = daily_dates(pair, today, sorted(daily), holidays, basis)
        vols = {dd[n]: q["atm"]["mid"] for n, q in daily.items() if q.get("atm") and q["atm"].get("mid")}
        pillar_dates = tenor_dates(pair, today, holidays, tenor_list(rec))
        res = implied_daily_weights(pair, today, vols, rec.get("weights"), holidays,
                                    pillars=list(pillar_dates.values()))
        atm = _atms(rec)
        model = interpolate_atm(pair, atm, today, sorted(vols), eff, holidays)
        ccys = split_pair(pair)
        cur_events: dict[str, list] = {}
        for ev in eff["events"]:
            cur_events.setdefault(ev["date"], []).append(ev)
        cal = {}      # labels: manual calendar events and relevant Bloomberg ones, on their expiry
        rules = self.store.load_event_defaults()["rows"]
        for ev in cal_events:
            if ev["ccy"] in ccys and (ev.get("source") == "manual" or rule_weight(ev, rules) is not None):
                d = expiry_for(ev, ccys, holidays, self.cut_of(rec)).isoformat()
                cal.setdefault(d, []).append(ev["desc"])
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
                "labels": _labels([e.get("desc", "") for e in cur_events.get(iso, [])], cal.get(iso, [])),
            })
            prev_d, prev_tv = d, tv
        events = []
        for ev in res["events"]:
            names = _labels([e.get("desc") for e in cur_events.get(ev["date"], [])], cal.get(ev["date"], []))
            events.append({"date": ev["date"], "desc": names[0] if names else "FMD daily", "weight": ev["weight"]})
        # a default event inside the window that the market doesn't price is switched off (weight 0)
        window = (today.isoformat(), max(vols).isoformat())
        implied = {e["date"] for e in events}
        for ev in eff["events"]:
            if ev.get("source") == "default" and window[0] < ev["date"] <= window[1] and ev["date"] not in implied:
                events.append({"date": ev["date"], "ccy": ev.get("ccy"), "desc": ev["desc"], "weight": 0.0})
        events = split_by_ccy(events, eff["events"], split_pair(pair))   # half to each currency
        return {"rows": rows, "events": events, "warnings": res["warnings"], "basis": basis,
                "window_end": max(vols).isoformat() if vols else None, "feed": m["feed"]}

    # ---- event weights: desk defaults, curve, test dates ---------------
    def cut_of(self, rec: dict | None) -> dict:
        """The pair's option expiry cut: {"tz", "time"} (config "cuts", NYC by default)."""
        cuts = self.store.config().get("cuts") or {}
        return cuts.get((rec or {}).get("cut") or "NYC") or cuts.get("NYC") or DEFAULT_CUT

    def effective_weights(self, pair: str, weights: dict | None, cal_events: list[dict] | None = None,
                          holidays: dict | None = None, rec: dict | None = None) -> dict:
        if cal_events is None or holidays is None:
            holidays, cal_events = self.store.load_calendar()
        rec = rec or self.get(pair)
        w = with_defaults(pair, weights, cal_events, self.store.load_event_defaults()["rows"],
                          holidays, self.cut_of(rec))
        w["cut"] = (rec or {}).get("cut") or "NYC"         # its name, for the Fenics labels
        w["cut_events_other_ccy"] = (self.store.config().get("weights_export") or {}).get("cut_events_other_ccy", True) is not False
        return w

    def pair_default_events(self, pair: str, cut: str | None = None) -> list[dict]:
        """Upcoming relevant events of this pair with their default weight, on their expiry."""
        holidays, cal = self.store.load_calendar()
        rec = dict(self.get(pair) or {}, **({"cut": cut} if cut else {}))
        return default_events(pair, cal, self.store.load_event_defaults()["rows"],
                              start=date.today().isoformat(), holidays=holidays, cut=self.cut_of(rec),
                              display_tz=self.store.config().get("calendar_tz", "Europe/Zurich"))

    def event_defaults(self) -> dict:
        """The list of relevant events, with how many upcoming calendar events each one matches."""
        d = self.store.load_event_defaults()
        _, cal = self.store.load_calendar()
        today = date.today().isoformat()
        upcoming = sorted((e for e in cal if e["date"] >= today), key=lambda e: (e["date"], e.get("time") or ""))
        for r in d["rows"]:
            hits = [e for e in upcoming if rule_weight(e, [r]) is not None]
            r["matches"] = len(hits)
            r["next"] = f"{hits[0]['date']} {hits[0].get('time') or ''} {hits[0]['ccy']} {hits[0]['desc']}" if hits else None
        d["imports"] = self.store.bloomberg_imports()
        return d

    # ---- event weights to Fenics (XML poster) and Front Arena -----------
    def _weights_target(self, pair: str) -> tuple[dict, dict, dict, date]:
        rec = self.get(pair)
        if rec is None:
            raise ValueError(f"Unknown pair {pair}")
        holidays, cal = self.store.load_calendar()
        return rec, self.effective_weights(pair, rec.get("weights"), cal, holidays, rec), holidays, date.today()

    def weights_status(self, pair: str) -> dict:
        """What would be sent for the published version, and whether it differs from the last send."""
        rec, w, holidays, today = self._weights_target(pair)
        cfg = self.store.config()["weights_export"]
        fx, ws = weights_export.frontarena_lines(pair, w, holidays, today, cfg["date_format"], self._fa_ccy())
        items = weights_export.fenics_items(pair, w, holidays, today)
        last = self._last_weights_sent(pair)
        fp = weights_export.fingerprint(pair, w, holidays, today)
        return {"version": rec["version"], "fingerprint": fp, "last": last,
                "changed": not last or not last.get("ok") or last.get("fingerprint") != fp,
                "url": cfg["fenics_url"], "fenics": [dict(e, date=e["date"].isoformat()) for e in items],
                "fa_fx": fx, "fa_ws": ws, "weekdays": w["weekdays"],
                "holiday1": w["holiday1"], "holiday2": w["holiday2"]}

    def send_weights(self, pair: str, username: str, password: str) -> dict:
        """Write the Front Arena event files and post the weights to Fenics
        (published version). The password is only passed on, never stored.
        Without one, the app's Fenics login is used."""
        if not password and self.fenics.credentials():
            username, password = self.fenics.credentials()
        rec, w, holidays, today = self._weights_target(pair)
        cfg = self.store.config()["weights_export"]
        fx, ws = weights_export.frontarena_lines(pair, w, holidays, today, cfg["date_format"], self._fa_ccy())
        c1, c2 = exports.fa_pair(pair, self._fa_ccy())
        for key, lines in (("fa_fx_dir", fx), ("fa_ws_dir", ws)):
            atomic_write(self._export_dir(cfg[key]) / f"{c1}{c2}_events.txt", "".join(l + "\r\n" for l in lines))
        fp = weights_export.fingerprint(pair, w, holidays, today)
        entry = {"ts": now_iso(), "user": self.user, "pair": pair, "version": rec["version"],
                 "fingerprint": fp, "fa": True, "fenics": False, "ok": False}
        xml = weights_export.fenics_xml(pair, w, holidays, today, username, password)
        shown = xml.replace(f"<password>{weights_export.escape(password)}</password>", "<password></password>")
        atomic_write(self._export_dir(cfg["fenics_dir"]) / f"{c1}{c2}_weights.xml", shown)
        response = ""
        if not cfg["fenics_url"]:
            entry["error"] = "No Fenics server set (config.json: weights_export.fenics_url)"
        elif not username or not password:
            entry["error"] = "Fenics user and password needed"
        else:
            try:
                response = weights_export.post(cfg["fenics_url"], xml)
                entry["fenics"] = "error" not in response.lower()
                entry["ok"] = entry["fenics"]
                if not entry["ok"]:
                    entry["error"] = "Fenics answered with an error"
            except OSError as e:
                entry["error"] = f"Could not reach Fenics: {e}"
        log = self.store.root / "exports" / "weights_sent.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return dict(entry, response=response[:4000], xml=shown)

    def _last_weights_sent(self, pair: str) -> dict | None:
        """The pair's last send, from the end of the log backwards."""
        log = self.store.root / "exports" / "weights_sent.jsonl"
        if not log.exists():
            return None
        for line in _reverse_lines(log):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("pair") == pair:
                return e
        return None

    def _export_dir(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.store.root / path

    # ---- Bloomberg ECO import -------------------------------------------
    def bloomberg_preview(self, data: bytes, filename: str) -> dict:
        """Parse an export and list its distinct events, marking the ones already
        on the list of relevant events, so the desk can pick what to keep."""
        tz = self.store.config().get("calendar_tz", "Europe/Zurich")
        parsed = bbg.parse_eco(data, tz)
        evs = parsed["events"]
        if not evs:
            raise ValueError("No events in this file")
        ccys = sorted({e["ccy"] for e in evs})
        rules = self.store.load_event_defaults()["rows"]
        today = date.today().isoformat()
        distinct: dict[tuple, dict] = {}
        for e in sorted(evs, key=lambda e: (e["date"], e.get("time") or "")):
            k = (e["ccy"], e["name"])
            x = distinct.setdefault(k, {"ccy": e["ccy"], "name": e["name"], "ticker": e["ticker"],
                                        "relevance": e["relevance"], "count": 0, "next": None, "times": set()})
            x["count"] += 1
            x["relevance"] = max(x["relevance"] or 0, e["relevance"] or 0)
            if e.get("time"):
                x["times"].add(e["time"])
            if x["next"] is None and e["date"] >= today:
                x["next"] = f"{e['date']} {e.get('time') or ''}".strip()
            w = rule_weight({"ccy": e["ccy"], "desc": e["name"], "event": e["event"], "ticker": e["ticker"]}, rules)
            if w is not None:
                x["weight"] = w
        found = {(x["ccy"], x["name"].lower()) for x in distinct.values()} | \
                {(x["ccy"], x["ticker"].lower()) for x in distinct.values()}
        missing = [r for r in rules if r["ccy"] in ccys and (r["ccy"], (r["event"] or "").lower()) not in found
                   and (r["ccy"], (r.get("ticker") or "").lower()) not in found]
        return {
            "file": filename, "ccys": ccys, "downloaded": parsed["downloaded"], "source_tz": tz,
            "count": len(evs), "from": min(e["date"] for e in evs), "to": max(e["date"] for e in evs),
            "skipped": len(parsed["skipped"]), "skipped_list": parsed["skipped"][:50],
            "assumed": parsed["assumed"], "missing": missing,
            "events": sorted(({**x, "times": sorted(x["times"]), "relevant": "weight" in x}
                              for x in distinct.values()),
                             key=lambda x: (not x["relevant"], -(x["relevance"] or 0), x["name"])),
        }

    def bloomberg_import(self, data: bytes, filename: str, selection: list[dict] | None,
                         expected_version: str | None) -> dict:
        """Store the export (it replaces the previous one of the same currency)
        and, if given, the desk's choice of relevant events and their weights
        for the currencies in the file."""
        tz = self.store.config().get("calendar_tz", "Europe/Zurich")
        parsed = bbg.parse_eco(data, tz)
        evs = parsed["events"]
        if not evs:
            raise ValueError("No events in this file")
        ccys = sorted({e["ccy"] for e in evs})
        if selection is not None:
            cur = self.store.load_event_defaults()
            keep = [r for r in cur["rows"] if r["ccy"] not in ccys]
            # rules of these currencies that the file doesn't contain (one-offs like elections) stay
            names = {(e["ccy"], e["name"].lower()) for e in evs} | {(e["ccy"], e["ticker"].lower()) for e in evs if e["ticker"]}
            keep += [r for r in cur["rows"] if r["ccy"] in ccys and (r["ccy"], r["event"].lower()) not in names
                     and (r["ccy"], (r.get("ticker") or "").lower()) not in names]
            chosen = [{"ccy": x["ccy"], "event": x["name"], "ticker": x.get("ticker") or "",
                       "weight": float(x.get("weight") or 0)} for x in selection if x.get("ccy") in ccys]
            self.save_event_defaults(keep + chosen, expected_version)
        for c in ccys:
            self.store.save_bloomberg(c, {
                "ts": now_iso(), "user": self.user, "file": filename, "downloaded": parsed["downloaded"],
                "source_tz": tz, "events": [e for e in evs if e["ccy"] == c]})
        self.store.journal({"ts": now_iso(), "kind": "event_defaults", "user": self.user,
                            "instance": self.instance})
        self._notify({"type": "event_defaults", "user": self.user})
        return self.event_defaults()

    def save_event_defaults(self, rows: list[dict], expected_version: str | None) -> dict:
        d = self.store.save_event_defaults(rows, expected_version, self.user, now_iso())
        self.store.journal({"ts": now_iso(), "kind": "event_defaults", "user": self.user,
                            "instance": self.instance})
        self._notify({"type": "event_defaults", "user": self.user})
        return d

    def _state_for(self, pair: str, draft: dict | None) -> dict | None:
        if not draft:
            return self.get(pair)
        state, _ = self._derive(self._prepare(pair, draft))
        return state

    def curve(self, pair: str, draft: dict | None = None, days: int = 185) -> dict:
        """Interpolated ATM per good day (published, and the draft if given)."""
        pub = self.get(pair)
        if pub is None:
            raise ValueError(f"Unknown pair {pair}")
        holidays, cal = self.store.load_calendar()
        today = date.today()
        ccys = split_pair(pair)
        targets = [d for d in (today + timedelta(days=i) for i in range(1, days + 1))
                   if is_expiry_day(d)]

        def run(rec):
            atm = _atms(rec)
            w = self.effective_weights(pair, rec.get("weights"), cal, holidays, rec)
            return interpolate_atm(pair, atm, today, targets, w, holidays), w["events"]
        vols, events = run(pub)
        out = {"points": [[d.isoformat(), round(vols[d], 4), None] for d in targets if d in vols],
               "events": events}
        if draft:
            dv, events = run(self._state_for(pair, draft))
            out["points"] = [[d, v, round(dv[date.fromisoformat(d)], 4)
                              if date.fromisoformat(d) in dv else None] for d, v, _ in out["points"]]
            out["events"] = events
        return out

    def test_dates(self, pair: str, dates: list[str], draft: dict | None = None) -> dict:
        """Interpolated ATM on trader-chosen expiry dates, for the draft and the
        published version, with the weighted days and the forward vol between rows."""
        pub = self.get(pair)
        if pub is None:
            raise ValueError(f"Unknown pair {pair}")
        rec = self._state_for(pair, draft) or pub
        today = date.today()
        ds = sorted({date.fromisoformat(d) for d in dates[:MAX_TEST_DATES]})
        ds = [d for d in ds if today < d <= today + timedelta(days=5 * 366)]
        if not ds:
            return {"rows": []}
        holidays, cal = self.store.load_calendar()
        ccys = split_pair(pair)
        w = self.effective_weights(pair, rec.get("weights"), cal, holidays, rec)
        wp = self.effective_weights(pair, pub.get("weights"), cal, holidays, pub)
        v = interpolate_atm(pair, _atms(rec), today, ds, w, holidays)
        vp = interpolate_atm(pair, _atms(pub), today, ds, wp, holidays)
        cum = cumulative_weights(pair, today, ds[-1], w, holidays)
        evs: dict[str, list] = {}
        for e in w["events"]:
            evs.setdefault(e["date"], []).append(e)
        tenors = tenor_list(rec)
        tdates = tenor_dates(pair, today, holidays, tenors)
        rows, prev_n, prev_tv = [], 0, 0.0
        for d in ds:
            n = (d - today).days
            vol = v.get(d)
            tv = vol ** 2 * n / 365 if vol is not None else None
            fwd = None
            if tv is not None and prev_tv is not None and n > prev_n:
                fwd = max(tv - prev_tv, 0) * 365 / (n - prev_n)
                fwd = fwd ** 0.5
            iso = d.isoformat()
            after = [t for t in tenors if tdates[t] >= d]
            rows.append({
                "date": iso, "weekday": d.strftime("%a"), "days": n,
                "weighted_days": round(cum[n], 3), "day_weight": round(cum[n] - cum[n - 1], 3),
                "good_day": is_expiry_day(d),
                "atm": vol, "published": vp.get(d), "fwd": fwd,
                "between": after[0] if after else tenors[-1],
                "pillar": next((t for t in tenors if tdates[t] == d), None),
                "events": [f"{e['desc']} ({e['weight']:+g})" for e in evs.get(iso, [])],
            })
            prev_n, prev_tv = n, tv
        return {"rows": rows}

    def calibrate_test_quotes(self, pair: str, quotes: list[dict], draft: dict | None = None,
                              floor: bool = True) -> dict:
        """Event weights that give the draft the forward variance between the
        external quotes (bid/offer typed on test dates); see calibrate_to_quotes."""
        rec = self._state_for(pair, draft) or self.get(pair)
        if rec is None:
            raise ValueError(f"Unknown pair {pair}")
        mids = {}
        for q in quotes[:MAX_TEST_DATES]:
            vs = [float(q[k]) for k in ("bid", "offer") if q.get(k) not in (None, "")]
            if vs:
                mids[date.fromisoformat(q["date"])] = sum(vs) / len(vs)
        holidays, cal = self.store.load_calendar()
        w = self.effective_weights(pair, rec.get("weights"), cal, holidays, rec)
        out = calibrate_to_quotes(pair, _atms(rec), date.today(),
                                  mids, w, holidays, floor=floor)
        out["events"] = split_by_ccy(out["events"], w["events"], split_pair(pair))   # half to each currency
        return out

    def poll_market(self, notify: bool = True) -> None:
        d = self.store.root / "market"
        if not d.is_dir():
            return
        changed = []
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        bdir = d / "bbg"
        try:
            bbg_entries = list(os.scandir(bdir)) if bdir.is_dir() else []
        except OSError:
            bbg_entries = []
        names = {e.name for e in bbg_entries}
        with self._lock:                  # pairs switched back to FMD: their Bloomberg file is gone
            for p in [p for p in self.market_bbg if f"{p}.json" not in names]:
                self.market_bbg.pop(p)
                self._market_mtimes.pop(f"bbg/{p}.json", None)
                changed.append(p)
        for e in bbg_entries:
            if not e.name.endswith(".json"):
                continue
            try:
                mt = e.stat().st_mtime
                if self._market_mtimes.get(f"bbg/{e.name}") == mt:
                    continue
                data = json.loads(Path(e.path).read_text())
            except (OSError, ValueError):
                continue          # being replaced - next poll
            self._market_mtimes[f"bbg/{e.name}"] = mt
            with self._lock:
                self.market_bbg[data["pair"]] = data["quotes"]
            changed.append(data["pair"])
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
            elif e.name == BBG_STATUS:
                if bool(data.get("connected")) != bool(self.bbg_status.get("connected")):
                    changed += list(self.market_bbg)        # Bloomberg on or off: those pairs' views change
                self.bbg_status = data
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
        if not self.quotes_for(rec) or not self.market_alive(rec):
            return None
        m = self.market_for(rec["pair"], draft=rec)
        snap = {t: {k: q.get("mid") for k, q in marks.items()} for t, marks in m["view"].items()}
        return {"brand": m["brand"], "mids": snap} if snap else None

    def _resolved_mid_atm(self, pair: str) -> dict | None:
        r = self.get(pair)
        if r is None:
            return None
        return {"version": r["version"], "ts": r["ts"],
                "atm": _atms(r)}

    def new_tenor_row(self, pair: str, draft: dict, tenor: str) -> dict:
        """Marks for a tenor being added to the draft (an extra one, or a standard one the pair
        had removed): the ATM the curve already gives its expiry (interpolated with the event
        weights, so adding it changes nothing until it is marked), the rest interpolated in
        days between its neighbours (flat beyond)."""
        t = str(tenor or "").strip().upper()
        removed = [x for x in (draft.get("removed_tenors") or []) if x != t]
        if t in TENORS:
            if t not in (draft.get("removed_tenors") or []):
                raise ValueError(f"{t} is already a tenor of {pair}")
        else:
            t = norm_extra_tenor(t)
        draft = dict(draft, removed_tenors=removed + ([t] if t in TENORS else []),
                     extra_tenors={k: v for k, v in (draft.get("extra_tenors") or {}).items()
                                   if k.strip().upper() != t})
        rec = self._state_for(pair, draft)
        if rec is None:
            raise ValueError(f"Unknown pair {pair}")
        holidays, cal = self.store.load_calendar()
        today = date.today()
        others = tenor_list(rec)
        dates = tenor_dates(pair, today, holidays, others + [t])
        d = dates[t]
        w = self.effective_weights(pair, rec.get("weights"), cal, holidays, rec)
        atm = interpolate_atm(pair, _atms(rec), today, [d], w, holidays).get(d)
        before = [x for x in others if dates[x] < d]
        after = [x for x in others if dates[x] >= d]
        lo, hi = (before or after)[-1 if before else 0], (after or before)[0 if after else -1]
        frac = 0.0 if dates[hi] == dates[lo] else (d - dates[lo]).days / (dates[hi] - dates[lo]).days
        a, b = rec["tenors"][lo], rec["tenors"][hi]
        row = {}
        for f, va in a.items():
            vb = b.get(f)
            if f in ("atm", "corr", "psprd") or va is None or vb is None:
                continue
            row[f] = round(va + (vb - va) * frac, 4)
        row.update(atm=None if atm is None else round(atm, 4), corr=None, psprd=None)
        return {"tenor": t, "expiry": d.isoformat(), "row": row}

    def preview(self, pair: str, draft: dict) -> dict:
        state = self._prepare(pair, draft)
        state, warnings = self._derive(state)
        return {"state": state, "resolved": resolve(state),
                "warnings": warnings + check_surface(state)}

    # ---- per-user preferences (sidebar layout, spread references) -----
    def _prefs_path(self) -> Path:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", self.user).strip(".") or "user"
        return self.store.root / "users" / f"{name}.json"

    def prefs(self) -> dict:
        try:
            p = json.loads(self._prefs_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return p if isinstance(p, dict) else {}

    def save_prefs(self, prefs: dict) -> dict:
        if not isinstance(prefs, dict):
            raise ValueError("Preferences must be a JSON object")
        text = json.dumps(prefs, indent=1)
        if len(text) > 200_000:
            raise ValueError("Preferences too large")
        atomic_write(self._prefs_path(), text)
        return prefs

    # ---- write side ---------------------------------------------------
    def create_pair(self, pair: str, mode: str, bases: list[str] | None, key: bool,
                    group: str | None = None) -> dict:
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
        state["group"] = group
        if mode == "derived":
            # start from the base-implied ATM at zero correlation; a tenor whose ATM comes
            # from an existing export file keeps it and gets its correlation calibrated
            for t in TENORS:
                state["tenors"][t]["corr"] = 0.0
        seeded = self._seed_from_export(state)
        comment = f"created from {seeded}" if seeded else "created"
        return self.publish(pair, state, expected_version=0, comment=comment)

    def _seed_from_export(self, state: dict) -> str | None:
        """A new pair starts from the Front Arena file already in the export folder
        (e.g. copied there from the Excel workbook's exports), so its first publish
        rewrites that file with the same marks instead of blanking it. Returns the
        file name it read, or None."""
        found = self._read_export_file(state["pair"])
        if not found:
            return None
        self._apply_marks(state, found[2])
        return found[0].name

    def _read_export_file(self, pair: str) -> tuple[Path, float, dict] | None:
        """The pair's newest readable Front Arena file (export folder, or Excel's
        folder if one is set): (path, mtime, marks per tenor, possibly partial)."""
        c1, c2 = exports.fa_pair(pair, self._fa_ccy())
        cfg = self.store.config()
        dirs = [self.store.path_from_config("frontarena_dir")]
        if cfg.get("excel_dir"):
            dirs.append(self.store.path_from_config("excel_dir"))
        best = None
        for d in dirs:
            path = d / f"{c1}{c2}volsurface.txt"
            try:
                mt = path.stat().st_mtime
                marks = exports.read_frontarena(path.read_text(encoding="utf-8", errors="replace"),
                                                pair, partial=True, fa_ccy=self._fa_ccy())
            except (OSError, ValueError):
                continue
            if any(marks.values()) and (best is None or mt > best[1]):
                best = (path, mt, marks)
        return best

    @staticmethod
    def _apply_marks(state: dict, marks: dict) -> None:
        """Put a file's marks into a state; 10D factors are calibrated to them."""
        for t in TENORS:
            if t not in state["tenors"]:
                continue                                 # a tenor the pair doesn't mark
            row, m = state["tenors"][t], marks[t]
            row.update(m)
            if "atm" in m:
                if state["mode"] == "derived":
                    row["corr"] = None                   # calibrated to the file's ATM
                if state.get("follow_parent"):
                    row["psprd"] = None                  # spread to the parent calibrated to it
            for w in ("rr", "bf"):
                v25, v10 = row[f"{w}25"], row.get(f"{w}10")
                if v10 is None:
                    continue
                f = v10 / v25 if abs(v25) > 1e-12 else None
                if f is not None and f > 0:
                    row[f"{w}10f"] = f
                elif abs(v10) > 1e-12:
                    state[f"lock_{w}10f"] = False   # no positive factor gives this 10D: keep it as typed

    def load_export_file(self, pair: str, draft: dict | None = None) -> dict:
        """The draft with the marks of the pair's current Front Arena file, e.g.
        after someone marked it in Excel meanwhile. Nothing is saved: the trader
        checks the draft and publishes it."""
        pair = normalise_pair(pair)
        rec = self.get(pair)
        if rec is None:
            raise ValueError(f"Unknown pair {pair}")
        state = self._prepare(pair, draft or rec)
        if state.get("excel"):
            raise ValueError(f"{pair} is marked in Excel: VolMarker already reads its file")
        if state.get("follow_market"):
            raise ValueError(f"{pair} follows the market: switch off Follow market in Setup to load a file")
        found = self._read_export_file(pair)
        if not found:
            c1, c2 = exports.fa_pair(pair, self._fa_ccy())
            raise ValueError(f"No readable {c1}{c2}volsurface.txt in the Front Arena folder")
        path, mt, marks = found
        self._apply_marks(state, marks)
        return {"state": clean_state(state), "file": path.name, "mtime": mt,
                "missing": [t for t in TENORS if not marks[t]],
                "ours": mt <= datetime.fromisoformat(rec["ts"]).timestamp() + 30}

    def publish(self, pair: str, draft: dict, expected_version: int | None,
                comment: str = "", source: str = "manual") -> dict:
        pair = normalise_pair(pair)
        if source == "manual" and any(not e.get("deleted") and not event_complete(e)
                                      for e in (draft.get("weights") or {}).get("events") or []):
            raise ValueError("Event weights: a new event needs a date and a weight (or remove it with ✕)")
        state = self._prepare(pair, draft)
        self._check_cycle(state)
        with self.store.lock(pair, self.user):
            if source == "manual" and expected_version is not None:
                state, expected_version = self._rebase_over_market(state, expected_version)
            state, warnings = self._derive(state)
            rec = self._append(state, warnings, expected_version, comment, source,
                               marked=source == "manual")
        self._schedule(pair, source)
        return rec

    def save_settings(self, pair: str, draft: dict, expected_version: int | None) -> dict:
        """The Setup tab's Save: a new version with the draft's pair settings on
        top of the latest marks. Mark edits stay in the trader's draft, and the
        pair's staleness clock is not reset."""
        pair = normalise_pair(pair)
        with self.store.lock(pair, self.user):
            latest = self.store.latest(pair)
            if latest is None:
                raise ValueError(f"Unknown pair {pair}")
            if expected_version is not None and latest["version"] > expected_version:
                base = self.store.history(pair, latest["version"] - expected_version + 1)[-1]   # newest first
                if any(base.get(k) != latest.get(k) for k in SETTING_KEYS):
                    raise ConflictError(latest)          # someone else changed the settings meanwhile
            state = {k: copy.deepcopy(latest.get(k)) for k in STATE_KEYS}
            for k in SETTING_KEYS:
                state[k] = copy.deepcopy(draft.get(k))
            # what switching a setting does to the marks (as the Setup tab does in the draft)
            rederive = (state["mode"], state["bases"]) != (latest["mode"], latest.get("bases"))
            reparent = (state["spread_ref"], bool(state["follow_parent"])) != \
                (latest.get("spread_ref"), bool(latest.get("follow_parent")))
            follow_on = bool(state["follow_market"]) and not latest.get("follow_market")
            dt = draft.get("tenors") or {}
            for t in tenor_list(state):
                if t not in state["tenors"]:             # a tenor added in Setup: the draft's interpolated row
                    if t not in dt:
                        raise ValueError(f"{t}: no marks for the new tenor")
                    state["tenors"][t] = copy.deepcopy(dt[t])
                row = state["tenors"][t]
                if rederive:
                    row["corr"] = None                   # calibrated to the current ATMs
                if reparent:
                    row["psprd"] = None
                if follow_on and t in dt:
                    row.update({f: dt[t].get(f) for f in follow_fields(state)})   # FMD mids the draft took
            new_deltas = set(state["deltas"] or []) - set(latest.get("deltas") or [])
            if new_deltas:                               # a delta added in Setup: its lock as the draft has it
                locks = dict(state.get("wing_locks") or {})
                for d in new_deltas:
                    for w in ("rr", "bf"):
                        locks[f"{w}{d}"] = (draft.get("wing_locks") or {}).get(f"{w}{d}", True)
                state["wing_locks"] = locks
            if bool(state["follow_market"]) != bool(latest.get("follow_market")) or \
                    bool(state["excel"]) != bool(latest.get("excel")):
                for k in ("lock_rr10f", "lock_bf10f", "wing_locks"):  # may have been unticked for factors a lock can't hold
                    state[k] = copy.deepcopy(draft.get(k))
            state = self._prepare(pair, state)
            self._check_cycle(state)
            state, warnings = self._derive(state)
            rec = self._append(state, warnings, latest["version"], "Settings", "settings", marked=False)
        self._schedule(pair, "settings")
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
        if any(r.get("source") not in ("market", "propagation", "excel", "event-prune") for r in newer):
            return state, expected                    # a real conflict - reported by append
        if any(r.get("source") == "event-prune" for r in newer) and state.get("weights"):
            today = date.today().isoformat()             # the leader removed the past events meanwhile: so does the draft
            state["weights"]["events"] = [e for e in state["weights"].get("events") or [] if not (e.get("date") or today) < today]
        if state.get("follow_market") or state.get("excel"):
            for t in tenor_list(state):
                if t not in latest["tenors"]:
                    continue
                for f in follow_fields(state) + (["spread"] if state.get("excel") else []):
                    if f in latest["tenors"][t]:
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
        with self._lock:
            recs = list(self.cache.values())
        cands: dict[tuple, dict] = {}
        for rec in recs:
            pair = rec["pair"]
            if rec.get("follow_market"):
                self._follow_candidates(rec, cands)
                continue
            if not acfg.get("enabled", True) or rec.get("alerts_off") or not self.market_alive(rec) \
                    or not self.quotes_for(rec):
                continue
            view = self.fresh_view(pair, rec, cfg)
            res = resolve(rec)
            own = rec.get("alert_tol") or {}
            marks = ["atm"] if rec.get("alerts_smile_off") else all_marks(rec)     # RR and BF muted, ATM kept
            for t in tenor_list(rec):
                for m in marks:
                    q = view.get(t, {}).get(m)
                    v = res[t]["mid"][m]
                    if not q or q.get("stale") or q.get("alt_atm") or v is None:
                        continue
                    dev = deviation(v, q, acfg.get("basis", "mid"))
                    if dev is None:
                        continue
                    tm = tol_mark(m)       # an extra delta: the 10D's tolerance (25D's inside 25D)
                    tol = (own.get(tm) or acfg["tolerance"][tm]) * acfg.get("tenor_scale", {}).get(t, 1.0)
                    cands[(pair, t, m)] = {
                        "level": level(dev, tol, acfg.get("clear_ratio", 0.8)), "kind": "market",
                        "pair": pair, "tenor": t, "mark": m, "value": v, "mkt": q.get("mid"),
                        "bid": q.get("bid"), "offer": q.get("offer"), "brand": q.get("brand"),
                        "dev": round(dev, 4), "tol": round(tol, 4), "source": rec.get("source"),
                        "msg": f"{pair} {t} {m.upper()}: ours {v:.2f}, {q.get('brand') or 'market'} {q.get('mid'):.2f} "
                               f"({dev:+.2f}, tol {tol:.2f})"}
        for pair, mt in self.excel_foreign.items():
            at = datetime.fromtimestamp(mt).strftime("%H:%M")
            cands[(pair, "", "excel")] = {
                "level": "breach", "kind": "excel", "confirm": 0, "pair": pair, "tenor": "", "mark": "excel",
                "msg": f"{pair}: its export file was changed outside VolMarker at {at}. If Excel still marks "
                       f"this pair, tick Marked in Excel in Setup, or the two overwrite each other"}
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
        src = "Bloomberg" if self.source_of(rec) == BBG else "FMD"
        why = (f"the {src} feed is down" if not self.source_alive(rec) else
               f"the follower failed ({st.get('error')})" if st.get("state") == "error" else
               "the follower is not running")
        cands[(pair, "", "follow")] = {        # not after Bloomberg's daily stop: that is expected
            "level": "breach" if health == "down" else "clear", "kind": "follow", "confirm": 10,
            "pair": pair, "tenor": "", "mark": "follow",
            "msg": f"{pair} follows {src} but {why}: Fenics keeps v{rec['version']} "
                   f"from {rec['ts'][11:16]} UTC"}
        if health in ("down", "stopped"):
            return
        for h in st.get("held") or []:
            d = h["to"] - (h["from"] or 0)
            cands[(pair, h["tenor"], h["mark"] + "-jump")] = {
                "level": "breach", "kind": "jump", "confirm": 0, "pair": pair, "tenor": h["tenor"],
                "mark": h["mark"], "value": h["from"], "mkt": h["to"], "dev": round(-d, 4),
                "msg": f"{pair} {h['tenor']} {h['mark'].upper()}: {src} moved {d:+.2f}, held back; "
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
        """Derived pairs from their bases, pairs moving with their parent from the parent."""
        if state["mode"] == "derived":
            return apply_derivation(state, {b: self.store.latest(b) for b in state["bases"]})
        if state.get("follow_parent"):
            return apply_parent(state, self.store.latest(state["spread_ref"]))
        return state, []

    def _append(self, state: dict, warnings: list[str], expected_version: int | None,
                comment: str, source: str, marked: bool, extra: dict | None = None,
                marked_ts: str | None = None, user: str | None = None) -> dict:
        prev = self.store.latest(state["pair"])
        record = {k: state.get(k) for k in STATE_KEYS}
        record.update({
            "ts": now_iso(), "user": user or self.user, "comment": comment, "source": source,
            "warnings": warnings + check_surface(state), "corr_conv": CORR_CONV,
        })
        # marked_ts = last time a person touched it (staleness); propagation keeps it
        record["marked_ts"] = marked_ts or (record["ts"] if marked or prev is None else prev["marked_ts"])
        if state.get("excel") and prev:               # which Excel export the marks come from
            for k in ("excel_mtime", "excel_file"):
                if prev.get(k) is not None:
                    record[k] = prev[k]
            if not prev.get("excel"):
                # switched to Excel: a file VolMarker wrote itself is the baseline, not an Excel export
                c1, c2 = exports.fa_pair(state["pair"], self._fa_ccy())
                try:
                    mt = (self._excel_dir() / f"{c1}{c2}volsurface.txt").stat().st_mtime
                    if mt <= datetime.fromisoformat(prev["ts"]).timestamp() + 30:
                        record["excel_mtime"] = mt
                except OSError:
                    pass
        record.update(extra or {})
        if marked:
            snap = self._market_snapshot(record)
            if snap:
                record["market_at_publish"] = snap
        if state["mode"] == "derived":
            record["base_versions"] = {b: (self.store.latest(b) or {}).get("version")
                                       for b in state["bases"]}
        if state.get("follow_parent"):
            record["parent_version"] = (self.store.latest(state["spread_ref"]) or {}).get("version")
        rec = self.store.append(record, expected_version)
        # the version is saved: everything below must happen even if an export file is blocked
        with self._lock:
            self.cache[rec["pair"]] = rec
        self.store.journal({"ts": rec["ts"], "pair": rec["pair"], "version": rec["version"],
                            "user": rec["user"], "source": source,
                            "instance": self.instance})
        self._notify({"type": "update", "pair": rec["pair"], "version": rec["version"],
                      "user": rec["user"], "source": source})
        self._try_export(rec)
        return rec

    def _try_export(self, rec: dict) -> bool:
        """Write the pair's export files; if one is blocked (Windows: another program
        has it open), log it once and leave the pair for retry_exports."""
        pair = rec["pair"]
        try:
            self._export(rec)
        except OSError as e:
            if self.export_errors.get(pair) != str(e):
                log.warning("%s v%s saved, but its export files could not be written (retrying): %s",
                            pair, rec["version"], e)
            self.export_errors[pair] = str(e)
            return False
        if self.export_errors.pop(pair, None) is not None:
            log.info("%s v%s export files written", pair, rec["version"])
        return True

    def retry_exports(self) -> None:
        """Export again the pairs whose files were blocked, at their latest version."""
        for pair in list(self.export_errors):
            rec = self.get(pair)
            if rec is None:
                self.export_errors.pop(pair, None)
            else:
                self._try_export(rec)

    def _export(self, rec: dict) -> None:
        if rec.get("excel"):            # Excel writes this pair's files; never overwrite them
            return
        dirs = {k: self.store.path_from_config(f"{k}_dir") for k in ("fenics", "frontarena", "dc_spread", "history")}
        # a history copy for manual publishes, as the Excel macro made (empty history_dir: none); the
        # pair's history.jsonl keeps every version anyway
        copy = rec.get("source") == "manual" and bool(self.store.config().get("history_dir"))
        exports.write_exports(rec, resolve(rec), dirs, history=copy, fa_ccy=self._fa_ccy())

    def _fa_ccy(self) -> dict:
        """Currencies Front Arena knows under another code (config frontarena_ccy, e.g. CNH -> CNY)."""
        m = self.store.config().get("frontarena_ccy")
        return {str(k).upper(): str(v).upper() for k, v in m.items()} if isinstance(m, dict) else {}

    # ---- pairs marked in Excel ------------------------------------------
    def _excel_dir(self) -> Path:
        cfg = self.store.config()
        return self.store.path_from_config("excel_dir" if cfg.get("excel_dir") else "frontarena_dir")

    def poll_excel(self, now: float | None = None) -> None:
        """Pairs marked in Excel: read a newer export file in as a new version
        (one app does it, the leader). Other pairs: note when their file was
        changed outside VolMarker, i.e. Excel still exports it too."""
        now = time.time() if now is None else now
        d = self._excel_dir()
        foreign = {}
        with self._lock:
            recs = list(self.cache.values())
        for rec in recs:
            c1, c2 = exports.fa_pair(rec["pair"], self._fa_ccy())
            path = d / f"{c1}{c2}volsurface.txt"
            try:
                mt = path.stat().st_mtime
            except OSError:
                continue
            if rec.get("excel"):
                if mt > (rec.get("excel_mtime") or 0) + 1e-6 and now - mt >= 2 and self._leader:
                    self._import_excel(rec["pair"], path, mt)        # 2 s: Excel may still be writing it
            elif mt > datetime.fromisoformat(rec["ts"]).timestamp() + 30 and not self._own_export(rec["pair"], path, mt):
                foreign[rec["pair"]] = mt
        if foreign.keys() != self.excel_foreign.keys():
            self._notify({"type": "alerts", "new": [], "cleared": [], "count": len(self.alerts.active)})
        self.excel_foreign = foreign

    def _own_export(self, pair: str, path: Path, mtime: float) -> bool:
        """The file is newer than the version, but is it what VolMarker writes for the
        pair's latest version? (written late after being blocked, by a propagation this
        app hasn't heard of yet, or the share's clock differs from the PCs'.) Checked
        once per file change."""
        key = (path, mtime)
        if key not in self._own_checked:
            try:
                rec = self.store.latest(pair)
                own = rec is not None and path.read_bytes() == \
                    exports.frontarena_text(rec, resolve(rec), self._fa_ccy()).encode("utf-8")
            except (OSError, ValueError, KeyError):
                return False                     # unreadable just now: ask again next time
            self._own_checked = {k: v for k, v in self._own_checked.items() if k[0] != path}
            self._own_checked[key] = own
        return self._own_checked[key]

    def _import_excel(self, pair: str, path: Path, mtime: float) -> dict | None:
        try:
            marks = exports.read_frontarena(path.read_text(encoding="utf-8", errors="replace"), pair,
                                            fa_ccy=self._fa_ccy())
        except (OSError, ValueError) as e:
            if self.excel_errors.get(pair) != str(e):
                log.warning("Excel file %s: %s", path, e)
            self.excel_errors[pair] = str(e)
            return None
        self.excel_errors.pop(pair, None)
        local = datetime.fromtimestamp(mtime).strftime("%d.%m. %H:%M:%S")
        with self.store.lock(pair, self.user):
            latest = self.store.latest(pair)
            if not latest or not latest.get("excel") or (latest.get("excel_mtime") or 0) >= mtime:
                return None
            state = {k: copy.deepcopy(latest.get(k)) for k in STATE_KEYS}
            for t in TENORS:
                row, m = state["tenors"][t], marks[t]
                row.update(atm=m["atm"], spread=m["spread"], rr25=m["rr25"], rr10=m["rr10"],
                           bf25=m["bf25"], bf10=m["bf10"])
                for w in ("rr", "bf"):
                    if abs(m[f"{w}25"]) > 1e-12:
                        row[f"{w}10f"] = m[f"{w}10"] / m[f"{w}25"]
            state, warnings = self._derive(clean_state(state))
            rec = self._append(state, warnings, latest["version"], f"Excel export {local} ({path.name})",
                               "excel", marked=True, user="Excel",
                               marked_ts=datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds"),
                               extra={"excel_mtime": mtime, "excel_file": str(path)})
        self._schedule(pair, "excel")
        return rec

    # ---- propagation to derived pairs and pairs moving with their parent --
    # A publish saves and exports the pair itself and queues its dependents; a
    # worker thread recomputes them (start_propagation). Without the worker
    # (tests, scripts) this happens straight away. Moves of the same pair
    # waiting in the queue collapse into one, computed from the latest versions.
    THROTTLED = ("market", "reconcile")      # FMD-driven moves and catch-ups: min_change / min_interval_s

    def _schedule(self, pair: str, source: str, kind: str = "deps") -> None:
        """kind "deps": recompute the pairs that depend on `pair`; "self": `pair` itself, then its dependents."""
        if self._prop_thread is None:
            self._run_propagation(kind, pair, source)
            return
        with self._prop_cv:
            prev = self._prop_pending.get((kind, pair))
            # a manual publish is never throttled, even if an FMD move of the same pair is waiting
            self._prop_pending[(kind, pair)] = source if prev is None or prev in self.THROTTLED else prev
            self._prop_cv.notify()

    def start_propagation(self) -> None:
        def loop():
            while not self._stop.is_set():
                with self._prop_cv:
                    now = time.time()
                    for dep, (due, src) in list(self._prop_deferred.items()):
                        if due <= now:
                            del self._prop_deferred[dep]
                            self._prop_pending.setdefault(("self", dep), src)
                    if not self._prop_pending:
                        nxt = min((d for d, _ in self._prop_deferred.values()), default=now + 1.0)
                        self._prop_cv.wait(max(0.05, min(1.0, nxt - now)))
                        continue
                    (kind, pair), source = next(iter(self._prop_pending.items()))
                    del self._prop_pending[(kind, pair)]
                    self._prop_busy = True       # set while still holding the lock: idle checks can't miss it
                try:
                    self._run_propagation(kind, pair, source)
                except Exception:
                    log.exception("propagation from %s failed", pair)
        self._prop_thread = threading.Thread(target=loop, daemon=True, name="propagation")
        self._prop_thread.start()

    def propagation_idle(self, timeout: float = 10.0) -> bool:
        """Wait until nothing is queued (deferred moves aside); for tests and shutdown."""
        end = time.time() + timeout
        while time.time() < end:
            with self._prop_cv:
                if not self._prop_pending and not self._prop_busy:
                    return True
            time.sleep(0.02)
        return False

    def _run_propagation(self, kind: str, pair: str, source: str) -> None:
        self._prop_busy = True
        try:
            if kind == "self":
                self._recompute(pair, None, source)
            self._propagate(pair, source)
        finally:
            self._prop_busy = False

    @staticmethod
    def _ups(r: dict) -> list[str]:
        """The pairs this one is computed from."""
        return (list(r.get("bases") or []) if r["mode"] == "derived" else []) + \
               ([r["spread_ref"]] if r.get("follow_parent") and r.get("spread_ref") else [])

    def _downstream(self, pair: str) -> list[str]:
        """Every pair computed (directly or not) from `pair`, each after the
        pairs it is computed from: EURCHF from EURUSD and USDCHF comes after USDCHF."""
        with self._lock:
            ups = {p: self._ups(r) for p, r in self.cache.items()}
        down, stack = set(), [pair]
        while stack:
            p = stack.pop()
            for q, u in ups.items():
                if p in u and q not in down and q != pair:
                    down.add(q)
                    stack.append(q)
        order, done = [], set()
        def visit(p, path=()):
            if p in done or p in path:
                return
            for u in ups.get(p, []):
                if u in down:
                    visit(u, path + (p,))
            done.add(p)
            order.append(p)
        for p in sorted(down):
            visit(p)
        return order

    def _propagate(self, pair: str, source: str = "manual") -> None:
        """Recompute every derived pair, and every pair moving with its parent, that uses `pair`."""
        for dep in self._downstream(pair):
            self._recompute(dep, pair, source)

    def _recompute(self, dep: str, cause: str | None, source: str) -> None:
        # decide without the lock (most FMD-driven follow-ups are skipped), lock only to write
        plan = self._plan_recompute(dep, source)
        if plan is None:
            return
        with self.store.lock(dep, self.user):
            plan = self._plan_recompute(dep, source)        # again: someone may have published meanwhile
            if plan is None:
                return
            latest, new, warnings = plan
            ups = (new.get("bases") or []) if new["mode"] == "derived" else [new.get("spread_ref")]
            self._append(new, warnings, latest["version"], f"{cause or ' / '.join(u for u in ups if u)} moved",
                         "propagation", marked=False)

    def _plan_recompute(self, dep: str, source: str):
        """(latest, new state, warnings) if `dep` should get a new version now, else None."""
        latest = self.store.latest(dep)
        if latest is None:
            return None
        new, warnings = self._derive({k: latest.get(k) for k in STATE_KEYS})
        if new["tenors"] == latest["tenors"]:
            return None
        if source in self.THROTTLED:
            pc = self.store.config().get("propagation") or {}
            def move(f):
                return max((abs((new["tenors"][t][f] or 0) - (latest["tenors"][t][f] or 0))
                            for t in new["tenors"] if t in latest["tenors"]), default=0.0)
            if move("atm") < pc.get("min_change", 0.005) and move("corr") < pc.get("min_corr_change", 0.005):
                return None             # too small to republish; it adds up until it isn't
            wait = pc.get("min_interval_s", 5) - (time.time() - datetime.fromisoformat(latest["ts"]).timestamp())
            if latest.get("source") == "propagation" and wait > 0:
                with self._prop_cv:     # taken later, from the then latest versions
                    self._prop_deferred.setdefault(dep, (time.time() + wait, source))
                return None
        return latest, new, warnings

    def reconcile(self) -> int:
        """Catch-up (leader, from housekeeping): a pair computed from others that
        is behind their latest versions gets recomputed, e.g. after an app was
        closed with work still queued. Returns how many were queued."""
        n = 0
        with self._lock:
            recs = {p: r for p, r in self.cache.items()}
        for p, r in recs.items():
            if r["mode"] == "derived":
                behind = any((recs.get(b) or {}).get("version") != (r.get("base_versions") or {}).get(b)
                             for b in r.get("bases") or [])
            elif r.get("follow_parent") and r.get("spread_ref") in recs:
                behind = recs[r["spread_ref"]]["version"] != r.get("parent_version")
            else:
                continue
            if behind and p not in self._prop_deferred:
                self._schedule(p, "reconcile", kind="self")
                n += 1
        return n

    def _check_cycle(self, state: dict) -> None:
        ups = self._ups
        if not ups(state):
            return
        with self._lock:
            graph = {p: ups(r) for p, r in self.cache.items()}
        graph[state["pair"]] = ups(state)
        stack, seen = list(graph[state["pair"]]), set()
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
            if e.get("kind") == "event_defaults":
                self._notify({"type": "event_defaults", "user": e.get("user")})
                continue
            if not e.get("pair"):
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
                        not self._leader_beating(cur):
                    # read only under this lock, so writing it in place when the share refuses a replace is safe
                    write_shared(path, json.dumps({"instance": self.instance, "heartbeat": time.time()}))
                    return True
                return False
        except LockTimeout:
            return False

    def _leader_beating(self, cur: dict) -> bool:
        """The leader's heartbeat moves. No hurry to take over: until it has been seen
        to move or stand still, only a clearly old heartbeat (any PC's clock) counts as gone."""
        hb = cur.get("heartbeat")
        b = self.live.beating("leader", hb, LEADER_STALE, value=(cur.get("instance"), hb))
        return b if b is not None else wall_alive(hb, LEADER_FIRST_SIGHT_S, time.time())

    def housekeeping(self) -> None:
        self._leader = self.is_leader()
        if not self._leader:
            self._rec_last = {}          # a later leadership starts with every quote again
            return
        self.reconcile()
        if self._archived_on != date.today():
            self._archived_on = date.today()
            self.archive_history()
        try:
            self.record_market()
        except Exception:
            log.exception("recording the market failed")
        if self._analysed_on != date.today():
            self._analysed_on = date.today()
            self.export_analysis()          # first: yesterday's event weights go to the history ...
            self.prune_past_events()        # ... then they leave the pairs
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
        self.expire_tenors()

    def prune_past_events(self, today: date | None = None) -> list[str]:
        """Once a day (the leader): each pair's own events on expiries before today, deleted
        ones included, removed with a new version. They no longer weigh on anything; the
        versions and the analysis events table keep them. Returns the pairs changed."""
        today = (today or date.today()).isoformat()
        done = []
        for pair in sorted(self.cache):
            rec = self.get(pair)
            evs = (rec.get("weights") or {}).get("events") or []
            keep = [e for e in evs if not (e.get("date") or today) < today]
            if len(keep) == len(evs):
                continue
            state = {k: copy.deepcopy(rec.get(k)) for k in STATE_KEYS}
            state["weights"]["events"] = keep
            try:
                self.publish(pair, state, rec["version"], f"{len(evs) - len(keep)} past event(s) removed", "event-prune")
                done.append(pair)
            except Exception as e:          # conflict etc. - retried tomorrow
                log.info("removing past events of %s deferred: %s", pair, e)
        return done

    def expire_tenors(self, today: date | None = None) -> list[str]:
        """The leader removes extra tenors on their removal date (a new version, so the
        exports drop them). Returns the pairs changed."""
        today = (today or date.today()).isoformat()
        done = []
        for pair in sorted(self.cache):
            rec = self.get(pair)
            gone = [t for t, until in (rec.get("extra_tenors") or {}).items() if until and until <= today]
            if not gone:
                continue
            state = {k: copy.deepcopy(rec.get(k)) for k in STATE_KEYS}
            for t in gone:
                state["extra_tenors"].pop(t)
                state["tenors"].pop(t, None)
            try:
                self.publish(pair, state, rec["version"], f"extra tenor {', '.join(gone)} removed (end date)",
                             "tenor-expiry")
                done.append(pair)
            except Exception as e:      # conflict etc. - retried next round
                log.info("tenor removal for %s deferred: %s", pair, e)
        return done

    def archive_history(self, today: date | None = None) -> int:
        """Once a day (the leader): whole months older than archive_keep_days
        move from each pair's history.jsonl to its monthly archive files.
        Nothing is deleted. Returns the number of versions moved."""
        keep = int(self.store.config().get("archive_keep_days") or 0)
        if keep <= 0:
            return 0
        first = ((today or date.today()) - timedelta(days=keep)).replace(day=1)
        cutoff = datetime(first.year, first.month, 1, tzinfo=timezone.utc)
        moved = 0
        for pair in self.store.list_pairs():
            if not self.store.archive_due(pair, cutoff):
                continue
            try:
                with self.store.lock(pair, self.user):
                    n = self.store.archive_pair(pair, cutoff)
                moved += n
                log.info("archived %d versions of %s from before %s", n, pair, first)
            except Exception:                # tried again tomorrow; nothing is lost either way
                log.exception("archiving %s failed", pair)
        return moved

    def export_analysis(self, today: date | None = None) -> list[date]:
        """Once a day (the leader): the finished days since the last export appended to
        the analysis tables (analysis_dir; empty = off). See volmarker.analysis."""
        if not self.store.config().get("analysis_dir"):
            return []
        try:
            days = Exporter(self.store).daily(today)
            if days:
                log.info("analysis tables: %s exported", ", ".join(d.isoformat() for d in days))
            return days
        except Exception:                    # tried again tomorrow; the state says where it got to
            log.exception("analysis export failed")
            return []

    def record_market(self, now: float | None = None) -> int:
        """The leader, every market_record.interval_min: the FMD quotes that changed
        since the last round, appended to <dir>/YYYY-MM.jsonl.gz (UTC month), one
        line per pair: {"t": time, "p": pair, "full": bool, "q": [[tenor, mark,
        ptype, brand, bid, offer, mid, quote time], ...]}. The first round of a local
        day (calendar_tz; and of a new leader) writes every quote ("full"), so a day
        can be read on its own. Unchanged quotes cost nothing. Returns the quotes written."""
        cfg = self.store.config().get("market_record") or {}
        every = float(cfg.get("interval_min") or 0) * 60
        now = time.time() if now is None else now
        if every <= 0 or now - self._rec_at < every:
            return 0
        self._rec_at = now
        t = datetime.fromtimestamp(now, timezone.utc)
        day = to_local(t, self.store.config().get("calendar_tz") or "Europe/Zurich").strftime("%Y-%m-%d")
        if day != self._rec_day:
            self._rec_day, self._rec_last = day, {}
        brands = set(cfg.get("brands") or [])
        with self._lock:          # FMD's quotes of every pair, and Bloomberg's of the pairs that prefer it
            market = {p: list(q) for p, q in self.market.items()}
            if self.bbg_alive():
                for p, q in self.market_bbg.items():
                    market[p] = market.get(p, []) + list(q)
        lines, n = [], 0
        for pair in sorted(market):
            last = self._rec_last.get(pair)
            cur, rows = {}, []
            for q in market[pair]:
                if brands and q.get("brand") not in brands:
                    continue
                key = (q.get("tenor"), q.get("mark"), q.get("ptype"), q.get("brand"))
                val = (q.get("bid"), q.get("offer"), q.get("mid"))
                cur[key] = val
                if last is None or last.get(key) != val:
                    rows.append([*key, *val, q.get("ts")])
            self._rec_last[pair] = cur
            if rows:
                lines.append(json.dumps({"t": iso(now), "p": pair, "full": last is None, "q": rows},
                                        separators=(",", ":")))
                n += len(rows)
        if lines:
            path = self.store.path_from_config_value(cfg.get("dir") or "market_history") / f"{t:%Y-%m}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            with self.store.lock("market-record", self.user), gzip.open(path, "ab") as f:
                f.write(("\n".join(lines) + "\n").encode("utf-8"))      # one gzip member per round
        return n

    def _journal_round(self, n: int) -> None:
        self.poll_journal()
        n += 1
        if n % 10 == 0:
            self.retry_exports()
            try:
                self.poll_excel()
            except Exception:
                log.exception("Excel file check failed")
        if n % 2 == 0:
            self.poll_market()
            try:
                self.evaluate_alerts()
            except Exception:
                log.exception("alert evaluation failed")

    def start_background(self) -> None:
        self.start_propagation()

        def journal_loop():
            n = 0
            while not self._stop.wait(0.5):
                try:
                    self._journal_round(n)
                except Exception:        # never let one bad round end the loop: the app would go deaf
                    log.exception("background poll failed")
                n += 1

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
        self.fenics.logout()
        self.bbg.stop("VolMarker closed")
