"""Surface state and its resolution into mids and quoted (skewed) values.

A pair's stored state is a plain dict (it is what goes into the history
file). Per tenor the trader-owned inputs are:

    atm      ATM mid vol (marked pairs; on derived pairs this is the output)
    spread   ATM bid-offer spread in vol points
    rr25     25D risk reversal
    rr10f    10D/25D RR factor
    rr10     10D risk reversal
    bf25     25D butterfly
    bf10f    10D/25D BF factor
    bf10     10D butterfly
    corr     derived pairs only: triangle correlation of the two base pairs, as in Excel
    psprd    pairs moving with their spread parent: ATM minus the parent's ATM
    skew_atm, skew_rr, skew_bf   quoted-only shifts (Fenics file only)

Pair-level flags lock_rr10f / lock_bf10f (default True) decide which of the
10D value and its factor is the input:
    locked:    10D = 25D * factor          (factor typed, 10D follows the 25D)
    unlocked:  factor = 10D / 25D          (10D quote typed, factor calibrated)

Extra deltas (pair setting deltas, e.g. [5, 1] for steep wings, or 30) work like the 10D:
per tenor rr5, rr5f, bf5, bf5f, ..., with their locks in wing_locks ({"rr5": True, ...}).
Extra tenors (extra_tenors: {"3W": "2026-12-04"}) are marked like the standard ones until
their removal date, when the leader removes them (None: kept until removed by hand).

Other pair-level settings: group (sidebar group, e.g. "Scandies"), spread_ref
(spread parent: the grid shows ATM minus the parent's ATM), cut (option expiry
cut, e.g. NYC or TOK: decides which expiry an event weighs on), excel (marked
in Excel: read from Excel's export file, never written by VolMarker), dc_factor
(Front Arena DC spread file = ATM spread x 0.5 x factor; none below 0.01), follow_market (the feed process publishes FMD
mids into FOLLOW_FIELDS), alert_tol (per-mark alert tolerance overrides),
alerts_off, alerts_smile_off (mute the RR and BF alerts only, keep the ATM ones), follow_bases (derived pairs, default on: the ATMs move with the
bases at a fixed correlation; off: the ATMs stay put and the correlation is
recalculated as the bases move; always so on a derived pair marked in Excel or following FMD,
whose ATMs come from outside), rr_put (favour puts: the GUI shows and takes RRs as put - call;
stored and exported RRs stay call - put) and inverse (Excel's Quotation = Inverse: Fenics and FMD
quote the pair's RRs in the inverted pair's terms, e.g. NOKCHF as CHFNOK call - put, so the Fenics
export and the FMD market view flip their sign; stored RRs and Front Arena stay in the desk's terms).

Vols are in percentage points, as typed by the desk (7.25 means 7.25%).
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from datetime import date

from .conventions import (MAX_EXTRA_DELTAS, MAX_EXTRA_TENORS, TENORS, check_extra_delta, norm_extra_tenor,
                          normalise_pair, sort_tenors, split_pair, tenor_days)
from .derived import cross_vol, implied_corr, orientation

TENOR_FIELDS = ["atm", "spread", "rr25", "rr10f", "rr10", "bf25", "bf10f", "bf10",
                "corr", "psprd", "skew_atm", "skew_rr", "skew_bf"]

ALERT_MARKS = ["atm", "rr25", "rr10", "bf25", "bf10"]
# fields the market follower owns on a pair with follow_market (trader keeps spread, skew, weights)
FOLLOW_FIELDS = ["atm", "rr25", "rr10f", "rr10", "bf25", "bf10f", "bf10"]

DEFAULT_TENOR = {
    "atm": None, "spread": 0.5, "rr25": 0.0, "rr10f": 1.9, "rr10": None,
    "bf25": 0.2, "bf10f": 3.3, "bf10": None, "corr": None, "psprd": None,
    "skew_atm": 0.0, "skew_rr": 0.0, "skew_bf": 0.0,
}


# starting factors of an extra delta (to 25D), interpolated in delta; the desk sets its own
RR_FACTOR = {1: 4.0, 5: 2.6, 10: 1.9, 15: 1.5, 20: 1.2, 25: 1.0, 30: 0.8, 35: 0.6, 40: 0.4, 45: 0.2}
BF_FACTOR = {1: 12.0, 5: 6.0, 10: 3.3, 15: 2.1, 20: 1.4, 25: 1.0, 30: 0.7, 35: 0.45, 40: 0.25, 45: 0.1}


def default_factor(w: str, d: int) -> float:
    tab = RR_FACTOR if w == "rr" else BF_FACTOR
    ks = sorted(tab)
    lo = max(k for k in ks if k <= d)
    hi = min(k for k in ks if k >= d)
    return tab[lo] if lo == hi else round(tab[lo] + (tab[hi] - tab[lo]) * (d - lo) / (hi - lo), 2)


def extra_deltas(state: dict) -> list[int]:
    return sorted(state.get("deltas") or [], reverse=True)


def wing_deltas(state: dict) -> list[int]:
    """The deltas quoted as a factor of the 25D: 10 and the extra ones, inside out."""
    return sorted([10] + list(state.get("deltas") or []), reverse=True)


def all_deltas(state: dict) -> list[int]:
    return sorted([25, 10] + list(state.get("deltas") or []), reverse=True)


def all_marks(state: dict) -> list[str]:
    """ATM, the RRs and the BFs of every delta the pair marks: atm, rr30, rr25, rr10, rr5, bf30, ..."""
    ds = all_deltas(state)
    return ["atm"] + [f"rr{d}" for d in ds] + [f"bf{d}" for d in ds]


def wing_fields(d: int) -> list[str]:
    return [f"rr{d}f", f"rr{d}", f"bf{d}f", f"bf{d}"]


def follow_fields(state: dict) -> list[str]:
    """FOLLOW_FIELDS and the extra deltas' values and factors."""
    return FOLLOW_FIELDS + [f for d in extra_deltas(state) for f in wing_fields(d)]


