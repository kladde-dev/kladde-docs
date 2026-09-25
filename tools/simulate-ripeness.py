#!/usr/bin/env python3
"""Check how well a page's ripeness can be estimated from its own losses.

Simulates pages of statements, each statement belonging to a share that dies
at its own rate, and cleans each page once its *estimated* ripeness index
reaches 1/kappa.  The benchmark cleans it once its *true* index does: the
exact index of the shares the page actually holds, with the floor R_MIN
applied as the draft applies it.  A policy's cost, per page, is kappa per
epoch for each page of space that live content does not fill, plus the copy
of the survivors, plus what the survivors will cost under the optimal policy
afterwards; the tables print how much more each estimator costs than the
benchmark, over all pages of a scenario.

Estimators, all ranked the draft's way between losses (the split held, the
rate decaying by e^-beta per epoch):

  previous  the static-share draft's first estimator: a loss average, the
            bytes lost since the write, and psi(r t) = l t / D.
  fit       the discounted maximum-likelihood fit of l e^(r k) to the losses,
            with the starting estimate as n0 pseudo-epochs.
  tested    as fit, but keeping a static share only if it beats the fit of
            one draining share by `c` statements' worth of log-likelihood.
  single    the single-rate draft (branch `ripeness`): a discounted average
            of the fraction of live bytes lost per epoch.

Usage:
    tools/simulate-ripeness.py compare [--items 32-256] [--beta 0.1] ...
    tools/simulate-ripeness.py sweep --beta 0.05,0.1,0.2 --c 1,2,3
    tools/simulate-ripeness.py example

Needs numpy (`pip install numpy`, in a virtual environment if the system
Python is externally managed).
"""

import argparse
import math

import numpy as np

PAGE = 4096
R_MIN = 1e-4

# The scenarios: each page starts full (u0 = 1), with shares (size, rate); a
# rate of 0 is static.  `prior` is the page's starting estimate (r, a, s);
# `parts` are the sources a page was mixed from, as (a, r, s) each;
# `seed_age` makes the page that old at open, with nothing known of it.
SCENARIOS = {
    "A": ("one share at 0.05, prior exact", dict(shares=[(1.0, 0.05)], prior=(0.05, 1.0, 0.0))),
    "B": ("one share at 0.01, prior exact", dict(shares=[(1.0, 0.01)], prior=(0.01, 1.0, 0.0))),
    "C": ("one share at 0.2, prior 0.05", dict(shares=[(1.0, 0.2)], prior=(0.05, 1.0, 0.0))),
    "D": ("one share at 0.01, prior 0.05", dict(shares=[(1.0, 0.01)], prior=(0.05, 1.0, 0.0))),
    "E": ("0.3 at 0.1 over 0.7 static, prior exact",
          dict(shares=[(0.3, 0.1), (0.7, 0.0)], prior=(0.1, 0.3, 0.7))),
    "F": ("0.3 at 0.5, 0.3 at 0.02, 0.4 static, mixed",
          dict(shares=[(0.3, 0.5), (0.3, 0.02), (0.4, 0.0)],
               parts=[(0.3, 0.5, 0.0), (0.3, 0.02, 0.0), (0.0, 0.0, 0.4)])),
    "G": ("0.5 at 0.2, 0.5 at 0.002, mixed",
          dict(shares=[(0.5, 0.2), (0.5, 0.002)], parts=[(0.5, 0.2, 0.0), (0.5, 0.002, 0.0)])),
    "H": ("0.3 at 0.2, 0.3 at 0.02, 0.4 static, seeded at 30",
          dict(shares=[(0.3, 0.2), (0.3, 0.02), (0.4, 0.0)], seed_age=30)),
}
ESTIMATORS = ("previous", "fit", "tested", "single")


# ---------------------------------------------------------------- the rule

def h(x):
    x = np.clip(x, 1e-12, 1.0)
    return (1 - x) / x - np.log(1 / x)


