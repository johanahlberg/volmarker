"""Business logic: publish, propagate to derived pairs, export, housekeeping."""

from __future__ import annotations

import copy
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
from .conventions import TENORS, normalise_pair
from . import bbg, weights_export
from .calendar import (DEFAULT_CUT, cumulative_weights, daily_dates, default_events, expiry_for,
                       implied_daily_weights, interpolate_atm, is_good_day, normalise_weights,
                       rule_weight, tenor_dates, with_defaults)
from .conventions import split_pair
from .fmd.book import daily_n
from .market import compare, market_view, parse_fix_time
from .store import LockTimeout, Store, atomic_write
from .surface import (ALERT_MARKS, FOLLOW_FIELDS, apply_derivation, check_surface, clean_state,
                      new_state, now_iso, resolve, skew_active)

log = logging.getLogger("volmarker")

STATE_KEYS = ["pair", "mode", "bases", "key", "skew_expiry", "weights", "atm_type",
              "lock_rr10f", "lock_bf10f", "follow_market", "alert_tol", "alerts_off", "group", "spread_ref", "spread_off", "cut", "dc_factor", "tenors"]
LEADER_STALE = 45.0
FEED_STALE = 15.0
FOLLOW_STALE = 30.0
SPREAD_SCAN = 5000      # versions read per pair for the spread history
MAX_TEST_DATES = 60


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
                "key": r.get("key", False), "group": r.get("group"), "spread_ref": r.get("spread_ref"), "version": r["version"],
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
        eff = self.effective_weights(pair, rec.get("weights"), cal_events, holidays, rec)
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
                events.append({"date": ev["date"], "desc": ev["desc"], "weight": 0.0})
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
        return with_defaults(pair, weights, cal_events, self.store.load_event_defaults()["rows"],
                             holidays, self.cut_of(rec or self.get(pair)))

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
        fx, ws = weights_export.frontarena_lines(pair, w, holidays, today, cfg["date_format"])
        items = weights_export.fenics_items(pair, w, holidays, today)
        last = self._weights_sent().get(pair)
        fp = weights_export.fingerprint(pair, w, holidays, today)
        return {"version": rec["version"], "fingerprint": fp, "last": last,
                "changed": not last or not last.get("ok") or last.get("fingerprint") != fp,
                "url": cfg["fenics_url"], "fenics": [dict(e, date=e["date"].isoformat()) for e in items],
                "fa_fx": fx, "fa_ws": ws, "weekdays": w["weekdays"],
                "holiday1": w["holiday1"], "holiday2": w["holiday2"]}

    def send_weights(self, pair: str, username: str, password: str) -> dict:
        """Write the Front Arena event files and post the weights to Fenics
        (published version). The password is only passed on, never stored."""
        rec, w, holidays, today = self._weights_target(pair)
        cfg = self.store.config()["weights_export"]
        fx, ws = weights_export.frontarena_lines(pair, w, holidays, today, cfg["date_format"])
        c1, c2 = split_pair(pair)
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

    def _weights_sent(self) -> dict[str, dict]:
        """Last send per pair (from the log)."""
        out: dict[str, dict] = {}
        log = self.store.root / "exports" / "weights_sent.jsonl"
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                    out[e["pair"]] = e
                except (ValueError, KeyError):
                    continue
        return out

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
                   if is_good_day(d, ccys, holidays)]

        def run(rec):
            atm = {t: rec["tenors"][t]["atm"] for t in TENORS}
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
        v = interpolate_atm(pair, {t: rec["tenors"][t]["atm"] for t in TENORS}, today, ds, w, holidays)
        vp = interpolate_atm(pair, {t: pub["tenors"][t]["atm"] for t in TENORS}, today, ds, wp, holidays)
        cum = cumulative_weights(pair, today, ds[-1], w, holidays)
        evs: dict[str, list] = {}
        for e in w["events"]:
            evs.setdefault(e["date"], []).append(e)
        tdates = tenor_dates(pair, today, holidays)
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
            after = [t for t in TENORS if tdates[t] >= d]
            rows.append({
                "date": iso, "weekday": d.strftime("%a"), "days": n,
                "weighted_days": round(cum[n], 3), "day_weight": round(cum[n] - cum[n - 1], 3),
                "good_day": is_good_day(d, ccys, holidays),
                "atm": vol, "published": vp.get(d), "fwd": fwd,
                "between": after[0] if after else TENORS[-1],
                "events": [f"{e['desc']} ({e['weight']:+g})" for e in evs.get(iso, [])],
            })
            prev_n, prev_tv = n, tv
        return {"rows": rows}

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

    def spread_history(self, pair: str, ref: str, tenor: str, days: float = 5.0,
                       points: int = 480) -> dict:
        """Published mid of `pair` minus `ref` per mark at one tenor, sampled on
        a regular time grid over the last `days` (each pair's value is the
        version live at that moment)."""
        if tenor not in TENORS:
            raise ValueError(f"Unknown tenor {tenor}")
        for p in (pair, ref):
            if p not in self.cache:
                raise ValueError(f"Unknown pair {p}")
        days = min(max(float(days), 0.05), 31.0)
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        truncated = False

        def steps(p: str) -> list[tuple[datetime, dict]]:
            nonlocal truncated
            out, recs = [], self.store.history(p, SPREAD_SCAN)      # newest first
            for r in recs:
                ts = datetime.fromisoformat(r["ts"])
                out.append((ts, resolve(r)[tenor]["mid"]))
                if ts <= start:
                    break
            else:
                truncated |= len(recs) == SPREAD_SCAN
            return out[::-1]

        a, b = steps(pair), steps(ref)
        step = (now - start) / (points - 1)
        out, ia, ib = [], -1, -1
        for k in range(points):
            t = start + step * k
            while ia + 1 < len(a) and a[ia + 1][0] <= t:
                ia += 1
            while ib + 1 < len(b) and b[ib + 1][0] <= t:
                ib += 1
            if ia < 0 or ib < 0:
                continue
            va, vb = a[ia][1], b[ib][1]
            out.append([t.isoformat(timespec="seconds"),
                        {m: None if va[m] is None or vb[m] is None else round(va[m] - vb[m], 4)
                         for m in ALERT_MARKS}])
        return {"pair": pair, "ref": ref, "tenor": tenor, "from": start.isoformat(timespec="seconds"),
                "to": now.isoformat(timespec="seconds"), "points": out, "truncated": truncated,
                "versions": sum(ts > start for ts, _ in a) + sum(ts > start for ts, _ in b)}

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
        dirs = {k: self.store.path_from_config(f"{k}_dir") for k in ("fenics", "frontarena", "dc_spread", "history")}
        # no history copy for the FMD follower's publishes (every 15 s); the pair's history.jsonl has them all
        exports.write_exports(rec, resolve(rec), dirs, history=rec.get("source") != "market")

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