def tenor_list(state: dict) -> list[str]:
    """The standard tenors and the pair's extra ones, by maturity."""
    return sort_tenors(TENORS + list(state.get("extra_tenors") or {}))


def is_locked(state: dict, w: str, d: int) -> bool:
    if d == 10:
        return state.get(f"lock_{w}10f") is not False
    return (state.get("wing_locks") or {}).get(f"{w}{d}") is not False


def tol_mark(m: str) -> str:
    """The standard mark whose alert tolerance an extra delta uses: the 10D's for wings
    beyond 25D, the 25D's for deltas inside it."""
    if m == "atm" or m in ALERT_MARKS:
        return m
    return m[:2] + ("10" if int(m[2:]) < 25 else "25")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_state(pair: str, mode: str = "marked", bases: list[str] | None = None) -> dict:
    ccy1, ccy2 = split_pair(pair)
    state = {
        "pair": ccy1 + ccy2,
        "mode": mode,
        "bases": bases if mode == "derived" else None,
        "skew_expiry": None,
        "lock_rr10f": True,
        "lock_bf10f": True,
        "tenors": {t: copy.deepcopy(DEFAULT_TENOR) for t in TENORS},
    }
    if mode == "derived":
        orientation(state["pair"], *bases)  # validates
    return state


def _num(x):
    return None if x is None or x == "" else float(x)


