---
title: Consolidation
---

Reclaiming garbage: which pages to rewrite, in what order, and how much per flush.

Consolidation rewrites live content out of sparse pages so their slots become reusable.
It is [not part of the specification](../spec/allocations.md#reclamation-is-not-part-of-this-specification) — an implementation that never consolidates is conforming, and merely useless — but the policy below is what makes the copy-on-write design's space overhead bounded rather than unbounded.

## The high-level picture

Consolidation fights **two independent debts**, and the counters that measure one are blind to the other.

**Description fragmentation** is a property of an *allocation*: how many statements it takes to say what its bytes are.
It is measured by [`fragment_count` and `statement_bytes`](in-memory-state.md#3-the-allocation-map) against `size`; it costs memory in the fragment map, bytes in the address table, and time at load; and no page-level counter can see it, because an allocation described by two hundred statements packed densely into one full page makes that page look perfect.

**Page fragmentation** is a property of a *page*, `Data` and `AddressTable` alike: how much of it current state still relies on.
It is measured by [`coverage[p]`](liveness.md#coverage) against capacity; it costs file size and nothing else; and no per-id counter can see it, because an allocation described by one `Ref` per flush has ideal `fragment_count` however thinly its bytes end up spread.

Each converts into the other in exactly one direction, which is why neither can be left to the other's mechanism:

- description fragmentation *becomes* address-table page fragmentation, since more statements need more table pages;
- repairing description fragmentation *creates* data-page fragmentation, since rewriting a stipple as one `Ref` kills the patches it supersedes.

**Only page fragmentation is urgent, and that asymmetry is what decides how each one is scheduled.**
Page fragmentation grows with write traffic and compounds, since garbage left uncleaned occupies the pages the next flush wanted to reuse — so it is attacked greedily, worst first.
Description fragmentation is a standing debt that does not grow while nobody writes the allocation — so it need only be paid down *eventually*, which a rotating sweep achieves with no ranking structure at all.

### Four mechanisms

| mechanism                                                                       | pays down                                              | selected by                       | reads                                                                      | writes                                                                  |
| ------------------------------------------------------------------------------- | ------------------------------------------------------ | --------------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| [Evacuation](#victims-are-chosen-as-a-set)                                      | page fragmentation of `Data` pages                     | the sparsest data pages, as a set | the mirror, via the [reverse index](#finding-the-referrers-of-a-data-page) | data pages                                                              |
| [Id-window rewrite](#victim-selection-for-address-table-pages)                  | page fragmentation of `AddressTable` pages             | a rotating id window              | the fragment map                                                           | one table page (merged into existing queue of statements to be written) |
| [Table-page rewrite](address-table-operations.md#rewrite_pagevictim---new_page) | one stubborn `AddressTable` page that no window drains | that page's coverage              | the victim page, decoded                                                   | one table page                                                          |
| [Defragmentation](#defragmentation-rides-the-id-window)                         | description fragmentation                              | the ids an id window passes over  | the mirror                                                                 | data *and* table pages                                                  |

All four end in the same place — fresh pages plus fresh statements and address-table child pages — which is what lets [one placement pass](#how-a-flush-and-consolidation-compose) serve all of them, and one budget price them.

### Why address-table pages have two mechanisms

**The id window is the workhorse and the table-page rewrite is an escape hatch**, and the two need no arbitration because both are priced from the same number, `coverage[p]`.

Rewriting an id window dominates whenever a window with real benefit exists.
At the identical cost of one page write it drains *many* pages at once, restores delta-encoding density across the range it rewrites, and decodes nothing.
Rewriting a victim page does none of that: its output covers whatever scattered id set the victim happened to hold, so the delta encoding stays sparse, and it must decode the victim to learn which ids those even are.

What the page rewrite can do that no window can is **drain a page that no window would ever choose**.
A table page holding forty live statements spread across the whole id space contributes one statement to each of forty different windows, so its `drain[Q]² / coverage[Q]` term is negligible in every one of them — and yet it holds a whole page hostage.
Such a page survives indefinitely under window selection alone, because the rotation reaches every id but the *ranking* need never pick the window that would help.

So the table-page rewrite is gated rather than freely competing: it becomes a candidate only for a page whose coverage is low in absolute terms and which no window has drained for a full rotation of the sweep cursor.
The decode it then pays for is cheap, but that is a consequence of residency rather than a reason for it.

**[Address-table pages stay resident](index.md#the-mirror) for `Inline` payloads, which the common path dereferences.**
An id-window rewrite copies the payload of every inline statement it re-emits, and windows are chosen precisely to drain *many* pages at once, so a non-resident address table would add a scattered read per drained page to every consolidating flush — not to the rare one.
On the table-page rewrite's account alone the balance would indeed tip the other way, exactly as an escape hatch should: read the victim back from the file when it is needed.

If the ~8 MB per million allocations ever outweighs those reads, the thing to hold is an **inline-payload arena** rather than whole pages.
It keeps exactly the bytes the rewrite path dereferences and drops all the framing, so it saves the non-inline fraction of address-table bytes — nearly everything in a `Ref`-dominated file and nearly nothing in an `Inline`-dominated one, which is why the mix has to be measured before it is worth building.

### Defragmentation rides the id window

**Defragmentation needs no victim selection of its own**, because the id-window rewrite already walks every fragment of a contiguous id range and therefore already sees which allocations are stippled.

The decision is per id and entirely local: the window is re-emitting this id's statements anyway, so the only question is whether to emit them as they stand or to replace a patched region with one `Ref` over fresh, zero-filled data bytes.

**Both sides carry data bytes and statement bytes, and getting that right is the whole of the criterion.**
The data terms very nearly cancel, because the fragments a rewrite supersedes lose their coverage exactly where they pointed:

```
freed(a, b)    = Σ live bytes of the `Bytes` fragments in [a, b)   // coverage that drops
               + Σ framing of the statements that die
written(a, b)  = (b − a)                                           // including the gaps
               + one `Ref`'s framing
```

Subtracting leaves a criterion that is worth stating on its own, because it is the thing to reason about and it is not what the ratio looks like at first glance:

> **A defragmentation pays when the statement bytes it retires exceed the zero bytes it materialises.**
> `framing_retired − ref_framing > (1 − density) · (b − a)`, with `density` the fraction of `[a, b)` that currently resolves through a statement.

### The envelope is a maximum-subarray problem

**There is no need for a heuristic, and no need to choose between "all" and "nothing".**
Attribute the criterion above to each fragment and the best subrange is the maximum-weight contiguous run of them — Kadane's algorithm, one pass, on a sequence the window walk is traversing anyway.

```rust
/// Best subrange of `id` to rewrite as one `Ref`, or None. `lambda` is the budget
/// loop's exchange rate: bytes of space that must be freed per byte written.
fn defrag_candidate(id: Id, size: u32, lambda: f32) -> Option<Candidate> {
    // What rewriting a fragment frees, minus what writing it costs. `share` spreads a
    // statement's framing over the fragments it owns — `pins` counts them — so a
    // statement that falls entirely inside the chosen range is credited its whole framing.
    let weight = |f: &Fragment, len: u32| {
        let share = f.statement().map_or(0.0, |s| framing_len(s) as f32 / pins(s) as f32);
        let freed = match f {
            Bytes{..}          => len as f32 + share,  // its coverage drops where it pointed
            ZeroExplicitly{..} => share,               // nothing to reclaim but the framing
            ZeroByDefault      => 0.0,                 // owned by nobody; frees nothing
        };
        freed - lambda * len as f32
    };
    let (gain, range) = kadane(fragments.range((id, 0)..=(id, size)), weight)?;
    (gain > lambda * REF_FRAMING).then(|| Candidate { id, range, .. })
}
```

The weights are what make the answer come out right at both ends.
A run of `ZeroByDefault` scores `−λ · len`, so the envelope stops at the edge of a stipple rather than swallowing the sparse allocation around it.
A single large `Ref` scores `len · (1 − λ) + share`, which is barely positive at `λ ≈ 1` and negative above it — so an already-contiguous region is excluded, which is correct, since rewriting it merges no statements and only moves bytes.
And exactly the case you describe — a long allocation carrying a short, dense knot of tiny `Inline` patches — is where the run of large positive weights is, so it is what Kadane returns.

### Where it is called, and how it is executed

**From the window walk, but priced on its own.**
The walk is already visiting this id's fragments in offset order, so Kadane costs nothing extra; what it cannot do is fold the result into the window's own score, because a defragmentation's cost is *data* pages while a window's is address-table bytes.
So `table_window_candidate` returns its window **plus** the defrag candidates its walk turned up, and [the budget loop](#the-budget-loop) ranks those beside everything else.
That is the precise sense in which defragmentation rides the window: the window supplies *discovery*, which is the part that would otherwise need a structure of its own, and not *pricing*.

Executing one needs no new operation.
It is [`write_bytes(id, a, b − a, dest)`](address-table-operations.md#write_bytesid-offset-bytes) over the range's resolved content — read from the mirror, gaps `memset` to zero — and the existing path turns that into one `Ref` that supersedes everything under it, with `overwrite` releasing the old owners' pins and `coalesce_around` collapsing the entries.

This is what pays off the debt that [the single ranked queue would otherwise starve](#the-budget-loop): the sweep cursor is not ratio-driven, so a cold, heavily patched allocation is reached within one rotation of the id space whether or not it ever looks urgent.
What the arrangement gives up is *worst-first* order, which is exactly the right thing to give up here, since description debt does not grow while it waits.

A per-id bucket queue over `statement_bytes * B / max(size, 1)` remains available if one rotation ever proves too long to wait.
It should not be built before that measurement exists, being a second structure to maintain on every statement created or destroyed.

### How a flush and consolidation compose

**A fold produces statements with exactly one field missing, and that hole is the whole interface between the two.**
The fold settles an emitted statement's `id`, `offset`, `size`, and kind; what it cannot settle is a new `Ref`'s `(page, page_offset)`, because the page does not exist yet.

```rust
struct Pending {
    stmt: Statement,        // geometry and kind: settled by the fold
    bytes: Option<Source>,  // None for Zero, Shrink, Grow, Tombstone — they need no page
}

enum Source {
    Arena(ArenaPos),                   // written this flush; awaiting a page
    Resident(PageNumber, PageOffset),  // already in the file
}
```

`Resident` is what makes consolidation composable rather than bolted on, and it splits by what placement decides to do with it: a piece left where it is emits a `Ref` at its existing address and writes nothing, while a piece being relocated needs a page like any freshly written range.
**A consolidation survivor is therefore the same object as a dirty range** — a **chunk**, a byte range plus a `Pending` with an unfilled hole — differing only in whether its bytes are read from the mirror or from the journal's arena.
Placement cannot tell them apart, and does not need to.

#### Mandatory and elastic

**One stream must be written whole and the other may be trimmed**, and that asymmetry is the only thing the packer needs to know about where a chunk came from.
A commit that omits part of what the journal produced is not a commit.
A survivor left behind costs a delay and nothing else.

That settles the choice between your (i) and (ii) in favour of **(i) with (ii)'s benefit kept**.
Neither stream is cut into pages before the other is visible, because pre-cutting the mandatory stream freezes exactly the boundaries that the elastic stream exists to avoid.
The elastic stream is then *trimmed* rather than cut, which is where (ii)'s "stop at a convenient point" comes back — and trimming is available only because the elastic stream is chosen after the mandatory one has been measured.

What this document described before was neither: it placed the flush's ranges first and filled afterwards, which is (ii) with the trimming left out — so it inherited (ii)'s frozen boundaries without buying anything for them.

#### Slack is cheaper than a split

**Do not cut a chunk to make a page come out exactly full.**
Your instinct is right, and the asymmetry is sharper than the byte counts suggest: page slack is *recoverable*, since the next consolidation to pick the page up re-packs it, whereas a split is a statement and a fragment-map entry that survive until [defragmentation](#defragmentation-rides-the-id-window) pays them off — and description debt is the debt with no natural drain.

| | costs | until |
| --- | --- | --- |
| leaving slack | at most the smallest chunk still in the packer's look-ahead, which bottoms out at the 64-byte [`Inline` threshold](flush.md#the-inline-threshold) against a 4081-byte page | the page is next consolidated |
| splitting to fill it | ~10 bytes of address table, ~20 bytes of fragment map, and one more entry in every scan of that id | something rewrites the range as one `Ref` |

Two exceptions survive, and both are forced rather than chosen.
A chunk longer than `MAX_PAGE_CONTENT` must be cut whatever the policy, and cutting whole pages off its front is the cut that costs least.
And the one page per flush that ends up genuinely short — the tail of the packing, never a middle page — is worth topping up from the elastic stream when its slack runs to a few hundred bytes rather than sixty; [relocated content was going to be re-described anyway](flush.md#cutting-and-packing-concretely), so cutting it there costs one statement rather than two.

#### Packing in id order, with look-ahead

**Pack in `(id, offset)` order and fit by looking ahead, rather than first-fit over a size-sorted list.**
First-fit-decreasing packs tightly and destroys id locality completely, which costs two things worth keeping: a smaller [reverse index](#finding-the-referrers-of-a-data-page), which holds one entry per `(page, id)` pair, and the chance that a later evacuation of the page relocates one id's bytes together and merges their fragments.

```rust
fn pack(chunks: impl Iterator<Item = Chunk>, sinks: &mut Sinks) {
    let mut ahead = VecDeque::new();                  // bounded look-ahead, L ~ 16
    for chunk in chunks.chain(repeat_with(|| None)) {
        ahead.extend(chunk);
        if ahead.len() < L && chunk.is_some() { continue; }
        // Place the first chunk that fits the open page, not the largest one.
        match ahead.iter().position(|c| c.len <= sinks.open().room()) {
            Some(i) => sinks.open().place(ahead.remove(i)),
            None    => sinks.close_and_open(),        // or stop, if the tail is elastic
        }
    }
}
```

Slack is then bounded by the smallest chunk *in the look-ahead window* rather than by the smallest chunk overall, and id order survives up to a reordering of `L`.
Whole-page chunks bypass the window entirely, being cut off the front of an over-long range and written to pages of their own, exactly as [the current placement](flush.md#cutting-and-packing-concretely) does.

#### Choosing the victim set so the total lands near a boundary

**The remaining slack is removed by choosing victims to fit, not by cutting chunks to fit.**

The fold gives the flush's own chunk bytes `D` exactly.
Rounding up to `P = ceil(D / C)` pages leaves `P · C − D` bytes that would otherwise be slack, so consolidation aims at that plus whatever the budget buys:

1. Take victims in ratio order while their live bytes fit the target.
2. When the next one overshoots, **do not take it**: scan the next `K` candidates for the one whose live bytes come closest to the remaining room — a best fit over *victims*, cheap because the sparsest bucket is already at hand.
3. Whatever room is left is then under one candidate's live-byte count, and simply goes unfilled.

A victim is all-or-nothing, since relocating only some of a page's survivors reclaims nothing, which is why the fitting is done at victim granularity and never at chunk granularity.
The estimate of `P` is allowed to be wrong, because the error lands in the elastic stream: one victim more or less, never a torn commit.

#### One statement queue, and why its length is computed last

**Every mechanism emits into one queue, and the queue's encoded length is computed only once it is sorted**, because [delta encoding](../spec/address-table.md#physical-format) makes a statement's length a property of its predecessor rather than of itself.

That gives the table side the same two-stream shape as the data side, with the same trimming:

- the flush's statements, evacuation's restatements, and the windows' re-emissions all go into one queue;
- after sorting, one pass computes each statement's length against its predecessor, and that pass is also what discovers how much room is left in the last page;
- an id window is then chosen to fill that room, bounded by *encoded length* rather than by statement count.

**The bound you point out is what makes that choice cheap.**
`|encode(A ∪ B)| ≤ |encode(A)| + |encode(B)|`, because merging only ever brings each element's predecessor closer and a varint's length is monotone in the value it encodes.
So a window's encoded length, measured in isolation during scoring, is a safe upper bound on what it adds to the queue: filling to that bound under-fills rather than overflows, and a second pass over the merged queue recovers the difference when it is worth another window.

#### The flush as a straight line

1. **Fold** the journal into chunks and `Pending`s ([Phase A](flush.md#phase-a--the-fold)), which also yields `D` and the provisional coverage drops below.
2. **Choose victims** against coverage as this flush will leave it, aiming at `P · C − D` plus the budget, and fitting the last one.
3. **Pack** the union of both chunk streams in `(id, offset)` order with look-ahead, cutting only what must be cut.
4. **Fill every hole** — a `Ref`'s `(page, page_offset)` is known once its chunk has a page — and push every statement into the shared queue.
5. **Sort the queue, compute encoded lengths**, top the last table page up with an id window, and evict from the header whatever no longer fits.
6. **Write, unlink** the leaves whose coverage reached zero, and **commit** by [the flush protocol](../spec/durability.md#the-flush-protocol).

**Coverage falls in step 4, not in step 1**, and that is what decides what step 2 is allowed to see.
The fold computes and commits nothing, so no counter moves: coverage falls when [`apply`](address-table-operations.md#applying-a-statement) re-owns a range and releases the previous owner's pin, which needs the new statements to exist.
Selecting victims against raw committed coverage would therefore evacuate pages that this very flush is about to empty — pure waste — so the fold, which walks the overwritten fragments anyway, accumulates a **provisional drop per page** that step 2 subtracts without committing it.
What remains genuinely one flush behind is only the flush's *own output*: the pages it writes and the statements it emits cannot be victims of the flush that produced them, and there is no reason to want them to be.

The one accounting trap is that **the budget must be charged for the address-table pages a candidate causes, not only for its data pages.**
Evacuating a data page emits a statement per relocated chunk, and enough of those overflow the header into a leaf the flush never planned to write.
Defragmentation is where this bites hardest, being the one mechanism whose two costs pull in opposite directions: it writes data bytes in order to *stop* writing statements.

## One currency

The design has one throttle — a per-flush **maintenance budget** in pages beyond the flush's own dirty content — and one queue of candidates competing for it, ranked by `bytes reclaimed / bytes written`.
One further parameter, [the churn floor `λ`](#the-churn-floor-is-a-parameter-not-an-identity), says when a candidate is not worth taking at all.

Three candidate kinds compete in that queue, which the [four mechanisms](#four-mechanisms) map onto unevenly:

1. **Evacuation** — relocate the live content of a set of sparse data pages into the fill of pages being written anyway, plus however many fresh pages the set still needs; reclaims dead data bytes.
2. **An id-window rewrite** — re-emit the live statements of an id range, sorted and dense; reclaims dead statement bytes *and* restores delta-encoding density.
   **Defragmentation is priced inside this candidate** rather than competing as a fourth, since [the window's own walk is what finds the stippled allocations](#defragmentation-rides-the-id-window).
3. **A table-page rewrite** — drain one stubborn table page directly.
   It is [gated on the starvation condition](#why-address-table-pages-have-two-mechanisms) rather than competing freely, so that it can never outrank a window that would do strictly more for the same page write.

Defragmentation is worth naming as a mechanism of its own even so, because it is the only one that pays down [description fragmentation](#the-high-level-picture) and the only one that shrinks the in-memory fragment map.
An allocation stippled with `[data] [zeros] [data] …` patches costs statements forever until someone rewrites the region as one `Ref` — and the [`Zero` usage note](../spec/address-table.md#statement-types) explicitly permits rewriting such a stipple as one `Ref` over a range whose gaps the consolidator fills with actual zeros, which is what makes the rewrite legal.
Those bytes are being written anyway, so zeroing them costs nothing beyond the `memset`.

### The structures behind the ranking

Every candidate that starts from a *page* is ranked from **bucket queues**, which is the structure that makes "give me a good victim" `O(1)` instead of `O(log P)`.

```rust
struct Buckets {
    lists: [Vec<PageNumber>; B],    // B is small; 16 is plenty
    at: HashMap<PageNumber, (u8, u32)>,   // which bucket, and which slot in it
}
```

A page's bucket is `coverage[p] * B / capacity`, so moving a page is: swap-remove it from its old list, push it onto the new one, and fix the `at` entry of whatever the swap moved.
Three array writes and two map updates, no comparisons, no rebalancing — which matters because coverage changes on **every** fragment created or destroyed, not once per flush.

**Kind 3 gets its own instance, and with a smaller `B`.**
One shared instance would cost the `O(1)` the structure exists for: data pages outnumber table pages by roughly the ratio of their contents, so `lowest_non_empty()` on a mixed structure would keep handing back data pages and the table candidate would scan past them.
The smaller `B` follows from what that candidate is for — a gated escape hatch that needs "nearly empty" rather than a fine ranking — so four buckets do where the data side wants sixteen.
Header pages belong in neither, [being rewritten unconditionally](flush.md#the-header-as-write-buffer) and therefore never victims.

Kind 2 needs no bucket structure of its own, being [seeded from the table-page buckets and from a rotating cursor](#seeding-a-window), and neither does the defragmentation priced inside it.

**Why bucketing is enough.**
A bucket queue answers "roughly the sparsest" rather than "the sparsest", and the ranking is a heuristic whose inputs are estimates anyway.
What it must not do is *miss* a good victim, and it cannot: a page in the sparsest non-empty bucket is within `1/B` of the true minimum live fraction.

### The budget loop

```rust
fn consolidate(budget: Budget) -> Plan {
    let mut plan = Plan::new();

    // 1. Free filling. These sinks are the slack in pages that must be written
    //    anyway, so nothing here is traded off against anything: it runs to exhaustion.
    plan.fill(flush.data_slack(),  evacuation.survivors());
    plan.fill(flush.table_slack(), id_windows.statements());

    // 2. Budgeted cleaning: pages that exist only to reclaim. Each source offers its
    //    next *marginal* victim, and within a source those arrive in decreasing ratio
    //    order — so one greedy pass over the merged streams is the whole policy.
    while plan.extra_pages() < budget.pages {
        let Some(v) = [evacuation.peek(), id_windows.peek(), table_pages.peek()]
                          .max_by_key(|v| v.reclaimed / v.written) else { break };
        if v.reclaimed < budget.lambda * v.written { break; }    // the churn floor
        plan.take(v);
    }
    plan
}
```

**The sources are combined, not traded off — but they do compete.**
You are right that the result is one *set* and that the three sources add to a shared plan rather than one of them winning outright; a whole evacuation was never a `Candidate`, and having `evacuation_candidate` return a set while this loop treated it as a single page was a genuine inconsistency.
What survives of the trade-off is that they compete for one scarce thing, the page budget, which is why the merge is greedy over *marginal* victims rather than over whole mechanisms.
Each source's marginal ratio decreases as it takes more, so one greedy pass over the merged stream is the right amount of machinery.

#### The churn floor is a parameter, not an identity

**You are right that `reclaimed ≤ written` is arbitrary, and the reason is a units error.**
A *ranking* only needs two candidates' ratios to be comparable, and they are.
A *threshold* needs the ratio to have an absolute meaning, and comparing the two sides at 1 quietly asserts one: that a byte of I/O is worth exactly a byte of space.
Nothing supports that rate, and asserting it is precisely what produces the `u = 1/2` floor.

So the floor is a parameter `λ` — space freed per byte written — and its price is explicit.
Cleaning pages at live fraction `u` costs `u / (1 − u)` bytes written per byte of space freed, so a total write amplification of `1 / (1 − u)`: 2× at `u = 1/2`, 5× at `0.8`, 10× at `0.9`.

**But `λ` is the wrong knob for the file fill you want.**
An average fill of 80–90 % does not mean cleaning pages that are 80–90 % live, and implementing it that way would buy the last few percent at five to ten times the write traffic.
It means the distribution of live fractions is bimodal enough that most pages sit near full while the garbage concentrates in a few nearly empty ones — which is what [the age term](#scoring-a-data-page) is for, and the whole reason LFS's cost-benefit cleaner beats plain greedy.

The two knobs therefore do different jobs, and only one of them is a floor:

- **`λ` is a churn floor.** It stops the cleaner grinding when there is nothing worth cleaning, and it belongs *low* — near 1, i.e. near `u = 1/2` — precisely so that it is not what decides the file's fill.
- **The per-flush budget is the throttle**, and it is what converges the file on a target fill `τ`: measure `live_bytes / (pages · C)` after each commit and move the budget up while it sits below `τ`, down while above.

That also sharpens [the bound this buys](#the-bound-this-buys): `live_size / τ` is reached by spending budget, and the floor's only job is to stop spending it on pages that cannot repay.

**The known failure mode of the single ranked queue.**
Ratio-greedy ranking starves **costs that current ratios cannot see**, and this design has exactly two of them.
Description debt on a cold allocation — one that will be read forever and never written again — never looks urgent, because nothing about it is changing.
A table page whose live statements are scattered across the id space never looks urgent either, because no single window reclaims much of it.
Both are taken out of the ranking rather than weighted inside it: the first rides [the rotating sweep cursor](#defragmentation-rides-the-id-window), and the second fires on [a gate](#why-address-table-pages-have-two-mechanisms).
Folding an invisible cost into the ranking — as LFS's cost-benefit policy does with segment age — is the alternative, and it is the right one where a cost is *urgent but mismeasured*.
It is the wrong one for both of these, which are not urgent at all: it would buy a tuning parameter for a problem a rotation solves without one.

### The complexity this buys

Strictly logarithmic work *per flush* is unattainable, because a flush must at least record what it changed.
The attainable form, and what this design achieves:

- `O((k + s) · log F)` in-memory work for a flush dirtying `k` ranges and rewriting `s` statements under the budget;
- `O(1)` victim selection per candidate;
- page writes bounded by `dirty data pages + c + depth` for budget `c`;
- with the common small flush writing **no** address-table page beyond the header.

## Two forms of consolidation

Consolidation runs inside every flush, in two forms that differ in [the price of the sink](#victims-are-chosen-as-a-set) and, following from that, in whether they are traded off against anything at all:

- **Free filling.** Survivors land in the slack of pages that must be written anyway, so they cost no page write and compete with nothing. It runs to exhaustion.
- **Budgeted extra pages.** Beyond that, pages that exist only to reclaim. These compete, under [the budget and the churn floor](#the-budget-loop).

Both draw from the same [elastic stream](#mandatory-and-elastic), and every knob in this document applies to the second.

## Victim selection for data pages

The classic greedy rule of log-structured file systems — **lowest live fraction first** — refined by age the way LFS and SSD flash translation layers refine it: prefer pages that are sparse *and* old.

The refinement rests on one assumption worth stating explicitly: **temporal update locality** — bytes written together tend to be rewritten together, and soon, or else not for a long time.

It transfers from the workload to pages mechanically, with no appeal to spatial locality in the logical data structures: a data page's residents are exactly the content that one flush wrote together, so if the application is still working on whatever produced them, more of its residents will be shadowed shortly.
Such a "hot" page gets sparser without help, and cleaning it early wastes writes on relocating survivors that are about to die anyway; a page whose live fraction has been stable for a long time is "cold", and its survivors are the ones worth paying to move.

Co-written-implies-co-updated is the empirically load-bearing assumption behind LFS's cost-benefit cleaner, and it **fails gracefully**: under a workload with no locality at all — uniform random updates — the age term carries no information and the policy degrades to plain greedy, which is the right fallback anyway.

### Scoring a data page

Take LFS's cost-benefit ratio unchanged, with `u = coverage[p] / capacity` the live fraction and `age = current_epoch − page_epoch[p]`:

```
score(p) = (1 − u) · age / (1 + u)
```

The numerator is what the eviction frees times how long it has been stable; the denominator is what it costs — one page read plus `u` of a page written back.

**Evaluating it without scanning every page.**
The bucket gives `u` only to within `1/B` and says nothing at all about age, so the next victim is the highest-scoring of `K` pages sampled from the sparsest non-empty bucket, for a small constant `K` — eight, say.

That keeps selection `O(1)` and gets the age term back, and it is robust in the way that matters: the sampled set is already known to be sparse, so the worst outcome of a bad sample is cleaning a page slightly less old than the best one in the same bucket.
Rotating the start of the sampling window across flushes stops the same unlucky prefix from being examined forever.

### Victims are chosen as a set

**The number of output pages is a consequence of the victim set, never a second decision.**
[Free re-cutting](flush.md#cutting-content-across-pages) fills every output page completely, so `k` victims holding `L` live bytes between them produce exactly `ceil(L / C)` output pages for page capacity `C`, reclaim `k · C − L` bytes, and write `L`.

The ratio that ranks the operation therefore depends only on the set's **mean** live fraction, `ū = L / (k · C)`:

```
reclaimed / written = (1 − ū) / ū
```

Two things follow, and both simplify the policy rather than complicate it.

**Sparsest-first is exactly right, and the size of the set is free.**
Adding victims in increasing live fraction raises `ū` and lowers the ratio monotonically, so there is no combinatorial choice to make and no reason to consider any set but a prefix of the sorted order: draw from the sparsest bucket until the budget runs out or the next victim stops paying for itself.
A marginal victim frees `(1 − u_v) · C` bytes and writes `u_v · C`.

**The `1 − u_v` is not the victim's empty fraction being claimed as a gain**, which would indeed be double counting — that space is already unusable and is about to be released whatever we do.
It is what remains after paying for the survivors: the victim's `C` bytes do all come back, exactly as you say, and `u_v · C` of them are immediately spent again, because the survivors occupy space in their new page exactly as they occupied it in the victim.
Net space gained is `C − u_v · C`.
The two-epoch quarantine changes only the timing, and it delays the release and the consumption alike.

What does change the arithmetic is a *free* sink, which is the next point: a survivor landing in slack that was going to be wasted consumes nothing, so the net gain is the whole `C` and the ratio is `1 / u_v`, above 1 for every `u_v`.

The set-average form of the marginal test is the [budget loop](#the-budget-loop)'s floor, since `reclaimed = λ · written` reads `ū = 1/(1 + λ)` — and the marginal test always binds first, which is why a set stops growing before the plan as a whole stops paying.

**Free filling and budgeted consolidation differ only in the price of the sink.**
A survivor landing in the leftover space of a page the flush was writing anyway costs no page write at all, so the marginal victim reclaims `1 − u_v` for nothing and *any* `u_v < 1` is worth taking.
That is the whole of the distinction between [the two forms](#two-forms-of-consolidation), and it is why they are one pass over a stream of sinks rather than two mechanisms.

Putting the two together, evacuation is a **source of marginal victims** that [the budget loop](#the-budget-loop) draws from, not a candidate that wins or loses as a unit:

```rust
impl Evacuation {
    /// The next victim this source would take, and what it would cost. The budget
    /// loop compares this against the other sources' next victims and takes the best.
    fn peek(&self, room: u32) -> Option<Victim> {
        let bucket = self.buckets.lowest_non_empty();
        let best = bucket.sample(K).max_by_key(|p| score(p))?;
        // Do not overshoot the room left in the pages already being written: past
        // that boundary a victim buys a whole new page for a sliver of content.
        if coverage[best] > room {
            // Best fit over victims, not a split of chunks; see "Choosing the victim
            // set". Returns None when nothing in the sample fits, which stops the
            // source — elastic, so stopping is free.
            return bucket.sample(K).filter(|p| coverage[p] <= room).max_by_key(|p| score(p));
        }
        Some(best)
    }
}
```

The `room` argument is the fix for stopping an iteration too late: without it the source hands over a victim whose survivors spill a few bytes past a page boundary, and the plan pays a whole page for them.
With it, the overshooting victim is simply passed over in favour of one that fits, and the source stops when none does.

A caveat on the arithmetic rather than on the policy: `coverage` counts [aliased bytes once per claimant](liveness.md#coverage), so `L` over-states both what an evacuation writes and what it reclaims, and the ratio above is a lower bound wherever aliasing is present.

## Victim selection for address-table pages

Consolidated by **id range** rather than page identity.

The quantity to optimize is reclaimed coverage per byte written.
Rewriting all live statements of an id range into one fresh page costs one page write, and its benefit is the coverage it drains from the pages currently holding those statements — *weighted*, because drained coverage only helps on pages that end up empty or nearly so: draining a page from 60 live bytes to 0 frees a slot; draining it from 3000 to 2940 frees nothing.

**Picking a window needs no new index.**
The fragment map is ordered by id, and every owned fragment names its statement, whose page and framing the slab already records.
So a window is scored by walking fragments:

```rust
fn table_window_candidate() -> Candidate {
    let mut best = None;
	let mut drain: SmallMap<PageNumber, u32> = SmallMap::new();
	let mut seen: SmallSet<StatementRef> = SmallSet::new();
    for _ in 0..WINDOWS_PER_FLUSH {                 // a small constant, e.g. 8
	    drain.clear();
	    seen.clear();
        let mut encoded = 0;

        // Walk fragments from the cursor until one page's worth of statements would
        // be re-emitted. `sweep_cursor` is a fragment-map key, kept across flushes.
        for (key, fragment) in fragments.range(sweep_cursor..) {
            let Some(s) = fragment.statement() else { continue };   // ZeroByDefault
            // Framing is per statement, so it is guarded by `seen`. An inline payload
            // is live only where the statement still owns a fragment, so it is not.
            if seen.insert(s) { drain[slab.page_of(s)] += slab.framing_len[s]; }
            if s.is_inline()  { drain[slab.page_of(s)] += fragment.len(); }
            encoded += re_encoded_size(fragment); // Needs id and offset of last encoded fragment to calculate size of delta-encoding
            if encoded >= MAX_PAGE_CONTENT { break; }
        }

        best = max(best, Candidate { benefit: benefit(&drain), written: 1, .. });
        sweep_cursor = next_window_start();
    }
    best
}
```

**The inline payload has to be counted**, and leaving it out would misrank exactly the pages most worth draining.
[Coverage charges an inline payload per byte to the table page carrying it](liveness.md#coverage), so draining an inline statement drops framing *and* payload from that page's counter.
The two accumulate differently, which is the part that is easy to get wrong: framing is per statement, and therefore guarded by `seen`, while a partially shadowed inline has live payload only where it still owns a fragment.
A page of 64-byte inlines is ~90 % payload, so counting framing alone would under-report its drain by an order of magnitude and rank it below pages holding nothing but `Ref`s.

### The cursor is a fragment-map key

**Yes, `(Id, AllocationOffset)`, which is the fragment map's own key** — and that is the argument for it: a window becomes a plain range of the structure being walked, with no special case at either end.

The edge case you name is real rather than hypothetical, since any id with a long enough history of small `Inline` patches reaches it, and an id-keyed cursor fails at it badly: the window never gets past that id, so `next_window_start()` returns to it forever and the sweep stalls there permanently.
An offset-keyed cursor simply resumes mid-allocation on the next window.

The one thing to check is the anchor, which is invisible in the fragment map and therefore has no position in a window.
[`rewrite_id_range`](address-table-operations.md#rewrite_id_rangelo-hi)'s rule survives unaltered, because it is a question about pages rather than about coverage of the id: it transfers an anchor when the rewrite drains the page holding it.
A partial window that emits `replacement_anchor(id)` emits `Shrink(id, size(id))`, which is correct whatever else that window covered, and one that does not emit it leaves an anchor whose page was not drained — also correct.

### Seeding a window

**Seed from the sparsest table page rather than from a random one**, which is your idea with the selection put back into it.

The mechanism you are reaching for is real: a window straddling two pages retires neither, while one aligned with a page's id span retires it outright.
Seeding at a page's first statement is what aligns it, and it works because [pages tend to hold coherent id ranges](flush.md#the-header-as-write-buffer) — every mechanism here emits sorted.

Two refinements make it strictly better than random:

- **Record each table page's key span in the page table** — `(first_key, last_key)` over the same `(Id, AllocationOffset)` space as the cursor, eight bytes a page.
  It is known when the page is written and never changes afterwards, so it turns "seed at this page" into a lookup rather than a decode.
- **Take the seed page from the address-table buckets.**
  The sparsest page is the one most worth retiring, and its span says in advance whether one window can even cover it — which is [the starvation test](#why-address-table-pages-have-two-mechanisms) made cheap, and therefore what decides when to fall back to decoding the page instead.

Reproducibility then needs no pseudorandom stream at all, the choice being a deterministic function of the page table.

**Keep the rotating cursor alongside it, for one window per flush.**
Page-seeded windows go where the garbage is and would never visit a densely packed page, but [defragmentation rides these windows](#defragmentation-rides-the-id-window) and depends on reaching *every* id eventually — which only a rotation guarantees.

**The benefit is drained bytes weighted by the fraction drained**, which is what encodes "draining a page from 60 live bytes to 0 frees a slot; draining it from 3000 to 2940 frees nothing":

```
benefit = Σ over pages Q of  drain[Q] · drain[Q] / coverage[Q]
```

**`coverage[Q]` sits inside a sum over pages and `encoded` does not**, which is what settles the slot each belongs in: the benefit is shaped per drained page, while the cost is a property of the window as a whole.
`drain[Q] / coverage[Q]` is the *fraction* of `Q` this window takes away, so `drain[Q] · drain[Q] / coverage[Q]` reads "bytes drained, discounted by how far they get `Q` toward empty".
Putting `encoded` there would divide every term by the same window-wide constant, which changes no comparison between pages and merely moves the constant inside the sum.

Where you are right is that the cost is `encoded` and not a page.
`written: 1` is wrong once [every mechanism shares one statement queue](#one-statement-queue-and-why-its-length-is-computed-last), because a window no longer buys a page: its statements join the queue, and it is the queue that rounds up to pages.
So the candidate's ratio is `benefit / encoded`, in the same bytes-per-byte currency as everything else.

The square is doing real work.
Plain `Σ drain[Q]` would rate a window that shaves 2 % off fifty pages exactly as highly as one that empties two — identical bytes reclaimed, and one of them useless.

**Two properties worth knowing about this walk.**
Each statement is counted once even though it may own several fragments, which is what `seen` is for and which is easy to forget, since the fragment map is the only thing being walked.
And a window's cost is bounded by construction: it stops at one page's worth of re-encoded statements, so it touches on the order of 700 fragments, whatever the file's size.

Because a window's statements are rewritten sorted and adjacent, the same pass **re-establishes delta-encoding density**.
Density is restored by cleaning, not maintained by an invariant — which is the cheaper of the two, since maintaining it would constrain every write.

**Executing a window decodes nothing**, which is the property that separates it from every other rewrite in the design: the fragment map already holds resolved truth for the range, so [`rewrite_id_range`](address-table-operations.md#rewrite_id_rangelo-hi) emits statements straight from it.
The drained pages are never touched.
They get emptier because their statements *lose their pins* when the fresh ones supersede them, and coverage follows pins rather than physical presence.

**Nothing waits for a load**; your reading of the accounting is the right one and the earlier wording was simply wrong.
With framing and inline payloads both charged per byte, executing a window drops a fully drained table page's coverage to zero within the same flush, and [the two-generation quarantine](../spec/durability.md#the-reuse-rule) does the rest: fallback at the next commit, writable one commit after that.
The dead bytes then sit in the page until it is overwritten, which is a statement about the bytes and not about the page's availability.

One step does stand between zero coverage and reuse, and it is structural rather than an accounting overhang.
**A table page with zero coverage is still named by its parent**, so it remains reachable from the header and [I2](../spec/durability.md#the-two-invariants) forbids overwriting it.
The flush that drains it must therefore also unlink it, which [compacts the parent's child array during a rewrite that was happening anyway](flush.md#the-shape-of-the-tree) and costs nothing at depth 1, where the parent is the header.
Data pages have no such step, having no parent.

The one correctness obligation on any such rewrite is [transferring the anchor](address-table-operations.md#replacement_anchorid--the-one-correctness-obligation).

## Finding the referrers of a data page

**This is an unresolved gap**, and it is worth stating plainly rather than leaving implicit.

Rewriting an address-table page is self-describing — decode the victim, and its statements name the `(id, offset)` keys to look up — which is precisely what makes [the table-page rewrite](#why-address-table-pages-have-two-mechanisms) usable as an escape hatch, and why address-table pages need nothing of what follows.
A **data** page is opaque bytes with no ids in it, so decoding it tells you nothing, and the fragment map is keyed by `(id, offset)` rather than by address.
Nothing in the design currently gets from a victim data page number to the `Ref` statements that must be re-pointed.

Four candidate answers, roughly in order of how well they fit:

- **A reverse `page → ids` index, maintained in memory.**
  To find the fragments referencing page `P`, sweep each listed id's range of the fragment map and keep the fragments pointing into `P`.
  Cheaper than an exact `page → fragments` index on both axes: one entry per `(page, id)` pair rather than per fragment, and insertion can be idempotent with pruning done lazily on use, so fragment destruction does nothing at all on the hot path.
  It is also **sound under splices**, which is the property that matters: a splice restates a statement with a new offset but the same id and address, so the index stays true through it.
  Refine it with an offset window per `(page, id)` — the bytes a page holds from one id span at most one page's worth of that id's offset space — and the sweep becomes proportional to the fragments actually in `P` rather than to the whole allocation.
  The window is a hint that is always wide enough and not necessarily tight; see [below](#the-shape-of-the-index) for the shape that holds it.

  Three rules keep it correct, and the third is the one a splice breaks if it is stated the other way round:

  1. **Widen on fragment creation.** Creating a `Bytes` fragment into `P` unions its range into the window for `(P, id)`. `O(1)`, on the only path that can invalidate the index.
  2. **Do nothing on fragment destruction.** The index is then a superset, which costs a wasted scan and never a missed referrer.
  3. **Recompute the window only while using it.** A scan that visits `(P, id)` already learns the true extent of `P`'s fragments in that id, so it can narrow the window then, and drop the entry when it finds none. Narrowing it at any *other* time is unsound: a splice moves bytes to offsets outside the recorded window, and it is rule 1 — not the window — that keeps the entry honest.
- **Make data-page consolidation allocation-driven**, the way kinds 2 and 3 already are: start from an id whose bytes are spread over sparse pages rather than from the page.
  Needs no new structure at all, at the cost of not being able to target a specific page — which does not cover the free-filling form, since that is page-driven by construction.
- **An exact `page → fragments` index.** `O(fragments in page)` and exact, but a second index to maintain on every fragment created or destroyed.
- **LFS-style segment summaries** — a trailer in each data page listing `(id, offset, page_offset, size)` per resident piece.
  This is the classic answer, but it does **not** transfer directly here: LFS's summary is sound because a block's back-reference `(inode, block#)` is immutable, whereas a kladde splice re-points existing bytes to a new offset, so a written-once trailer produces false *negatives* — bytes judged dead that are live, which frees a referenced page.
  It can be repaired by letting coverage adjudicate: use the trailer only to locate live extents, verify each against the fragment map, and evacuate only if the verified bytes sum to `coverage[P]`; otherwise skip the victim.

### The shape of the index

The first option is the current preference, but not in the shape the bullet sketches.

**The index must be keyed by page alone**, because the query is "which ids have bytes in `P`", and a map keyed by `(PageNumber, Id)` cannot answer it without scanning every entry it holds.
The per-`(page, id)` window is still the right *payload*; it was the outer key that was wrong.

```rust
struct ReverseIndex {
    // Keyed by page, because that is the query. The inner list is kept sorted by id
    // and is short: nothing below the `Inline` threshold ever reaches a data page,
    // so a 4 KiB page holds at most ~64 ids and in practice a handful.
    pages: HashMap<PageNumber, SmallVec<[(Id, Range<AllocationOffset>); 4]>>,
}
```

## Pacing

Each flush consumes at least one reusable page, often a few, so consolidation must reclaim at least that many on average.

The situation where no good victim exists is the situation where pages are largely full — i.e. there is little garbage and the file is close to its live size — so "cleaning cannot keep up" and "the file is honestly this large" coincide, which is the benign coincidence.

The one obligation this leaves is [headroom](../spec/durability.md#headroom), which is a requirement rather than a policy.

## The bound this buys

Garbage accrues in proportion to write traffic and drains in proportion to consolidation effort, so the steady state is set by the budget: enforcing "consolidate until live fraction ≥ τ" bounds the file at `live_size / τ` for a chosen τ, at the price of the corresponding cleaning work.
The bound is controllable and, in relative terms, independent of file size.

## Constants still to be chosen

- The live-fraction target τ, and the feedback rule that moves [the budget](#the-churn-floor-is-a-parameter-not-an-identity) toward it.
- The churn floor λ, which should be near 1 and is not the knob that sets τ.
- The per-flush page budget's starting value and its bounds.
- The age weighting in victim selection.
- The look-ahead depth `L` and the split threshold in [packing](#packing-in-id-order-with-look-ahead) — the first sets how much slack survives, the second how much slack is worth a statement.
- The bucket counts: 16 for data pages, ~4 for address-table pages.
- The page size, 4 KiB versus 16 KiB, to be measured on macOS/iOS and Linux — a per-file header property either way.
