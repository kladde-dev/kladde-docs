---
title: Bayesian ripeness
---

**Status: a proposal, not adopted.**
It would replace how both variants of [Cleaning by ripeness](ripeness.md) estimate a page's drain and decide on it: the single-rate draft on branch `ripeness`, and the draft with a static share on branch `ripeness2`.
It assumes both drafts, and uses their notation: fills relative to $u_0$, $x$ for a page's fill, $a$ and $s$ for its draining and static shares, $z = a/(1 - s)$, $h$, $g$, $\kappa$, $\beta$, and $R_\text{min}$.

## Executive summary

**Keep a posterior over each page's drain rather than a point estimate, and decide from it: with a static share, that is what makes the static share pay.**
A page's estimate rests on little: a data page in kladde-bench's files holds 10 to 30 chunks, and the fall that would show a static share comes to a few of them ([Ripeness with a static share](../evaluation/ripeness2.md#how-often-pages-earn-a-static-share)).
The drafts cope with that by devices of their own, a forgetting rate, a weight for the starting estimate, a test, and a floor; here each becomes a parameter of one prior.

- **Evidence is counted in bytes, and weighed in loss events.**
  A page loses the bytes that writes supersede, on a data page often part of a `Ref`'s payload at a time; divided by the dispersion of the events' sizes, $\mathbb{E}[s^2]/\mathbb{E}[s]$, bytes lost and bytes exposed count as the independent observations they are.
- **With one rate per page, the posterior is a Gamma distribution, exact even for a rate that drifts, and its mean is exactly the single-rate draft's estimate.**
  What it adds is how certain the estimate is, and an explicit weight for the start.
- **With a static share, the posterior is a mixture over how many of the page's untouched chunks drain.**
  A chunk that has lost bytes is known to drain, and a prior on the draining fraction takes the place of the static-share draft's test.
- **How the decision reads the posterior matters little.**
  Deciding by the expected gain (a) or by the probability of a gain (b) differs from the posterior mean by about a point in simulation, except that (b) costs a few points more on pages of a few large chunks; (c), which guards against every loss a page could still suffer, is too cautious.
- **In simulation, the cure model's posterior costs half of what the static-share draft's fit does where chunks lose their bytes in parts, 11.8 % more than cleaning at the true index against 24.3 %, and 3 to 30 % less where they die whole.**
  It sees a static share almost exactly once chunks lose their bytes in parts, since a chunk's first loss shows that it drains.
  The single-rate posterior costs a point or two less than its draft.

These are simulations, costed by the drafts' own model; nothing has run on kladde-bench.

## The decision under a posterior

### The gain of cleaning now

**Cleaning a page now rather than one epoch later gains $\gamma = \kappa \, \bigl[(1 - x) - a \, \varphi(r/\kappa)\bigr]$ per epoch, where $\varphi$ solves $\varphi - \ln(1 + \varphi) = y$ for $y = r/\kappa$.**
Waiting one more epoch costs $\kappa \, (1 - x)$, the room the page's survivors would not fill, and saves what the bytes that die meanwhile would have cost: $r \, a$ of them, each of which would have been copied now and cost $V$ afterwards.
At its own threshold $x^*$, content of rate $r$ balances the two, so $r \, (1 + u_0 V) = \kappa \, g(x^*)$, and since $h = g - \ln(1 + g)$, $g(x^*)$ is $\varphi(r/\kappa)$.
$\gamma / \kappa$ is measured in pages: the room a cleaning frees, less the draining share weighted by what waiting would still save on it.
The page is ripe once $\gamma \geq 0$, which is the drafts' rule: $a \, \varphi(r/\kappa) \leq 1 - x$ exactly when $h(z) \geq r/\kappa$, and with one rate per page, $a = x$ and $z = x$.

$\varphi$ rises from 0, like $\sqrt{2y}$ for small $y$ and like $y + \ln(1 + y)$ for large $y$, and it is concave.

| $y = r/\kappa$ | 0.0001 | 0.01 | 1 | 10 | 100 |
| --- | --- | --- | --- | --- | --- |
| $\varphi(y)$ | 0.014 | 0.15 | 2.1 | 12.6 | 104.7 |

### Three ways to decide

**With a posterior over the page's parameters $\theta$, which are $r$ with one rate per page and $(r, a)$ with a static share, $\gamma$ becomes a random variable, and there are three ways to turn it into a decision.**

- **(a) The expected gain**: clean once $\mathbb{E}[\gamma] \geq 0$.
  This is what a decision-maker who is neutral to risk does; over a mixture posterior, it is the weighted sum of the components' expected gains.
- **(b) The probability of a gain**: clean once $P(\gamma \geq 0) \geq 1/2$, that is, once cleaning now is more likely than not to be right.
  The step function in $P(\gamma \geq 0) = \mathbb{E}[\mathbf{1}\{\gamma \geq 0\}]$ counts how often cleaning is right, not by how much.
- **(c) Ripe whatever it loses next**: clean once the page would be ripe by the posterior mean, or by (a), even after any number of further losses.
  [Waiting teaches](#waiting-teaches-and-the-drafts-rule-does-not-see-it) derives it: the other two clean at the first epoch the posterior calls a page ripe, which is early, because a page's next loss can make it unripe again.
  [In simulation](#checking-in-simulation), it is too cautious to pay.

**All three keep the drafts' ranking by an index, since each is monotone in $\kappa$.**
For every $\theta$, $\gamma/\kappa$ rises with $\kappa$, since $\varphi$ rises with $r/\kappa$; so $\mathbb{E}[\gamma]/\kappa$ and $P(\gamma \geq 0)$ rise with $\kappa$ too, and each criterion holds for every $\kappa$ above one price $\kappa^*$.
A page's index is $I = 1/\kappa^*$, the $1/\kappa$ at which it becomes ripe, as in the drafts, so the budget loop, the controller, and the floor stay as they are: a page is ripe once $I \geq 1/\kappa$, and the loop takes the highest index first.

**Between losses, the ranking ages each posterior's scale, as the drafts age a rate.**
A posterior changes every epoch, losses or not, but the ranking can only afford a change when a page changes.
So each page's index is computed exactly at each of its losses, and between them its rate is taken to fall by $e^{-\beta}$ per epoch in distribution: the posterior's scale shrinks and its shape stays.
Every criterion below then grows the index by the same factor $e^{\beta}$ per epoch, on every page alike, which is what the drafts' ordered sets need.
Where that differs from the exact update, it takes the posterior for more certain than it is between losses, which [the section on learning](#waiting-teaches-and-the-drafts-rule-does-not-see-it) shows to be the safe side.

## One rate per page

### Losses counted in bytes, weighed in loss events

**The observations are the bytes a page loses, weighed by how many independent events they came in: a write supersedes a range, and the bytes of one range are one observation, not many.**
The model is the single-rate draft's: a page's live bytes die at a rate $r$, a fraction $r$ of them per epoch, whatever their age.
But they do not die one at a time: a write supersedes a range, which on a data page is often part of a `Ref`'s payload, the rest of which lives on, and on a leaf is a whole statement.
So a page's losses are a *compound* Poisson process: events arriving at a rate proportional to $r$ and to the live bytes, each removing some $s$ bytes.
Over an *exposure* $E$, the live bytes times the epochs they were watched, the bytes lost $L$ have mean $r E$ and a variance larger than that by the factor $\sigma = \mathbb{E}[s^2]/\mathbb{E}[s]$, the dispersion of the events' sizes.
Divided by $\sigma$, $L$ and $E$ behave as a Poisson count and its exposure, and the likelihood of $r$ is

$$
p(L \mid r) \propto r^{L/\sigma} \, e^{-r E/\sigma},
$$

exact where the events are of one size, and $L/\sigma$ counts them, and a match of the first two moments otherwise.
Over an epoch in which a page starts with $x$ live and loses $L$, its exposure is $x - L/2$, counting what died as live for half the epoch; $x$, $L$, and $\sigma$ may be in pages or bytes, as long as all three are in the same unit.

- **Counting every byte as an observation** would make the posterior $\sigma$ times too sure: 250 times, for statements of 250 bytes that die whole.
- **Counting statements** would miss that a data page's statements lose their payloads in parts and live on, and would count a statement's many partial losses as none.

The fold knows each event's size, since it supersedes one range at a time, so it can keep $\mathbb{E}[s]$ and $\mathbb{E}[s^2]$ as running averages over the file, one pair for data pages and one for leaves.
On a leaf, whose statements die whole, an event is a statement's death, and $\sigma$ is the mean size of the statements that die, weighted by their size.
The dispersion sets only how sure the posterior is: the posterior mean below is a ratio of bytes lost to bytes exposed, the same in any unit.

### The exact posterior is a Gamma distribution

**A Gamma prior on the rate gives a Gamma posterior, whose two parameters count the page's loss events and its exposure, in events.**
With a prior $r \sim \mathrm{Gamma}(A_0, B_0)$, of density proportional to $r^{A_0 - 1} e^{-B_0 r}$, the posterior after losing $L$ over an exposure $E$ is

$$
r \mid \text{losses} \sim \mathrm{Gamma}\bigl(A_0 + L/\sigma, \; B_0 + E/\sigma\bigr),
$$

with mean $A/B$ and a relative spread of $1/\sqrt{A}$, where $A$ and $B$ are the posterior's two parameters.
$A$ is the number of loss events the estimate rests on, the prior's included: the prior acts as $A_0$ events over an exposure of $B_0$, and a starting estimate $r_0$ worth $\nu$ epochs of the page's $x_0$ is $B_0 = \nu \, x_0/\sigma$ and $A_0 = r_0 \, B_0$.

### A rate that drifts keeps the posterior exact

**A prior under which the rate drifts by random factors, of mean one, turns the update into a discounted one, and keeps it exact: $A \leftarrow \delta A + L/\sigma$ and $B \leftarrow \delta B + (x - L/2)/\sigma$ each epoch, with $\delta = e^{-\beta}$.**
The drafts forget old losses because a page's rate changes as its content does.
The Bayesian way to say so is a prior over the rate's path, and one prior makes the forgetting exact: between epochs, $r_t = r_{t-1} \, \eta_t / \delta$, with $\eta_t$ drawn from a $\mathrm{Beta}\bigl(\delta A, (1 - \delta) A\bigr)$ distribution independent of $r_{t-1}$, where $A$ is the posterior's shape before the step: the gamma–beta model of Smith and Miller, *A non-Gaussian state space model and application to prediction of records* (J. R. Stat. Soc. B, 1986), known in forecasting as the Poisson–gamma steady model with a discount factor.
A $\mathrm{Gamma}(A, B)$ variable times an independent $\mathrm{Beta}(\delta A, (1 - \delta) A)$ one is $\mathrm{Gamma}(\delta A, B)$, so the rate's prior for the next epoch is $\mathrm{Gamma}(\delta A, \delta B)$: the same mean, and a variance larger by $1/\delta$.
The epoch's losses then update it as above.
The drafts' forgetting rate $\beta$ becomes the prior's drift, its third parameter beside $A_0$ and $B_0$.

### The single-rate draft is this posterior's mean

**Once a page's exposure has settled, the posterior mean follows exactly the single-rate draft's update.**
With $x$ live, $B$ settles at $x/\bigl(\sigma (1 - \delta)\bigr)$, and then $(\delta A + L/\sigma)/B = \delta \, A/B + (1 - \delta) \, L/x$, which is the draft's `lose`.
The posterior adds two things.

- **Its mean is right while the exposure is still growing.**
  Until then, the mean is the ratio of the discounted losses to the discounted exposure, as Adam's bias correction would make it, but with the prior's weight, $\nu$ epochs, as an explicit parameter; the draft's start weighs about $1/\beta$ epochs, fixed.
- **Its shape counts the loss events the estimate rests on.**
  At the settled exposure, $A \approx r \, x / (\sigma \beta)$.
  A page at fill 0.5 draining at 0.01 per epoch, with $\beta = 0.1$, rests on 3.2 events if it loses 64 bytes at a time, and its rate is uncertain by 56 %; if it loses whole statements of 250 bytes, it rests on 0.8 of one.

### What a page keeps

**A page keeps $A$, $B$, and the epoch it last lost bytes, all updated in closed form: one number more than the single-rate draft.**
Over $d$ epochs without a loss, with $x$ live, $A \leftarrow \delta^d A$ and $B \leftarrow \delta^d B + x \, (1 - \delta^d)/\bigl(\sigma (1 - \delta)\bigr)$.

```rust
/// Per page, beside kind, epoch and coverage: 12 bytes.
struct Drain {
    a:  f32,   // the posterior's shape: loss events, discounted, with the prior's
    b:  f32,   // its rate: exposure in events, discounted, with the prior's
    at: u32,   // the epoch of the page's last natural loss, from the session's base
}

impl Drain {
    /// A flush's fold superseded `lost` of the page's `live` bytes; `sigma` is
    /// the dispersion of loss events on pages of its kind.
    fn lose(&mut self, lost: u32, live: u32, sigma: f32, now: u32) {
        let (lost, live) = (lost as f32 / sigma, live as f32 / sigma);
        // The epochs since the last loss, without one:
        let (delta, d) = ((-BETA).exp(), (now - self.at - 1) as f32);
        let decay = delta.powf(d);
        self.a *= decay;
        self.b = self.b * decay + live * (1.0 - decay) / (1.0 - delta);
        // This epoch:
        self.a = delta * self.a + lost;
        self.b = delta * self.b + live - 0.5 * lost;
        self.at = now;
    }
}
```

The dispersion drifts as the application's writes change, and a page's sums mix the values it had when they were added, which matters little since it moves slowly.

**A page starts from what it holds, as a prior; a page the consolidator state cannot vouch for starts from the seed, as a past it was watched through.**
A new page's prior has the drafts' starting rate $r_0$, the byte-weighted mean of its content's rates, and a weight of $\nu$ epochs: $B_0 = \nu \, x_0/\sigma$, $A_0 = r_0 \, B_0$.
The seed at open has the drafts' rate, and weighs what the page's past would have, had the page been watched through it losing bytes at that rate: $B$ is the discounted exposure of the bytes it would have held, over $\sigma$, and $A = \hat r \, B$.

**The floor becomes a rate that no content falls below.**
Adding $R_\text{min}$ times each epoch's exposure to its loss events asserts a background rate $R_\text{min}$ for all content, and keeps the posterior mean at or above it; it changes every draining page's rate by $R_\text{min}$, which is negligible, and it caps a frozen page's index as the drafts' floor does.

### Deciding on one rate

**With $r = G/B$ and $G \sim \mathrm{Gamma}(A, 1)$, every criterion reduces to the drafts' index with an effective rate that depends on the posterior's shape $A$.**

- **The posterior mean**, for comparison: $I_0 = h(x) \, B/A$.
- **(a)** $\mathbb{E}[\gamma]/\kappa = (1 - x) - x \, \Phi_A(B \kappa)$, with $\Phi_A(m) = \mathbb{E}\bigl[\varphi(G/m)\bigr]$.
  The page is ripe once $\Phi_A(B\kappa) \leq g(x)$, so $I_a = B / m^*$ with $\Phi_A(m^*) = g(x)$.
  $\Phi$ has no closed form, but it is a smooth function of two variables, so a table of $m^*$ over $A$ and $g(x)$, computed once, gives the index in two lookups.
- **(b)** $P(\gamma \geq 0) = P(G \leq B \kappa \, h(x))$, the regularized incomplete gamma function $P\bigl(A, B\kappa \, h(x)\bigr)$, which is $1/2$ where $B \kappa \, h(x)$ is the median of $G$.
  So $I_b = h(x) \, B / \operatorname{med}(A)$: the drafts' index at the posterior's median rate, with $\operatorname{med}(A) \approx A - 1/3$ for $A \geq 1$.

**Both make an uncertain page riper than its posterior mean does, and the less the page has lost, the riper.**
$\varphi$ is concave, so $\Phi_A(m) \leq \varphi(A/m)$ and $I_a \geq I_0$; and the median of a Gamma distribution lies below its mean, so $I_b \geq I_0$.
The factor $I/I_0$, for a page at fill 0.5:

| $A$, the loss events the posterior rests on | 0.5 | 1 | 2 | 5 | 20 |
| --- | --- | --- | --- | --- | --- |
| (a), expected gain | 1.29 | 1.15 | 1.08 | 1.03 | 1.01 |
| (b), probability of a gain | 2.20 | 1.44 | 1.19 | 1.07 | 1.02 |

For (a), the factor is larger at higher fills, 1.23 at $A = 1$ and fill 0.8, and smaller at lower ones; for (b), it does not depend on the fill.

### Waiting teaches, and the drafts' rule does not see it

**The drafts clean a page at the first epoch at which one more epoch of waiting does not pay; that is optimal only if a ripe page stays ripe, and under an estimated rate it does not: the page's next loss can make it unripe again.**
The static-share draft says so: "once ripe, a page stays ripe, since $a$ and $x$ only fall as it drains; for a stopping problem of that kind, stopping at the first epoch at which one more epoch of waiting does not pay is optimal."
With the rate known, that holds.
With the rate estimated, a loss does two things: it lowers the fill, which ripens the page, and it raises the estimated rate, which unripens it.

**At the drafts' constants, the second wins at every fill above 1/11.**
Take the posterior mean at a settled exposure, $B \approx x/(\sigma \beta)$, and a loss event of $\sigma$.
It raises $A$ by one, and so the estimated rate by about $\sigma \beta / x$; and it lowers the fill by $\sigma$, which raises $h(x)$ by $\sigma \, g(x)/x$, since $h'(x) = -(1 - x)/x^2$.
A page that has just become ripe, $h(x) = \hat r/\kappa$, is still ripe after the loss only if $\sigma \, g(x)/x \geq \sigma \beta/(x \kappa)$, that is, if $g(x) \geq \beta/\kappa$, whatever the size of the event.
At $\beta = 0.1$ and $\kappa = 0.01$, that asks for $g(x) \geq 10$, a fill of at most 1/11; at the price of 0.025 that the single-rate branch's controller settled on under skewed overwrites, a fill of at most 0.2.
Above those fills, a page that has just become ripe is unripe again after its next loss, until the estimate has fallen again.

**So every rule that cleans at the first ripe epoch cleans early, and the more so the fewer losses its estimate rests on.**
It cleans a page at the first dip of its estimate, not at the epoch at which the page's content has drained enough.
That holds for the drafts' point estimates as much as for (a) and (b), which only add to it by making uncertain pages riper still, and it is part of what the static-share draft's simulation saw on pages that drain slowly.

**(c) Clean a page only once no loss it could still suffer would make it unripe again.**
The index for (c) is the least of the indexes the page would have after $j$ further loss events, for every $j$:

$$
I_c = \min_{j \geq 0} I\bigl(A + j, \; x - j \sigma\bigr),
$$

with $I$ any of the indexes above.
Losses at once are the case to guard against: losses spread over time are no worse, since the estimate falls between them, and epochs without a loss only ripen a page further.
So once a page's (c) index has reached $1/\kappa$, no observation it could still make would reverse the decision, which restores the drafts' argument, at the price of cleaning later than the best rule would when the losses that would unripen the page are unlikely.
The minimum lies a few losses out: for a page at fill 0.5 that loses a thirtieth of a page at a time, with a posterior resting on $A = 2$, it lies at $j = 3$, 24 % below the page's present index by the posterior mean.
Rule (c) needs no parameter of its own, and costs a handful of evaluations of the underlying index per loss.
But it guards against losses that a page is unlikely to suffer as firmly as against likely ones, and [in simulation](#checking-in-simulation) that makes it wait too long on every page but those that drain slowly as one share.

## A draining share over a static one

### The cure model, per chunk

**Each chunk a page is written with drains with some probability $\pi$ and is static otherwise; a draining chunk loses its bytes at the rate $r$, in loss events, and a static one never loses any.**
A chunk is what the page holds of one statement: on a data page, a `Ref`'s payload, and on a leaf, the statement itself.
The class is the chunk's rather than each byte's because temperature belongs to allocations, so a chunk's bytes share one fate.
In survival analysis, this is a *mixture cure model*: a population of which a fraction never experiences the event.

**A chunk that has lost bytes is known to drain, and on a data page it usually lives on.**
A write that supersedes part of a `Ref`'s payload shows that the chunk drains, and leaves the rest of it live; on a leaf, a statement's first loss is its death.
So the draining share has a known part and an unknown one: the live bytes of the *touched* chunks, $x_t$, drain for certain, and of the $n$ *untouched* chunks, some number $j$ drain, so that $a = x_t + j \, \bar c$, with $\bar c$ the untouched chunks' mean size.

### The exact posterior is a mixture over the untouched chunks that drain

**With a Gamma prior on $r$ and a Beta prior on $\pi$, the posterior is a mixture of $n + 1$ Gamma–Beta products, one for each number $j$ of untouched chunks that drain.**
Take a page written $T$ epochs ago with $N$ chunks, of which $K$ have been touched, and $n = N - K$ not.
The touched chunks drain, and their losses, $L$ in all over an exposure $E_t$ of their bytes since the page's write, bear on the rate as in the single-rate posterior.
Each untouched chunk either drains and has lost nothing in $T$ epochs of it, which for $c$ bytes has probability $e^{-r c T/\sigma}$, or is static:

$$
\mathcal{L}(r, \pi) \propto \pi^K \, r^{L/\sigma} \, e^{-r E_t/\sigma} \, \bigl(1 - \pi + \pi \, e^{-r \bar c T/\sigma}\bigr)^{n}
= \pi^K \, r^{L/\sigma} \, e^{-r E_t/\sigma} \sum_{j=0}^{n} \binom{n}{j} \, \pi^j \, e^{-r j \bar c T/\sigma} \, (1 - \pi)^{n - j},
$$

taking the untouched chunks at their mean size $\bar c$, which the binomial expansion needs; exactly, the sum runs over the subsets of untouched chunks that drain, each with its own size.
Its term $j$ is the case that exactly $j$ of the untouched chunks drain, and each term is a Gamma density in $r$ times a Beta density in $\pi$, so with the priors $r \sim \mathrm{Gamma}(A_0, B_0)$ and $\pi \sim \mathrm{Beta}(p_0, q_0)$, the posterior is the mixture

$$
p(r, \pi \mid \text{losses}) = \sum_{j=0}^{n} w_j \;
\mathrm{Gamma}\bigl(r;\, A, \, B_j\bigr) \;
\mathrm{Beta}\bigl(\pi;\, p_0 + K + j, \, q_0 + n - j\bigr),
\qquad
w_j \propto \binom{n}{j} \frac{\mathrm{B}(p_0 + K + j, \; q_0 + n - j)}{B_j^{A}},
$$

with $A = A_0 + L/\sigma$, $B_j = B_0 + (E_t + j \, \bar c \, T)/\sigma$, and $\mathrm{B}$ the Beta function.
The weight $w_j$ is the posterior probability that exactly $j$ of the untouched chunks drain, and given $j$, the rate has the single-rate posterior of a page whose $j$ draining untouched chunks have added their exposure to that of the touched ones.

- **The Beta factor** prefers splits like the page's past: $K + j$ draining chunks out of $N$, against the prior's $p_0$ and $q_0$.
- **The Gamma factor** $B_j^{-A}$ charges each draining untouched chunk for having lost nothing: the more of them drain, the lower the rate must be to explain their survival, and the less the losses that did happen fit.
- **$j = n$** is the single-rate posterior: every chunk drains, and $B_n$ is the page's whole exposure.

### The prior replaces the test

**The static-share draft makes a static share earn $c$ statements' worth of log-likelihood; here the prior on $\pi$ does that, with a weight of its own.**
A page starts from the draining fraction $\pi_0$ of what it holds, 1 for fresh content, which the draft starts with no static share; the prior is $\mathrm{Beta}\bigl(\nu_\pi \pi_0 + \tfrac12, \; \nu_\pi (1 - \pi_0) + \tfrac12\bigr)$, worth $\nu_\pi$ chunks, with half a chunk of each kind so that neither is ruled out.
For fresh content, the weights of splits with static chunks start small, and grow only as the untouched chunks outlive what draining content would.
Unlike the test, the prior does not decide between one share and two: the posterior keeps both, weighted, and the decision sees the mixture.

### A drifting rate

**Discounting all of the evidence, the class count with the rest, keeps the mixture's form; it is an approximation, since the drift has no exact update here.**
The single-rate posterior's discounting carries over to everything that bears on the rate: $L$, the touched chunks' exposure $E_t$, and $T$, which becomes the discounted age $T_\delta = (1 - \delta^T)/(1 - \delta)$, the exposure of a byte live throughout; the prior's $A_0$ and $B_0$ are discounted with them.
With $j = n$, the discounted $B_n$ is again the single-rate draft's settled exposure, so the two models agree where every chunk drains.
A touched chunk's live bytes stay in the draining share for good, and the rate says how fast they drain now: if the chunk has since gone cold, the rate falls.

**The count of touched chunks is discounted too, although a chunk's class does not drift, so that it weighs against as much of the untouched chunks' survival as the posterior remembers.**
The untouched chunks' exposure $j \, \bar c \, T_\delta$ is discounted, so a chunk that has outlived its page's draining content by many epochs counts for only the last $1/\beta$ or so of them.
Kept whole, the count of touched chunks would go on weighing, undiminished, against that shortened survival: a page whose draining chunks were touched long ago would go on counting them as evidence that its untouched chunks drain too, when their long survival says they do not.
And "draining" lumps together content that drains at different rates: on the page of [the static-share draft's example](ripeness.md#estimating-how-fast-a-page-still-drains), the fast share's early losses would count, for good, as evidence that the untouched chunks drain, although they drain at a twenty-fifth of that rate if at all.
Discounted, the class evidence fades with the rate evidence it belongs to, which [in simulation](#checking-in-simulation) costs less.

### What a page keeps, and what a loss costs

**A page keeps its discounted loss events, the touched chunks' discounted exposure, the discounted count of touched chunks, the epoch of its last loss, the count and bytes of its untouched chunks, and the prior's two class counts: 22 bytes; and each statement needs one bit, set at its first loss.**
The prior on the rate enters the discounted sums as pseudo-observations, as in [the single-rate posterior](#one-rate-per-page); the prior on $\pi$ is the two class counts, $p_0$ and $q_0$, a byte each in halves of a chunk, which a page keeps from its write.
$T_\delta$ follows from the page's epoch.

**The fold learns a chunk's first loss from a bit in its statement's record.**
[The statement slab's record](../impl/in-memory-state.md#2-the-statement-slab) counts a statement's pins in 32 bits, which leaves room for one more.
When the fold supersedes part of a `Ref`'s payload whose bit is clear, it sets the bit, moves the chunk's bytes, the extent of its fragment before the split, from the page's untouched counts to its touched ones, and adds their exposure since the page's write, $c \, T_\delta$, to the touched exposure.
At open, a `Ref` whose live fragments no longer cover its whole payload is touched; on a leaf, a statement's first loss is its death, and no bit is needed.

**Each loss recomputes the $n + 1$ weights, $O(n)$ work in the untouched chunks, some 30 terms on a data page at most and some 300 on a leaf**; the heaviest few carry almost all the weight, and the rest can be dropped.

### Deciding on a mixture

**Every criterion is a sum over the mixture, with one Gamma posterior per split.**
With $a_j = x_t + j \, \bar c$ and $z_j = a_j/(1 - x + a_j)$:

- **(a)** $\mathbb{E}[\gamma]/\kappa = (1 - x) - \sum_j w_j \, a_j \, \Phi_A(B_j \kappa)$.
  All components share the shape $A$, so one table of $\Phi_A$ serves every $j$.
- **(b)** $P(\gamma \geq 0) = \sum_j w_j \, P\bigl(A, \, B_j \kappa \, h(z_j)\bigr)$, where a term with nothing draining, $a_j = 0$, counts as ripe at any price.

The index is the $1/\kappa$ at which the criterion holds with equality, found by bisection, each step $O(n)$.
Aging the rate's scale between losses multiplies every $B_j$ by the same factor, which leaves the weights $w_j$ as they are and scales the index, so the ranking ages uniformly here too.

### How it relates to the draft's fit

**The draft's fit reads the page's losses alone; the cure model also reads which chunks they came from, and which they did not.**
The fit asks how fast a page's losses fall, and infers the draining share from the losses' current rate; the untouched content enters only as a cap on it.
The cure model knows the touched chunks drain without waiting for them to die, and asks, of every untouched chunk, how likely it is to drain, given that it has lost nothing in the epochs the posterior remembers, and given how the page's other chunks behaved.
So a page whose losses keep coming from a few chunks has evidence of a static share in all of its other chunks, even when its losses are few, which is the case [the evaluation](../evaluation/ripeness2.md#how-often-pages-earn-a-static-share) found the fit unable to see.

## Checking in simulation

**In simulation, the cure model's posterior costs half of what the static-share draft's fit does where chunks lose their bytes in parts, as a data page's `Ref`s do, and 3 to 30 % less where they die whole; the single-rate posterior costs about what its draft does; and how (a) and (b) read a posterior matters less than the posterior itself.**
`tools/simulate-ripeness.py compare --set bayes` runs the eight scenarios of [the static-share draft's simulation](ripeness.md#checking-the-fit-in-simulation), with its measure: the excess cost of cleaning each page by an estimate, over cleaning it once its true index reaches $1/\kappa$.
Its pages hold chunks, which either die whole, as statements on a leaf do, or lose their bytes in pieces, each piece dying at its chunk's rate, as a `Ref`'s payload loses the parts that writes supersede; the dispersion $\sigma$ is that of the pieces, as the fold would measure it, and the tested fit counts its test in loss events too.
It runs 400 pages a scenario rather than 800, since the mixture is slower, with $\beta = 0.1$, a prior weight of $\nu = 3$ epochs on the starting rate and $\nu_\pi = 10$ chunks on the draining fraction, (c) for one rate per page only, over the posterior mean, and the drafts' floor as a cap on the index rather than as a background rate.
The summed excess cost of the eight scenarios:

| chunks of | losses | the single-rate draft | posterior mean | (a) | (b) | (c) |
| --- | --- | --- | --- | --- | --- | --- |
| 16 to 64 bytes | whole | 27.4 % | 27.0 % | 26.3 % | 26.0 % | 46.8 % |
| 32 to 256 bytes | whole | 46.3 % | 44.3 % | 44.1 % | 44.8 % | 44.6 % |
| 32 to 256 bytes | pieces of 32 bytes | 27.2 % | 26.7 % | 26.0 % | 25.3 % | 58.1 % |
| 256 to 1024 bytes | whole | 112.8 % | 111.8 % | 113.7 % | 117.3 % | 111.6 % |
| 256 to 1024 bytes | pieces of 64 bytes | 30.6 % | 29.8 % | 29.3 % | 29.2 % | 40.4 % |

| chunks of | losses | the static-share draft's tested fit | cure model, posterior means | (a) | (b) |
| --- | --- | --- | --- | --- | --- |
| 16 to 64 bytes | whole | 27.4 % | 21.5 % | 19.6 % | 19.3 % |
| 32 to 256 bytes | whole | 45.0 % | 40.4 % | 40.1 % | 40.0 % |
| 32 to 256 bytes | pieces of 32 bytes | 24.3 % | 12.7 % | 11.8 % | 11.5 % |
| 256 to 1024 bytes | whole | 109.3 % | 106.0 % | 106.2 % | 109.6 % |
| 256 to 1024 bytes | pieces of 64 bytes | 30.3 % | 15.7 % | 14.5 % | 15.1 % |

The pages are the same for every estimator, but a difference of a point or so in a sum is within what a different draw of pages moves.

- **Where chunks lose their bytes in parts, the cure model sees a static share almost exactly.**
  With chunks of 32 to 256 bytes losing 32 at a time, it costs 0.1 % on the page with a static share, against the tested fit's 6.4 % and the single-rate draft's 9.4 %, and 1.3 to 2.1 % on the page of the draft's example, against 7.1 %.
  A chunk's first loss shows that it drains, long before it dies, so the chunks that never lose anything stand out as static within a few epochs.
- **Where chunks die whole, the cure model still gains**, 40 % against the tested fit's 45 %: some 12 points on the pages with a static share, from the untouched chunks that outlive the draining content, less some 5 on the pages that drain as one share, where the fit's test keeps out static shares that are not there.
- **With one rate per page, the posterior costs about what the draft does**, a point or two less, since its mean is the draft's estimate; what it adds, the certainty, changes little in the decisions.
- **Partial losses make every estimate better**, since they are more events: the single-rate draft's excess cost falls from 46 % to 27 %, and the cure model's from 40 % to 12 %.
- **(a) and (b) differ little from the posterior mean, and from each other**, except on pages of a few large chunks that die whole, where (b) costs 3 to 4 points more than (a).
  They make uncertain pages riper, as [derived above](#deciding-on-one-rate), which gains a little on pages with a static share and loses a little on pages that drain slowly as one share.
- **(c) is too cautious wherever losses come in many small events**: with chunks losing 32 bytes at a time, it costs 58 % against the posterior mean's 27 %, since it guards against as many further losses as the page has pieces.
  It gains only on pages that drain slowly as one share, 0.7 % against 1.1 % on the page draining at 0.01.
  The argument behind it stands, since a loss can make a ripe page unripe, but the remedy has to weigh each further loss by its predictive probability, as a Bayes-optimal rule would, which is the [open question](#open-questions) this leaves.
- **A lighter prior pays**: with $\nu = 10$, as heavy as the single-rate draft's start, the single-rate posterior costs 30.5 % against 26.7 % with chunks losing 32 bytes at a time, most of it on the page whose start is wrong, where a heavy prior holds on to the wrong rate while the page shrinks; the cure model costs 14.3 to 15.4 % against 11.5 to 12.7 %.
- **Discounting the class count pays**: kept whole, it costs the cure model 14.1 to 17.3 % against 11.5 to 12.7 %, with chunks losing 32 bytes at a time.

## What would change

**In either draft, the estimate becomes a posterior, the index is computed from it by (a), and every constant the estimate had becomes a parameter of the prior.**

- **"Estimating how fast a page still drains"** would keep a posterior: with one rate per page, the Gamma posterior's two parameters in place of `rho`; with a static share, the discounted loss events and exposure, the touched and untouched chunks' counts, and the class counts in place of the fit's sums, and the mixture in place of the fit and its test.
- **The page table** would keep a `Drain` of 12 bytes with one rate per page, against the single-rate draft's 8, or 22 with a static share, against the static-share draft's 18; with a static share, each statement's record would also give a bit to mark its first loss.
- **The fold** would keep two running averages per page kind, of the sizes of the ranges it supersedes and of their squares, for the dispersion $\sigma$.
- **Ranking** would compute a page's index by (a) at each of its losses, from a table of $\Phi$ computed once, and age it between losses as the drafts do; (b) needs no table, and cost a few points more in simulation only on pages of a few large chunks that die whole.
- **Bytes that consolidation moves out of a page** would lower its live bytes without counting as losses; the posterior's evidence on the rate stays, and with a static share, a chunk moved out leaves the page's touched or untouched counts, whichever held it.
- **The constants** would all become the prior's:

| the drafts' constant | becomes |
| --- | --- |
| the forgetting rate $\beta$ | the drift of the prior over the rate's path, $\delta = e^{-\beta}$ |
| the starting estimate's weight, $1/\beta$ epochs or $n_0$ | the prior's weight $\nu$, in epochs of the page's bytes |
| the static-share test's $c$ | the weight $\nu_\pi$ of the prior on the draining fraction, in chunks |
| the floor $R_\text{min}$ | a background rate that the model asserts for all content |

## Open questions

- **How much a starting estimate should weigh, and in what unit.**
  The prior's weight is exposure, fixed in byte-epochs, so on a page that drains fast, and shrinks, a wrong start outweighs the page's own losses for longer than the drafts' averages of fractions let it.
  Scaling the prior's exposure with the page's live bytes would behave like the drafts, but is no longer a prior.
- **Chunks of unequal size.**
  The mixture takes the untouched chunks at their mean size; the exact posterior weighs each subset of them by its own sizes, which a page of a few large chunks and many small ones would notice.
- **What a loss event is.**
  The dispersion treats every superseded range as one event, but one write that supersedes ranges on several pages, or several ranges of one page, is arguably one event; which grouping calibrates the posterior best is a question for measurement.
- **Pooling evidence across pages.**
  A single page has little evidence, but the pages one flush wrote hold content the application wrote together; a hierarchical prior, with the rates of one flush's pages drawn around a rate shared by them, would lend each page the others' losses.
- **A rule that values learning at its worth.**
  A page's next loss can make a ripe page unripe, so cleaning at the first ripe epoch cleans early, but (c), which guards against every loss the page could still suffer, likely or not, waits too long.
  With one rate per page, the Bayes-optimal rule, which weighs each loss by its predictive probability, depends only on the posterior's shape, the fill, and the size of a loss event relative to it, and could be tabulated offline.
  A one-step approximation of it, the knowledge gradient, was too slow to simulate in full and no better on the one scenario it finished.
- **Moved content's evidence.**
  A page mixed from others starts from the byte-weighted mean of their rates with a fixed weight; carrying each source's posterior over, in proportion to the bytes moved, would give the start the weight its evidence has, and chunks moved from a source where they were touched could arrive known to drain.
- **Epochs, not time.**
  The model counts rates per epoch, as the drafts do, and so shares their open question about the clock.