def clean_state(state: dict) -> dict:
    """Coerce types and fill missing fields; raises ValueError on bad input."""
    s = copy.deepcopy(state)
    s.setdefault("skew_expiry", None)
    s["atm_type"] = s.get("atm_type") or "DNS"
    if s["atm_type"] not in ("DNS", "ATMF"):
        raise ValueError("atm_type must be DNS or ATMF")
    for k in ("lock_rr10f", "lock_bf10f"):
        s[k] = True if s.get(k) is None else bool(s[k])
    if s.get("mode") not in ("marked", "derived"):
        raise ValueError("mode must be 'marked' or 'derived'")
    s["follow_market"] = bool(s.get("follow_market"))
    s["excel"] = bool(s.get("excel"))
    if s["excel"] and s["follow_market"]:
        raise ValueError("A pair can't be marked in Excel and follow FMD at the same time")
    deltas = sorted({check_extra_delta(d) for d in (s.get("deltas") or [])}, reverse=True)
    if len(deltas) > MAX_EXTRA_DELTAS:
        raise ValueError(f"At most {MAX_EXTRA_DELTAS} extra deltas")
    extra = {}
    for t, until in (s.get("extra_tenors") or {}).items():
        t = norm_extra_tenor(t)
        if t in extra:
            raise ValueError(f"Extra tenor {t} twice")
        extra[t] = date.fromisoformat(until).isoformat() if until else None
    if len(extra) > MAX_EXTRA_TENORS:
        raise ValueError(f"At most {MAX_EXTRA_TENORS} extra tenors")
    if s["excel"] and (deltas or extra):
        raise ValueError("A pair marked in Excel has the standard tenors and 25D/10D only "
                         "(remove its extra tenors and deltas first)")
    s["deltas"] = deltas or None
    s["extra_tenors"] = dict(sorted(extra.items(), key=lambda kv: tenor_days(kv[0]))) or None
    locks = s.get("wing_locks") or {}
    s["wing_locks"] = {f"{w}{d}": locks.get(f"{w}{d}") is not False for d in deltas for w in ("rr", "bf")} or None
    s["alerts_off"] = bool(s.get("alerts_off"))
    s["alerts_smile_off"] = bool(s.get("alerts_smile_off"))
    s["rr_put"] = bool(s.get("rr_put"))
    s["inverse"] = bool(s.get("inverse"))
    s["follow_bases"] = s.get("follow_bases") is not False
    s["group"] = " ".join(str(s.get("group") or "").split())[:40] or None
    s["follow_parent"] = bool(s.get("follow_parent"))
    if s["follow_parent"] and (not s["spread_ref"] or s["mode"] != "marked" or s["follow_market"] or s.get("excel")):
        raise ValueError("Move with parent needs a spread parent, on a marked pair that doesn't follow FMD "
                         "and isn't marked in Excel")
    s["cut"] = str(s.get("cut") or "NYC").strip().upper()[:8]
    dc = s.get("dc_factor")
    s["dc_factor"] = None if dc in (None, "") else float(dc)
    if s["dc_factor"] is not None and not 0 <= s["dc_factor"] <= 10:
        raise ValueError("DC spread factor must be between 0 and 10")
    ref = s.get("spread_ref")
    s["spread_ref"] = normalise_pair(ref) if ref else None
    if s["spread_ref"] == normalise_pair(s["pair"]):
        raise ValueError("The spread reference must be another pair")
    tol = {}
    for m, v in (s.get("alert_tol") or {}).items():
        if m not in ALERT_MARKS:
            raise ValueError(f"Unknown alert mark {m}")
        if v is not None and v != "":
            if float(v) <= 0:
                raise ValueError(f"Alert tolerance for {m} must be positive")
            tol[m] = float(v)
    s["alert_tol"] = tol or None
    if s["mode"] == "derived":
        bases = s.get("bases") or []
        if len(bases) != 2:
            raise ValueError("A derived pair needs exactly two base pairs")
        orientation(s["pair"], *bases)
    else:
        s["bases"] = None
    tenors = {}
    fields = TENOR_FIELDS + [f for d in deltas for f in wing_fields(d)]
    for t in tenor_list(s):
        row = dict(DEFAULT_TENOR)
        for d in deltas:
            row.update({f"rr{d}f": default_factor("rr", d), f"rr{d}": None,
                        f"bf{d}f": default_factor("bf", d), f"bf{d}": None})
        row.update((s.get("tenors") or {}).get(t, {}))
        for f in fields:
            row[f] = _num(row[f])
        for f in ("spread", "rr25", "bf25", "skew_atm", "skew_rr", "skew_bf"):
            if row[f] is None:
                row[f] = DEFAULT_TENOR[f]
        for d in [10] + deltas:
            for w in ("rr", "bf"):
                if row[f"{w}{d}f"] is None:
                    row[f"{w}{d}f"] = default_factor(w, d)
        if row["atm"] is not None and row["atm"] <= 0:
            raise ValueError(f"{t}: ATM must be positive")
        if row["spread"] < 0:
            raise ValueError(f"{t}: spread cannot be negative")
        for d in [10] + deltas:
            for w in ("rr", "bf"):
                # market mode: FMD quotes the 10D directly (it may even have the other sign than the 25D),
                # so the 10D is the input and the factor is for information only
                lock = is_locked(s, w, d) and not s["follow_market"] and not s["excel"]
                v25, f, vd = f"{w}25", f"{w}{d}f", f"{w}{d}"
                if lock or row[vd] is None:
                    if row[f] <= 0:
                        raise ValueError(f"{t}: {d}D {w.upper()} factor must be positive")
                    row[vd] = row[v25] * row[f]
                elif abs(row[v25]) > 1e-12:
                    row[f] = row[vd] / row[v25]
                # 25D of zero with an unlocked wing: factor undefined, keep the last one
        if not s["follow_parent"]:
            row["psprd"] = None
        if row["corr"] is not None and not -1 <= row["corr"] <= 1:
            raise ValueError(f"{t}: correlation must be between -1 and 1")
        tenors[t] = {f: row[f] for f in fields}
    s["tenors"] = tenors
    return s