# h inverted by table; near x = 1, h is computed from 1 - x to keep it exact
_D = np.concatenate([1 - np.logspace(-9, -1, 100000), np.logspace(-0.05, -6, 100000)])
_LOG_H = np.log(_D / (1 - _D) + np.log1p(-_D))[::-1]  # increasing
_X = (1 - _D)[::-1]


def x_star(y):
    """The fill x in (0, 1) at which h(x) = y."""
    y = np.asarray(y, float)
    return np.where(y <= 0, 1.0, np.interp(np.log(np.maximum(y, 1e-300)), _LOG_H, _X))


def true_index(b, rates, x):
    """The 1/kappa at which a page holding shares `b` (pages x shares) at
    `rates` becomes ripe: where 1 - x = sum_i b_i g(x*(r_i / kappa))."""
    lo, hi = np.full(len(x), -8.0), np.full(len(x), 25.0)  # ln(1/kappa)
    for _ in range(60):
        mid = (lo + hi) / 2
        F = sum(b[:, i] * (1 - xs) / xs for i in np.nonzero(rates > 0)[0]
                for xs in [x_star(rates[i] * np.exp(mid))])
        ripe = F < 1 - x
        lo, hi = np.where(ripe, mid, lo), np.where(ripe, hi, mid)
    index = np.where(b[:, rates > 0].sum(1) > 0, np.exp((lo + hi) / 2), np.inf)
    return np.minimum(index, h(x) / R_MIN)


def index(a, s, rate, x):
    """The draft's index for a draining share `a` at `rate` over a static `s`."""
    z = a / np.maximum(1 - s, 1e-12)
    with np.errstate(divide="ignore", invalid="ignore"):
        i = np.where((a > 0) & (rate > 0), h(z) / rate, np.inf)
    return np.minimum(i, h(x) / R_MIN)


def future_cost(r, kappa):
    """What one unit of content at rate r costs from its packing on, under the
    optimal policy; static content costs nothing more."""
    if r <= 0:
        return 0.0
    xs = float(x_star(r / kappa))
    return (kappa * (math.log(1 / xs) - (1 - xs)) / r + xs) / (1 - xs)


# ------------------------------------------------------ starting estimates

def mix_draft(parts):
    """The draft's: static shares add up, draining ones average by bytes."""
    a = sum(p[0] for p in parts)
    loss = sum(p[0] * p[1] for p in parts)
    return (loss / a if a else 0.0), a, sum(p[2] for p in parts)


def mix_loss(parts):
    """Match the mixture's loss rate and how fast it falls."""
    x = sum(p[0] + p[2] for p in parts)
    l1 = sum(p[0] * p[1] for p in parts)
    l2 = sum(p[0] * p[1] ** 2 for p in parts)
    if l1 <= 0:
        return 0.0, 0.0, x
    return l2 / l1, l1 * l1 / l2, x - l1 * l1 / l2


# ------------------------------------------------------------- estimators

def psi_inverse(y):
    w = np.linspace(1e-6, 60, 200000)
    return np.interp(-y, -(w / np.expm1(w)), w)


class Previous:
    def __init__(self, prior, x, seed_age, o):
        r0, a0, _ = prior
        self.o, self.seed_age = o, seed_age
        self.loss, self.fast, self.at = r0 * a0, a0.copy(), np.zeros(len(x))
        self.lost = (1 - x) if seed_age else np.zeros(len(x))

    def step(self, t, L, x, x_before):
        beta, hit = self.o.beta, L > 0
        age = self.seed_age + t
        loss = self.loss * np.exp(-beta * (t - self.at)) - np.expm1(-beta) * L
        self.loss = np.where(hit, loss, self.loss)
        self.at = np.where(hit, t, self.at)
        self.lost = self.lost + L
        slowed = self.loss * age / np.maximum(self.lost, 1e-12)
        rate = psi_inverse(np.minimum(slowed, 0.999999)) / age
        fast = np.where(slowed < 1, np.minimum(self.loss / rate, x), x)
        self.fast = np.minimum(np.where(hit, fast, self.fast), x)
        rate = self.loss * np.exp(-beta * (t - self.at)) / np.maximum(self.fast, 1e-12)
        return index(self.fast, x - self.fast, rate, x)


