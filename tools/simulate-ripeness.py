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

With `--set bayes`, the estimators of drafts/bayesian-ripeness.md instead,
which keep a posterior per page, from the bytes lost and exposed, counted in
loss events: divided by the dispersion E[s^2]/E[s] of the events' sizes.

  posterior the single-rate Gamma posterior, discounted, ranked by its mean.
  gain      the same posterior, ranked by (a), the expected gain.
  median    the same posterior, ranked by (b), the probability of a gain.
  robust    the same posterior, ranked by (c): the least index the page would
            have after any number of further loss events, by its mean.
  option    the same posterior, ranked by (c'): cleaning once it gains, per
            epoch, what the option to wait is worth under the predictive of
            the losses within the posterior's memory, by its mean.
  cure-mean the static-share posterior, per chunk (a cure model), ranked by
            its means of the draining share and the rate.
  cure      the same, ranked by (a).
  cure-prob the same, ranked by (b).
  cure-opt  the same, ranked by (c'), by its means.

There, `tested` counts its test in loss events too.  A statement's payload,
a chunk, dies whole by default; with `--pieces w`, it loses its bytes in
pieces of w bytes, each dying at the chunk's rate, as a data page's `Ref`
loses the parts of its payload that writes supersede.  The Bayesian
estimators start from the scenario's rate at a weight of `--nu` epochs, or
with `--prior matched`, from the moments of its sources' posteriors, or with
`--prior empirical`, from the file's empirical prior, learned from other
pages' first epochs.  `--names` runs a subset of the estimators.

Usage:
    tools/simulate-ripeness.py compare [--items 32-256] [--beta 0.1] ...
    tools/simulate-ripeness.py compare --set bayes --pages 400 [--pieces 32] ...
    tools/simulate-ripeness.py compare --set bayes --prior empirical --names posterior,cure ...
    tools/simulate-ripeness.py sweep --beta 0.05,0.1,0.2 --c 1,2,3
    tools/simulate-ripeness.py example

Needs numpy, and scipy for `--set bayes` (`pip install numpy scipy`, in a
virtual environment if the system Python is externally managed).
"""

import argparse
import math

import numpy as np

PAGE = 4096
R_MIN = 1e-4
SOURCE_FILL = 0.5  # the fill of a source page whose posterior moved content brings

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
SETS = {
    "drafts": ("previous", "fit", "tested", "single"),
    "bayes": ("single", "posterior", "gain", "median", "robust", "option",
              "tested", "cure-mean", "cure", "cure-prob", "cure-opt"),
}
ESTIMATORS = SETS["drafts"]


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

    def step(self, t, L, x, x_before, **counts):
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

    def step(self, t, L, x, x_before, **counts):
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

    def step(self, t, L, x, x_before, **counts):
        beta, hit = self.o.beta, L > 0
        rho = self.rho * np.exp(-beta * (t - self.at)) - np.expm1(-beta) * L / np.maximum(x_before, 1e-12)
        self.rho = np.where(hit, rho, self.rho)
        self.at = np.where(hit, t, self.at)
        return h(x) / np.maximum(self.rho * np.exp(-beta * (t - self.at)), R_MIN)


# ----------------------------------------------------- Bayesian estimators

# phi(y) solves phi - ln(1 + phi) = y: the g(x*) of content at y = r/kappa.
_PHI = np.logspace(-8, 8, 4001)
_PHI_LY, _PHI_LP = np.log(_PHI - np.log1p(_PHI)), np.log(_PHI)


def phi(y):
    y = np.maximum(y, 1e-300)
    ly = np.log(y)
    out = np.exp(np.interp(ly, _PHI_LY, _PHI_LP))
    out = np.where(ly < _PHI_LY[0], np.sqrt(2 * y), out)
    return np.where(ly > _PHI_LY[-1], y + np.log1p(y), out)


# Phi_A(m) = E phi(G/m), G ~ Gamma(A, 1), by quadrature over G's quantiles,
# tabulated per page over ln m.
_QUANTILES = (np.arange(32) + 0.5) / 32
_LOG_M = np.linspace(-14, 14, 281)


def phi_tables(A):
    from scipy.special import gammaincinv
    q = gammaincinv(np.asarray(A)[:, None], _QUANTILES)
    return phi(q[:, None, :] / np.exp(_LOG_M)[None, :, None]).mean(-1)


def phi_at(tables, log_m):
    """Each page's table at its own points: log_m is (pages, points)."""
    f = np.clip((log_m - _LOG_M[0]) / (_LOG_M[1] - _LOG_M[0]), 0, len(_LOG_M) - 1.000001)
    i = f.astype(int)
    f -= i
    rows = np.arange(len(tables))[:, None]
    return tables[rows, i] * (1 - f) + tables[rows, i + 1] * f


def memory(o):
    """The epochs a discounted posterior remembers: 1 / (1 - e^-beta)."""
    return 1 / -math.expm1(-o.beta)


def predictive(A, B, exposure, j):
    """The negative-binomial predictive of j loss events over `exposure`
    (in events) under a Gamma(A, B) posterior on the rate, per page."""
    from scipy.special import gammaln
    A, j = np.maximum(A, 1e-6)[:, None], np.asarray(j, float)
    p = np.minimum(B / (B + exposure), 1 - 1e-16)[:, None]  # no exposure: no events
    return np.exp(gammaln(A + j) - gammaln(A) - gammaln(j + 1) + A * np.log(p) + j * np.log1p(-p))


def option_index(A, B, a, x, o):
    """(c'): the 1/kappa at which cleaning now gains, per epoch, at least what
    the option to wait is worth: the expected gain the page would forgo, by
    the posterior mean, if the losses of the posterior's memory, drawn from
    the predictive, left it unripe.  The draining share `a` drains at the
    rate A/B; after H epochs and J loss events, the posterior is discounted
    by delta^H, with the J events and H epochs' exposure added."""
    sd, H = o.dispersion, memory(o)
    delta = math.exp(-o.beta)
    decay = delta ** H
    kept = (1 - decay) / (H * (1 - delta))  # an event's mean discount over H
    J = np.arange(max(int((a / sd).max()), 0) + 1)[None, :]
    aJ = a[:, None] - J * sd
    weights = predictive(A, B, a / sd * H, J) * (aJ >= 0)
    weights /= np.maximum(weights.sum(1, keepdims=True), 1e-300)
    AJ = decay * A[:, None] + J * kept
    BJ = decay * B[:, None] + (a[:, None] - J * sd / 2) * H * kept / sd
    rate, rateJ = A / B, AJ / np.maximum(BJ, 1e-300)
    aJ, xJ = np.maximum(aJ, 0), x[:, None] - (a[:, None] - np.maximum(aJ, 0))

    def F(lk):
        kappa = np.exp(lk)
        now = (1 - x) - a * phi(rate / kappa)
        later = (1 - xJ) - aJ * phi(rateJ / kappa[:, None])
        return now - (weights * np.maximum(0, -later)).sum(1)

    return np.minimum(np.exp(-bisect(F, len(x), -20.0, 12.0)), h(x) / R_MIN)


def bisect(F, P, lo, hi, iters=40):
    """The root of F, increasing in its argument, per page."""
    lo, hi = np.full(P, lo), np.full(P, hi)
    for _ in range(iters):
        mid = (lo + hi) / 2
        up = F(mid) >= 0
        lo, hi = np.where(up, lo, mid), np.where(up, mid, hi)
    return (lo + hi) / 2


class Posterior:
    """One rate per page: a Gamma posterior over it, discounted by e^-beta
    per epoch, from the bytes lost and the live bytes exposed, both counted in
    loss events: divided by the dispersion of the events' sizes, E[s^2]/E[s]."""

    def __init__(self, prior, x, seed_age, o, rule, chunks):
        r0, a0, s0 = prior
        self.o, self.rule = o, rule
        delta, sd = math.exp(-o.beta), o.dispersion
        rate = r0 * a0 / np.maximum(a0 + s0, 1e-12)
        if seed_age:
            # The seed's past, watched: x e^(r k) of a page live k epochs ago.
            k = np.arange(seed_age)[None, :]
            self.B = (delta ** k * x[:, None] * np.exp(rate[:, None] * k)).sum(1) / sd
            self.A = rate * self.B
        elif o.start is not None:
            self.A, self.B = (v.copy() for v in o.start["single"])
        else:
            self.B = o.nu * x / sd
            self.A = rate * self.B
        self.I, self.at = self.index(x, self.A), np.zeros(len(x))

    def index(self, x, A):
        from scipy.special import gammaincinv
        A, B = np.maximum(A, 1e-6), self.B
        if self.rule == "mean":
            return h(x) * B / A
        if self.rule == "median":
            return h(x) * B / np.maximum(gammaincinv(A, 0.5), 1e-300)
        if self.rule == "gain":
            tables = phi_tables(A)
            g = (1 - x) / np.maximum(x, 1e-12)
            log_m = bisect(lambda lm: g - phi_at(tables, lm[:, None])[:, 0], len(x),
                           _LOG_M[0], _LOG_M[-1])
            return B / np.exp(log_m)
        # robust: the index by the mean after j more loss events, at once:
        # the least of them, or their median under the predictive of the
        # losses within the posterior's memory.
        sd = self.o.dispersion
        j = np.arange(max(int((x / sd).max()), 1))[None, :]
        xj = x[:, None] - j * sd
        ij = np.where(xj > 0, h(np.maximum(xj, 1e-9)) * B[:, None] / (A[:, None] + j), np.inf)
        if self.rule == "robust":
            return ij.min(1)
        return option_index(A, B, x, x, self.o)

    def step(self, t, L, x, x_before, chunks):
        delta, sd = math.exp(-self.o.beta), self.o.dispersion
        self.A = delta * self.A + L / sd
        self.B = delta * self.B + (x_before - L / 2) / sd
        hit = L > 0
        if hit.any():
            self.I = np.where(hit, self.index(x, self.A), self.I)
            self.at = np.where(hit, t, self.at)
        return np.minimum(self.I * np.exp(self.o.beta * (t - self.at)), h(x) / R_MIN)


class Cure:
    """A draining share over a static one, per chunk: each chunk drains with
    probability pi, and a draining chunk loses its bytes at the rate r, in
    loss events; a static chunk never loses a byte.  A chunk that has lost
    bytes is known to drain, and the posterior is a mixture over j, how many
    of the untouched chunks drain too.  The evidence is discounted, and so,
    with `--class-counts discounted`, the count of chunks known to drain."""

    def __init__(self, prior, x, seed_age, o, rule, chunks):
        r0, a0, s0 = prior
        self.o, self.rule = o, rule
        delta, sd = math.exp(-o.beta), o.dispersion
        P = len(x)
        pi0 = np.clip(a0 / np.maximum(a0 + s0, 1e-12), 0, 1)
        self.p0, self.q0 = o.nu_pi * pi0 + 0.5, o.nu_pi * (1 - pi0) + 0.5
        self.events = np.zeros(P)  # bytes lost, discounted, over the dispersion
        if seed_age:
            k = np.arange(seed_age)
            self.age = np.full(P, (delta ** k).sum())
            self.B0 = (delta ** k[None, :] * x[:, None] * np.exp(r0[:, None] * k[None, :])).sum(1) / sd
            # Chunks touched before open are known to drain, since their write.
            self.drained = chunks["touched"].astype(float)
            self.known = chunks["touched_bytes0"] * self.age
        else:
            self.age = np.zeros(P)
            self.B0 = o.nu * np.maximum(pi0 * x, sd) / sd
            self.drained, self.known = np.zeros(P), np.zeros(P)
        self.drained_whole = self.drained.copy()
        self.A0 = r0 * self.B0
        if not seed_age and o.start is not None:
            self.A0, self.B0 = (v.copy() for v in o.start["cure"])
        self.I, self.at = self.index(x, chunks), np.zeros(P)

    def posterior(self, chunks):
        from scipy.special import betaln, gammaln
        n = chunks["untouched"]
        j = np.arange(int(n.max()) + 1)[None, :]
        m = n[:, None]
        mean_chunk = chunks["untouched_bytes"] / np.maximum(n, 1)
        A = np.maximum(self.A0 + self.events, 1e-6)
        B = (self.B0 + self.known / self.o.dispersion)[:, None] \
            + j * (mean_chunk * self.age / self.o.dispersion)[:, None]
        drained = self.drained if self.o.class_counts == "discounted" else self.drained_whole
        lw = (gammaln(m + 1) - gammaln(j + 1) - gammaln(np.maximum(m - j, 0) + 1)
              - A[:, None] * np.log(np.maximum(B, 1e-300))
              + betaln((self.p0 + drained)[:, None] + j, self.q0[:, None] + np.maximum(m - j, 0)))
        lw = np.where(j <= m, lw, -np.inf)
        w = np.exp(lw - lw.max(1, keepdims=True))
        a = chunks["touched_live"][:, None] + j * mean_chunk[:, None]
        return A, B, w / w.sum(1, keepdims=True), j, a

    def index(self, x, chunks):
        from scipy.special import gammainc
        A, B, w, j, a = self.posterior(chunks)
        a = np.minimum(a, x[:, None])
        if self.rule in ("mean", "option"):
            a_mean = (w * a).sum(1)
            rate = (w * A[:, None] / B).sum(1)
            if self.rule == "mean":
                return index(a_mean, x - a_mean, rate, x)
            return option_index(A, A / np.maximum(rate, 1e-300), a_mean, x, self.o)
        if self.rule == "gain":
            tables = phi_tables(A)

            def F(lk):
                log_m = np.log(np.maximum(B, 1e-300)) + lk[:, None]
                return (1 - x) - (w * a * phi_at(tables, log_m)).sum(1)
        else:
            z = a / np.maximum(1 - x[:, None] + a, 1e-12)
            hz = np.where(a > 0, h(np.maximum(z, 1e-12)), np.inf)

            def F(lk):
                arg = np.minimum(B * np.exp(lk)[:, None] * hz, 1e300)
                return (w * gammainc(A[:, None], arg)).sum(1) - 0.5
        return np.exp(-bisect(F, len(x), -20.0, 12.0))

    def step(self, t, L, x, x_before, chunks):
        delta, sd = math.exp(-self.o.beta), self.o.dispersion
        self.A0, self.B0 = delta * self.A0, delta * self.B0
        # The exposure of bytes known to drain: those of chunks touched before
        # this epoch, all of this one; those of chunks it touches, since their
        # write; less half of what the epoch lost, all of it from such chunks.
        self.known = (delta * self.known + chunks["touched_live_before"]
                      + chunks["new_bytes0"] * (delta * self.age + 1) - L / 2)
        self.age = delta * self.age + 1
        self.events = delta * self.events + L / sd
        self.drained = delta * self.drained + chunks["new"]
        self.drained_whole = self.drained_whole + chunks["new"]
        hit = L > 0
        if hit.any():
            self.I = np.where(hit, self.index(x, chunks), self.I)
            self.at = np.where(hit, t, self.at)
        return np.minimum(self.I * np.exp(self.o.beta * (t - self.at)), h(x) / R_MIN)


def make(name, prior, x, seed_age, o, chunks):
    return {"previous": lambda: Previous(prior, x, seed_age, o),
            "fit": lambda: Fit(prior, x, seed_age, o, tested=False),
            "tested": lambda: Fit(prior, x, seed_age, o, tested=True),
            "single": lambda: Single(prior, x, seed_age, o),
            "posterior": lambda: Posterior(prior, x, seed_age, o, "mean", chunks),
            "gain": lambda: Posterior(prior, x, seed_age, o, "gain", chunks),
            "median": lambda: Posterior(prior, x, seed_age, o, "median", chunks),
            "robust": lambda: Posterior(prior, x, seed_age, o, "robust", chunks),
            "option": lambda: Posterior(prior, x, seed_age, o, "option", chunks),
            "cure-mean": lambda: Cure(prior, x, seed_age, o, "mean", chunks),
            "cure-opt": lambda: Cure(prior, x, seed_age, o, "option", chunks),
            "cure": lambda: Cure(prior, x, seed_age, o, "gain", chunks),
            "cure-prob": lambda: Cure(prior, x, seed_age, o, "prob", chunks)}[name]()


# -------------------------------------------------------------- simulation

def starting_priors(sc, P, o):
    """The Bayesian estimators' starting Gamma priors on the rate, per page:
    `matched` to the moments of the sources' posteriors, each a source page
    at SOURCE_FILL whose posterior has settled, or the file's `empirical` one."""
    if o.prior == "empirical":
        start = tuple(np.full(P, v) for v in o.empirical)
        return {"single": start, "cure": start}
    parts = sc.get("parts") or [(sc["prior"][1], sc["prior"][0], sc["prior"][2])]
    delta = math.exp(-o.beta)

    def matched(components):  # (bytes, rate) of each source
        w = np.array([c[0] for c in components], float)
        m = np.maximum([c[1] for c in components], R_MIN)
        w /= w.sum()
        var = m * o.dispersion * (1 - delta) / SOURCE_FILL  # m^2 / (the source's events)
        mean = (w * m).sum()
        spread = (w * (var + m ** 2)).sum() - mean ** 2
        return np.full(P, mean ** 2 / spread), np.full(P, mean / spread)

    draining = [(a, r) for a, r, s in parts if a > 0]
    single = matched(draining + [(s, 0.0) for a, r, s in parts if s > 0])
    return {"single": single, "cure": matched(draining) if draining else single}


def empirical_prior(o, epochs=10, seed=2):
    """The file's empirical-Bayes prior on a fresh page's rate: the method of
    moments over the first epochs' losses of pages of every scenario but the
    seeded one, drawn afresh; the rates' spread is what is left of the pages'
    spread once the Poisson noise of their losses is taken out."""
    o.warmup, events, exposure = epochs, [], []
    for key, (_, sc) in SCENARIOS.items():
        if not sc.get("seed_age"):
            k, e = run(key, o, seed)
            events.append(k)
            exposure.append(e)
    o.warmup = 0
    k, e = np.concatenate(events), np.concatenate(exposure)
    mean = k.sum() / e.sum()
    q = ((k - mean * e) ** 2 / e).sum()
    spread = (q - (len(k) - 1) * mean) / (e.sum() - (e ** 2).sum() / e.sum())
    spread = max(spread, mean ** 2 / 1000)
    return mean ** 2 / spread, mean / spread


def run(key, o, seed=1):
    """Excess cost of each estimator over the benchmark, for one scenario."""
    _, sc = SCENARIOS[key]
    rng = np.random.default_rng([seed, ord(key)])
    shares = sc["shares"]
    rates = np.array([r for _, r in shares])
    P, T, kappa = o.pages, o.epochs, o.kappa
    lo, hi = o.items

    # Each page: statements of lo..hi bytes, filling each share's quota.  A
    # statement's payload, a chunk, loses its bytes in pieces of `o.pieces`
    # bytes, each dying at the chunk's rate, or whole if `o.pieces` is 0.
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
    pieces = []
    for row in rows:
        split = []
        for c, (sz, i) in enumerate(row):
            w = o.pieces or sz
            split += [(w, i, c)] * int(sz // w) + ([(sz % w, i, c)] if sz % w else [])
        pieces.append(split)
    n = max(len(r) for r in pieces)
    n_chunks = max(len(r) for r in rows)
    size = np.zeros((P, n))
    share = np.full((P, n), -1)
    chunk = np.zeros((P, n), int)
    for p, row in enumerate(pieces):
        for j, (sz, i, c) in enumerate(row):
            size[p, j], share[p, j], chunk[p, j] = sz, i, c
    alive = size > 0
    death = 1 - np.exp(-rates[np.maximum(share, 0)])
    flat = (np.arange(P)[:, None] * n_chunks + chunk).ravel()

    def per_chunk(v):
        return np.bincount(flat, weights=v.ravel(), minlength=P * n_chunks).reshape(P, n_chunks)

    size0 = per_chunk(size) / PAGE
    exists = size0 > 0
    # The events' dispersion, E[s^2]/E[s], which the fold would measure.
    o.dispersion = (size ** 2).sum() / size.sum() / PAGE

    def held():
        return np.stack([(size * alive * (share == i)).sum(1) for i in range(len(shares))], 1) / PAGE

    seed_age = sc.get("seed_age", 0)
    for _ in range(seed_age):
        alive &= ~(rng.random(size.shape) < death)
    touched = exists & (per_chunk(size * ~alive) > 0)
    b = held()
    x = b.sum(1)
    o.statement = o.dispersion if o.set == "bayes" else (lo + hi) / 2 / PAGE

    def chunk_state(live, touched):
        untouched = exists & ~touched
        return {"untouched": untouched.sum(1),
                "untouched_bytes": (size0 * untouched).sum(1),
                "touched_live": (live * touched).sum(1)}

    live = per_chunk(size * alive) / PAGE
    chunks = chunk_state(live, touched)
    chunks.update(touched=(touched & (live > 0)).sum(1),
                  touched_bytes0=(size0 * touched * (live > 0)).sum(1))

    if seed_age:
        prior = (np.log(1 / x) / seed_age, x.copy(), np.zeros(P))
    else:
        p = sc.get("prior") or (mix_loss if o.mix == "loss" else mix_draft)(sc["parts"])
        prior = tuple(np.full(P, v) for v in p)
    o.start = (starting_priors(sc, P, o)
               if o.prior != "given" and not seed_age and not o.warmup else None)

    if o.warmup:
        # A fresh page's early losses and exposure, in events, for the file's
        # empirical prior.
        events, exposure = np.zeros(P), np.zeros(P)
        for _ in range(o.warmup):
            x_before = x
            dies = alive & (share >= 0) & (rng.random(size.shape) < death)
            L = (size * dies).sum(1) / PAGE
            alive &= ~dies
            x = held().sum(1)
            events += L / o.dispersion
            exposure += (x_before - L / 2) / o.dispersion
        return events, exposure

    names = o.names or SETS[o.set]
    est = {k: make(k, prior, x, seed_age, o, chunks) for k in names}

    future = np.array([future_cost(r, kappa) for r in rates])
    held_space = np.zeros(P)
    costs = []
    ripe = {k: np.full(P, -1) for k in ("true", *names)}
    for t in range(1, T + 1):
        held_space += kappa * (1 - x)
        x_before, live_before, touched_before = x, live, touched
        dies = alive & (share >= 0) & (rng.random(size.shape) < death)
        L = (size * dies).sum(1) / PAGE
        alive &= ~dies
        b = held()
        x = b.sum(1)
        costs.append(held_space + x + b @ future)
        live = per_chunk(size * alive) / PAGE
        new = exists & ~touched_before & (per_chunk(size * dies) > 0)
        touched = touched_before | new
        chunks = chunk_state(live, touched)
        chunks.update(new=new.sum(1), new_bytes0=(size0 * new).sum(1),
                      touched_live_before=(live_before * touched_before).sum(1))
        indexes = {k: e.step(t, L, x, x_before, chunks=chunks) for k, e in est.items()}
        indexes["true"] = true_index(b, rates, x)
        for k, i in indexes.items():
            ripe[k][(ripe[k] < 0) & (i >= 1 / kappa)] = t
    costs = np.stack(costs)

    def cost(k):
        return costs[np.where(ripe[k] > 0, ripe[k], T) - 1, np.arange(P)].mean()

    return {k: cost(k) / cost("true") - 1 for k in names}


def compare(o):
    names = o.names or SETS[o.set]
    if o.prior == "empirical":
        o.empirical = empirical_prior(o)
        print(f"the file's empirical prior on a fresh page's rate: Gamma({o.empirical[0]:.3g}, "
              f"{o.empirical[1]:.3g}), mean {o.empirical[0] / o.empirical[1]:.3g}")
    print(f"excess cost over cleaning at the true index; beta={o.beta}, n0={o.n0}, c={o.c}, "
          f"nu={o.nu}, nu_pi={o.nu_pi}, the {o.prior} prior, {o.class_counts} class counts, "
          f"statements of {o.items[0]}-{o.items[1]} bytes, "
          f"{f'losing pieces of {o.pieces} bytes' if o.pieces else 'dying whole'}, "
          f"the {o.mix} mix, {o.pages} pages")
    print(f"{'':52s}" + "".join(f"{k:>10s}" for k in names))
    total = dict.fromkeys(names, 0.0)
    for key, (name, _) in SCENARIOS.items():
        e = run(key, o)
        for k in names:
            total[k] += e[k]
        print(f"{key} {name:50s}" + "".join(f"{e[k]:10.1%}" for k in names), flush=True)
    print(f"{'sum':52s}" + "".join(f"{total[k]:10.1%}" for k in names))


def sweep(o, betas, cs, n0s):
    names = SETS[o.set]
    print(f"summed excess cost, statements of {o.items[0]}-{o.items[1]} bytes, the {o.mix} mix")
    print(f"{'beta':>6s} {'n0':>4s} {'c':>4s}" + "".join(f"{k:>10s}" for k in names))
    for beta in betas:
        for n0 in n0s:
            for c in cs:
                o.beta, o.n0, o.c = beta, n0, c
                total = dict.fromkeys(names, 0.0)
                for key in SCENARIOS:
                    for k, v in run(key, o).items():
                        total[k] += v
                print(f"{beta:6g} {n0:4d} {c:4g}" + "".join(f"{total[k]:10.1%}" for k in names),
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
    ap.add_argument("--set", choices=tuple(SETS), default="drafts")
    ap.add_argument("--pieces", type=int, default=0,
                    help="bytes a chunk loses at a time; 0 for whole chunks")
    ap.add_argument("--nu", type=float, default=3.0,
                    help="the prior's weight on the starting rate, in epochs of the page's bytes")
    ap.add_argument("--nu-pi", type=float, default=10.0,
                    help="the prior's weight on the draining fraction, in chunks")
    ap.add_argument("--class-counts", choices=("discounted", "whole"), default="discounted")
    ap.add_argument("--prior", choices=("given", "matched", "empirical"), default="given",
                    help="the Bayesian estimators' start: the scenario's rate at weight nu, "
                         "moment-matched to its sources' posteriors, or the file's empirical prior")
    ap.add_argument("--names", default="", help="estimators to run, in place of the set's")
    o = ap.parse_args()
    o.names = tuple(n for n in o.names.split(",") if n)
    o.start, o.warmup = None, 0
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
