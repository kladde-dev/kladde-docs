---
title: Bayesian ripeness
---

**Status: a proposal, not adopted.**
It would replace how both variants of [Cleaning by ripeness](ripeness.md) estimate a page's drain and decide on it: the single-rate draft on branch `ripeness`, and the draft with a static share on branch `ripeness2`.
It assumes both drafts, and uses their notation: fills relative to $u_0$, $x$ for a page's fill, $a$ and $s$ for its draining and static shares, $z = a/(1 - s)$, $h$, $g$, $\kappa$, $\beta$, and $R_\text{min}$.

**Keep a posterior over each page's parameters rather than a point estimate, and decide from the posterior.**
A data page in kladde-bench's files holds 10 to 30 statements, a page that drains slowly loses one every few dozen flushes, and the fall that would show a static share comes to about four statements ([Ripeness with a static share](../evaluation/ripeness2.md#how-often-pages-earn-a-static-share)).
Estimates that rest on so little are uncertain, and the drafts cope with that by devices of their own: a forgetting rate, a weight for the starting estimate, a test that a static share must pass, and a floor.
Here each becomes a parameter of one prior, and the uncertainty they cope with becomes a posterior that the cleaning decision can see.

- **One rate per page has an exact posterior, a Gamma distribution, even with a rate that drifts; its mean is the single-rate draft's estimate, and its shape counts the statements' worth of evidence the estimate rests on.**
- **A draining share over a static one has an exact posterior too: a mixture over how many of the page's live statements still drain.**
  A prior on the draining fraction takes the place of the static-share draft's test, and the posterior reads a page's survivors as well as its losses, which the draft's fit could not.
- **Deciding by the expected gain (a) or by the probability of a gain (b) makes an uncertain page riper than its posterior mean does, although a page's next loss can make a ripe page unripe again, so that a rule that cleans at the first ripe epoch already cleans early.**
  Rule (c), cleaning a page only once no loss it could still suffer would unripen it, guards against that, but too cautiously, since it weighs unlikely losses like likely ones.
- **In simulation, the cure model's posterior, decided by (b), costs 38 % more than cleaning at the true index, summed over eight scenarios, where either draft costs 46 %; with many small statements, 18.5 % against 27 %.**
  The single-rate posterior costs 44 % against its draft's 46 %, once its starting estimate weighs 3 epochs rather than the draft's 10.
  Rules (a) and (b) differ little from the posterior mean, and (c) costs more than any.

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

### Losses counted in statements

**The observations are the statements a page loses, not its bytes: a statement dies whole, so its bytes are one piece of evidence, not many.**
The model is the single-rate draft's: each live statement dies at a rate $r$, independently of the others and of its age.
Over an epoch in which a page starts with $n$ live statements and loses $k$ of them, the likelihood of $r$ is $r^k \, e^{-r e}$, with the *exposure* $e = n - k/2$: the time the page's statements spent alive during the epoch, counting those that died as alive for half of it.
Over many epochs, the likelihood is $r^D \, e^{-r E}$, with $D$ the statements lost and $E$ the total exposure; this is exact for lifetimes that are exponential, but for not knowing when in an epoch a statement died.

Counting bytes instead would treat a statement of 250 bytes as 250 independent deaths, and make the posterior 250 times too sure.
The page table would keep each page's live statement count, 2 bytes, or estimate it from the page's coverage and the file's mean statement size, as the static-share draft's test does.

### The exact posterior is a Gamma distribution

**A Gamma prior on the rate gives a Gamma posterior, whose two parameters count the page's losses and its exposure.**
With a prior $r \sim \mathrm{Gamma}(A_0, B_0)$, of density proportional to $r^{A_0 - 1} e^{-B_0 r}$, the posterior after $D$ losses in an exposure $E$ is

$$
r \mid \text{losses} \sim \mathrm{Gamma}(A_0 + D, \; B_0 + E),
$$

with mean $A/B$ and a relative spread of $1/\sqrt{A}$, where $A$ and $B$ are the posterior's two parameters.
The prior acts as $A_0$ statements lost in an exposure of $B_0$: a starting estimate $r_0$ worth $\nu$ epochs of the page's $n_0$ statements is $B_0 = \nu \, n_0$ and $A_0 = r_0 \, B_0$.

### A rate that drifts keeps the posterior exact