class Fit:
    """l e^(r k) fitted to the losses by discounted maximum likelihood."""

    R = np.linspace(0, 3, 3000)

    def __init__(self, prior, x, seed_age, o, tested):
        self.o, self.seed_age, self.tested = o, seed_age, tested
        self.tables = {}
        r0, a0, s0 = prior
        beta, n0 = o.beta, o.n0
        self.S0, self.S1 = np.zeros(len(x)), np.zeros(len(x))

        def observe(k, loss):  # a loss `k` epochs before now
            self.S0 += np.exp(-beta * k) * loss
            self.S1 += np.exp(-beta * k) * k * loss

        # The starting estimate, as the losses it predicts for the page's
        # first n0 epochs, observed a second time: at birth, they lie ahead.
        # A seeded page's estimate starts at its write, when it was full.
        start = np.ones(len(x)) if seed_age else a0
        for j in range(1, n0 + 1):
            observe(seed_age - j, start * np.exp(-r0 * (j - 1)) * -np.expm1(-r0))
        # A seeded page's past, observed to lose what the seed predicts.
        for j in range(1, seed_age + 1):
            observe(seed_age - j, start * np.exp(-r0 * (j - 1)) * -np.expm1(-r0))
        self.a, self.rate, self.at = a0.copy(), r0.copy(), np.zeros(len(x))

    def table(self, age):
        """The mean lag the model predicts at each rate, and ln E(r): the
        exposure is the epochs since the write, plus the n0 pseudo-epochs."""
        if age not in self.tables:
            n0, beta = self.o.n0, self.o.beta
            lags = np.concatenate([np.arange(age), age - np.arange(1, n0 + 1)])
            w = (self.R[:, None] - beta) * lags[None, :]
            top = w.max(1, keepdims=True)
            e = np.exp(w - top)
            self.tables[age] = ((e * lags).sum(1) / e.sum(1), np.log(e.sum(1)) + top[:, 0])
        return self.tables[age]

    def step(self, t, L, x, x_before):
        beta, hit = self.o.beta, L > 0
        age = self.seed_age + t
        self.S1 = np.exp(-beta) * (self.S1 + self.S0)
        self.S0 = np.exp(-beta) * self.S0 + L
        mean_lag, log_e = self.table(age)
        S0 = np.maximum(self.S0, 1e-300)
        r = np.interp(self.S1 / S0, mean_lag, self.R)
        loss = S0 / np.exp(np.interp(r, self.R, log_e))
        with np.errstate(divide="ignore", invalid="ignore"):
            a = np.where(r > 1e-9, loss / np.expm1(r), np.inf)
        one = a >= x
        rate = np.where(one, np.log1p(loss / np.maximum(x, 1e-12)), r)
        a = np.minimum(a, x)
        if self.tested:
            # The fit of one share draining alone, l = x (e^r - 1), and how
            # much worse it explains the losses, in statements.
            em = np.expm1(self.R[1:])[None, :]
            ll_one = (S0[:, None] * np.log(np.maximum(x[:, None] * em, 1e-300))
                      + self.R[None, 1:] * self.S1[:, None]
                      - x[:, None] * em * np.exp(log_e[1:])[None, :])
            best = ll_one.argmax(1)
            gain = S0 * np.log(np.maximum(loss, 1e-300)) + r * self.S1 - S0 \
                - ll_one[np.arange(len(x)), best]
            static = ~one & (gain / self.o.statement >= self.o.c)
            a = np.where(static, a, x)
            rate = np.where(static, rate, self.R[1:][best])
        self.a = np.where(hit, a, np.minimum(self.a, x))
        self.rate = np.where(hit, rate, self.rate)
        self.at = np.where(hit, t, self.at)
        return index(self.a, x - self.a, self.rate * np.exp(-beta * (t - self.at)), x)


