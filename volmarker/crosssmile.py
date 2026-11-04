"""Cross smiles from the two leg smiles: lognormal mixtures joined through a shared stress state.

Research code for scripts/cross_smile_backtest.py; the app doesn't use it (yet).

Leg fit. Each leg's smile at one expiry (ATM, 25D and 10D RR and BF) is fitted with two
lognormals under the leg's own pricing measure (its terms currency): a calm component and a
stress component with a higher vol and its own forward. Four parameters (stress weight p,
stress forward, the two vols) for five quotes, so the fit is close but not always exact;
fit_leg reports the misfit. BFs are broker (market) strangles by default, as the desk marks
them: the fit matches the strangle's price, and the RRs at the model's own smile.

Joining. Both legs are moved to the measure of the common currency (USD for CHFZAR from
USDZAR and USDCHF). A mixture of lognormals stays one under a change of numeraire: the
weights and the means move, nothing else. There each leg is calm or stressed, so there are
four joint states. Stress in both legs at once has probability p_common = max(lo, share *
min(p1, p2)), where lo = max(0, p1 + p2 - 1) is the least the two marginals allow; share = 1
puts as much of the stress in common as the marginals allow, independent stress is
share = max(p1, p2). Within a state the two log spots are jointly normal with the triangle
correlation rho_stress if both legs are stressed and rho_calm otherwise. Triangle
correlation as in derived.py: var(cross) = var1 + var2 - 2 * rho * vol1 * vol2.

Cross pricing. The cross's log spot is s1 * L1 + s2 * L2 (derived.orientation), normal within
each state. After a last change of measure to the cross's terms currency, a cross vanilla is a
weighted sum of Black prices: closed form, no integration.

Conventions (all simplifications of a vol-only backtest; the analysis tables carry no spots or
rates): every forward is 1, only log-moneyness matters; deltas are forward deltas; ATM is
delta-neutral straddle; BFs are broker strangles (bf="smile": smile BFs, 25D call and put vols
= ATM + BF +- RR / 2); the delta is premium-adjusted except for XXXUSD pairs with XXX in EUR, GBP, AUD, NZD (see
premium_adjusted). RRs are call - put in the pair's own terms. Vols are in percentage points
in and out, decimals inside.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

from .conventions import split_pair
from .derived import orientation

_N = NormalDist()
SQRT2 = math.sqrt(2.0)
PILLARS = ("10P", "25P", "ATM", "25C", "10C")
MARKS = ("atm", "rr25", "bf25", "rr10", "bf10")
NON_PA_BASES = {"EUR", "GBP", "AUD", "NZD"}


def ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / SQRT2)


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def premium_adjusted(pair: str) -> bool:
    """Premium-adjusted delta unless the premium is paid in the terms currency, which for the
    desk's universe means the XXXUSD pairs (EURUSD, GBPUSD, AUDUSD, NZDUSD)."""
    base, terms = split_pair(pair)
    return not (terms == "USD" and base in NON_PA_BASES)


# ---------------------------------------------------------------- Black (forward 1 by default)

def black(f: float, k: float, var: float, call: bool = True) -> float:
    """Undiscounted Black price; var is the total variance sigma^2 T."""
    if var <= 1e-16:
        return max(f - k, 0.0) if call else max(k - f, 0.0)
    sd = math.sqrt(var)
    d1 = (math.log(f / k) + 0.5 * var) / sd
    d2 = d1 - sd
    c = f * ncdf(d1) - k * ncdf(d2)
    return c if call else c - f + k


def vega(f: float, k: float, sd: float) -> float:
    """dPrice / d(sigma sqrt T)."""
    d1 = (math.log(f / k) + 0.5 * sd * sd) / sd
    return f * npdf(d1)