def skew_active(state: dict, at: str | None = None) -> bool:
    exp = state.get("skew_expiry")
    if exp and (at or now_iso()) >= exp:
        return False
    return any(r.get("skew_atm") or r.get("skew_rr") or r.get("skew_bf")
               for r in state["tenors"].values())


def external(state: dict) -> bool:
    """Marks come from outside VolMarker: Excel's export file or the FMD follower."""
    return bool(state.get("excel") or state.get("follow_market"))


def apply_derivation(state: dict, base_states: dict[str, dict]) -> tuple[dict, list[str]]:
    """Compute ATM (or implied corr) of a derived state from its bases.

    For each tenor:
      * follow_bases off, or marks from Excel / FMD: ATM is kept as it is;
                     corr is set to the implied value (shown for information).
      * corr set:    ATM = triangle(base ATMs, corr).
      * corr unset but ATM typed: corr is calibrated to the ATM.
    Returns (new_state, warnings).
    """
    s = copy.deepcopy(state)
    warnings = []
    if s["mode"] != "derived":
        return s, warnings
    b1, b2 = s["bases"]
    hold = s.get("follow_bases") is False or external(s)
    orientation(s["pair"], b1, b2)  # validates
    st1, st2 = base_states.get(b1), base_states.get(b2)
    if st1 is None or st2 is None:
        missing = [b for b, st in ((b1, st1), (b2, st2)) if st is None]
        return s, [f"Base pair missing: {', '.join(missing)}"]
    for t in tenor_list(s):
        row = s["tenors"][t]
        if t not in st1["tenors"] or t not in st2["tenors"]:
            continue              # an extra tenor the bases don't mark: its ATM is typed
        v1, v2 = st1["tenors"][t]["atm"], st2["tenors"][t]["atm"]
        if v1 is None or v2 is None:
            warnings.append(f"{t}: base ATM missing")
            continue
        if hold or (row["corr"] is None and row["atm"] is not None):
            if row["atm"] is not None:
                rho = implied_corr(row["atm"], v1, v2)
                if not -1 <= rho <= 1:
                    warnings.append(f"{t}: ATM {row['atm']:.3f} implies correlation "
                                    f"{rho:.3f}, outside [-1, 1]")
                    rho = max(-1.0, min(1.0, rho))
                row["corr"] = rho
        elif row["corr"] is not None:
            try:
                row["atm"] = cross_vol(v1, v2, row["corr"])
            except ValueError as e:
                warnings.append(f"{t}: {e}")
    return s, warnings


