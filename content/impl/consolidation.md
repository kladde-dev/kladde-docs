---
title: Consolidation
---

Reclaiming garbage: which pages to rewrite, in what order, and how much per flush.

Consolidation rewrites live content out of sparse pages so their slots become reusable, and merges short spans so the description stays small.
It is [not part of the specification](../spec/sequence.md#reclamation-is-not-part-of-this-specification) — an implementation that never consolidates is conforming, and merely useless — but the policy below is what makes the copy-on-write design's space overhead bounded rather than unbounded.

## The high-level picture

Consolidation fights **three independent debts**, and the counters that measure one are blind to the others.

**Description fragmentation** is a property of a *region of the sequence*: how many spans it takes to show its bytes.
Runs of short spans arise where edits happen, since every splice and every split leaves a boundary; they cost entries in table pages, memory in the span table and the rope, and time at open; and no page-level counter can see them, because a run of short spans whose entries sit densely in one full leaf makes that leaf look perfect.

**Page fragmentation** is a property of a *page*, `Data` and `Table` alike: how much of it current state still relies on.
It is measured by [coverage](in-memory-state.md#3-the-page-table) against capacity; it costs file size and nothing else; and nothing that counts spans can see it, because long spans can lie thinly spread over sparse pages.

**Outer fragmentation** is a property of the *file*: reusable pages below its last live page.
It costs file size, since a file can only shrink from its end, and neither kind of counter can see it, because every page involved is either wholly reusable or wholly live.
It arises whenever the live size falls and stays down, since pages freed anywhere but at the end become holes that only future writes can fill.

The first two convert into each other, each in one direction: more spans need more table pages, and merging spans writes their bytes again, leaving the old ones as data-page garbage.

**Only page fragmentation is urgent, and that asymmetry decides how each debt is scheduled.**
Page fragmentation grows with write traffic and compounds, since garbage left uncleaned occupies the pages the next flush wanted to reuse — so it is attacked greedily, worst first.
Description fragmentation is bounded already by [the span policy's](flush.md#the-span-policy) merges as entries leave the header, and what remains is a standing debt that does not grow while nobody writes the region — so a rotating walk pays it down *eventually*, without any ranking structure of its own.
Outer fragmentation is not a debt at all while the file grows, because growth fills the holes first; it becomes one only once the live size has fallen, and is then paid down in [a mode of its own](#outer-fragmentation).

### Three mechanisms

| mechanism | pays down | selected by | reads | writes |
| --- | --- | --- | --- | --- |
| [Evacuation](#evacuation) | page fragmentation of `Data` pages; outer fragmentation, in [compaction mode](#compaction-mode) | the sparsest data pages, one victim at a time — the highest first, in compaction mode | the victim's spans, through the [reverse index](in-memory-state.md#4-the-reverse-index) | the spans' bytes, into data pages, each span restated with its new address |
| [Page rewrite](#the-page-rewrite) | page fragmentation of `Table` pages; outer fragmentation, in compaction mode | the sparsest table pages, one victim at a time — the highest first, in compaction mode | the victim's live entries, from the span table | those entries, restated at the cut |
| [Rotating walk](#merging-spans-rides-the-rotating-walk) | description fragmentation | a cursor that rotates through the sequence | the bytes of the runs it merges | one span per merged run, among the flush's new bytes |

All three end in the same place — fresh pages plus fresh entries — which is what lets one placement pass serve all of them.

**Every one of them is simpler than it would be over allocations**, because a span's id never changes: evacuation writes a victim's bytes elsewhere and states each span again with its new address, and nothing else changes; a page rewrite states each live span in it again under its own id, with no size to state again and no tombstone to carry; and merging a run of spans materializes no zeros, since no span has gaps.

## Evacuation

**Evacuating a data victim writes the bytes of its reached spans into new pages and states each span again with its new address.**
The victim's spans come from the reverse index, exactly and in `O(1)` each.
A span is all or nothing — its bytes move together, since a `Ref` shows one contiguous run — except that a span may be cut at a page boundary where [packing](#slack-is-cheaper-than-a-split--up-to-a-point) needs it, its tail becoming a new span that leads the next page.
Neighbouring short spans in a victim can be merged in the same pass, at no extra write, since their bytes are being written anyway.

### Victim selection for data pages

The classic greedy rule of log-structured file systems — **lowest live fraction first** — refined by age the way LFS and SSD flash translation layers refine it: prefer pages that are sparse *and* old.

The refinement rests on one assumption worth stating explicitly: **temporal update locality** — bytes written together tend to be rewritten together, and soon, or else not for a long time.
It transfers from the workload to pages mechanically: a data page's residents are exactly the content that one flush wrote together, so if the application is still working on whatever produced them, more of its residents will be superseded shortly.
Such a "hot" page gets sparser without help, and cleaning it early wastes writes on relocating survivors that are about to die anyway; a page whose live fraction has been stable for a long time is "cold", and its survivors are the ones worth paying to move.
It **fails gracefully**: under a workload with no locality at all, the age term carries no information and the policy degrades to plain greedy, which is the right fallback anyway.

### Scoring a data page

Take LFS's cost-benefit ratio unchanged, with `u = coverage[p] / capacity` the live fraction and `age = current_epoch − page_epoch[p]`:

```
score(p) = (1 − u) · age / (1 + u)
```

The numerator is what the eviction frees times how long it has been stable; the denominator is what it costs — one page read plus `u` of a page written back.

**Evaluating it without scanning every page**: the next victim is the highest-scoring of `K` pages sampled from the sparsest non-empty bucket, for a small constant `K` — eight, say.
That keeps selection `O(1)` and gets the age term back, and the worst outcome of a bad sample is cleaning a page slightly less old than the best one in the same bucket.
Rotating the start of the sampling window across flushes stops the same unlucky prefix from being examined forever.

### Victims are pulled one at a time

**A victim is taken only when all of its spans have somewhere to go in this flush**, since relocating only some of a page's spans reclaims nothing.
In free filling that means the page's room holds all of them; on a budgeted page, one span may be cut at the boundary, its tail leading the next page — never on the budget's last page, where the tail would have nowhere to go.
Free filling makes one exception once nothing whole fits: moving part of a victim reclaims nothing yet, but in a page that is written anyway it costs nothing either, and it leaves the victim sparser.

A marginal victim frees `(1 − u_v) · C` bytes and writes `u_v · C`: all `C` of the victim's bytes come back, and `u_v · C` of them are spent again at once, because the survivors take up as much space in their new page as they did in the victim.
**Sparsest first is exactly right**, since the marginal ratio `(1 − u_v) / u_v` falls as `u_v` rises, and the churn floor then reads `u_v ≤ 1 / (1 + λ)`.

**A free sink changes the arithmetic, not the order.**
A survivor landing in room that a page being written anyway would otherwise waste consumes nothing, so the victim's net gain is the whole `C`: free filling takes any victim that fits, and the churn floor does not apply to it.

`coverage` counts bytes shown by several spans once per span, so where they are, `u_v` over-states both what an evacuation writes and what it reclaims.

## The page rewrite

**A table victim is drained by restating its live entries elsewhere**: each live span in it is stated again, under its own id, at the cut, and the victim then holds nothing live.
Victims come from the table pages' buckets — sparsest first, sampled and scored as data pages are, or the tail first in [compaction mode](#compaction-mode) — and a victim is taken in one of two ways: into the room of the cut's last page, if all of its live entries fit there, which costs nothing, or under the budget, whose offers gather victims while their entries fit one leaf.

Its cost is those entries' encoded bytes, inline payloads included, which coverage already counts; its benefit is the whole victim.
So it is priced exactly as evacuation is, `(C − written) / written`, and the two compete in [the budget loop](#the-budget-loop) on equal terms, but for the fill floor, which holds evacuation's page alone, since the page rewrite opens none.
Restated entries lie together in id order in their new leaf, so a page rewrite also repairs the delta encoding of entries that went cold far apart in the id space and were evicted together.

A victim with children — an interior page — is rewritten the same way, and its child references move with it; see [unlinking](#unlinking-an-emptied-page).

### Unlinking an emptied page

**Nothing waits for a load.**
With entries, inline payloads, and child references all charged per byte, a table page whose last live entry is restated elsewhere reaches zero coverage in the same flush, and [the quarantine](../spec/durability.md#the-reuse-rule) does the rest.

One step stands between zero coverage and reuse: **an emptied table page is still named by its parent**, so it stays reachable from the header, and [I2](../spec/durability.md#the-two-invariants) forbids overwriting it until a flush has also unlinked it.
When the parent is the header, the flush that emptied the page unlinks it for free, since the header is written last.
Deeper, the parent is rewritten without the child's reference, its other children carried over, and so is *its* parent, up to the header — the `depth − 1` path copy of a [structural update](flush.md#the-shape-of-the-tree).
A writer must not rely on its own shallow trees here, since another writer may shape the tree differently, so:

- **The page table records each table page's parent**, which [the tree property](../spec/span-table.md#physical-format) makes a single page number.
- **A child reference is charged to its parent's coverage**, and released when the child is unlinked; without that charge, a parent whose entries had all moved elsewhere would reach zero coverage while still holding the only reference to live children, and reusing it would orphan them.
- **A page rewritten while it has children carries them along**: into the header if its child list has room, which also flattens the tree, and otherwise into the page's replacement.

Data pages need none of this, having no parent.

## Merging spans rides the rotating walk

**A rotating walk over the rope looks at a fixed number of spans per flush, and merges runs of cold spans whose entries lie in leaves where the entry bytes the merge retires pay for the bytes it writes.**
The decision is per run and local: the walk visits the spans anyway, so the only question is whether a run is worth writing again as one span.

> **A merge pays when the entry bytes it retires exceed `μ` for every byte it writes.**

The data terms cancel, since the bytes the merged spans showed lose their coverage exactly where they lay, so a run's gain is the entries it retires, less the one it adds, less `μ` per byte.
**The best runs are a maximum-subarray problem**: weigh each cold span by its entry's bytes less `μ` times its length, and Kadane's algorithm, in one pass over the spans the walk is traversing anyway, finds the maximal runs of positive weight, each cut into tiles of at most a page, since a `Ref` shows one page at most.
A long span scores `entry − μ · len`, negative for any span longer than `entry / μ` — a few hundred bytes at `μ = 0.02` — so an already-long run is left alone, which is correct, since writing it again merges nothing and only moves bytes.
A short, dense knot of tiny spans is exactly where the run of large positive weights is, so it is what Kadane returns.

**Only cold spans take part**, those whose bytes have gone unwritten for some number of flushes, since a run the application is still editing would be split again by its next edit, which would waste the merge; the [heat](in-memory-state.md#heat) of each span says how long.
And only spans whose entries lie in leaves: [the header is exempt](flush.md#the-span-policy).

**The walk runs before placement**, so a merged run's bytes join the flush's own new bytes, and are packed among them.
`μ` is a price of the same kind as the [churn floor `λ`](#the-churn-floor-is-a-parameter-not-an-identity) — file bytes freed per byte written — and sits far lower, near 0, since what a merge chiefly buys is invisible in the file: an entry in memory per span retired, and the work of resolving it at every open.

**The walk can also give a run of spans consecutive ids**, so that their successors become implicit and their id deltas short, which shrinks entries without writing any data byte.

A new session resumes the walk where the last one left it, if the [consolidator state](consolidator-state.md) says where, or else where [open seeds it](opening.md#the-seeds-of-the-rotating-walk-and-of-heat).

## How a flush and consolidation compose

**Every source changes spans as it goes, and entries come last; that gap is the whole interface between a flush and its consolidation.**
The fold, the span policy's merges, evacuation, the page rewrite and the walk all mark the spans they change as stated, and the cut [derives every entry](flush.md#the-cut) from the span table as it finally stands, so what the flush states is by construction exactly what it changed, each span once.

**A relocated span is the same object as a fresh write**: a run of bytes that has no place yet, differing only in whether its bytes are read from a data page or from the flush's new bytes.
Placement cannot tell them apart, and does not need to.

### Mandatory and elastic

Placement combines two streams: what the flush must write and what consolidation may add.
**One stream must be written whole and the other may be cut short**, and that asymmetry is the only thing the packer needs to know about where a chunk came from: a commit that omits part of what the journal produced is not a commit, while a survivor left behind costs a delay and nothing else.
The walk's merges belong to the first stream, since they are chosen before packing, and once chosen they are writes like any other.

The elastic stream is drawn **on demand**: a victim is pulled into a page only at the moment that page has room its own content cannot use, and only as much of it as fits.
That is what lets consolidation fill every page the flush writes to nearly full without ever opening a page it cannot fill.

### Slack is cheaper than a split — up to a point

**Do not cut a chunk to make a page come out exactly full; cut only to keep a page from closing more than `θ` empty.**
Every cut is one more span, an entry and a node in the rope, which stays until a merge removes it, whereas slack is recovered by the next consolidation that picks the page up.

|  | costs | until |
| --- | --- | --- |
| leaving slack | at most `θ` of a page, on every page but a flush's last, and usually far less | the page is next consolidated |
| splitting to fill it | one more entry and one more span in memory | a merge writes the run again as one span |

**Slack has no small bound of its own, which is what `θ` is for.**
Chunks all just over half a page are the adversarial case — no two can ever share a page, so if they were never cut, every page would close half empty, whatever the look-ahead.
So a page that would close more than `θ` empty is filled instead, in the order that costs least:

1. with whole victims, which ride for free in a page that is being written anyway;
2. failing that, by cutting the flush's next chunk at the page boundary.

That makes the guarantee unconditional: **every data page a flush writes, except its last, is at least `1 − θ` full**, at a cost of at most one cut per page.
On a page that holds content of the flush's own, that content is what gets cut, not a victim's survivor: the flush's own content is what the application is working on, so its cut is the likelier to be overwritten soon and take the extra span with it.
A page opened only for consolidation has no such content, and there a survivor is cut instead, its tail leading the next page.

### Packing in sequence order, with look-ahead

**Pack in sequence order and fit by looking ahead, rather than first-fit over a size-sorted list.**
First-fit-decreasing packs tightly and destroys locality, which costs two things worth keeping: neighbouring spans in one page, which a later evacuation moves together and can merge, and consecutive ids with implicit successors.

```rust
fn pack(mut chunks: Lookahead<Chunk>, elastic: &mut Elastic) -> Vec<Page> {
    let mut pages = Vec::new();
    if chunks.is_empty() { return pages; }       // no page is being written anyway
    let mut open = Page::new();
    loop {
        // 1. The first of the flush's own chunks that fits, in sequence order, within reach.
        if let Some(c) = chunks.take_first_fit(open.room()) { open.place(c); continue; }
        // 2. None fits: free filling, with whatever fits, in the order that pays most.
        while let Some(e) = elastic.take_fitting(open.room(), open.number()) { open.place_all(e); }
        if chunks.is_empty() { break; }          // the flush's last page: short only if nothing fits
        // 3. Still more than θ empty: cut the next chunk so the page closes full.
        if open.room() > THETA * MAX_PAGE_CONTENT {
            let (head, tail) = chunks.pop_next_to_cut().split_at(open.room());
            open.place(head);
            chunks.push_front(tail);             // shorter than the `Inline` threshold: an `Inline`
        }
        pages.push(mem::take(&mut open));
    }
    pages.push(open);
    pages
}
```

Chunks longer than a page never reach `pack`: whole pages are cut off their front first and written on their own.
**Step 2 fills the room with anything that fits and costs nothing**, on every page the last included: whole victims first — the tail first, in [compaction mode](#compaction-mode) — since each frees a page; then part of a victim too big to take whole, a whole span at a time, which frees no page yet but leaves the victim sparser.

## The budget

The design has one throttle — a per-flush **maintenance budget** in pages written beyond the flush's own content — and one queue of offers competing for it, ranked by `bytes reclaimed / bytes written`.
One further parameter, [the churn floor `λ`](#the-churn-floor-is-a-parameter-not-an-identity), says when an offer is not worth taking at all.

Two sources compete for that budget, and they are one mechanism applied to two kinds of page: **evacuation** relocates a data victim's spans and reclaims dead data bytes; **the page rewrite** restates a table victim's live entries and reclaims dead entry bytes.
The rotating walk stays out of the ranking: what it retires is description, measured in entries and memory rather than in file bytes, and its price `μ` is its own floor.

### The structures behind the ranking

Every source that starts from a *page* is ranked from **bucket queues**, which make "give me a good victim" `O(1)` instead of `O(log P)`.

```rust
struct Buckets {
    lists: [Vec<PageNumber>; B],          // B is small; 16 is plenty
    at: HashMap<PageNumber, (u8, u32)>,   // which bucket, and which slot in it
}
```

A page's bucket is `coverage[p] * B / capacity`, so moving a page is a swap-remove from its old list, a push onto the new one, and a fix of the `at` entry of whatever the swap moved — which matters because coverage changes on every span stated or dropped, not once per flush.
**Each page kind gets its own instance**, since data pages outnumber table pages by roughly the ratio of their contents; header pages belong in neither.
A page in the sparsest non-empty bucket is within `1/B` of the true minimum live fraction, which is all a heuristic ranking needs.
A page whose coverage reaches zero leaves its bucket at once, since it is retired at the next commit either way.

### The budget loop

A flush and its consolidation run as one straight line:

```rust
fn flush(journal: Journal, budget: Budget) {
    // 1. The fold, mapped onto spans; the header's entries chosen, and the short
    //    spans of the runs that leave it merged; the rotating walk's merges.
    let fold = fold(journal);
    map_pieces_to_spans(fold);
    choose_header_and_merge_evicted();
    rotating_walk();

    // 2. Data pages: the flush's own chunks in sequence order, with victims riding
    //    along wherever a page has room its own content cannot use. See `pack`.
    let mut data = pack(new_chunks(), &mut elastic);

    // 3. The budget, in pages written, offer by offer to whichever page kind reclaims
    //    more per byte. An offer of data victims opens a page, and only one it fills;
    //    an offer of table victims opens none, since what it restates joins the
    //    leaves step 4 lays out, and costs the budget the pages those take.
    let mut spent = 0.0;
    while spent < budget.pages {
        let left = budget.pages - spent;
        let Some(offer) = [evacuation.offer_page(left), page_rewrite.offer(left)]
                              .into_iter().flatten()
                              .max_by_key(|o| o.reclaimed / o.written) else { break };
        if !offer.holds_tail() {        // compaction mode's tail passes both floors
            if offer.reclaimed < budget.lambda * offer.written { break; }   // the churn floor
            if offer.opens_page() && offer.fill() < 1.0 - THETA { break; }  // the fill floor
        }
        spent += if offer.opens_page() { 1.0 } else { offer.written / C };
        offer.take(&mut data);
    }

    // 4. Table pages: the hottest entries in the header, the rest in leaves in id
    //    order, the last leaf's room to whole sparse leaves that fit.
    let table = cut();

    // 5. Unlink emptied pages, write, fsync, commit; then truncate a reusable tail.
    commit(data, table);
}
```

**The two page kinds are combined, not traded off — but they do compete** for the one budget, counted in the pages they write; each source's offers decline as it takes more, so one greedy pass over the two is the right amount of machinery.

**Free filling happens inside steps 2 and 4, and step 3 is the only place a page is opened for consolidation's sake.**
It opens one only for an offer of data victims, and only when the offer fills it to `1 − θ`, which keeps a victim too many from costing a nearly empty page; only an offer holding [compaction mode's tail](#compaction-mode) is exempt.
An offer of data victims is what the page would hold: whole victims first, by best fit, and then — if that leaves more than `θ` empty — one more victim with a span cut at the page boundary, exactly as `pack` cuts the flush's own chunk.

**An offer of table victims opens no page, so no fill floor holds it, and it costs the budget the pages its restatements take.**
The restatements of a sparse leaf take a fraction of a leaf, among everything else the flush states, so there is no page a floor could keep from closing short; the churn floor already weighs their bytes against the page they free.
Held to the fill floor as well, leaves would stay half empty, since an offer would have to restate `1 − θ` of a leaf, which leaves less than half full could do only three or more at a time.

#### The churn floor is a parameter, not an identity

The floor is a parameter `λ` — space freed per byte written — and its price is explicit.
Cleaning pages at live fraction `u` costs `u / (1 − u)` bytes written per byte of space freed, so a total write amplification of `1 / (1 − u)`: 2× at `u = 1/2`, 5× at `0.8`, 10× at `0.9`.

**But `λ` is the wrong knob for a target file fill.**
An average fill of 80–90 % does not mean cleaning pages that are 80–90 % live, and implementing it that way would buy the last few percent at five to ten times the write traffic.
It means the distribution of live fractions is bimodal enough that most pages sit near full while the garbage concentrates in a few nearly empty ones — which is what the age term is for, and the whole reason LFS's cost-benefit cleaner beats plain greedy.

So the two knobs do different jobs, and only one of them is a floor:

- **`λ` is a churn floor.** It stops the cleaner grinding when there is nothing worth cleaning, and it belongs *low* — near 1, i.e. near `u = 1/2` — precisely so that it is not what decides the file's fill.
- **The per-flush budget is the throttle**, and it is what converges the file on a target fill `τ`: measure `live_bytes / (pages · C)` after each commit, over every page but the headers, and move the budget up while it sits below `τ`, down while above — multiplicatively, `budget · exp(4 · (τ − fill))` within its bounds, is a rule that works.
  Counting every page rather than only the live ones makes holes raise the budget as sparse pages do.

**Ratio-greedy ranking starves costs that current ratios cannot see**, and one such cost remains: description debt in a cold region, which never looks urgent, because nothing about it is changing.
It is taken out of the ranking rather than weighted inside it: the rotating walk reaches every span in turn.

### The complexity this incurs

- `O(r · log k)` in-memory work for a flush of `r` records over `k` spans, plus `O(log k)` per span stated, merged, or relocated under the budget;
- `O(1)` victim selection per candidate;
- page writes bounded by the flush's new data pages plus the budget plus the tree's depth;
- with the common small flush writing **no** table page beyond the header.

## Two forms of consolidation

- **Free filling.** Victims, and parts of victims too big to take whole, ride in the room of pages that are being written anyway — the flush's own data pages, and the cut's last leaf — so they cost no page write and compete with nothing.
  It runs to exhaustion.
- **Budgeted cleaning.** Beyond that, data pages that exist only to reclaim, offered one at a time and opened only when the offer fills them, and table victims whose restatements take leaves of their own.
  These compete, under the budget and the churn floor.

## Outer fragmentation

**Placement decides whether holes form, truncation returns what reaches the end, and a compaction mode moves what never would.**

### Lowest page first

**Reusable pages are handed out lowest first rather than in the order they became reusable, and the file grows only when none is left.**
Lowest first keeps new content away from the end of the file, so pages that die there stay dead and the tail drains of its own accord; a first-in-first-out pool would refill the tail as readily as any hole.
The pool is an ordered set rather than a min-heap, because it is used from both ends: allocation takes the lowest page, and truncation removes the highest ones.
Lowest first gives up the contiguity that a run from the end of the file would offer a large write, but [contiguity buys nothing](flush.md#page-contiguity-buys-nothing).
Journal pages come from the same pool, so the journal, too, starts in the lowest hole rather than at the end, and so do payload pages, which become data pages where they lie.

### Truncation

After a commit, the file may shrink to end right after its highest live or fallback page — the [truncation rule](../spec/durability.md#the-truncation-rule).
It is safe for the same reason reuse is: no header that recovery could choose reaches past that point, and a power cut that loses the truncation only leaves the file longer than necessary.
The journal's last page does not count while it is empty, since a reader treats [a journal page past the end of the file](../spec/file-format.md#the-journal-pointer) as empty too.
The new end is always a page boundary, never inside the last page's padding.

Truncating is cheap, but shrinking and regrowing the file on every flush of a workload that oscillates is not, so both reference implementations truncate only once the reusable tail exceeds a threshold, and at close and open.

**An explicit full compaction would serve a file about to be exported or archived**: flush, and keep flushing in compaction mode, with an unlimited budget, until no hole is left and the file ends at its last live page.
Neither the reference algorithms nor kladde-rs offer one yet; closing after a few flushes comes close.

### Compaction mode

Placement and truncation return the tail only if the pages there die by themselves, and cold, full pages never do.
So while **holes** — pages below the highest live one that are reusable now, not in quarantine — exceed a share `h` of the file, the highest non-journal live page is offered to [the budget loop](#the-budget-loop) ahead of every other victim, whatever its fill: evacuated if it is a data page, rewritten if it is a table page, with lowest-first placement landing its content in holes.

**The mode needs no hysteresis**, because switching in and out of it costs nothing: a tail page once moved stays moved.

**An offer holding the tail passes the churn floor, and the fill floor if it opens a data page, and a table tail is taken even when its restatements need more than one leaf, so it is the budget alone that paces the mode.**
The floors weigh what a page returns to the pool, and a tail victim returns more: a whole page to the file system.
The return is realised only once every page above the victim has gone too, which is why the mode works strictly from the top down; journal pages are skipped, since the next flush moves the journal to the lowest hole by itself.

**The tail moves only while a page that is reusable now lies below it.**
Right after a mass removal, the pages it released sit in quarantine for two commits, so content moved then would land in pages the file grows by, which become the next tail, and the same content would move again.
Holes count only reusable pages for a related reason: the pages the last commits released are working space, which the next flushes reuse by themselves.

**Free filling takes part in the mode too**: while it lasts, `take_fitting` offers the highest non-journal live page ahead of the sparsest, whenever that page is of its kind, lies above the page being filled, and its content fits the room.

Finding the highest live page needs no structure of its own: a cached index, lowered past reusable pages as the tail empties and raised when a page above it is written, is `O(1)` amortised.

## Pacing

Each flush consumes at least one reusable page, often a few, so consolidation must reclaim at least that many on average.
The situation where no good victim exists is the situation where pages are largely full — little garbage, and the file close to its live size — so "cleaning cannot keep up" and "the file is honestly this large" coincide, which is the benign coincidence.
The one obligation this leaves is [headroom](../spec/durability.md#headroom), which is a requirement rather than a policy.

## The bound this buys

Garbage accrues in proportion to write traffic and drains in proportion to consolidation effort, so the steady state is set by the budget: converging on a live fraction `τ` bounds the file at `live_size / τ`, at the price of the corresponding cleaning work.
The bound is controllable and, in relative terms, independent of file size; [the guiding principle](index.md#overhead-and-work-independent-of-size) adds the description's share.

## Constants

kladde-rs's defaults, of which only the span policy's were [measured](../evaluation/one-sequence.md#the-span-policy):

- the target fill `τ`, 0.8, and the budget's start and bounds, 8 pages within 1 and 256;
- the churn floor `λ`, 1;
- the fill floor `θ`, 0.05, and the look-ahead, 16 chunks;
- the walk's length, 256 spans per flush, its price `μ`, 0.02, and the flushes after which a span counts as cold, 16;
- the victims sampled per pick, 8, and 16 buckets for each page kind;
- the hole share `h`, 0.25, and a reusable tail of 16 pages to trigger truncation.