def implied_sd(price: float, k: float, call: bool, f: float = 1.0) -> float:
    """Total standard deviation sigma sqrt T of an undiscounted Black price (Newton, bisection fallback)."""
    intrinsic = max(f - k, 0.0) if call else max(k - f, 0.0)
    if price <= intrinsic + 1e-15:
        return 0.0
    lo, hi = 1e-6, 5.0
    x = math.sqrt(2 * math.pi) * price / f if abs(math.log(f / k)) < 0.05 else 0.5 * (lo + hi) * 0.1
    x = min(max(x, lo), hi)
    for _ in range(60):
        p = black(f, k, x * x, call) - price
        if abs(p) < 1e-14:
            return x
        if p > 0:
            hi = x
        else:
            lo = x
        v = vega(f, k, x)
        nx = x - p / v if v > 1e-14 else 0.5 * (lo + hi)
        x = nx if lo < nx < hi else 0.5 * (lo + hi)
        if hi - lo < 1e-12:
            break
    return x


# ---------------------------------------------------------------- strikes from deltas

def strike(delta: float, call: bool, sd: float, pa: bool) -> float:
    """Strike (forward 1) of a forward delta (0.25 for 25D), plain or premium-adjusted."""
    q = _N.inv_cdf(delta)
    if not pa:
        # call: N(d1) = delta; put: N(-d1) = delta
        x = -sd * q + 0.5 * sd * sd if call else sd * q + 0.5 * sd * sd
        return math.exp(x)
    if not call:
        # put: delta = K N(-d2), increasing in K: bisection on log K
        lo, hi = -12 * sd - 1, sd * sd
        for _ in range(100):
            m = 0.5 * (lo + hi)
            d2 = -m / sd - 0.5 * sd
            if math.exp(m) * ncdf(-d2) < delta:
                lo = m
            else:
                hi = m
        return math.exp(0.5 * (lo + hi))
    # call: delta = K N(d2), which rises then falls in K; take the root right of the maximum
    hi = -sd * q + 0.5 * sd * sd               # the plain-delta strike lies to the right of it
    f = lambda m: math.exp(m) * ncdf(-m / sd - 0.5 * sd)
    # maximum of K N(d2): N(d2) = n(d2) / sd; walk left from hi to bracket it
    lo = hi
    step = 0.25 * sd
    while f(lo) < delta:
        nlo = lo - step
        if f(nlo) <= f(lo):                     # passed the maximum: delta not reachable
            return math.exp(lo)
        lo = nlo
    for _ in range(100):
        m = 0.5 * (lo + hi)
        if f(m) > delta:
            lo = m
        else:
            hi = m
    return math.exp(0.5 * (lo + hi))


def atm_strike(sd: float, pa: bool) -> float:
    """Delta-neutral straddle strike (forward 1)."""
    return math.exp(-0.5 * sd * sd if pa else 0.5 * sd * sd)


def quotes_from_vols(vols: dict[str, float]) -> dict[str, float]:
    """ATM, RRs and smile BFs from the five pillar vols."""
    return {"atm": vols["ATM"],
            "rr25": vols["25C"] - vols["25P"], "bf25": (vols["25C"] + vols["25P"]) / 2 - vols["ATM"],
            "rr10": vols["10C"] - vols["10P"], "bf10": (vols["10C"] + vols["10P"]) / 2 - vols["ATM"]}


def _strike(pillar: str, sd: float, pa: bool) -> float:
    return atm_strike(sd, pa) if pillar == "ATM" else strike(int(pillar[:2]) / 100, pillar[2] == "C", sd, pa)


def _opt(k: float, vol: float, t: float, call: bool) -> tuple[float, bool, float, float]:
    """(strike, call, Black price at vol (pct), dPrice/dvol in vol points)."""
    sd = vol / 100 * math.sqrt(t)
    return k, call, black(1.0, k, sd * sd, call), max(vega(1.0, k, sd), 1e-12) * math.sqrt(t) / 100