class Single:
    """The single-rate draft: a discounted average of lost / live."""

    def __init__(self, prior, x, seed_age, o):
        r0, a0, s0 = prior
        self.o = o
        self.rho = r0 * a0 / np.maximum(a0 + s0, 1e-12)
        self.at = np.zeros(len(x))

    def step(self, t, L, x, x_before):
        beta, hit = self.o.beta, L > 0
        rho = self.rho * np.exp(-beta * (t - self.at)) - np.expm1(-beta) * L / np.maximum(x_before, 1e-12)
        self.rho = np.where(hit, rho, self.rho)
        self.at = np.where(hit, t, self.at)
        return h(x) / np.maximum(self.rho * np.exp(-beta * (t - self.at)), R_MIN)


# -------------------------------------------------------------- simulation

def run(key, o, seed=1):
    """Excess cost of each estimator over the benchmark, for one scenario."""
    _, sc = SCENARIOS[key]
    rng = np.random.default_rng([seed, ord(key)])
    shares = sc["shares"]
    rates = np.array([r for _, r in shares])
    P, T, kappa = o.pages, o.epochs, o.kappa
    lo, hi = o.items

    # Each page: statements of lo..hi bytes, filling each share's quota.
    rows = []
    for _ in range(P):
        row = []
        for i, (frac, _) in enumerate(shares):
            quota = frac * PAGE
            while quota > 16:
                size = min(rng.integers(lo, hi + 1), quota)
                row.append((size, i))
                quota -= size
        rows.append(row)
    n = max(len(r) for r in rows)
    size = np.zeros((P, n))
    share = np.full((P, n), -1)
    for p, row in enumerate(rows):
        for j, (sz, i) in enumerate(row):
            size[p, j], share[p, j] = sz, i
    alive = size > 0
    death = 1 - np.exp(-rates[np.maximum(share, 0)])

    def held():
        return np.stack([(size * alive * (share == i)).sum(1) for i in range(len(shares))], 1) / PAGE

    seed_age = sc.get("seed_age", 0)
    for _ in range(seed_age):
        alive &= ~(rng.random(size.shape) < death)
    b = held()
    x = b.sum(1)
    o.statement = (lo + hi) / 2 / PAGE

    if seed_age:
        prior = (np.log(1 / x) / seed_age, x.copy(), np.zeros(P))
    else:
        p = sc.get("prior") or (mix_loss if o.mix == "loss" else mix_draft)(sc["parts"])
        prior = tuple(np.full(P, v) for v in p)
    est = {"previous": Previous(prior, x, seed_age, o),
           "fit": Fit(prior, x, seed_age, o, tested=False),
           "tested": Fit(prior, x, seed_age, o, tested=True),
           "single": Single(prior, x, seed_age, o)}

    future = np.array([future_cost(r, kappa) for r in rates])
    held_space = np.zeros(P)
    costs = []
    ripe = {k: np.full(P, -1) for k in ("true", *ESTIMATORS)}
    for t in range(1, T + 1):
        held_space += kappa * (1 - x)
        x_before = x
        dies = alive & (share >= 0) & (rng.random(size.shape) < death)
        L = (size * dies).sum(1) / PAGE
        alive &= ~dies
        b = held()
        x = b.sum(1)
        costs.append(held_space + x + b @ future)
        indexes = {k: e.step(t, L, x, x_before) for k, e in est.items()}
        indexes["true"] = true_index(b, rates, x)
        for k, i in indexes.items():
            ripe[k][(ripe[k] < 0) & (i >= 1 / kappa)] = t
    costs = np.stack(costs)

    def cost(k):
        return costs[np.where(ripe[k] > 0, ripe[k], T) - 1, np.arange(P)].mean()

    return {k: cost(k) / cost("true") - 1 for k in ESTIMATORS}


