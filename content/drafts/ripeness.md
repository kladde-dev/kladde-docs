---
title: Cleaning by ripeness
---

**Status: a proposal, not adopted.**
It would replace data-page scoring, the churn floor, and part of free filling in [Consolidation](../impl/consolidation.md).
[Cleaning by ripeness](../evaluation/ripeness.md) evaluates an earlier version, which estimated one rate per page.

**Clean a page once waiting no longer pays, not once it has become sparse.**
A page whose content is still dying should wait, since every byte that dies before the page is cleaned is a byte the cleaning need not copy.
A page whose content has stopped dying gains nothing by waiting, however full it is, and its garbage stays until something moves it.
This draft turns that into three things: a threshold per page, derived from a price for space; an estimate of how much of each page still drains and how fast, chosen so that pages can be ranked by the threshold without rescanning them as time passes; and a placement rule that makes fewer frozen pages in the first place.

## The problem

### A mixed page drains, then freezes

**A page written with hot and cold content together loses its hot share within a few flushes and then stops losing anything: it freezes at its cold share, which can be any fill.**
Nothing in a kladde file is overwritten in place: an update writes new bytes to a fresh page and leaves the old ones dead where they were.
So a page is written nearly full and afterwards only loses live bytes.
Workloads are skewed — a small hot set is rewritten over and over, a cold majority seldom or never — and a page holds whatever one flush wrote together.
Packed in key order, the allocations the application keeps rewriting sit next to ones it has just created and will not touch again.

Under a naive policy that cleans only below a fixed fill threshold, the pages of a file would therefore fall into two populations.
Pages whose content keeps dying cycle: written full, drained to the threshold, cleaned, written again.
Frozen pages stay wherever they stopped, and nothing about skew says where that is; all that is known about their fill is that it lies somewhere between the cleaning threshold and full.
Such a policy keeps every frozen page above that fill for good, and its garbage with it.

### Why the current design never cleans a frozen page