**A prior under which the rate drifts by random factors, of mean one, turns the update into a discounted one, and keeps it exact: $A \leftarrow \delta A + k$ and $B \leftarrow \delta B + e$ each epoch, with $\delta = e^{-\beta}$.**
The drafts forget old losses because a page's rate changes as its content does.
The Bayesian way to say so is a prior over the rate's path, and one prior makes the forgetting exact: between epochs, $r_t = r_{t-1} \, \eta_t / \delta$, with $\eta_t$ drawn from a $\mathrm{Beta}\bigl(\delta A, (1 - \delta) A\bigr)$ distribution independent of $r_{t-1}$, where $A$ is the posterior's shape before the step: the gamma–beta model of Smith and Miller, *A non-Gaussian state space model and application to prediction of records* (J. R. Stat. Soc. B, 1986), known in forecasting as the Poisson–gamma steady model with a discount factor.
A $\mathrm{Gamma}(A, B)$ variable times an independent $\mathrm{Beta}(\delta A, (1 - \delta) A)$ one is $\mathrm{Gamma}(\delta A, B)$, so the rate's prior for the next epoch is $\mathrm{Gamma}(\delta A, \delta B)$: the same mean, and a variance larger by $1/\delta$.
The epoch's losses then update it as above.
The drafts' forgetting rate $\beta$ becomes the prior's drift, its third parameter beside $A_0$ and $B_0$.

### The single-rate draft is this posterior's mean

**Once a page's exposure has settled, the posterior mean follows exactly the single-rate draft's update.**
With $n$ live statements, $B$ settles at $n/(1 - \delta)$, and then $(\delta A + k)/B = \delta \, A/B + (1 - \delta) \, k/n$, which is the draft's `lose` with the fraction of statements lost in place of the fraction of bytes.
The posterior adds two things.

- **Its mean is right while the exposure is still growing.**
  Until then, the mean is the ratio of the discounted losses to the discounted exposure, as Adam's bias correction would make it, but with the prior's weight, $\nu$ epochs, as an explicit parameter; the draft's start weighs about $1/\beta$ epochs, fixed.
- **Its shape counts the losses the estimate rests on.**
  At the settled exposure, $A \approx r \, n / \beta$: a page of 20 statements draining at 0.01 per epoch, with $\beta = 0.1$, rests on 2 statements, and its rate is uncertain by $1/\sqrt{2}$, 70 %.

### What a page keeps

**A page keeps $A$, $B$, and the epoch it last lost a statement, all updated in closed form: one number more than the single-rate draft.**
Over $d$ epochs without a loss, with $n$ statements live, $A \leftarrow \delta^d A$ and $B \leftarrow \delta^d B + n \, (1 - \delta^d)/(1 - \delta)$.

```rust
/// Per page, beside kind, epoch, coverage, and its live statement count `n`: 12 bytes.
struct Drain {
    a:  f32,   // the posterior's shape: statements lost, discounted, with the prior's
    b:  f32,   // its rate: statement-epochs of exposure, discounted, with the prior's
    at: u32,   // the epoch of the page's last natural loss, from the session's base
}

impl Drain {
    /// A flush's fold superseded `k` of the page's statements, of `n` live before it.
    fn lose(&mut self, k: u32, n: u32, now: u32) {
        // The epochs since the last loss, without one:
        let (delta, d) = ((-BETA).exp(), (now - self.at - 1) as f32);
        let decay = delta.powf(d);
        self.a *= decay;
        self.b = self.b * decay + n as f32 * (1.0 - decay) / (1.0 - delta);
        // This epoch:
        self.a = delta * self.a + k as f32;
        self.b = delta * self.b + n as f32 - 0.5 * k as f32;
        self.at = now;
    }
}
```

**A page starts from what it holds, as a prior; a page the consolidator state cannot vouch for starts from the seed, as a past it was watched through.**
A new page's prior has the drafts' starting rate $r_0$, the byte-weighted mean of its content's rates, and a weight of $\nu$ epochs: $B_0 = \nu \, n_0$, $A_0 = r_0 \, B_0$.
The seed at open has the drafts' rate, and weighs what the page's past would have, had the page been watched through it losing statements at that rate: $B$ is the discounted exposure of the statements it would have held, and $A = \hat r \, B$.

