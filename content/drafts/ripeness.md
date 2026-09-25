---
title: Cleaning by ripeness
---

**Status: a proposal, not adopted.**
It would replace data-page scoring, the churn floor, and part of free filling in [Consolidation](../impl/consolidation.md).

**Clean a page once waiting no longer pays, not once it has become sparse.**
A page whose content is still dying should wait, since every byte that dies before the page is cleaned is a byte the cleaning need not copy.
A page whose content has stopped dying gains nothing by waiting, however full it is, and its garbage stays until something moves it.
This draft turns that into three things: a threshold per page, derived from a price for space; an estimate of how fast each page still drains, chosen so that pages can be ranked by the threshold without rescanning them as time passes; and a placement rule that makes fewer frozen pages in the first place.

## The problem

### A mixed page drains, then freezes

**A page written with hot and cold content together loses its hot share within a few flushes and then stops losing anything: it freezes at its cold share, which can be any fill.**
Nothing in a kladde file is overwritten in place: an update writes new bytes to a fresh page and leaves the old ones dead where they were.
So a page is written nearly full and afterwards only loses live bytes.
Workloads are skewed — a small hot set is rewritten over and over, a cold majority seldom or never — and a page holds whatever one flush wrote together.
Packed in key order, the allocations the application keeps rewriting sit next to ones it has just created and will not touch again.

The pages of a file therefore fall into two populations.
Pages whose content keeps dying cycle: written full, drained, cleaned, written again.
Frozen pages stay wherever they stopped, and nothing about skew says where that is; all that is known about their fill is that it lies somewhere between the cleaning threshold and full.
A policy that cleans only below a fixed fill keeps every frozen page above that fill for good, and its garbage with it.

### Why the current design never cleans a frozen page