def quote_targets(q: dict, t: float, pa: bool, bf: str = "broker", smile_bf: dict | None = None,
                  marks=MARKS) -> list[tuple]:
    """What a model price function must hit for the quotes q (pct), as a list of targets for
    quote_errors. RRs are matched at the 25D/10D strikes of the smile ATM + smile BF +- RR / 2;
    with broker BFs the smile BF isn't quoted, so it is smile_bf (the model's own, from a
    previous round) or, the first time, the broker BF. Broker BFs are matched as the price of
    the market strangle: both strikes at ATM + BF, priced at that one vol."""
    a, out = q["atm"], []
    for m in marks:
        if m == "atm":
            out.append(("atm", _opt(_strike("ATM", a / 100 * math.sqrt(t), pa), a, t, True)))
            continue
        d = int(m[2:])
        sbf = q[f"bf{d}"] if bf == "smile" or not smile_bf else smile_bf[d]
        if m.startswith("rr") or bf == "smile":
            legs = []
            for call, sign in ((True, 1), (False, -1)):
                v = a + sbf + sign * q[f"rr{d}"] / 2
                legs.append(_opt(strike(d / 100, call, v / 100 * math.sqrt(t), pa), v, t, call))
            out.append((m[:2], *legs))
        else:
            v = a + q[f"bf{d}"]
            sd = v / 100 * math.sqrt(t)
            out.append(("strangle", _opt(strike(d / 100, True, sd, pa), v, t, True),
                        _opt(strike(d / 100, False, sd, pa), v, t, False)))
    return out


def quote_errors(price, targets: list[tuple]) -> list[float]:
    """Model minus quote, in vol points, for each target (first-order in price)."""
    out = []
    for kind, *opts in targets:
        e = [(price(k, call) - p) / vg for k, call, p, vg in opts]
        if kind == "atm":
            out.append(e[0])
        elif kind == "rr":
            out.append(e[0] - e[1])
        elif kind == "bf":
            out.append((e[0] + e[1]) / 2)
        else:                                  # strangle: price of both over the vega of both
            (kc, cc, pc, vc), (kp, cp, pp, vp) = opts
            out.append((price(kc, cc) + price(kp, cp) - pc - pp) / (vc + vp))
    return out


# ---------------------------------------------------------------- Nelder-Mead (no numpy at the bank)