**The floor becomes a rate that no content falls below.**
Adding $R_\text{min}$ times each epoch's exposure to its losses asserts a background rate $R_\text{min}$ for all content, and keeps the posterior mean at or above it; it changes every draining page's rate by $R_\text{min}$, which is negligible, and it caps a frozen page's index as the drafts' floor does.

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

| $A$, the statements the posterior rests on | 0.5 | 1 | 2 | 5 | 20 |
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
Take the posterior mean at a settled exposure, $B \approx n/\beta$.
A loss raises $A$ by one, and so the estimated rate by about $\beta/n$; and it lowers the fill by about $x/n$, which raises $h(x)$ by $g(x)/n$, since $h'(x) = -(1 - x)/x^2$.
A page that has just become ripe, $h(x) = \hat r/\kappa$, is still ripe after the loss only if $g(x)/n \geq \beta/(n \kappa)$, that is, if $g(x) \geq \beta/\kappa$.
At $\beta = 0.1$ and $\kappa = 0.01$, that asks for $g(x) \geq 10$, a fill of at most 1/11; at the price of 0.025 that the single-rate branch's controller settled on under skewed overwrites, a fill of at most 0.2.
Above those fills, a page that has just become ripe is unripe again after its next loss, until the estimate has fallen again.

**So every rule that cleans at the first ripe epoch cleans early, and the more so the fewer losses its estimate rests on.**
It cleans a page at the first dip of its estimate, not at the epoch at which the page's content has drained enough.
That holds for the drafts' point estimates as much as for (a) and (b), which only add to it by making uncertain pages riper still, and it is part of what the static-share draft's simulation saw on pages that drain slowly.

**(c) Clean a page only once no loss it could still suffer would make it unripe again.**
The index for (c) is the least of the indexes the page would have after $j$ further losses, for every $j$:

$$
I_c = \min_{j \geq 0} I\Bigl(A + j, \; x \, \bigl(1 - j/n\bigr)\Bigr),
$$