**Two rules keep a frozen page from ever being chosen, and a third makes new frozen pages.**
[Victim selection](../impl/consolidation.md#scoring-a-data-page) keeps data pages in bucket queues ordered by fill alone, so the age term of its score can only be evaluated on a sample: the next victim is the best-scoring of `K` pages drawn from the sparsest non-empty bucket.

1. The pages that keep draining keep that bucket stocked, so a frozen page in a fuller bucket is never drawn, and age only ever decides between pages of about the same fill.
2. [The churn floor](../impl/consolidation.md#the-churn-floor-is-a-parameter-not-an-identity) rejects every page more than `1 / (1 + λ)` live — about half, for `λ` near 1 — whatever its age.
3. [Free filling](../impl/consolidation.md#two-forms-of-consolidation) puts survivors, which are cold by selection, into the room of the flush's own pages, next to fresh content; once the fresh content dies, the page freezes at the survivors' share.

### What LFS did about it

**LFS cleaned cold segments at a high fill and packed their survivors together, and it needed both.**

- Cleaning sorts content by temperature.
  A survivor of a cleaned segment is cold almost by definition, having outlived everything around it, and a segment written from survivors alone stays full: a frozen segment is rewritten once, and its content never drains again.
- Its cost-benefit score, `(1 − u) · age / (1 + u)`, values a cold segment's free space more, because that space stays free.
  In LFS's simulations of a hot-and-cold workload, the cleaner took cold segments at about 75 % utilisation and let hot ones drain to about 15 %.

This draft keeps both ideas, but derives the threshold from a cost model rather than tuning a score, and estimates temperature in a way that makes ranking by it cheap.

## The proposal

### When waiting no longer pays

**Clean a page of fill `u` whose content dies at rate `r` once `h(u) ≥ r / κ`, with `h(u) = (1 − u)/u − ln(1/u)`.**
Here `κ` is the price of one page of space held for one epoch, measured in page writes: at `κ = 0.01`, a page of garbage kept for a hundred flushes costs as much as writing a page.
Such a page is **ripe**.

The model behind it makes three assumptions:

- A page's live content dies at a rate `r`, a fraction `r` of it per epoch, however long it has lived, so survivors keep dying at `r` after they are moved.
- A policy pays `κ` per page of garbage per epoch and 1 per page of survivors it copies, and is judged by its cost per epoch in the long run.
- It cleans a page once its fill has fallen to a threshold `u*`, and packs the survivors with content that dies at the same rate.

Follow one unit of content from the moment it is written into a full page.
The page reaches `u*` after `T = ln(1/u*) / r` epochs.
If the unit dies at some `s < T`, it is garbage until `T`, which costs `κ · (T − s)`; otherwise it is copied at `T`, which costs 1, and starts over in a fresh page.
So its expected cost over its whole life, `V`, satisfies

```
V = κ · E[(T − s)⁺] + u* · (1 + V),      with E[(T − s)⁺] = (ln(1/u*) − (1 − u*)) / r
V = (κ · (ln(1/u*) − (1 − u*)) / r + u*) / (1 − u*)
```

and setting `dV/du* = 0` gives `h(u*) = r / κ`.
The same threshold balances the two sides of waiting one more epoch at fill `u`: it costs `κ · (1 − u)` in garbage and saves `r · u · (1 + V)`, since each byte that dies meanwhile would have cost a copy now and `V` afterwards.

| `r / κ` | `u*`, cleaning at | the myopic rule `(1 − u)/u ≥ r / κ` would clean at |
| --- | --- | --- |
| 0 | any fill, up to the cap below | any fill |
| 0.001 | 0.96 | 0.999 |
| 0.01 | 0.87 | 0.99 |
| 0.1 | 0.66 | 0.91 |
| 1 | 0.32 | 0.5 |
| 10 | 0.07 | 0.09 |

Content that has stopped dying is cleaned at almost any fill, and content that dies fast only once it is nearly gone, which is LFS's behaviour, derived rather than tuned.

**The churn floor is the special case in which every page drains at the same rate, without the logarithm.**
`(1 − u)/u` is the space a cleaning frees per byte it writes, which is exactly what the churn floor compares with `λ`.
The `− ln(1/u)` term accounts for survivors that go on dying after they are moved; the myopic rule, which leaves it out, would clean every page too early, and pages that drain slowly far too early.
Under uniform random updates every page does drain at one rate, so there ripeness reduces to a single fill threshold, as the churn floor is today.

**No page above `1 − θ` is ripe, whatever its rate.**
Packing promises no fuller page than that, so cleaning such a page might free nothing.
This also keeps full pages, among them nearly every page a flush has just written, out of the ranking altogether.

### Estimating how fast a page still drains

**Each page's rate is estimated from its own recent losses, with older losses forgotten at a rate `β`.**

```rust
/// Per page, beside kind, epoch and coverage: 8 bytes.
struct Drain {
    rho: f32,   // the rate estimate at epoch `at`: a fraction of live bytes per epoch
    at:  u32,   // the epoch of the page's last natural loss, from the session's base
}

impl Drain {
    /// A flush's fold superseded `lost` of the page's `live` bytes.
    fn lose(&mut self, lost: u32, live: u32, now: u32) {
        self.rho = self.rho * (-BETA * (now - self.at) as f32).exp()
                 + (1.0 - (-BETA).exp()) * lost as f32 / live as f32;
        self.at = now;
    }

    fn rate(&self, now: u32) -> f32 {
        (self.rho * (-BETA * (now - self.at) as f32).exp()).max(R_MIN)
    }
}
```

A page that loses a fraction `f` of its live bytes every epoch settles at `rate() = f`.

**Only natural losses count**: bytes that the application's writes, frees, and shrinks supersede, which reach a page through the fold.
Bytes that consolidation moves out of a page say nothing about how fast the rest will die, so evacuation, the page rewrite, the rotating window, and [the cursor](#the-flushs-own-pages-take-survivors-from-the-previous-flush) leave the estimate alone.

**The estimate follows a mixed page as its content changes.**
A page holding a share `a` of content that dies at `r_A` and a share `b = 1 − a` that dies at `r_B < r_A` has fill `u(t) = a·e^(−r_A·t) + b·e^(−r_B·t)`.
Its rate, `−u′/u`, starts at `a·r_A + b·r_B` and falls to `r_B` as the fast share dies, and the estimate does the same: high while the losses come, and decaying once they stop.

**The floor `R_MIN` is the assumption that no content lasts forever**: without it, an estimate that had decayed to nothing would make a frozen page ripe at any fill up to the cap.
With it, a frozen page is ripe up to the fill `u_cold` for which `h(u_cold) = R_MIN / κ` — at `R_MIN = 10⁻⁴` and `κ = 0.01`, that is 0.87.

**A new page starts from what it holds.**
Its `rho` is the byte-weighted mean of its content's rates: moved content at its source page's rate, and fresh content at a running estimate of what pages lose in the epoch after they are written.

**Open restores each page's estimate from the [consolidator state](../impl/consolidator-state.md#what-the-ripeness-draft-would-add), and seeds a page the state cannot vouch for from its fill and its age.**
The seed assumes that the page has drained at one rate since it was written, from the fill its framing records, `content_size`, to its current coverage: `rho = ln(content_size / coverage) / age`, with `at` set to the current epoch.
`age` counts from the epoch the session's first flush will have, so that no page is younger than one epoch.
That averages over the page's whole life, so a page that drained early and then froze looks at first like one still draining slowly; a few multiples of `1/β` epochs of the session's own observations correct it.

### Ranking pages by ripeness

**Rank every page below `1 − θ` by its ripeness index `I = h(u) / rate`, which is the `1/κ` at which the page becomes ripe.**
Because every estimate decays by the same factor `e^(−β)` per epoch, every index grows by the same factor, so the order changes only when a page's own content does, and an ordered map keeps it.

**Two ways would rank by a time-dependent score exactly, and the second is chosen.**
The current design samples only because its bucket queues order pages by fill, and the age term changes every page's score every epoch.

- LFS's score is a line in time for each page, with a slope `(1 − u)/(1 + u)` of its own, so pages overtake one another as time passes.
  Maintaining the maximum of a changing set of lines is what a kinetic tournament does: `O(log P)` per change of a page's fill, plus a repair every time one page overtakes the one it was compared with.
  That second cost would be paid per epoch, whether or not any page changed.
- A score whose dependence on time is common to all pages keeps its order until a page changes.
  The ripeness index has that property as long as a page's estimate stays above the floor:

```
ln I(now) = K + β · now,      with K = ln h(u) − ln rho − β · at
```

`K` changes only when the page loses content, is written, or has content moved out of it, and then once per flush however many of its fragments changed.

```rust
/// A log-index, totally ordered.
type Key = OrderedF32;

struct Ripeness {
    draining: BTreeSet<(Key, PageNumber)>,   // K; the index grows as e^(β·now)
    settled:  BTreeSet<(Key, PageNumber)>,   // ln h(u) − ln R_MIN; the index is constant
}

impl Ripeness {
    /// The page with the highest index, if it is ripe at price `kappa`.
    fn next_ripe(&mut self, now: u32, kappa: f32) -> Option<PageNumber> {
        // A draining page whose estimate has reached the floor is overstated by its key,
        // so it can surface here but never hide further down: it is settled lazily.
        while let Some(&(k, p)) = self.draining.last() {
            if drain[p].rate(now) > R_MIN { break; }
            self.draining.remove(&(k, p));
            self.settled.insert((key_settled(p), p));
        }
        let draining = self.draining.last().map(|&(k, p)| (k + BETA * now, p));  // ln I(now)
        let (ln_index, p) = draining.max(self.settled.last().copied())?;
        (ln_index >= -kappa.ln()).then_some(p)
    }
}
```

This gives up the bucket queues' `O(1)` for `O(log P)` per changed page and per victim.
If that ever shows in a profile, note that the current value of `ln I` lies in a window of bounded width — `u` is at least one byte in a page, and `rate` is at least `R_MIN` and at most 1 — so a circular array of buckets over quantised keys, turned like a timer wheel as `β · now` advances, would do the same in `O(1)`.

**Both page kinds are ranked alike.**
A table page drains as the fold supersedes its statements, and a page of space and a page write cost the same whichever kind they are, so indexes compare across kinds and the budget loop takes the highest of either.

**At open, the index resembles LFS's score.**
With the seeded estimate, a page written full has `I = h(u) · age / ln(1/u)`, which for a nearly full page is `(1 − u) · age / 2` to first order, as LFS's score is.
As `u` falls toward 0 it grows without bound, where LFS's score levels off at `age`: a nearly empty page costs almost nothing to clean, whatever its age.

### The budget, and the price

**A flush cleans ripe pages, highest index first, until its page budget is spent: ripeness takes the churn floor's place.**
The budget stays what it is, a cap on the work one flush does, and so does the check that an offer fills its page.

**`κ`, not the budget, is what moves the file toward its target fill `τ`.**
Raising `κ` raises every page's threshold, so a controller that measures the live fraction after each commit and moves `κ` up while it lies below `τ`, and down while above, converges on `τ` — the job the current design gives the budget.
`λ` could not do that job, since it would clean frozen and draining pages at the same fill; `κ` can, because the thresholds it sets depend on how fast each page drains.

**Taking the highest index first is right when the budget binds.**
A binding budget acts like a lower price for space, since writes then cost more than `κ` says, and the pages ripe at a lower price are exactly those with the highest indexes.
If it binds flush after flush, ripe pages queue up and the controller cannot reach `τ`: the target and the cap conflict, and the cap wins, as it should.

### Where survivors go

**Whether to free a page now and where its survivors should go are separate decisions, and neither needs pairs.**
Whether freeing a page now pays depends only on that page, and is its ripeness.
What its survivors cost afterwards depends only on what they share a page with.
A counterfactual defined by pairs — wait until two pages fit into one — would not even exist for pages that never will, such as two frozen at 60 %.

#### What a mixed page costs

**Once its fast share has died, a mixed page is just a page of fill `b` whose content drains at `r_B`, so mixing is cheap when `b` lies below that content's own threshold `u*(r_B)`, and expensive when it lies above.**
For the page of the mixture above:

- **`b ≤ u*(r_B)`.** The page is ripe as soon as the estimate has seen its fast losses stop, a few multiples of `1/β` epochs, during which it holds its garbage `a`.
  Cleaning it then copies the slow share once more.
  When that share was moved in from a victim, whose own cleaning would have copied it anyway, mixing defers a copy rather than adding one, and frees the victim early.
  The smaller `b`, the shorter the wait, since `h(b)` grows like `1/b`.
- **`b > u*(r_B)`.** The page holds its garbage `a` until its slow share has drained down to `u*(r_B)`.
  For content that has stopped dying, which is what survivors of ripe pages are, that is never.

So the room of a page is best filled with content that dies at about the rate of what the page already holds.
Cold content in a hot page costs a wait that grows with its share, and above `u*` it costs the page's garbage for good.

#### Budgeted pages take survivors of ripe pages

**A page opened for consolidation holds survivors of ripe pages only, cold with cold, and stays full.**
This is LFS's segregation, and the budget loop provides most of it already.
What changes is which victims fill the page, in index order rather than by the churn floor, and that the loop's first page no longer takes the tail of a survivor cut on the flush's last page, whose room the cursor below fills instead.

#### The flush's own pages take survivors from the previous flush

**The room in every data page a flush writes anyway goes first to small ripe victims, then to the survivors of the previous flush's least-filled page.**

1. Whole ripe victims that fit the room and whose survivors take at most `θ` of a page, highest index first.
   Each frees a page for almost no room, and a host that freezes at under `θ` is ripe soon after its own content has died.
2. The survivors of the **cursor page**, as many of its statements' as fit, a whole statement's at a time, so that the cursor never cuts a statement.
   The cursor page is the least-filled data page of the latest earlier epoch that wrote data pages, chosen at open among the governing header's epoch's pages and again whenever the current one is empty.

**The previous flush's content is the closest in age to the flush's own that exists outside the flush itself.**
Unless the previous flush's last page closed short, its least-filled page is the one the current flush's fold drained most, which is the hottest of what the previous flush wrote.
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
  Flushes that end nearly full leave little room, and a cursor page drained slowly across many of them would hand out ever older content; once it is more than `W` epochs old, the cursor moves on to the least-filled page of the latest earlier epoch, and the rest of the old page is left to ripeness.

In [compaction mode](../impl/consolidation.md#compaction-mode), the tail page still takes the room ahead of both, since returning the tail is worth a mixed page.

## What would change in Consolidation

- [Scoring a data page](../impl/consolidation.md#scoring-a-data-page) would give way to the ripeness index, for table pages as well, and sampling would go.
  The bucket queues would give way to the two ordered sets, and victim selection would cost `O(log P)` rather than `O(1)`.
- [The churn floor](../impl/consolidation.md#the-churn-floor-is-a-parameter-not-an-identity) would give way to ripeness, and `λ` to `κ`; the controller would move `κ` toward `τ`, and the budget would remain a cap.
- [A free sink changes the arithmetic](../impl/consolidation.md#victims-are-pulled-one-at-a-time) — "free filling takes any victim that fits" — would no longer hold: free filling would take small ripe victims and the cursor's survivors.
- [Free filling's order](../impl/consolidation.md#packing-in-id-order-with-look-ahead) would shrink to those two sources, with the cursor page taking the place of the victim too big to take whole.
- [The page table](../impl/in-memory-state.md#4-the-page-table) would gain `Drain`, 8 bytes per page, and the [consolidator state](../impl/consolidator-state.md#what-the-ripeness-draft-would-add) would carry it, with `κ`, from one session to the next.
- [The constants still to be chosen](../impl/consolidation.md#constants-still-to-be-chosen) would lose `λ` and gain `κ`'s controller, `β`, `R_MIN`, and `W`.

## Open questions

- **The clock.**
  Rates per epoch count flushes rather than work: a run of tiny flushes makes every page look colder than it is.
  A clock that counts the bytes the application writes would measure drain per unit of work, but pages record only their epoch, so the seed at open would still have to use epochs.
- **The flush's own content.**
  Packed in key order, it mixes allocations the application keeps rewriting with ones it has just created, which is the largest source of mixed pages this draft leaves in place.
  [`last_written`](../impl/in-memory-state.md#3-the-allocation-map) could split a flush's chunks by whether their allocation was also written recently, at the price of key order; whether that pays is a question for measurement.
- **Description defragmentation's rewrites** are cold by selection and today share pages with the flush's fresh content.
  Packing them with the survivors of ripe pages instead would keep them apart, at the price of a second page per flush that may close short.
- **One rate per page** is crude for a page holding several shares.
  Two rates, fast and slow, would see a frozen page sooner, for 8 bytes more per page.
- **`β`** trades how soon a frozen page is recognised against noise: with a large `β`, a page that loses content in rare bursts looks frozen between them.