def nelder_mead(f, x0: list[float], step: float = 0.5, iters: int = 400, tol: float = 1e-10):
    n = len(x0)
    pts = [list(x0)] + [[x0[j] + (step if j == i else 0.0) for j in range(n)] for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=vals.__getitem__)
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol * (1 + abs(vals[0])):
            break
        c = [sum(p[j] for p in pts[:-1]) / n for j in range(n)]
        xr = [c[j] + (c[j] - pts[-1][j]) for j in range(n)]
        fr = f(xr)
        if fr < vals[0]:
            xe = [c[j] + 2 * (c[j] - pts[-1][j]) for j in range(n)]
            fe = f(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = [c[j] + 0.5 * (pts[-1][j] - c[j]) for j in range(n)]
            fc = f(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                for i in range(1, n + 1):
                    pts[i] = [pts[0][j] + 0.5 * (pts[i][j] - pts[0][j]) for j in range(n)]
                    vals[i] = f(pts[i])
    i = min(range(n + 1), key=vals.__getitem__)
    return pts[i], vals[i]


def _logistic(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x > -700 else 0.0


# ---------------------------------------------------------------- mixtures

@dataclass
class Mixture:
    """A mixture of lognormals at one expiry: (weight, forward, total sd) per component."""
    pair: str
    t: float
    comps: list[tuple[float, float, float]]

    @property
    def forward(self) -> float:
        return sum(w * f for w, f, _ in self.comps)

    def price(self, k: float, call: bool) -> float:
        return sum(w * black(f, k, sd * sd, call) for w, f, sd in self.comps)

    def vol(self, k: float) -> float:
        """Implied vol (pct) at strike k, against the mixture's own forward."""
        f = self.forward
        call = k >= f
        return implied_sd(self.price(k, call), k, call, f) / math.sqrt(self.t) * 100

    def smile_vols(self, pa: bool) -> dict[str, float]:
        """Vols at the model's own ATM DNS, 25D and 10D strikes (fixed point in vol)."""
        f, rt = self.forward, math.sqrt(self.t)
        out = {}
        for pl in PILLARS:
            v = self.vol(f)
            for _ in range(30):
                nv = self.vol(f * _strike(pl, v / 100 * rt, pa))
                if abs(nv - v) < 1e-7:
                    v = nv
                    break
                v = nv
            out[pl] = v
        return out

    def broker_bf(self, atm: float, d: int, pa: bool) -> float:
        """The market strangle BF the model prices: b with both strikes at ATM + b, priced at
        ATM + b, worth what the model says they are."""
        f, rt = self.forward, math.sqrt(self.t)

        def g(b):
            sd = (atm + b) / 100 * rt
            kc, kp = f * strike(d / 100, True, sd, pa), f * strike(d / 100, False, sd, pa)
            return (self.price(kc, True) + self.price(kp, False)
                    - black(f, kc, sd * sd, True) - black(f, kp, sd * sd, False))

        lo, hi = -0.5 * atm, 0.5 * atm
        while g(hi) > 0 and hi < 5 * atm:
            hi *= 2
        for _ in range(60):
            m = 0.5 * (lo + hi)
            if g(m) > 0:
                lo = m
            else:
                hi = m
        return 0.5 * (lo + hi)

    def quotes(self, pa: bool | None = None, bf: str = "broker") -> dict[str, float]:
        """ATM, RRs and BFs (pct) the model gives, at its own delta strikes."""
        pa = premium_adjusted(self.pair) if pa is None else pa
        q = quotes_from_vols(self.smile_vols(pa))
        if bf == "broker":
            for d in (25, 10):
                q[f"bf{d}"] = self.broker_bf(q["atm"], d, pa)
        return q


@dataclass
class Leg(Mixture):
    """One leg at one expiry: calm and stress lognormals in its own measure, forward 1."""
    rmse: float = 0.0                         # vol points over ATM, RRs and BFs

    @property
    def p(self) -> float:
        return self.comps[1][0]


def _leg_comps(x: list[float], s_atm: float):
    p = 0.5 * _logistic(x[0])
    fs = math.exp(x[1] * s_atm)
    if p * fs >= 0.999:
        return None
    fc = (1 - p * fs) / (1 - p)
    sc = s_atm * math.exp(x[2])
    ss = sc * (1 + math.exp(x[3]))
    return [(1 - p, fc, sc), (p, fs, ss)]


def _smile_bf(mix: Mixture, pa: bool) -> dict[int, float]:
    q = quotes_from_vols(mix.smile_vols(pa))
    return {25: q["bf25"], 10: q["bf10"]}


def fit_leg(pair: str, t: float, q: dict, pa: bool | None = None, bf: str = "broker") -> Leg:
    """Two-lognormal fit to one expiry's ATM, RR and BF quotes (pct) of a pair."""
    pa = premium_adjusted(pair) if pa is None else pa
    s_atm = q["atm"] / 100 * math.sqrt(t)
    tg = quote_targets(q, t, pa, bf)

    def err(x):
        comps = _leg_comps(x, s_atm)
        if comps is None or not all(f > 0 for _, f, _ in comps):
            return 1e6
        price = lambda k, call: sum(w * black(f, k, sd * sd, call) for w, f, sd in comps)
        e = sum(d * d for d in quote_errors(price, tg))
        # keep the flat-smile case from wandering: a weak pull towards p = 0.1, a modest stress vol
        return e + 1e-4 * ((x[0] + 1.4) ** 2 + x[3] ** 2 + x[1] ** 2 * 0.1)

    sign = 1.0 if q.get("rr25", 0) >= 0 else -1.0
    best = None
    for x0 in ([-1.4, sign, -0.05, 0.0], [-2.5, 2 * sign, -0.02, 0.5], [0.0, 0.5 * sign, -0.15, -0.5]):
        x, v = nelder_mead(err, x0, step=0.5, iters=600)
        x, v = nelder_mead(err, x, step=0.1, iters=400)
        if best is None or v < best[1]:
            best = (x, v)
    x = best[0]
    # broker BFs: match the RRs at the model's own smile, a couple of rounds
    for _ in range(2 if bf == "broker" else 0):
        tg = quote_targets(q, t, pa, bf, _smile_bf(Leg(pair, t, _leg_comps(x, s_atm)), pa))
        x, _ = nelder_mead(err, x, step=0.1, iters=400)
    leg = Leg(pair, t, _leg_comps(x, s_atm))
    got = leg.quotes(pa, bf)
    leg.rmse = math.sqrt(sum((got[m] - q[m]) ** 2 for m in MARKS) / len(MARKS))
    return leg


# ---------------------------------------------------------------- joining

@dataclass
class State:
    w: float
    mu: list[float]          # means of (L1, L2)
    cov: list[list[float]]   # 2x2


def _tilt(states: list[State], a: tuple[float, float]) -> list[State]:
    """Change of measure by exp(a . L), renormalised."""
    out = []
    for s in states:
        ca = [s.cov[0][0] * a[0] + s.cov[0][1] * a[1], s.cov[1][0] * a[0] + s.cov[1][1] * a[1]]
        m = math.exp(a[0] * s.mu[0] + a[1] * s.mu[1] + 0.5 * (a[0] * ca[0] + a[1] * ca[1]))
        out.append(State(s.w * m, [s.mu[0] + ca[0], s.mu[1] + ca[1]], s.cov))
    tot = sum(s.w for s in out)
    for s in out:
        s.w /= tot
    return out


def _leg_in(leg: Leg, common: str) -> list[tuple[float, float, float]]:
    """A leg's components as (weight, mean of log spot, sd) under the common currency's measure."""
    base, terms = split_pair(leg.pair)
    comps = [(w, math.log(f) - 0.5 * sd * sd, sd) for w, f, sd in leg.comps]
    if common == terms:
        return comps
    if common != base:
        raise ValueError(f"{common} is not in {leg.pair}")
    # measure of the base currency: density times X / F, i.e. weight * F_j, mean + sd^2
    out = [(w * math.exp(mu + 0.5 * sd * sd), mu + sd * sd, sd) for w, mu, sd in comps]
    tot = sum(c[0] for c in out)
    return [(w / tot, mu, sd) for w, mu, sd in out]


@dataclass
class Triangle:
    cross: str
    leg1: Leg
    leg2: Leg

    def __post_init__(self):
        self.s1, self.s2 = orientation(self.cross, self.leg1.pair, self.leg2.pair)
        common = set(split_pair(self.leg1.pair)) & set(split_pair(self.leg2.pair))
        if len(common) != 1:
            raise ValueError(f"{self.leg1.pair} and {self.leg2.pair} don't share one currency")
        self.common = common.pop()
        self.c1 = _leg_in(self.leg1, self.common)
        self.c2 = _leg_in(self.leg2, self.common)
        # the cross's terms currency: its price in the common currency is X_i or 1 / X_i
        terms = split_pair(self.cross)[1]
        self.tilt = [0.0, 0.0]
        for i, leg in enumerate((self.leg1, self.leg2)):
            b, tm = split_pair(leg.pair)
            if terms == b:
                self.tilt[i] = 1.0
            elif terms == tm:
                self.tilt[i] = -1.0
        self.t = self.leg1.t

    def p_common_bounds(self) -> tuple[float, float, float, float]:
        p1, p2 = self.c1[1][0], self.c2[1][0]
        return p1, p2, max(0.0, p1 + p2 - 1), min(p1, p2)

    def states(self, rho_calm: float, rho_stress: float, share: float | None) -> list[State]:
        """share None: independent stress (p_common = p1 p2)."""
        p1, p2, lo, hi = self.p_common_bounds()
        pc = p1 * p2 if share is None else max(lo, share * hi)
        probs = {(0, 0): 1 - p1 - p2 + pc, (1, 0): p1 - pc, (0, 1): p2 - pc, (1, 1): pc}
        sign = -self.s1 * self.s2                # corr(L1, L2) = -s1 s2 rho (triangle)
        out = []
        for (i, j), w in probs.items():
            if w <= 1e-14:
                continue
            _, m1, a = self.c1[i]
            _, m2, b = self.c2[j]
            r = sign * (rho_stress if (i and j) else rho_calm)
            out.append(State(w, [m1, m2], [[a * a, r * a * b], [r * a * b, b * b]]))
        return _tilt(out, tuple(self.tilt))

    def pricer(self, rho_calm: float, rho_stress: float, share: float | None) -> Mixture:
        comps = []
        for s in self.states(rho_calm, rho_stress, share):
            m = self.s1 * s.mu[0] + self.s2 * s.mu[1]
            v = max(s.cov[0][0] + s.cov[1][1] + 2 * self.s1 * self.s2 * s.cov[0][1], 1e-12)
            comps.append((s.w, math.exp(m + 0.5 * v), math.sqrt(v)))
        return Mixture(self.cross, self.t, comps)


# ---------------------------------------------------------------- calibration

def _bisect_rho(tri: Triangle, target: tuple, rho_of, share) -> float:
    """rho_calm that matches the cross ATM, with rho_stress = rho_of(rho_calm)."""
    k, call, mp, _ = target[1]
    lo, hi = -0.9999, 0.9999
    # the price falls as rho rises (less cross variance)
    for _ in range(60):
        m = 0.5 * (lo + hi)
        if tri.pricer(m, rho_of(m), share).price(k, call) > mp:
            lo = m
        else:
            hi = m
    return 0.5 * (lo + hi)


def _atm_target(tri: Triangle, q: dict, pa: bool | None):
    pa = premium_adjusted(tri.cross) if pa is None else pa
    return quote_targets(q, tri.t, pa, marks=("atm",))[0]


def calibrate_flat(tri: Triangle, q: dict, pa: bool | None = None) -> dict:
    """Baseline: one correlation in every state, stress independent across the legs; the
    correlation matches the cross ATM."""
    rho = _bisect_rho(tri, _atm_target(tri, q, pa), lambda r: r, None)
    return {"rho_calm": rho, "rho_stress": rho, "share": None}


def calibrate_fixed(tri: Triangle, q: dict, share: float, d_rho: float, pa: bool | None = None) -> dict:
    """Shared stress with given share and rho_stress - rho_calm; rho_calm matches the cross ATM."""
    clamp = lambda r: max(-0.9999, min(0.9999, r + d_rho))
    rho = _bisect_rho(tri, _atm_target(tri, q, pa), clamp, share)
    return {"rho_calm": rho, "rho_stress": clamp(rho), "share": share}


def calibrate_shared(tri: Triangle, q: dict, pa: bool | None = None, bf: str = "broker") -> dict:
    """Shared stress fitted to the cross's ATM, 25D RR and 25D BF: rho_calm, rho_stress and
    share. The 10D is left out of the fit, so it tests the model."""
    pa = premium_adjusted(tri.cross) if pa is None else pa
    tg = quote_targets(q, tri.t, pa, bf, marks=("atm", "rr25", "bf25"))
    start = calibrate_flat(tri, q, pa)["rho_calm"]

    def unpack(x):
        return math.tanh(x[0]), math.tanh(x[1]), _logistic(x[2])

    def err(x):
        pr = tri.pricer(*unpack(x))
        return sum(d * d for d in quote_errors(pr.price, tg)) + 1e-6 * x[2] ** 2

    a = math.atanh(max(-0.999, min(0.999, start)))
    best = None
    for x0 in ([a, a, 0.0], [a, a + 0.5, 2.0], [a, a - 0.5, -2.0]):
        x, v = nelder_mead(err, x0, step=0.3, iters=300)
        if best is None or v < best[1]:
            best = (x, v)
    x = best[0]
    for _ in range(2 if bf == "broker" else 0):
        tg = quote_targets(q, tri.t, pa, bf, _smile_bf(tri.pricer(*unpack(x)), pa), marks=("atm", "rr25", "bf25"))
        x, _ = nelder_mead(err, x, step=0.1, iters=300)
    rc, rs, sh = unpack(x)
    return {"rho_calm": rc, "rho_stress": rs, "share": sh}