with $I$ any of the indexes above, and the static share's counts advanced by $j$ draining deaths likewise.
Losses at once are the case to guard against: losses spread over time are no worse, since the estimate falls between them, and epochs without a loss only ripen a page further.
So once a page's (c) index has reached $1/\kappa$, no observation it could still make would reverse the decision, which restores the drafts' argument, at the price of cleaning later than the best rule would when the losses that would unripen the page are unlikely.
The minimum lies a few losses out: for a page at fill 0.5 with 15 statements and a posterior resting on $A = 2$, it lies at $j = 3$, 24 % below the page's present index by the posterior mean.
Rule (c) needs no parameter of its own, and costs a handful of evaluations of the underlying index per loss.
But it guards against losses that a page is unlikely to suffer as firmly as against likely ones, and [in simulation](#checking-in-simulation) that makes it wait too long on every page but those that drain slowly as one share.

## A draining share over a static one

### The cure model

**Each statement a page is written with drains with some probability $\pi$ and is static otherwise; a draining statement dies at the rate $r$, a static one never.**
This is the static-share draft's model, stated per statement rather than per byte, and in survival analysis it is a *mixture cure model*: a population of which a fraction never experiences the event.
The draft's split is what it says about the live statements: of the $n$ still live, some number $j$ drain, and the draining share is $a = x \, j/n$, counting statements of the page's mean size.

### The exact posterior is a mixture over the statements still draining

**With a Gamma prior on $r$ and a Beta prior on $\pi$, the posterior is a mixture of $n + 1$ Gamma–Beta products, one for each number $j$ of live statements that still drain.**
Take a page written $T$ epochs ago with $N$ statements, of which $D$ have died, at ages $t_1, \ldots, t_D$, and $n = N - D$ live.
Each death is a draining statement that died at its age, and each survivor either drains and has survived $T$ epochs of it, or is static:

$$
L(r, \pi) = \prod_{i=1}^{D} \pi \, r \, e^{-r t_i} \cdot \bigl(1 - \pi + \pi \, e^{-r T}\bigr)^{n}
= \pi^D \, r^D \, e^{-r S} \sum_{j=0}^{n} \binom{n}{j} \, \pi^j \, e^{-r j T} \, (1 - \pi)^{n - j},
$$

with $S = \sum_i t_i$, where the binomial expansion's term $j$ is the case that exactly $j$ of the survivors drain.
Each term is a Gamma density in $r$ times a Beta density in $\pi$, so with the priors $r \sim \mathrm{Gamma}(A_0, B_0)$ and $\pi \sim \mathrm{Beta}(p_0, q_0)$, the posterior is the mixture

$$
p(r, \pi \mid \text{losses}) = \sum_{j=0}^{n} w_j \;
\mathrm{Gamma}\bigl(r;\, A, \, B_j\bigr) \;
\mathrm{Beta}\bigl(\pi;\, p_0 + D + j, \, q_0 + n - j\bigr),
\qquad
w_j \propto \binom{n}{j} \frac{\mathrm{B}(p_0 + D + j, \; q_0 + n - j)}{B_j^{A}},
$$

with $A = A_0 + D$, $B_j = B_0 + S + j \, T$, and $\mathrm{B}$ the Beta function.
The weight $w_j$ is the posterior probability that exactly $j$ of the live statements drain, and given $j$, the rate has the single-rate posterior of a page whose $j$ draining survivors have added their $jT$ statement-epochs to its exposure.

- **The Beta factor** prefers splits like the page's past: $D + j$ draining statements out of $N$, against the prior's $p_0$ and $q_0$.
- **The Gamma factor** $B_j^{-A}$ charges each draining survivor for having survived: the more of them drain, the lower the rate must be to explain their survival, and the less the losses that did happen fit.
- **$j = n$** is the single-rate posterior: every live statement drains, and $B_n = B_0 + S + nT$ is the page's whole exposure.

### The prior replaces the test

**The static-share draft makes a static share earn $c$ statements' worth of log-likelihood; here the prior on $\pi$ does that, with a weight of its own.**
A page starts from the draining fraction $\pi_0$ of what it holds, 1 for fresh content, which the draft starts with no static share; the prior is $\mathrm{Beta}\bigl(\nu_\pi \pi_0 + \tfrac12, \; \nu_\pi (1 - \pi_0) + \tfrac12\bigr)$, worth $\nu_\pi$ statements, with half a statement of each kind so that neither is ruled out.
For fresh content, the weights of splits with static statements start small, and grow only as the page's survivors outlive what draining content would.
Unlike the test, the prior does not decide between one share and two: the posterior keeps both, weighted, and the decision sees the mixture.

### A drifting rate

**Discounting all of the evidence, the class counts with the rest, keeps the mixture's form; it is an approximation, since the drift has no exact update here.**
The single-rate posterior's discounting carries over to everything that bears on the rate: $D$ becomes the discounted count of deaths, $S$ the discounted exposure of the statements that died, and $T$ the discounted age $T_\delta = (1 - \delta^T)/(1 - \delta)$, the exposure of a statement live throughout; the prior's $A_0$ and $B_0$ are discounted with them.
With $j = n$, the discounted $B_n$ is again the single-rate draft's settled exposure, so the two models agree where every statement drains.

**The class counts are discounted too, although a statement's class does not drift, so that they weigh against as much of the survivors' survival as the posterior remembers.**
The survivors' exposure $j \, T_\delta$ is discounted, so a survivor that has outlived its page's draining content by many epochs counts for only the last $1/\beta$ or so of them.
Kept whole, the count of the dead would go on weighing, undiminished, against that shortened survival: a page that lost its draining share long ago would go on counting those deaths as evidence that its survivors drain, when their long survival says they do not.
And "draining" lumps together content that drains at different rates: on the page of [the static-share draft's example](ripeness.md#estimating-how-fast-a-page-still-drains), the fast share's early deaths would count, for good, as evidence that the survivors drain, although they drain at a twenty-fifth of that rate if at all.
Discounted, the class evidence fades with the rate evidence it belongs to, which [in simulation](#checking-in-simulation) costs less.

### What a page keeps, and what a loss costs

**A page keeps the discounted count of its dead statements, their discounted exposure, the epoch of its last loss, and the prior's two class counts: 16 bytes, and the live statement count.**
The prior on the rate enters the discounted sums as pseudo-observations, as in [the single-rate posterior](#one-rate-per-page); the prior on $\pi$ is the two class counts, $p_0$ and $q_0$, which a page keeps from its write.
$T_\delta$ follows from the page's epoch.
Each loss recomputes the $n + 1$ weights, $O(n)$ work, which is some 30 terms on a data page and some 300 on a leaf; the heaviest few carry almost all the weight, and the rest can be dropped.

### Deciding on a mixture

**Every criterion is a sum over the mixture, with one Gamma posterior per split.**
With $a_j = x \, j/n$ and $z_j = a_j/(1 - x + a_j)$:

- **(a)** $\mathbb{E}[\gamma]/\kappa = (1 - x) - \sum_j w_j \, a_j \, \Phi_A(B_j \kappa)$.
  All components share the shape $A$, so one table of $\Phi_A$ serves every $j$.
- **(b)** $P(\gamma \geq 0) = \sum_j w_j \, P\bigl(A, \, B_j \kappa \, h(z_j)\bigr)$, where the term $j = 0$, every live statement static, counts as ripe at any price.

The index is the $1/\kappa$ at which the criterion holds with equality, found by bisection, each step $O(n)$.
Aging the rate's scale between losses multiplies every $B_j$ by the same factor, which leaves the weights $w_j$ as they are and scales the index, so the ranking ages uniformly here too.

### How it relates to the draft's fit

**The draft's fit reads the page's losses alone; the cure model also reads its survivors.**
The fit asks how fast a page's losses fall, and infers the draining share from the losses' current rate; the survivors enter only as a cap on it.
The cure model asks, of every live statement, how likely it is to drain, given that it has survived the epochs the posterior remembers, and given how the page's other statements behaved.
Every survivor that has not died counts, not only the statements that did: a page whose survivors have outlived what content draining at the page's recent rate would, has evidence of a static share in all of them, even when its recent losses are few, which is the case [the evaluation](../evaluation/ripeness2.md#how-often-pages-earn-a-static-share) found the fit unable to see.

## Checking in simulation

**In simulation, the cure model's posterior costs a sixth to a third less than either draft, and the single-rate posterior a little less than its draft; how (a) and (b) read a posterior matters less than the posterior itself, and (c) reads it too cautiously.**
`tools/simulate-ripeness.py compare --set bayes` runs the eight scenarios of [the static-share draft's simulation](ripeness.md#checking-the-fit-in-simulation), with its measure: the excess cost of cleaning each page by an estimate, over cleaning it once its true index reaches $1/\kappa$.
It runs 400 pages a scenario rather than 800, since the mixture is slower, with $\beta = 0.1$, a prior weight of $\nu = 3$ epochs on the starting rate and $\nu_\pi = 10$ statements on the draining fraction, (c) taken over the posterior means, and the drafts' floor as a cap on the index rather than as a background rate.
The summed excess cost of the eight scenarios:

| statements of | the single-rate draft | posterior mean | (a) | (b) | (c) |
| --- | --- | --- | --- | --- | --- |
| 16 to 64 bytes | 27.4 % | 27.0 % | 26.5 % | 25.8 % | 46.8 % |
| 32 to 256 bytes | 46.3 % | 43.5 % | 43.8 % | 44.2 % | 45.0 % |
| 256 to 1024 bytes | 112.8 % | 110.3 % | 110.9 % | 113.2 % | 110.3 % |

| statements of | the static-share draft's tested fit | cure model, (a) | (b) | (c) |
| --- | --- | --- | --- | --- |
| 16 to 64 bytes | 27.4 % | 19.0 % | 18.5 % | 36.1 % |
| 32 to 256 bytes | 45.5 % | 37.8 % | 38.1 % | 38.3 % |
| 256 to 1024 bytes | 109.3 % | 103.3 % | 104.6 % | 102.2 % |

The pages are the same for every estimator, but a difference of a point or so in a sum is within what a different draw of pages moves.

- **The cure model gains where pages hold a static share, and holds its own elsewhere.**
  With statements of 32 to 256 bytes, by (a) or (b), it costs 6 to 7 % on the page with a static share against the tested fit's 12 %, 4 to 5 % on the page of the draft's example against 7 %, and 5 to 6 % on the seeded page against 7 %; on pages that drain as one share, it costs up to two points more than the tested fit.
  It sees the static share that the fit's test kept out, from the survivors that outlive the draining content.
- **With one rate per page, the posterior gains mostly by its lighter prior.**
  With $\nu = 10$, as heavy as the draft's start, the posterior mean costs 49 %, more than the draft's 46 %; the difference is almost all on the page whose start is wrong, where a heavy prior holds on to the wrong rate while the page shrinks.
  The cure model gains less from the lighter prior, 43 % against 38 %.
- **(a) and (b) change little against the posterior mean.**
  They make uncertain pages riper, as [derived above](#deciding-on-one-rate), which gains a little on pages with a static share and loses a little on pages that drain slowly as one share.
  Of the two, (b) is the cheaper: with one rate per page, it is the draft's index at the posterior's median, and with a static share, a sum of incomplete gamma functions, where (a) needs a table of $\Phi$.
- **(c) is too conservative: it gains only on pages that drain slowly as one share, and loses heavily where pages hold many statements.**
  With one rate per page and statements of 32 to 256 bytes, it costs 4.2 % against the posterior mean's 5.1 % on the page draining at 0.01, but 7.6 % against 6.4 % on the page of the draft's example.
  With statements of 16 to 64 bytes, a page holds up to 128 statements and (c) guards against as many further losses, and it costs 47 % against 27 %.
  It guards against every loss a page could still suffer, however unlikely: a page whose draining share holds a few statements will suffer few losses, and (c) still waits out the losses it does not have.
  The argument behind it stands, since a loss can make a ripe page unripe, but the remedy has to weigh each further loss by its predictive probability, as a Bayes-optimal rule would, which is the [open question](#open-questions) this leaves.
- **Discounting the class counts pays**: kept whole, they cost the cure model 40 % against 38 %, most of it on the page with a static share, 9.1 % against 6.7 % by (a).
- **Pages of a few statements** remain beyond every estimator, as in the draft's simulation, but the cure model still gains on the page with a static share, 8 to 11 % against 15 %.
- **Pages of many small statements** favour the cure model most: 19 % against the tested fit's 27 %, since the survivors it reads are many.

## What would change

**In either draft, the estimate becomes a posterior, the index is computed from it by (b), and every constant the estimate had becomes a parameter of the prior.**

- **"Estimating how fast a page still drains"** would keep a posterior: with one rate per page, the Gamma posterior's two parameters in place of `rho`; with a static share, the discounted deaths and exposure and the class counts in place of the fit's sums, and the mixture in place of the fit and its test.
- **The page table** would keep each page's live statement count, 2 bytes, beside a `Drain` of 12 bytes with one rate per page, or 16 with a static share, against 8 and 18 in the drafts.
- **Ranking** would compute a page's index by (b) at each of its losses, the cheaper of the two criteria that did as well as each other in simulation, and age it between losses as the drafts do.
- **Bytes that consolidation moves out of a page** would lower its live statement count without counting as losses; the posterior's evidence on the rate stays, and with a static share, the mixture simply runs over fewer live statements.
- **The constants** would all become the prior's:

| the drafts' constant | becomes |
| --- | --- |
| the forgetting rate $\beta$ | the drift of the prior over the rate's path, $\delta = e^{-\beta}$ |
| the starting estimate's weight, $1/\beta$ epochs or $n_0$ | the prior's weight $\nu$, in epochs of the page's statements |
| the static-share test's $c$ | the weight $\nu_\pi$ of the prior on the draining fraction, in statements |
| the floor $R_\text{min}$ | a background rate that the model asserts for all content |

## Open questions

- **How much a starting estimate should weigh, and in what unit.**
  The prior's weight is exposure, fixed in statement-epochs, so on a page that drains fast, and shrinks, a wrong start outweighs the page's own losses for longer than the drafts' averages of fractions let it.
  Scaling the prior's exposure with the page's live statements would behave like the drafts, but is no longer a prior.
- **Pooling evidence across pages.**
  A single page has little evidence, but the pages one flush wrote hold content the application wrote together; a hierarchical prior, with the rates of one flush's pages drawn around a rate shared by them, would lend each page the others' losses.
- **A rule that values learning at its worth.**
  A page's next loss can make a ripe page unripe, so cleaning at the first ripe epoch cleans early, but (c), which guards against every loss the page could still suffer, likely or not, waits too long.
  With one rate per page, the Bayes-optimal rule, which weighs each loss by its predictive probability, depends only on the posterior's shape, the live statement count, and the fill, and could be tabulated offline.
  A one-step approximation of it, the knowledge gradient, was too slow to simulate in full and no better on the one scenario it finished.
- **Moved content's evidence.**
  A page mixed from others starts from the byte-weighted mean of their rates with a fixed weight; carrying each source's posterior over, in proportion to the statements moved, would give the start the weight its evidence has.
- **Epochs, not time.**
  The model counts rates per epoch, as the drafts do, and so shares their open question about the clock.
