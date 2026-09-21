---
title: Consolidation
---

Reclaiming garbage: which pages to rewrite, in what order, and how much per flush.

Consolidation rewrites live content out of sparse pages so their slots become reusable.
It is [not part of the specification](../spec/allocations.md#reclamation-is-not-part-of-this-specification) — an implementation that never consolidates is conforming, and merely useless — but the policy below is what makes the copy-on-write design's space overhead bounded rather than unbounded.

## One currency

The design has exactly one knob — a per-flush **maintenance budget** in pages beyond the flush's own dirty content — and one queue of candidates competing for it, ranked by `bytes reclaimed / bytes written`.

Three candidate kinds compete in that queue:

1. **Data-page consolidation** — relocate the live content of low-live-fraction data pages into the fill of pages being written anyway; reclaims dead data bytes.
2. **Address-table consolidation** — rewrite the live statements of low-live-fraction table pages, gathered by id range and emitted sorted and dense; reclaims dead statement bytes *and* restores delta-encoding density.
3. **Allocation defragmentation** — rewrite a heavily patched region of one allocation as a single fresh `Ref` into fresh data pages; reclaims statement bytes *and* the partially dead data behind the patches, and is the only kind that also shrinks the in-memory fragment map.

Kind 3 is worth naming separately because it pays down a debt the other two cannot see.
An allocation stippled with `[data] [zeros] [data] …` patches costs statements forever until someone rewrites the region as one `Ref` — and the [`Zero` usage note](../spec/address-table.md#statement-types) explicitly permits rewriting such a stipple as one `Ref` over a range whose gaps the consolidator fills with actual zeros, which is what makes the rewrite legal.
Those bytes are being written anyway, so zeroing them costs nothing beyond the `memset`.

All three emit the same kind of output — fresh pages plus fresh statements — so they compose with the flush's ordinary work.

### The structures behind the ranking

All three kinds are ranked from **bucket queues**, which is the structure that makes "give me a good victim" `O(1)` instead of `O(log P)`.

```rust
struct Buckets {
    lists: [Vec<PageNumber>; B],    // B is small; 16 is plenty
    at: HashMap<PageNumber, (u8, u32)>,   // which bucket, and which slot in it
}
```

A page's bucket is `coverage[p] * B / capacity`, so moving a page is: swap-remove it from its old list, push it onto the new one, and fix the `at` entry of whatever the swap moved.
Three array writes and two map updates, no comparisons, no rebalancing — which matters because coverage changes on **every** fragment created or destroyed, not once per flush.

The same structure serves kind 3 over `statement_bytes * B / max(size, 1)`, keyed by id instead of page.
Kind 2 needs no bucket structure at all; see [below](#victim-selection-for-address-table-pages).

**Why bucketing is enough.**
A bucket queue answers "roughly the sparsest" rather than "the sparsest", and the ranking is a heuristic whose inputs are estimates anyway.
What it must not do is *miss* a good victim, and it cannot: a page in the sparsest non-empty bucket is within `1/B` of the true minimum live fraction.

### The budget loop

```rust
fn consolidate(budget_pages: u32) {
    // Free filling first: it writes no page that the flush was not writing anyway,
    // so its ratio is unbounded and it always outranks everything below.
    for page in flush.partially_filled_pages() { top_up(page); }

    for _ in 0..budget_pages {
        let c = [data_page_candidate(), table_window_candidate(), defrag_candidate()]
                    .max_by_key(|c| c.reclaimed / c.written);
        if c.reclaimed <= c.written { break; }   // writing more than we free
        execute(c);
    }
}
```

The loop is short because the ranking is one number, and the one number works because all three candidate kinds pay in the same currency: bytes written to make bytes reusable.
The `break` is the honest floor — a candidate that writes more than it frees is not consolidation, it is churn, and at that point the file is [as small as it honestly is](#pacing).

**The known failure mode of the single ranked queue.**
One budget with ratio-greedy ranking is the simplest policy that is not obviously wrong, and it inherits what every ratio-greedy cleaner inherits: **costs that current ratios cannot see get starved.**
Description debt on a cold allocation — one that will be read forever but never written again — never looks urgent, because nothing about it is changing.
If that bites, the established fix is to fold the invisible cost into the ranking, which is exactly what LFS's cost-benefit policy does with segment age; the queue structure stays.

### The complexity this buys

Strictly logarithmic work *per flush* is unattainable, because a flush must at least record what it changed.
The attainable form, and what this design achieves:

- `O((k + s) · log F)` in-memory work for a flush dirtying `k` ranges and rewriting `s` statements under the budget;
- `O(1)` victim selection per candidate;
- page writes bounded by `dirty data pages + c + depth` for budget `c`;
- with the common small flush writing **no** address-table page beyond the header.

## Two forms

Consolidation runs inside every flush, in two forms:

- **Free filling.** New data pages are topped up with live content relocated from the sparsest pages. This costs no extra page writes at all, since the page was being written anyway.
- **Budgeted extra pages.** Beyond that, a bounded number of additional pages per flush.

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
The bucket only gives `u` to within `1/B`, and it says nothing about age, so:

```rust
fn data_page_candidate() -> Candidate {
    let bucket = buckets.lowest_non_empty();
    bucket.iter().take(K)                    // K is a small constant, e.g. 8
          .max_by_key(|p| score(p))
}
```

Sampling `K` from the sparsest bucket keeps selection `O(1)` and gets the age term back, and it is robust in the way that matters: the sampled set is already known to be sparse, so the worst outcome of a bad sample is cleaning a page slightly less old than the best one in the same bucket.

Rotating the start of the `take(K)` window across flushes stops the same unlucky prefix from being examined forever.

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
    for _ in 0..WINDOWS_PER_FLUSH {                 // a small constant, e.g. 8
        let mut drain: SmallMap<PageNumber, u32> = SmallMap::new();
        let mut seen: SmallSet<StatementRef> = SmallSet::new();
        let mut encoded = 0;

        // Walk fragments from the rotating cursor until one page's worth of
        // statements would be re-emitted. `sweep_cursor` is an Id, kept across
        // flushes and wrapped at the end of the id space.
        for (key, fragment) in fragments.range((sweep_cursor, 0)..) {
            let Some(s) = fragment.statement() else { continue };   // ZeroByDefault
            if !seen.insert(s) { continue; }                        // one statement,
            drain[slab.page_of(s)] += slab.framing_len[s];          // many fragments
            encoded += re_encoded_size(fragment);
            if encoded >= MAX_PAGE_CONTENT { break; }
        }

        best = max(best, Candidate { benefit: benefit(&drain), written: 1, .. });
        sweep_cursor = next_window_start();
    }
    best
}
```

**The benefit is drained bytes weighted by the fraction drained**, which is what encodes "draining a page from 60 live bytes to 0 frees a slot; draining it from 3000 to 2940 frees nothing":

```
benefit = Σ over pages Q of  drain[Q] · drain[Q] / coverage[Q]
```

The square is doing real work.
Plain `Σ drain[Q]` would rate a window that shaves 2 % off fifty pages exactly as highly as one that empties two — identical bytes reclaimed, and one of them useless.

**Two properties worth knowing about this walk.**
Each statement is counted once even though it may own several fragments, which is what `seen` is for and which is easy to forget, since the fragment map is the only thing being walked.
And a window's cost is bounded by construction: it stops at one page's worth of re-encoded statements, so it touches on the order of 700 fragments, whatever the file's size.

Because a window's statements are rewritten sorted and adjacent, the same pass **re-establishes delta-encoding density**.
Density is restored by cleaning, not maintained by an invariant — which is the cheaper of the two, since maintaining it would constrain every write.

**Executing a window decodes nothing**, which is the property that separates it from every other rewrite in the design: the fragment map already holds resolved truth for the range, so [`rewrite_id_range`](address-table-operations.md#rewrite_id_rangelo-hi) emits statements straight from it.
The drained pages are never touched.
They get emptier because their statements *lose their pins* when the fresh ones supersede them, and coverage follows pins rather than physical presence — so a page can be drained to zero and become reusable while the dead bytes still sit in it, unread, until a load discards them.

The one correctness obligation on any such rewrite is [transferring the anchor](address-table-operations.md#replacement_anchorid--the-one-correctness-obligation).

## Finding the referrers of a data page

**This is an unresolved gap**, and it is worth stating plainly rather than leaving implicit.

Rewriting an address-table page is self-describing: decode the victim, and its statements tell you which `(id, offset)` keys to look up.
A **data** page is opaque bytes with no ids in it, so decoding it tells you nothing, and the fragment map is keyed by `(id, offset)` rather than by address.
Nothing in the design currently gets from a victim data page number to the `Ref` statements that must be re-pointed.

Four candidate answers, roughly in order of how well they fit:

- **A reverse `page → ids` index, maintained in memory.**
  To find the fragments referencing page `P`, sweep each listed id's range of the fragment map and keep the fragments pointing into `P`.
  Cheaper than an exact `page → fragments` index on both axes: one entry per `(page, id)` pair rather than per fragment, and insertion can be idempotent with pruning done lazily on use, so fragment destruction does nothing at all on the hot path.
  It is also **sound under splices**, which is the property that matters: a splice restates a statement with a new offset but the same id and address, so the index stays true through it.
  Refine it with an offset window per `(page, id)` — the bytes a page holds from one id span at most one page's worth of that id's offset space — and the sweep becomes proportional to the fragments actually in `P` rather than to the whole allocation.

  ```rust
  struct ReverseIndex {
      // The window is a hint that is always wide enough, never necessarily tight.
      windows: HashMap<(PageNumber, Id), Range<AllocationOffset>>,
  }
  ```

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

The first option is the current preference.

## Pacing

Each flush consumes at least one reusable page, often a few, so consolidation must reclaim at least that many on average.

The situation where no good victim exists is the situation where pages are largely full — i.e. there is little garbage and the file is close to its live size — so "cleaning cannot keep up" and "the file is honestly this large" coincide, which is the benign coincidence.

The one obligation this leaves is [headroom](../spec/durability.md#headroom), which is a requirement rather than a policy.

## The bound this buys

Garbage accrues in proportion to write traffic and drains in proportion to consolidation effort, so the steady state is set by the budget: enforcing "consolidate until live fraction ≥ τ" bounds the file at `live_size / τ` for a chosen τ, at the price of the corresponding cleaning work.
The bound is controllable and, in relative terms, independent of file size.

## Constants still to be chosen

- The live-fraction target τ.
- The per-flush page budget.
- The age weighting in victim selection.
- The page size, 4 KiB versus 16 KiB, to be measured on macOS/iOS and Linux — a per-file header property either way.