def apply_parent(state: dict, parent: dict | None) -> tuple[dict, list[str]]:
    """A pair that moves with its spread parent: ATM = parent ATM + spread per
    tenor. A tenor whose spread is unset but whose ATM is typed gets the spread
    calibrated to it (like the correlation of a derived pair)."""
    s = copy.deepcopy(state)
    if not s.get("follow_parent"):
        return s, []
    if parent is None:
        return s, [f"Spread parent {s['spread_ref']} missing"]
    warnings = []
    for t in tenor_list(s):
        if t not in parent["tenors"]:
            continue              # an extra tenor the parent doesn't mark: its ATM is typed
        row, pa = s["tenors"][t], parent["tenors"][t]["atm"]
        if pa is None:
            warnings.append(f"{t}: {s['spread_ref']} has no ATM; ATM kept")
            continue
        if row["psprd"] is None:
            if row["atm"] is not None:
                row["psprd"] = row["atm"] - pa
        else:
            row["atm"] = pa + row["psprd"]
            if row["atm"] <= 0:
                warnings.append(f"{t}: {s['spread_ref']} + spread is not positive")
    return s, warnings


def calibrate(state: dict, base_states: dict[str, dict], targets: dict[str, float]) -> dict:
    """Set corr per tenor so the derived ATM matches `targets` (tenor -> vol)."""
    s = copy.deepcopy(state)
    for t, target in targets.items():
        s["tenors"][t]["atm"] = float(target)
        s["tenors"][t]["corr"] = None
    s, _ = apply_derivation(s, base_states)
    s, _ = apply_derivation(s, base_states)  # second pass turns corr into ATM
    return s


def resolve(state: dict, at: str | None = None) -> dict:
    """Per tenor mid and quoted values for display and export."""
    use_skew = skew_active(state, at)
    out = {}
    wings = wing_deltas(state)
    for t in tenor_list(state):
        r = state["tenors"][t]
        atm = r["atm"]
        k = 1.0 if use_skew else 0.0
        q_atm = None if atm is None else atm + k * r["skew_atm"]
        mid = {"atm": atm, "rr25": r["rr25"], "bf25": r["bf25"]}
        quoted = {
            "atm": q_atm,
            "atm_bid": None if q_atm is None else q_atm - r["spread"] / 2,
            "atm_offer": None if q_atm is None else q_atm + r["spread"] / 2,
            "rr25": r["rr25"] + k * r["skew_rr"], "bf25": r["bf25"] + k * r["skew_bf"],
        }
        for d in wings:
            for w in ("rr", "bf"):
                f = r.get(f"{w}{d}f")
                v = r.get(f"{w}{d}")
                if v is None:
                    v = r[f"{w}25"] * (f if f is not None else default_factor(w, d))
                mid[f"{w}{d}"] = v
                # a 25D skew moves the wing by skew * factor
                quoted[f"{w}{d}"] = v + k * r[f"skew_{w}"] * (f if f is not None else default_factor(w, d))
        out[t] = {"mid": mid, "quoted": quoted}
    return out


def check_surface(state: dict) -> list[str]:
    """Sanity warnings: missing marks, calendar arbitrage, bid below zero."""
    warnings = []
    prev_var, prev_t = None, None
    for t in tenor_list(state):
        r = state["tenors"][t]
        atm = r["atm"]
        if atm is None:
            warnings.append(f"{t}: no ATM")
            continue
        if atm - r["spread"] / 2 <= 0:
            warnings.append(f"{t}: ATM bid is not positive")
        var = atm ** 2 * tenor_days(t)
        if prev_var is not None and var < prev_var - 1e-9:
            warnings.append(f"{t}: total ATM variance below {prev_t} (calendar arbitrage)")
        prev_var, prev_t = var, t
    return warnings