**Two rules keep a frozen page from ever being chosen, and a third makes new frozen pages.**
[Victim selection](../impl/consolidation.md#scoring-a-data-page) keeps data pages in bucket queues ordered by fill alone, so the age term of its score can only be evaluated on a sample: the next victim is the best-scoring of $K$ pages drawn from the sparsest non-empty bucket.

1. The pages that keep draining keep that bucket stocked, so a frozen page in a fuller bucket is never drawn, and age only ever decides between pages of about the same fill.
2. [The churn floor](../impl/consolidation.md#the-churn-floor-is-a-parameter-not-an-identity) rejects every page more than $1/(1 + \lambda)$ live — about half, for $\lambda$ near 1 — whatever its age.
3. [Free filling](../impl/consolidation.md#two-forms-of-consolidation) puts survivors, which are cold by selection, into the room of the flush's own pages, next to fresh content; once the fresh content dies, the page freezes at the survivors' share.

### What LFS did about it

**LFS cleaned cold segments at a high fill and packed their survivors together, and it needed both.**

- Cleaning sorts content by temperature.
  A survivor of a cleaned segment is cold almost by definition, having outlived everything around it, and a segment written from survivors alone stays full: a frozen segment is rewritten once, and its content never drains again.
- Its cost-benefit score, $(1 - u) \cdot \mathrm{age} / (1 + u)$, values a cold segment's free space more, because that space stays free.
  In LFS's simulations of a hot-and-cold workload, the cleaner took cold segments at about 75 % utilisation and let hot ones drain to about 15 %.

This draft keeps both ideas, but derives the threshold from a cost model rather than tuning a score, and estimates temperature in a way that makes ranking by it cheap.

## The proposal

### When waiting no longer pays

**Clean a page of fill $u$ whose content dies at rate $r$ once $h(u/u_0) \geq r / \kappa$, with $h(x) = (1 - x)/x - \ln(1/x)$ and $u_0$ the fill at which survivors are packed.**
Here $\kappa$ is the price of one page of space held for one epoch, measured in page writes: at $\kappa = 0.01$, a page of garbage kept for a hundred flushes costs as much as writing a page.
Such a page is **ripe**.
The rule sees a page's fill only relative to $u_0$, so write $x = u/u_0$ for it.

The model behind it makes three assumptions:

- A page's live content dies at a rate $r$, a fraction $r$ of it per epoch, however long it has lived, so survivors keep dying at $r$ after they are moved.
- A policy pays $\kappa$ per epoch for each page of space that live content does not fill, the unfilled room of a page as well as its garbage, and 1 for each page it writes, and is judged by its cost per epoch in the long run.
- It cleans a page once its fill has fallen to a threshold $u^* = x^* u_0$, and packs the survivors, with content that dies at the same rate, into pages of fill $u_0$.

Follow one unit of content, measured in pages, from the moment it is packed.
It takes up $1/u_0$ of a page, and its page reaches $u^*$ after $T = \ln(1/x^*) / r$ epochs.
Until then, whatever of that $1/u_0$ is not live costs $\kappa$ per epoch, which comes to $\kappa \, \bigl(T/u_0 - (1 - x^*)/r\bigr)$ in expectation.
With probability $x^*$ the unit survives to $T$, is copied, which costs $1/u_0$, and starts over.
So its expected cost over its whole life, $V$, satisfies

$$
\begin{aligned}
V &= \kappa \, \Bigl(\frac{T}{u_0} - \frac{1 - x^*}{r}\Bigr) + x^* \, \Bigl(\frac{1}{u_0} + V\Bigr),
\qquad \text{with} \quad T = \frac{\ln(1/x^*)}{r}, \\
V &= \frac{\kappa \, \bigl(\ln(1/x^*)/u_0 - (1 - x^*)\bigr) / r + x^*/u_0}{1 - x^*},
\end{aligned}
$$

and setting $\mathrm{d}V / \mathrm{d}x^* = 0$ gives $h(x^*) = r / \kappa$: $u_0$ drops out, except as the unit in which fill is measured.
The same threshold balances the two sides of waiting one more epoch at relative fill $x$: it costs $\kappa \, (1 - x)$, the page less the $x$ of a page its survivors would take, and saves $r \, x \, (1 + u_0 V)$, since each byte that dies meanwhile would have cost a copy now and $V$ afterwards.

| $r / \kappa$ | $x^*$, cleaning at | the myopic rule $(1 - x)/x \geq r / \kappa$ would clean at |
| --- | --- | --- |
| 0 | any fill, up to the cap below | any fill |
| 0.001 | 0.96 | 0.999 |
| 0.01 | 0.87 | 0.99 |
| 0.1 | 0.66 | 0.91 |
| 1 | 0.32 | 0.5 |
| 10 | 0.07 | 0.09 |

Content that has stopped dying is cleaned at almost any fill, and content that dies fast only once it is nearly gone, which is LFS's behaviour, derived rather than tuned.

**The churn floor is the special case in which every page drains at the same rate, without the logarithm.**
$(1 - x)/x$ is the space a cleaning frees per byte it writes, which is exactly what the churn floor compares with $\lambda$.
The $-\ln(1/x)$ term accounts for survivors that go on dying after they are moved; the myopic rule, which leaves it out, would clean every page too early, and pages that drain slowly far too early.
However, the more important difference between the current "churn floor" design and the proposed ripeness design is that the current design compares its score $(1 - x)/x$ against the same threshold $\lambda$ for every page, whereas the proposed ripeness design uses a different threshold $r / \kappa$ for each page, where the rate $r$ is [[#Estimating how fast a page still drains|estimated per page]].
Under uniform random updates every page does drain at one rate, so there ripeness reduces to a single fill threshold, as the churn floor is today.
[The ablation](#ablation-without-the-logarithm) works out what the logarithm contributes.

**No page at or above $u_0$ is ripe, whatever its rate.**
Its survivors would take a whole page or more, so cleaning it would free nothing: $1 - x$, the space a cleaning gains, is not positive.
The formula has to say so explicitly, since $h$, which falls to 0 at $x = 1$, rises again above it.
The draft takes $u_0 = 1 - \theta$, the fill that packing promises for every page but a flush's last.
Pages come out between $1 - \theta$ and full, so their expected fill is nearer $1 - \theta/2$; taking that instead would clean a little earlier, but would also make pages ripe whose survivors might fill a page of their own.
The cap also keeps full pages, among them nearly every page a flush has just written, out of the ranking altogether.

### A page holds a draining share and a static one

**Model a page's live content as a share $a$ that drains at rate $r$ and a share $s$ that does not drain at all, and clean it once $h(z) \geq r / \kappa$, with $z = a/(1 - s)$.**
Fills are relative to $u_0$, as above, so the page's fill is $x = a + s$.
$z$ is the fill of the draining share on the page with the static share taken out: the static share is copied once whenever the page is cleaned, and nothing about that changes by waiting, so it has no say in when.
With $s = 0$, this is the rule for content that dies at one rate.
With $a = 0$, the page would be ripe at any fill below $u_0$, which the floor $R_\text{min}$, [below](#estimating-how-fast-a-page-still-drains), limits.

**It is the mixture that the problem describes, as far as a page's losses can tell it.**
A page of hot and cold content is a draining share over a static one once its cold content has stopped dying, and until then the cold content is part of the draining share or the static one, whichever describes the page's losses better.
A page that starts with two shares that both drain, one fast and one slowly, becomes a draining share over a static one as the fast share dies: the slow share is then the draining one, and whatever has stopped dying the static one.
[The fit](#estimating-how-fast-a-page-still-drains) follows that change, with some lag.

**The rule follows from the same balance as before.**
Waiting one more epoch costs $\kappa \, (1 - x)$ and saves what the bytes that die meanwhile would have cost: $r \, a$ of them, each of which would have been copied now and cost $V$ afterwards.
At its own threshold $x^*$, content of rate $r$ balances the two, $\kappa \, (1 - x^*) = r \, x^* \, (1 + u_0 V)$, so $1 + u_0 V = (\kappa / r) \, g(x^*)$ with $g(x) = (1 - x)/x$, and waiting saves $\kappa \, a \, g(x^*)$.
The page is ripe once that no longer covers the cost:

$$
1 - x \geq a \, g(x^*)
\iff \frac{a}{a + 1 - x} \leq x^*
\iff h\Bigl(\frac{a}{1 - s}\Bigr) \geq \frac{r}{\kappa},
$$

since $g$ and $h$ both fall on $(0, 1)$ and $a + 1 - x = 1 - s$.
Once ripe, a page stays ripe, since $a$ and $x$ only fall as it drains; for a stopping problem of that kind, stopping at the first epoch at which one more epoch of waiting does not pay is optimal.

**A second draining share would need more than a page's losses determine, and a numerical solve for every index; the static share needs neither.**
With shares $a$ and $b$ draining at $r_1$ and $r_2$, the same argument makes a page ripe once $1 - x \geq a \, g\bigl(x^*(r_1/\kappa)\bigr) + b \, g\bigl(x^*(r_2/\kappa)\bigr)$, which has no closed form in $\kappa$, so every change to a page's estimate would need a root-finding to key it.
The page's history determines a draining share and its rate exactly, with nothing to spare, as [the fit](#estimating-how-fast-a-page-still-drains) shows, while two draining shares need one more statistic and a nonlinear fit in three unknowns, and separating two decay rates from one noisy decay curve is notoriously ill-conditioned.
The static share is the limit $r_2 \to 0$: $x^*(0) = 1$ and $g(1) = 0$, so the second term vanishes, and with it both problems.

### Estimating how fast a page still drains

**Each page keeps a running average of its losses, and refits its split from how far those losses have slowed since the page was written.**
The average forgets older losses at a rate $\beta$.
The split then follows from the average, the bytes the page has lost since it was written, and its age, with no further state.

**The page's history determines the draining share and its rate exactly.**
Under the model, a page written $t$ epochs ago with a draining share $a_0$ has since lost $D = a_0 \, (1 - e^{-r t})$ and now loses $\ell = r \, a_0 \, e^{-r t}$ per epoch.
Dividing one by the other leaves a single unknown:

$$
\frac{\ell \, t}{D} = \psi(r t),
\qquad \psi(w) = \frac{w}{e^w - 1},
$$

where the left side is the page's current loss rate over its average since it was written, and $\psi$ falls from 1 at $w = 0$ toward 0.

- **If the losses have slowed**, $\ell t / D < 1$, the fit solves $\psi(r t) = \ell t / D$ for $r$, and the draining share is what the current losses imply at that rate: $a = \ell / r$, and $s = x - a$.
  The more the losses have slowed, the faster the draining share must be dying, and the less of the page it can be.
- **If they have not**, $\ell t / D \geq 1$, nothing on the page is known to be static: $s = 0$, $a = x$, and $r = \ell / x$, the estimate for content that dies at one rate.

$\psi$ has no closed-form inverse, but it is smooth and monotonic, so a small table or a few Newton steps invert it, once per loss.

**Between losses, the split stays and the rate decays, which is what keeps the ranking cheap.**
`Drain::lose` only needs to be called in epochs where the page loses a nonzero amount of bytes.
Epochs that don't call `lose` are effectively treated as contributing zeros to the running discounted average, with the corresponding update of the running average performed lazily at the next call of `lose` and when the rate is inspected with `rate`.
The draining share's rate is the average over the share, so it decays with the average, by $e^{-\beta}$ per epoch, on every page alike.

```rust
/// Per page, beside kind, epoch and coverage: 12 bytes.
struct Drain {
    loss: f32,   // natural losses per epoch, in bytes, at epoch `at`: a discounted average
    at:   u32,   // the epoch of the page's last natural loss, from the session's base
    fast: u16,   // the live bytes of the draining share; the rest of the coverage is static
    lost: u16,   // the bytes the page has lost naturally since it was written
}

impl Drain {
    /// A flush's fold superseded `bytes` of the page's live bytes, `live` of which are left.
    fn lose(&mut self, bytes: u32, live: u32, age: u32, now: u32) {
        self.loss = self.loss * (-BETA * (now - self.at) as f32).exp()
                  + (1.0 - (-BETA).exp()) * bytes as f32;
        self.at = now;
        self.lost += bytes as u16;
        // How far have the losses slowed, against their average since the page was written?
        let slowed = self.loss * age as f32 / self.lost as f32;
        self.fast = if slowed < 1.0 {
            let rate = psi_inverse(slowed) / age as f32;
            (self.loss / rate).min(live as f32) as u16
        } else {
            live as u16
        };
    }

    /// The draining share's rate, a fraction of it per epoch.
    fn rate(&self, now: u32) -> f32 {
        self.loss * (-BETA * (now - self.at) as f32).exp() / self.fast as f32
    }
}
```

A page whose losses hold steady keeps $\ell t / D$ near 1, and is estimated as one draining population, at the rate of its losses.
A page with no draining share left, `fast == 0`, has no rate, and is ranked by the floor alone.

**The average needs no bias correction, because it never starts from zero.**
Adam divides its averages by $1 - e^{-\beta (t - t_0)}$ because they start at 0, which would bias them toward 0 until the observations had built up.
Here a page starts from a real estimate at its creation epoch $t_0$, as "A new page starts from what it holds" below describes, and after $k$ epochs the estimate is a weighted mean of that start and the losses observed since, with weights that already sum to 1:
$e^{-\beta k} + (1 - e^{-\beta}) \sum_{j=0}^{k-1} e^{-\beta j} = e^{-\beta k} + (1 - e^{-\beta k}) = 1$.
So the start plays the part of a prior worth about $1/\beta$ epochs of observations, and fades as they come in.
Starting a page from 0 instead would be exactly the bias Adam corrects for, and worse here: the page would look frozen, and so ripe, until its first losses arrived.
Below, $\hat r$ stands for a page's `rate()`, and $R_\text{min}$ for `R_MIN`.

**Only natural losses count**: bytes that the application's writes, frees, and shrinks supersede, which reach a page through the fold.
Bytes that consolidation moves out of a page say nothing about how fast the rest will die, so evacuation, the page rewrite, the rotating window, and [the cursor](#the-flushs-own-pages-take-survivors-from-the-previous-flush) leave the estimate alone.
Where they move out part of a page, which share they came from is unknown, so they shrink `loss`, `fast`, and `lost` in proportion, which leaves the fit where it was.

**The fit follows a page whose content changes character, if with a lag.**
Take a page that starts with a share of 0.3 dying at 0.5 per epoch, a share of 0.3 dying at 0.02, and a share of 0.4 that does not die at all, with $u_0 = 1$.
After 50 epochs, the first share is gone and 0.11 of the second is left, so the page is truly a draining share of 0.11 at 0.02 over a static share of 0.4, with an index of 137.
Given its loss rate exactly, the fit finds a draining share of 0.044 at 0.05 over a static share of 0.47, with an index of 173.
The first share's losses still count in $D$, so the losses seem to have slowed more than the second share's have: the fit takes the rate for higher and the draining share for smaller than they are, and the page for somewhat riper.
At $\kappa = 0.01$, both indexes lie above $1/\kappa = 100$, and the page is ripe either way.

**The floor $R_\text{min}$ is the assumption that no content lasts forever: no page is riper than it would be if all of its live content drained at $R_\text{min}$.**
Without it, a page whose draining share had died, or whose estimate had decayed to nothing, would be ripe at any fill below $u_0$.
With it, the index of a page is

$$
I = \min\Bigl(\frac{h(z)}{\hat r}, \; \frac{h(x)}{R_\text{min}}\Bigr),
$$

and a frozen page is ripe up to the relative fill $x_\text{cold}$ for which $h(x_\text{cold}) = R_\text{min} / \kappa$ — at $R_\text{min} = 10^{-4}$ and $\kappa = 0.01$, that is 0.87.
For a page without a static share, the minimum is a floor on the rate, $\max(\hat r, R_\text{min})$.
For one with a static share, it is exact at both ends, and in between it takes the page for riper than a static share that drains at $R_\text{min}$ would make it, since that share's slow drain would add to what waiting saves.

**A new page starts from what it holds.**
Fresh content joins its draining share, at a running estimate of what pages lose in the epoch after they are written.
Moved content brings its source page's split: what was static there is static here, and the rest joins the draining share at the source's rate.
`loss` starts as the sum, over the parts of the draining share, of each part's rate times its size, and the page keeps its starting split until its first loss.

**Open restores each page's estimate from the [consolidator state](../impl/consolidator-state.md#what-the-ripeness-draft-would-add), and seeds a page the state cannot vouch for from its fill and its age.**
The seed assumes that the page has drained at one rate since it was written, from the fill its framing records, `content_size`, to its current coverage, at $\hat r = \ln(\texttt{content\_size} / \mathrm{coverage}) / \mathrm{age}$.
So all of its content is draining, `fast` is the coverage, `loss` is $\hat r$ times it, `lost` is $\texttt{content\_size} - \mathrm{coverage}$, and `at` is the current epoch.
The age counts from the epoch the session's first flush will have, so that no page is younger than one epoch.
The file records nothing that could reveal a static share, so the seed has none, and averages over the page's whole life: a page that drained early and then froze looks at first like one still draining slowly.
The session's own observations correct it: the loss average decays, and the first losses refit the split.

**The seed counts the bytes consolidation moved out as losses, which errs on the safe side.**
The file records neither how a page lost its bytes nor when, so this cannot be avoided without the state.
Moved-out bytes make the page look as if it had drained faster than it did, which raises its rate, lowers its index, and so delays its cleaning: the error can hold garbage a little longer, but never makes a page ripe that the true rate would not.
It is also rare.
Evacuation, the page rewrite, and the rotating window move whole pages, which are then free rather than seeded; only the cursor and description defragmentation move part of a page, and the cursor empties its page within a flush or two.

### Ranking pages by ripeness

**Rank every page below $u_0$ by its ripeness index $I = \min\bigl(h(z)/\hat r, \, h(x)/R_\text{min}\bigr)$, which is the $1/\kappa$ at which the page becomes ripe.**
Because every rate estimate decays by the same factor $e^{-\beta}$ per epoch while the splits stay put, every index below its floor grows by the same factor, so the order changes only when a page's own content does, and an ordered map keeps it.

**Two ways would rank by a time-dependent score exactly, and the second is chosen.**
The current design samples only because its bucket queues order pages by fill, and the age term changes every page's score every epoch.

- LFS's score is a line in time for each page, with a slope $(1 - u)/(1 + u)$ of its own, so pages overtake one another as time passes.
  Maintaining the maximum of a changing set of lines is what a kinetic tournament does: $O(\log P)$ per change of a page's fill, plus a repair every time one page overtakes the one it was compared with.
  That second cost would be paid per epoch, whether or not any page changed.
- A score whose dependence on time is common to all pages keeps its order until a page changes.
  The ripeness index has that property as long as a page's index stays below its floor:

$$
\ln I(\texttt{now}) = K + \beta \cdot \texttt{now},
\qquad \text{with} \quad K = \ln h(z) - \ln \frac{\texttt{loss}}{\texttt{fast}} - \beta \cdot \texttt{at}
$$

$K$ changes only when the page loses content, is written, or has content moved out of it, and then once per flush however many of its fragments changed.

```rust
/// A log-index, totally ordered.
type Key = OrderedF32;

struct Ripeness {
    draining: BTreeSet<(Key, PageNumber)>,   // K; the index grows as e^(β·now)
    settled:  BTreeSet<(Key, PageNumber)>,   // ln h(x) − ln R_MIN; the index is constant
}

impl Ripeness {
    /// The page with the highest index, if it is ripe at price `e^(−neg_ln_kappa)`.
    fn next_ripe(&mut self, now: u32, neg_ln_kappa: f32) -> Option<PageNumber> {
        // A draining page whose index has passed its floor is overstated by its key,
        // so it can surface here but never hide further down: it is settled lazily.
        while let Some(&(k, p)) = self.draining.last() {
            if k + BETA * now <= key_settled(p) { break; }
            self.draining.remove(&(k, p));
            self.settled.insert((key_settled(p), p));
        }
        let draining = self.draining.last().map(|&(k, p)| (k + BETA * now, p));  // ln I(now)
        let (ln_index, p) = draining.max(self.settled.last().copied())?;
        (ln_index >= neg_ln_kappa).then_some(p)
    }
}
```

The controller keeps the price as $-\ln \kappa$, which is what the keys compare with, and a step in it changes $\kappa$ by a factor, which suits a price that ranges over orders of magnitude.

This gives up the bucket queues' $O(1)$ for $O(\log P)$ per changed page and per victim.
**An indexed binary max-heap would do as well as the B-trees, with less memory.**
Each page's entry in the page table would hold its position in its heap, 4 bytes, so that a changed key can be sifted up or down from where it is; every swap updates the two pages' positions, which is why a stock heap will not do.
The trees need no position, but hold the key and page number again in every node.
Both are $O(\log P)$ per changed key, and the heap is $O(1)$ for the maximum, which `next_ripe` reads on every call.
If that ever shows in a profile, note that the current value of $\ln I$ lies in a window of bounded width — $z$ and $x$ are at least one byte in a page, and a rate that matters is at least $R_\text{min}$ and at most the whole draining share per epoch — so a circular array of buckets over quantised keys, turned like a timer wheel as $\beta \cdot \texttt{now}$ advances, would do the same in $O(1)$.

**Both page kinds are ranked alike.**
A table page drains as the fold supersedes its statements, and a page of space and a page write cost the same whichever kind they are, so indexes compare across kinds and the budget loop takes the highest of either.

**At open, the index resembles LFS's score for nearly full pages, and exceeds it by far for nearly empty ones.**
Take a page written at $u_0$ and seeded with $\hat r = \ln(1/x) / \mathrm{age}$ and no static share, so that $z = x$.
Its index is $I = h(x) \cdot \mathrm{age} / \ln(1/x)$, proportional to its age, as LFS's score $(1 - x) \cdot \mathrm{age} / (1 + x)$ is; the two differ in how the factor depends on the fill.

- Nearly full, $x \to 1$: $h(x) \approx (1 - x)^2/2$ and $\ln(1/x) \approx 1 - x$, so $I \approx (1 - x) \cdot \mathrm{age} / 2$, which is LFS's score to first order.
  Both fall to 0.
- Nearly empty, $x \to 0$: $h(x) \approx 1/x$ grows faster than $\ln(1/x)$, so $I \approx \mathrm{age} / \bigl(x \ln(1/x)\bigr)$ grows without bound, while LFS's factor rises only to 1, leaving its score at $\mathrm{age}$.
  A nearly empty page costs almost nothing to clean, so ripeness takes it at any age, where LFS would still rank it by age.

### The budget, and the price

**A flush cleans ripe pages, highest index first, until its page budget is spent: ripeness takes the churn floor's place.**
The budget stays what it is, a cap on the work one flush does, and so does the check that an offer fills its page.

**$\kappa$, not the budget, is what moves the file toward its target fill $\tau$.**
Raising $\kappa$ raises every page's threshold, so a controller that measures the live fraction after each commit and moves $\kappa$ up while it lies below $\tau$, and down while above, converges on $\tau$ wherever the budget allows — the job the current design gives the budget.
$\lambda$ could not do that job, since it would clean frozen and draining pages at the same fill; $\kappa$ can, because the thresholds it sets depend on how fast each page drains.

**The policy does not change when the budget binds: it always takes the highest index first, which matters only then.**
While the budget does not bind, a flush cleans every ripe page, and the order is immaterial.
When it binds, the flush can clean only some of them, and the order decides which.
A binding budget means that page writes are scarcer than $\kappa$ assumes, which is the same as a lower price for space, $\kappa' < \kappa$; the pages that would be ripe at $\kappa'$ are those with $I \geq 1/\kappa'$, which are exactly the highest indexes.
So the one rule is right in both cases, and no mode switch is needed.
If the budget binds flush after flush, ripe pages queue up and the controller cannot reach $\tau$: the target and the cap conflict, and the cap wins, as it should.
The controller must then not wind up: raising $\kappa$ would only lengthen the queue, so it holds $\kappa$ while the budget binds.

### Where survivors go

**Whether to free a page now and where its survivors should go are separate decisions, and neither needs pairs.**
Whether freeing a page now pays depends only on that page, and is its ripeness.
What its survivors cost afterwards depends only on what they share a page with.
A counterfactual defined by pairs — wait until two pages fit into one — would not even exist for pages that never will, such as two frozen at 60 %.

#### What a mixed page costs

**Once its fast share $a$ has died, a mixed page is just a page of fill $b$ whose content drains at $r_B$, so mixing is cheap when $b$ lies below that content's own threshold $u^*(r_B)$, and expensive when it lies above.**
For the page of the mixture above:

- **$b \leq u^*(r_B)$.** The page is ripe as soon as the fit has seen its losses slow, which takes the few multiples of $1/\beta$ epochs the loss average needs to fall, during which it holds its garbage $a$.
  Cleaning it then copies the slow share once more.
  When that share was moved in from a victim, whose own cleaning would have copied it anyway, mixing defers a copy rather than adding one, and frees the victim early.
  The smaller $b$, the shorter the wait, since $h(b)$ grows like $1/b$.
- **$b > u^*(r_B)$.** The page holds its garbage $a$ until its slow share has drained down to $u^*(r_B)$.
  For content that has stopped dying, which is what survivors of ripe pages are, that is never.

So the room of a page is best filled with content that dies at about the rate of what the page already holds.
Cold content in a hot page costs a wait that grows with its share, and above $u^*$ it costs the page's garbage for good.

**So each source of survivors has one place it may go, decided by its temperature and its size, not by a partner.**
The case distinction gives the rule: survivors may join a hotter page only as a share $b$ small enough to stay below their own threshold $u^*(r_B)$; otherwise they need a page of their own temperature.
The next two sections apply it to the two kinds of page that take survivors:

| source | its share $b$ in the host | where it goes | case |
| --- | --- | --- | --- |
| survivors of ripe pages, except the small ones below | any | pages the budget loop opens, each filled with survivors of many ripe pages | cold with cold: no hot content to wait for |
| a ripe page whose survivors take at most $\theta$ of a page | at most $\theta$ | the room of a flush's own page | $b \leq u^*(r_B)$, as long as $\theta$ lies below the threshold of content that has stopped dying |
| the survivors of the previous flush | whatever fits | the room of a flush's own page | little mixing: about the host's temperature |

None of these needs a counterfactual about pairs: each source is judged by its own rate and size against a threshold, and the room is simply offered in that order.

#### Budgeted pages take survivors of ripe pages

**A page opened for consolidation should hold survivors of ripe pages only, cold with cold, so that it stays full.**
This is LFS's segregation.
The current budget loop already comes close: a page it opens holds survivors of victims and nothing else.
The proposal changes two things about it.
First, the victims are the ripe pages, taken in index order, rather than pages that pass the churn floor.
Second, the loop's first page no longer takes the rest of a victim cut to fill the flush's last page; the cursor below fills that room instead, so no victim is split between a hot page and a cold one.

#### The flush's own pages take survivors from the previous flush

**This is the placement half of the proposal, and apart from its first step it would help any cleaning policy.**
Ripeness decides when to clean; this decides what goes into the room of the pages a flush writes anyway, so that fewer frozen pages are made in the first place.
Only step 1 uses the index.

**The room in every data page a flush writes anyway goes first to small ripe victims, then to the survivors of the previous flush's least-filled page.**

1. Whole ripe victims that fit the room and whose survivors take at most $\theta$ of a page, highest index first.
   Each frees a page for almost no room, and a host that freezes at under $\theta$ is ripe soon after its own content has died.
2. The survivors of the **cursor page**, as many of its statements' as fit, a whole statement's at a time, so that the cursor never cuts a statement.
   The cursor page is the least-filled data page of the latest earlier epoch that wrote data pages, chosen at open among the governing header's epoch's pages and again whenever the current one is empty.

**A statement too long for the room cannot stop the cursor for good.**
The cursor takes whichever of the page's statements fit, not only the next in order, so a long one is skipped until a flush leaves room enough for it.
If no flush does, the page grows old, and the rule below that gives up a page after $W$ epochs moves the cursor on and leaves the rest of the page to ripeness.
A statement can be at most a page long, so it takes a flush whose last page leaves that much room; how often a page is abandoned this way is a question for measurement.

**The previous flush's content is the closest in age to the flush's own that exists outside the flush itself.**
A page *closes short* when it is written with more than $\theta$ of it empty, which only a flush's last page can, when the flush runs out of content and free filling finds nothing to fill the room with.
Unless the previous flush's last page closed short, its least-filled page is the one the current flush's fold drained most, which is the hottest of what the previous flush wrote; a page that closed short is least filled because of how it was written, not because anything died.
So the room of the flush's own pages fills with content of about their own temperature, and the shortfall of the flush's last page, about half a page on average, comes out of a page that the next flush or two empty and free, with no page written for it.
With about half a page of room per flush and at most a page to empty, the cursor stays about two epochs behind the flush.

Four details keep it from going wrong:

- **It still mixes, only less.**
  The previous flush's survivors are exactly what the current flush did not rewrite, so they are colder than fresh content.
  No other source matches fresh content better, and ripeness cleans whatever freezes.
- **It moves content without writing a page, but not for free.**
  Every survivor it moves is stated again in the flush's epoch, and its old statement becomes garbage in a table page.
  The volume is bounded by the room of the flush's own pages, about half a page per flush.
- **It does not count as a loss** in the cursor page's estimate, and the host's estimate starts as the byte-weighted mix of fresh and moved content.
- **It gives up a page that has grown old.**
  Flushes that end nearly full leave little room, and a cursor page drained slowly across many of them would hand out ever older content; once it is more than $W$ epochs old, the cursor moves on to the least-filled page of the latest earlier epoch, and the rest of the old page is left to ripeness.

In [compaction mode](../impl/consolidation.md#compaction-mode), the tail page still takes the room ahead of both, since returning the tail is worth a mixed page.

## What would change in Consolidation

- [Scoring a data page](../impl/consolidation.md#scoring-a-data-page) would give way to the ripeness index, for table pages as well, and sampling would go.
  The bucket queues would give way to the two ordered sets, and victim selection would cost $O(\log P)$ rather than $O(1)$.
- [The churn floor](../impl/consolidation.md#the-churn-floor-is-a-parameter-not-an-identity) would give way to ripeness, and $\lambda$ to $\kappa$; the controller would move $\kappa$ toward $\tau$, and the budget would remain a cap.
- [A free sink changes the arithmetic](../impl/consolidation.md#victims-are-pulled-one-at-a-time) — "free filling takes any victim that fits" — would no longer hold: free filling would take small ripe victims and the cursor's survivors.
- [Free filling's order](../impl/consolidation.md#packing-in-id-order-with-look-ahead) would shrink to those two sources, with the cursor page taking the place of the victim too big to take whole.
- [The page table](../impl/in-memory-state.md#4-the-page-table) would gain `Drain`, 12 bytes per page, and the [consolidator state](../impl/consolidator-state.md#what-the-ripeness-draft-would-add) would carry it, with $\kappa$, from one session to the next.
- [The constants still to be chosen](../impl/consolidation.md#constants-still-to-be-chosen) would lose $\lambda$ and gain $\kappa$'s controller, $\beta$, $R_\text{min}$, and $W$.

## Ablation: without the logarithm

**Dropping $-\ln(1/x)$ would make the split unnecessary, and keep the per-page threshold and with it the cleaning of frozen pages; but it would clean pages that drain slowly too early, with extra writes that grow the slower they drain, and could not tell a page on a static share from one that drains slowly throughout.**
The ablated rule is the myopic one: a page is ripe once $g(z) = (1 - z)/z \geq r/\kappa$, and its index is $g(z)/\hat r$.

**The split would drop out, and with it the fit.**
With $z = a/(1 - s)$ and $\hat r = \ell / a$, the index is $g(z)/\hat r = (1 - x)/\ell$: the page's garbage over its loss rate in bytes, whatever the split.
So `Drain` would shrink back to the loss average and its epoch, 8 bytes, and neither $\psi$ nor the refit would be needed.
The threshold would also have a closed form, $x^* = 1/(1 + r/\kappa)$, though nothing needs it.
The keys would still be logarithms, and the ordered sets, the floor, and the controller would stay as they are.

**It behaves as the full rule does in every limit but one.**

- **All pages drain at one rate.**
  Both rules reduce to a single fill threshold, as the churn floor does, and the controller moves $\kappa$ until that threshold gives $\tau$.
  All three then clean the same pages: the logarithm changes only which $\kappa$ gets there.
- **Nearly empty pages**, $x \to 0$: $h(x) \approx g(x) \approx 1/x$, so the two agree.
- **Content that dies fast**, $r/\kappa \gg 1$: both clean once the page is nearly empty, at 0.07 and 0.09 for $r/\kappa = 10$.
- **Content that drains slowly**, $r/\kappa \ll 1$, is the exception: near $x = 1$, $h(x) \approx (1 - x)^2/2$ but $g(x) \approx 1 - x$, so the thresholds are about $1 - \sqrt{2 r/\kappa}$ and $1 - r/\kappa$, exactly 0.87 and 0.99 for $r/\kappa = 0.01$.
  The myopic rule ignores that a byte which dies now would otherwise have been copied again and again, so it copies such a page as soon as a percent of it is garbage.

**It could not tell a page on a static share from one that drains slowly throughout.**
Take two pages at $x = 0.8$ that lose the same bytes per epoch: one a draining share of 0.1 at rate $r$ over a static share of 0.7, the other all draining, at $r/8$.
Their ablated indexes are equal, $2/r$.
The full index ranks the first at $h(1/3)/r = 0.90/r$ and the second at $8 \, h(0.8)/r = 0.22/r$, four times lower: waiting pays on the second, whose survivors would go on dying, and hardly at all on the first, whose survivors would not.

**Frozen pages are cleaned either way, since the floor makes every page's threshold its own.**
With $R_\text{min}$ in place of $r$, the ablated rule makes a frozen page ripe below 0.99 rather than 0.87, at $R_\text{min}/\kappa = 0.01$.
That page is copied once, its survivors never die, and so the early cleaning costs one copy, not a cycle of them.
It does rank frozen pages much higher: at $x = 0.95$, $g/R_\text{min} = 526$ against $h/R_\text{min} = 13$, so under a binding budget they would take writes that would free more elsewhere.

**Expect the same results on uniform workloads, and more page writes for the same space on skewed ones, most of them spent on warm content.**
Over its whole life, content that drains at $r/\kappa$ costs $V$ at the myopic threshold against $V$ at the optimal one (taking $u_0 = 1$):

| $r / \kappa$ | 10 | 1 | 0.1 | 0.01 |
| --- | --- | --- | --- | --- |
| cost of the myopic rule, relative to the optimum | 1.0 | 1.2 | 2.5 | 7 |

Retuning $\kappa$ cannot undo this, since the error lies in how the thresholds of pages of different rates relate, not in their level, and there is no error when all pages drain at one rate.
The warm content that pays is whatever lives for longer than $1/\kappa$ epochs but does die, 100 epochs at $\kappa = 0.01$.
Running the evaluation with $h$ replaced by $g$ would measure how much of a real workload that is.

## Open questions

- **The clock.**
  Rates per epoch count flushes rather than work: a run of tiny flushes makes every page look colder than it is.
  A clock that counts the bytes the application writes would measure drain per unit of work, but pages record only their epoch, so the seed at open would still have to use epochs.
- **The flush's own content.**
  Packed in key order, it mixes allocations the application keeps rewriting with ones it has just created, which is the largest source of mixed pages this draft leaves in place.
  [`last_written`](../impl/in-memory-state.md#3-the-allocation-map) could split a flush's chunks by whether their allocation was also written recently, at the price of key order; whether that pays is a question for measurement.
- **Description defragmentation's rewrites** are cold by selection and today share pages with the flush's fresh content.
  Packing them with the survivors of ripe pages instead would keep them apart, at the price of a second page per flush that may close short.
- **The fit's memory.**
  $D$ and $t$ count from the page's write, so a share that died early keeps weighing on the fit, as in the example above, however long ago it died.
  Discounting them as the loss average is discounted would forget it, but a discounted history can only measure rates below $\beta$, which the fast shares are not.
- **A static share that turns out not to be.**
  Losses on a page that had looked static make its current losses exceed their average, and the fit then treats the whole page as one share draining slowly, which is less ripe than before, until the loss average decays again.
  Whether that delay costs anything on real workloads is a question for measurement.
- **$\beta$** trades how soon a frozen page is recognised against noise: with a large $\beta$, a page that loses content in rare bursts looks frozen between them.