def compare(o):
    print(f"excess cost over cleaning at the true index; beta={o.beta}, n0={o.n0}, c={o.c}, "
          f"statements of {o.items[0]}-{o.items[1]} bytes, the {o.mix} mix")
    print(f"{'':52s}" + "".join(f"{k:>10s}" for k in ESTIMATORS))
    total = dict.fromkeys(ESTIMATORS, 0.0)
    for key, (name, _) in SCENARIOS.items():
        e = run(key, o)
        for k in ESTIMATORS:
            total[k] += e[k]
        print(f"{key} {name:50s}" + "".join(f"{e[k]:10.1%}" for k in ESTIMATORS), flush=True)
    print(f"{'sum':52s}" + "".join(f"{total[k]:10.1%}" for k in ESTIMATORS))


def sweep(o, betas, cs, n0s):
    print(f"summed excess cost, statements of {o.items[0]}-{o.items[1]} bytes, the {o.mix} mix")
    print(f"{'beta':>6s} {'n0':>4s} {'c':>4s}" + "".join(f"{k:>10s}" for k in ESTIMATORS))
    for beta in betas:
        for n0 in n0s:
            for c in cs:
                o.beta, o.n0, o.c = beta, n0, c
                total = dict.fromkeys(ESTIMATORS, 0.0)
                for key in SCENARIOS:
                    for k, v in run(key, o).items():
                        total[k] += v
                print(f"{beta:6g} {n0:4d} {c:4g}" + "".join(f"{total[k]:10.1%}" for k in ESTIMATORS),
                      flush=True)


def example(o):
    """The draft's worked example, without noise: expected losses."""
    shares = SCENARIOS["F"][1]["shares"]
    rates = np.array([r for _, r in shares])
    prior = tuple(np.array([v]) for v in mix_draft(SCENARIOS["F"][1]["parts"]))
    o.statement = 144 / PAGE

    def live(t):
        return sum(a * math.exp(-r * t) for a, r in shares)

    est = {"previous": Previous(prior, np.array([1.0]), 0, o),
           "fit": Fit(prior, np.array([1.0]), 0, o, tested=False),
           "tested": Fit(prior, np.array([1.0]), 0, o, tested=True)}
    print(f"{'epoch':>5s} {'fill':>6s} {'true':>6s}" + "".join(f"{k:>9s}" for k in est))
    for t in range(1, 51):
        x = np.array([live(t)])
        L = np.array([live(t - 1) - live(t)])
        i = {k: e.step(t, L, x, None)[0] for k, e in est.items()}
        if t in (10, 20, 50):
            b = np.array([[a * math.exp(-r * t) for a, r in shares]])
            print(f"{t:5d} {x[0]:6.3f} {true_index(b, rates, x)[0]:6.0f}"
                  + "".join(f"{i[k]:9.0f}" for k in est))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("compare", "sweep", "example"))
    ap.add_argument("--beta", default="0.1")
    ap.add_argument("--n0", default="10", help="pseudo-epochs of the starting estimate")
    ap.add_argument("--c", default="3", help="statements a static share must earn")
    ap.add_argument("--items", default="32-256", help="statement sizes, in bytes")
    ap.add_argument("--mix", choices=("draft", "loss"), default="draft")
    ap.add_argument("--pages", type=int, default=800)
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--kappa", type=float, default=0.01)
    o = ap.parse_args()
    o.items = tuple(int(v) for v in o.items.split("-"))
    betas = [float(v) for v in o.beta.split(",")]
    cs = [float(v) for v in o.c.split(",")]
    n0s = [int(v) for v in o.n0.split(",")]
    o.beta, o.c, o.n0 = betas[0], cs[0], n0s[0]
    if o.command == "compare":
        compare(o)
    elif o.command == "sweep":
        sweep(o, betas, cs, n0s)
    else:
        example(o)


if __name__ == "__main__":
    main()
