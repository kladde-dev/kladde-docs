---
title: Consolidation
---

Reclaiming garbage: which pages to rewrite, in what order, and how much per flush.

Consolidation rewrites live content out of sparse pages so their slots become reusable.
It is [not part of the specification](../spec/allocations.md#reclamation-is-not-part-of-this-specification) — an implementation that never consolidates is conforming, and merely useless — but the policy below is what makes the copy-on-write design's space overhead bounded rather than unbounded.

## The high-level picture

Consolidation fights **three independent debts**, and the counters that measure one are blind to the others.

**Description fragmentation** is a property of an *allocation*: how many statements it takes to say what its bytes are.
It is measured by [`fragment_count` and `statement_bytes`](in-memory-state.md#3-the-allocation-map) against `size`; it costs memory in the fragment map, bytes in the address table, and time at load; and no page-level counter can see it, because an allocation described by two hundred statements packed densely into one full page makes that page look perfect.

**Page fragmentation** is a property of a *page*, `Data` and `Table` alike: how much of it current state still relies on.
It is measured by [`coverage[p]`](liveness.md#coverage) against capacity; it costs file size and nothing else; and no per-id counter can see it, because an allocation described by one `Ref` per flush has ideal `fragment_count` however thinly its bytes end up spread.

**Outer fragmentation** is a property of the *file*: reusable pages below its last live page.
It costs file size, since a file can only shrink from its end, and neither kind of counter can see it, because every page involved is either wholly reusable or wholly live.
It arises whenever the live size falls and stays down — after a large deletion, say — since pages freed anywhere but at the end become holes that only future writes can fill.

The first two convert into each other, each in exactly one direction, which is why neither can be left to the other's mechanism:

- description fragmentation *becomes* address-table page fragmentation, since more statements need more table pages;
- repairing description fragmentation *creates* data-page fragmentation, since rewriting a stipple as one `Ref` kills the patches it supersedes.

**Only page fragmentation is urgent, and that asymmetry is what decides how each debt is scheduled.**
Page fragmentation grows with write traffic and compounds, since garbage left uncleaned occupies the pages the next flush wanted to reuse — so it is attacked greedily, worst first.
Description fragmentation is a standing debt that does not grow while nobody writes the allocation — so it need only be paid down *eventually*, which a rotating sweep discovers without any ranking structure of its own.
Outer fragmentation is not a debt at all while the file grows, because growth fills the holes first; it becomes one only once the live size has fallen, and is then paid down in [a mode of its own](#outer-fragmentation).

### Four mechanisms

| mechanism | pays down | selected by | reads | writes |
| --- | --- | --- | --- | --- |
| [Evacuation](#victims-are-pulled-one-at-a-time) | page fragmentation of `Data` pages; outer fragmentation, in [compaction mode](#compaction-mode) | the sparsest data pages, one victim at a time — the highest first, in compaction mode | the victim's survivors, from the file, found through the [reverse index](#finding-the-referrers-of-a-data-page) | the survivors, into data pages |
| [Page rewrite](#the-page-rewrite) | page fragmentation of `Table` pages; outer fragmentation, in compaction mode | the sparsest table pages, one victim at a time — the highest first, in compaction mode | the victim, decoded | the victim's live content, stated again at the cut |
| [Rotating window](#the-rotating-window) | delta-encoding density | a cursor that rotates through the fragment map | the fragment map | restatements, into the room the cut leaves over |
| [Description defragmentation](#description-defragmentation-rides-the-rotating-window) | description fragmentation | the stipples the rotating window's walk finds | the stipple's bytes, from the file | one `Ref` per candidate, at most a page long, among the flush's own chunks — or one `Inline`, below the threshold |

All four end in the same place — fresh pages plus fresh statements — which is what lets [one placement pass](#how-a-flush-and-consolidation-compose) serve all of them.

### Why address-table pages have two mechanisms

**The page rewrite drains table pages and the rotating window keeps them dense**, and it is [deriving every statement from one dirty set](#one-dirty-set-and-why-statements-are-derived-last) that divides the work that way.

Once every table-side mechanism merely takes fragments and the cut states them all together, a restatement costs its encoded bytes rather than a page, and the cheapest way to empty a victim is to restate exactly what its statements still own — which is what the page rewrite does.
A window that emptied the same victim would restate a superset: everything live in the victim's key span, including whatever other pages hold there.
So the page rewrite does the draining, and it selects victims exactly as evacuation does, sparsest first from [a bucket queue of its own](#the-structures-behind-the-ranking).
That includes a table page whose live statements are scattered across the whole id space, which no window could drain but which is, to the page rewrite, simply a sparse victim.

What the page rewrite cannot do is make its output denser in key space than its input.
Statements that went cold together and were evicted from the header together may lie far apart in the id space, and each of them then pays nearly full-width varints for its id and offset — two to four bytes more than among dense neighbours, on statements of about ten.
The rotating window repairs that: it restates a contiguous key range, so its output is as dense as the encoding allows.
That is worth too little per byte to compete for the budget, so the window gets only the room the victims leave over — which is also all it needs, since density is never urgent.

Both mechanisms copy `Inline` payloads out of table pages, and the page rewrite decodes its victim, so [address-table pages stay resident](index.md#the-mirror) by default; at ~8 MB per million allocations that is cheap.
Nothing depends on it, though: dropping leaves after load, as data pages are dropped, costs one read per victim and one per table page whose inline payload the window restates.

**Runs of small `Inline`s are handled in two stages, and neither needs a mechanism of its own.**
The cut [merges adjacent inline fragments](#one-dirty-set-and-why-statements-are-derived-last) into one `Inline` — every flush for the header's, and for a leaf's whenever a page rewrite or the window takes them — capping the merged payload at the [`Inline` threshold](flush.md#the-inline-threshold) rather than the format's 125 bytes, since a larger payload is exactly the cold, resident bulk the threshold exists to keep out of the address table.
A run that outgrows the threshold — a vector accumulating small elements over many flushes, say — is then a dense stipple, which [description defragmentation](#description-defragmentation-rides-the-rotating-window) finds and hoists into `Ref`s a page at a time; the cut could not do that itself, since it runs after `pack` and places no data.

### Description defragmentation rides the rotating window

**Description defragmentation needs no victim selection of its own**, because the rotating window already walks every fragment of a contiguous key range and therefore already sees which allocations are stippled.

The decision is per id and entirely local: the walk visits this id's fragments anyway, so the only question is whether a patched region is worth replacing with one fresh statement over zero-filled bytes.
That statement is a `Ref` over a new data chunk, or — when the region is no longer than the [`Inline` threshold](flush.md#the-inline-threshold) — an `Inline`, which needs no data page at all: its pending fragment is placed `Inline`, so `pack` never sees it, and the cut puts its payload in whichever table page it chooses.

**Both sides carry data bytes and statement bytes, and getting that right is the whole of the criterion.**
The data terms very nearly cancel, because the fragments a rewrite supersedes lose their coverage exactly where they pointed:

```
freed(a, b)    = Σ live bytes of the `Bytes` fragments in [a, b)   // coverage that drops
               + Σ framing of the statements that die
written(a, b)  = (b − a)                                           // including the gaps
               + the new statement's framing                       // a `Ref`'s or an `Inline`'s
```

Subtracting leaves a criterion that is worth stating on its own, because it is the thing to reason about and it is not what the ratio looks like at first glance:

> **A defragmentation pays when the statement bytes it retires exceed the zero bytes it materialises** — by a margin of `μ` for every byte it writes.
> `framing_retired − new_framing − (1 − density) · (b − a) > μ · (b − a)`, with `density` the fraction of `[a, b)` that currently resolves through a statement.

**`μ` and [the churn floor `λ`](#the-churn-floor-is-a-parameter-not-an-identity) are the same kind of price — file bytes freed per byte written — and differ because description defragmentation is paid mostly in a currency neither of them measures.**
Its net saving in file bytes is at most the framing it retires, while what it chiefly buys is invisible in the file: a fragment-map entry per statement retired, and the work of resolving those statements at every load.
So its floor has to sit far lower than evacuation's for it to do anything at all: `λ` near 1, and `μ ≥ 0` near 0 — at `μ = 0` a defragmentation must merely not grow the file.
Tying the two as `μ = λ − 1` would give sensible values only at `λ ≈ 1`: a cautious `λ = 2` would force `μ = 1`, where a rewrite had to save a byte of statements per byte it writes, which a run of 16-byte inlines saving 3 bytes of framing each never does.
If one price is wanted after all, the way to it is to make the hidden benefit explicit — credit each retired fragment with the memory it frees — and then weigh defragmentation against the same `λ` as everything else.

### The envelope is a maximum-subarray problem

**There is no need for a heuristic, and no need to choose between "all" and "nothing".**
Attribute the criterion above to each fragment and the best subrange is the maximum-weight contiguous run of them — Kadane's algorithm, one pass, on a sequence the rotating walk is traversing anyway.

```rust
/// The rewrites worth doing in `id`, each at most one page long. Weights are *net*
/// file bytes — what a rewrite releases minus what it consumes — less `mu` per byte
/// written, so a range that would grow the file, or cost more to write than it
/// saves, weighs below zero.
fn defrag_candidates(id: Id, size: u32, mu: f32) -> SmallVec<DefragCandidate> {
    // `share` spreads a statement's framing over the fragments it owns — `pins`
    // counts them — so a statement wholly inside the range is credited in full.
    let weight = |f: &Fragment, len: u32| {
        let share = f.statement().map_or(0.0, |s| framing_len(s) as f32 / pins(s) as f32);
        let net = match f {
            Bytes{..}          => share,                // its bytes move: released, consumed
            Zero{..}           => share - len as f32,   // its zeros become real bytes
            Pending(p)         => estimate(p, len),     // taken this flush; see below
        };
        net - mu * len as f32
    };
    let Some((_, range)) = kadane(fragments.range((id, 0)..(id, size)), weight) else {
        return SmallVec::new();
    };
    // A `Ref` holds at most one page, so a longer range becomes consecutive tiles,
    // each weighed on its own; see below. A tile's gain is net of the statement it
    // adds, an `Inline`'s framing or a `Ref`'s, so candidates rank by gain directly.
    tiles(range, MAX_PAGE_CONTENT)
        .map(|tile| {
            let framing = if tile.len() <= INLINE_THRESHOLD { INLINE_FRAMING } else { REF_FRAMING };
            DefragCandidate { id, gain: weight_of(tile, weight) - framing, age: age_of(id, tile), range: tile }
        })
        .filter(|c| c.gain > 0.0)
        .collect()
}

/// A lower bound on how long the application has left `tile` unwritten; see below.
fn age_of(id: Id, tile: Range) -> u32 {
    let youngest = fragments.range(tile)
        .map(|f| f.statement().map_or(0, flushes_since_placed))   // pending: 0
        .min();
    max(flushes_since(allocations[id].last_written), youngest.unwrap_or(0))
}
```

This code example shows the calculation of the maximum envelope as a free-standing function for best readability.
The real implementation folds Kadane's algorithm into [the rotating walk](#the-rotating-window), which iterates over fragments anyway, evaluating and resetting the max-weight subarray records at every `id` boundary of that walk.
A walk that starts or stops inside an id sees only part of it, and Kadane then finds the best subrange of that part; a stipple that a walk boundary cuts in two is judged as two halves, which errs toward doing less.

The weights are what make the answer come out right at both ends.
A long `Zero` scores nearly `−(1 + μ) · len`, so the envelope stops at the edge of a stipple rather than swallowing the sparse allocation around it.
A single large `Ref` scores `share − μ · len`, which is negative for any `Ref` longer than `share / μ` — a few hundred bytes at `μ = 0.02` — so an already-contiguous region is excluded, which is correct, since rewriting it merges no statements and only moves bytes.
And a long allocation carrying a short, dense knot of tiny `Inline` patches is exactly where the run of large positive weights is, so it is what Kadane returns.

**A candidate is at most one page long**, so that it is exactly one `Ref` — a `Ref` [holds at most one page's content](../spec/address-table.md#bounds) — and therefore exactly one chunk, or, if short enough, one `Inline` and no chunk at all.
Kadane's range has no such bound, so it is tiled into consecutive candidates of at most a page each, and each tile is weighed on its own and kept only if its gain, net of the statement it adds, is positive.
Tiling loses nothing, since a range longer than a page needs one `Ref` per page however it is rewritten, and it lets the reserved share take a long stipple a page at a time.
Bounding Kadane's range instead would find only the single best page-sized window, and would leave the rest of a long stipple to later rotations.

### Where it is called, and how it is executed

**Discovered by the rotating walk, executed from a share of its own.**
The walk visits every id's fragments in offset order, so Kadane costs nothing extra, and [`rotating_window`](#the-rotating-window) returns the candidates it found.
They cannot compete with evacuation for the budget, since what they pay down is description rather than space, and a net saving of a few bytes of framing would lose to any victim.
So they are paid from a **reserved share** of each flush's budget, and **the share is spent before packing, not during it**: at the start of each flush, the best-scoring candidates are chosen up to the share, and their rewrites join the flush's own chunks.
Once chosen, a rewrite is a write like any other, so [`pack`](#packing-in-id-order-with-look-ahead) places it in key order among the flush's own chunks, and every rewrite chosen is written.
Offering rewrites as filler instead would starve the valuable ones: what a page has left after the flush's own content is rarely room enough for a rewrite of any size, and one near a page long would fit only by the accident of a nearly empty page.

Among themselves, candidates are ranked the way data pages are,

```
score = gain · age / written
age   = max(the allocation's age, the youngest age among the tile's fragments)
```

Both terms are lower bounds on how long the application has left the tile's content unwritten, and each covers the other's blind spot:

- **The allocation's age** — flushes since [`last_written`](in-memory-state.md#3-the-allocation-map) — sees through restatements, which re-stamp a statement's epoch without touching its content.
  The header re-stamps everything it holds on every flush, so in a small file whose statements all live there, epochs alone would make every allocation look brand new forever.
- **A fragment's age** — flushes since the statement that owns it was placed, or zero if the flush in progress has taken it — sees through writes elsewhere in a large allocation, which reset the allocation's age but not the tile's.

The allocation's age survives a reopen through the [consolidator state](consolidator-state.md#content-ages); rebuilt from the file alone, it would make every allocation with inline content in the header look young at every open, since the header rewrites what it holds.

The age term is the same *prediction* it is in [data-page scoring](#scoring-a-data-page): a range the application patched recently is likely to be patched again, which would re-stipple it and waste the rewrite.
So a cold allocation's candidate rises the longer it waits, however marginal its gain, and the reserved share guarantees that it is reached.

**A candidate found in one flush is executed in a later one**, because the walk rides on the rotating window, which runs at the cut, after the flush's data pages are packed.
So it is re-checked when it is chosen, after the executing flush's fold has taken what it changes: its gain and age are recomputed from the fragments now in its range, which is cheap, and it is dropped unless its gain is still positive and **its age is at least one flush**.
That filter is what keeps a rewrite off bytes the executing flush is writing — a tile holding a fragment the fold just took has both terms at zero — and it has to be a filter rather than only a factor in the score, since a zero score can still be chosen while the share has room.
The walk itself may weigh fragments the flush has already taken only by estimate, since the statements that will own them are not bound yet; the re-check makes that harmless.

**A session's first flush draws on a read-only walk at open**, over the window the previous session's last walk covered, which the [consolidator state](consolidator-state.md#the-rotating-windows-position) records, or else from wherever [open seeds the rotation](#the-rotating-window).
Otherwise the candidates that last walk found would be lost at close, and a session of one flush would find candidates and never execute any.

Executing one needs no new operation: it is a [take](address-table-operations.md#takeid-range-pending-dirty) of the whole range, with the range's resolved content — read from the file and the resident table pages, gaps `memset` to zero, assembled in the journal's arena — as the bytes to write.
Taking replaces everything under the range with one pending fragment and releases the old owners' pins, and the cut states it as one `Ref`, or one `Inline` if the range is short enough.
Unlike [`write_bytes`](address-table-operations.md#write_bytesid-offset-len-origin), it leaves `last_written` alone, since the content does not change.

A per-id bucket queue over `statement_bytes * B / max(size, 1)` remains available if one rotation ever proves too slow to discover what needs doing.
It should not be built before that measurement exists, being a second structure to maintain on every statement created or destroyed.

### How a flush and consolidation compose

**Every source takes fragments as it goes, and statements come last; that gap is the whole interface between a flush and its consolidation.**
The fold, the header, evacuation, the page rewrite, the rotating window, and description defragmentation all do the same two things: they [take](address-table-operations.md#takeid-range-pending-dirty) the fragments they change, which releases the old owners at once, and they add the range to the flush's dirty set.
No statement exists until the address table is cut; then every one is [derived from the fragment map as it finally stands](#one-dirty-set-and-why-statements-are-derived-last), so that what the flush states is by construction exactly what it changed.

A taken fragment is **pending**, and its [entry](in-memory-state.md#during-a-flush) records where its bytes are now and where they will be stated from:

```rust
enum Origin {
    Arena(ArenaPos),   // written this flush
    File(Address),     // already in the file
    None,              // zeros: nothing to read
}

struct ArenaPos(u32);  // bounds the arena at 4 GiB; see below
```

**A pending fragment whose bytes have no place yet is a chunk**, and that is the one thing `pack` needs to know: it places the chunk, and the fragment has its location.
A pending fragment that keeps its location — carried by the header, restated by a page rewrite or the window, shifted by a splice — needs no page, only a statement.
**A relocated survivor is therefore the same object as a fresh write**: a chunk, differing only in whether its bytes are read from the file or from the journal's arena.
A survivor's bytes are always a data page's, since evacuation takes only data victims, and an inline payload never becomes a chunk by moving: a page rewrite takes it in place, and the cut copies it into its new table page.
The one chunk that reads resident table pages is a description defragmentation rewrite over a range holding inline payloads, and it assembles its bytes in the arena first, so to `pack` it too is a fresh write.
Placement cannot tell them apart, and does not need to.

**A 32-bit `ArenaPos` halves `Origin`**: six bytes of payload in its larger variant rather than eight, so eight bytes with the tag rather than sixteen.
That is a saving of width, not of a niche — `Option<Origin>` would cost nothing extra at either width, since the tag has spare values.
It also turns an assumption into a bound the implementation must enforce: the arena holds everything written since the last flush, so a flush must be forced before it would outgrow 32 bits, and a single transaction larger than that, which no flush can split, must fail cleanly — which is what [the bound on transactions](../spec/address-table.md#bounds) requires.

#### Mandatory and elastic

Placement combines two streams: what the flush must write and what consolidation may add.
**One stream must be written whole and the other may be cut short**, and that asymmetry is the only thing the packer needs to know about where a chunk came from.
A commit that omits part of what the journal produced is not a commit.
A survivor left behind costs a delay and nothing else.
Description defragmentation's rewrites belong to the first stream, since [they are chosen before packing](#where-it-is-called-and-how-it-is-executed), and once chosen they are writes like any other.

Neither stream is cut into pages before the other is visible, because pre-cutting the mandatory stream would freeze exactly the boundaries that the elastic stream exists to avoid.
The elastic stream is instead drawn **on demand**: a victim is pulled into a page only at the moment that page has room its own content cannot use, and only as much of it as fits.
That is what lets consolidation fill every page the flush writes to nearly full — cutting a fixed stream into pages would typically leave its last page sparse — without ever opening a page it cannot fill.

#### Slack is cheaper than a split — up to a point

**Do not cut a chunk to make a page come out exactly full; cut only to keep a page from closing more than `θ` empty.**
In principle, the format would allow cutting `Ref` statements at arbitrary offsets and thus packing all emitted data pages exactly full.
However, the reference implementation does not try to fit data pages *exactly* full and instead accepts a small amount of slack in each page.
Filling data pages exactly would reduce page fragmentation at the cost of description fragmentation, with an important asymmetry: page fragmentation is *recoverable*, since the next consolidation to pick the page up re-packs it, whereas a split is a statement and a fragment-map entry that survive until [description defragmentation](#description-defragmentation-rides-the-rotating-window) pays them off — and description debt is the debt with no natural drain.

|  | costs | until |
| --- | --- | --- |
| leaving slack | at most `θ` of a page, on every page but a flush's last, and usually far less | the page is next consolidated |
| splitting to fill it | ~10 bytes of address table, ~20 bytes of fragment map, and one more entry in every scan of that id | something rewrites the range as one `Ref` |

**Slack has no small bound of its own, which is what `θ` is for.**
Without it, what bounds slack is the smallest chunk still within the packer's reach, and that is only as good as the chunks are: close to the `Inline` threshold when small chunks are plentiful, and nothing like it when they are not.
Chunks all just over half a page are the adversarial case — no two can ever share a page, so if we didn't ever cut them then every page would close half empty, whatever the look-ahead.

So a page that would close more than `θ` empty is filled instead, in the order that costs least:

1. with whole victims, which ride for free in a page that is being written anyway;
2. failing that, by cutting the flush's next chunk at the page boundary.

That makes the guarantee unconditional: **every data page a flush writes, except its last, is at least `1 − θ` full**, whatever the chunk sizes, at a cost of at most one cut per page — and in the adversarial case the cuts are exactly what halve the page count.
The chunk cut is the next one in key order rather than the largest within reach, so its two halves stay adjacent in key order and in page order, and a tail shorter than the `Inline` threshold becomes an `Inline` rather than a chunk.

**On a page that holds content of the flush's own, that content is what gets cut, not a victim's survivor.**
Both cuts cost exactly one extra statement.
But the flush's own content is what the application is working on — the same [temporal update locality](#victim-selection-for-data-pages) that data-page scoring rests on — so its cut is the likelier to be overwritten soon and take the extra statement with it, whereas a survivor's cut would sit in cold data until description defragmentation found it.
For the same reason a description defragmentation rewrite, though it travels with the flush's own chunks, is cut only when none of the genuine ones is within reach: it is cold by selection, since that is what its age term chooses for.

A page opened only for consolidation has no such content, and there a survivor is cut instead, its tail leading the next page.
Refusing to would leave the adversarial file at half fill for good: every victim in it holds one chunk just over half a page, so no two victims could ever share a page, and consolidation could never improve it.

#### Packing in id order, with look-ahead

**Pack in `(id, offset)` order and fit by looking ahead, rather than first-fit over a size-sorted list.**
First-fit-decreasing packs tightly and destroys id locality completely, which costs two things worth keeping: a smaller [reverse index](#finding-the-referrers-of-a-data-page), which holds one entry per `(page, id)` pair, and the chance that a later evacuation of the page relocates one id's bytes together and merges their fragments.

```rust
fn pack(mut chunks: Lookahead<Chunk>, elastic: &mut Elastic) -> Vec<Page> {
    let mut pages = Vec::new();
    if chunks.is_empty() { return pages; }       // no page is being written anyway
    let mut open = Page::new();
    loop {
        // 1. The first of the flush's own chunks that fits, in key order, within reach.
        //    They include the description defragmentation rewrites the share paid for.
        if let Some(c) = chunks.take_first_fit(open.room()) { open.place(c); continue; }
        // 2. None fits: free filling, with whatever fits, in the order that pays most.
        while let Some(e) = elastic.take_fitting(open.room(), open.number()) { open.place_all(e); }
        if chunks.is_empty() { break; }          // the flush's last page: short only if nothing fits
        // 3. Still more than θ empty: cut the next chunk so the page closes full —
        //    a genuine one of the flush's own, unless only rewrites are within reach.
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

Chunks longer than a page never reach `pack`: whole pages are cut off their front first and written on their own, [as the flush's placement does](flush.md#cutting-and-packing-concretely), since that is the one cut that is forced rather than chosen.

**Step 2 fills the room with anything that fits and costs nothing**, on every page the last included, in the order that pays most:

1. whole victims — the tail first, in [compaction mode](#compaction-mode) — since each frees a page;
2. description defragmentation candidates beyond the share, [re-checked](#where-it-is-called-and-how-it-is-executed) as the share's are, since each retires statements for no page write;
3. part of a victim too big to take whole — the survivors of as many of its statements as fit, a whole statement's at a time, so that nothing is cut — which frees no page yet but leaves the victim sparser, and cheaper to take later.

So the last page closes short only when nothing at all fits.
Leaving it short on purpose, as a ready victim for the next flush, would not work anyway, since the age term would keep a page that young from being chosen.

**Victims are pulled in while packing, not chosen before it and fitted afterwards.**
So there is no estimate of the page count to get wrong: a page closes when the flush's own content no longer fits it, the room it has left at that moment is known exactly, and whatever fills that room is chosen to fit it.
Nor can a victim force a page open: step 2 moves only what fits the room, and a page that exists only for consolidation's sake is opened by [the budget loop](#the-budget-loop), and only when its offer fills it.
Slack in every page but the last is then at most `θ` of it, and usually far less, since step 2 fills the room with whatever victims fit before step 3 would cut; id order survives up to a reordering within the look-ahead.

#### One dirty set, and why statements are derived last

**The cut states exactly the dirty set, and derives the statements that do so only once nothing will change the fragment map again**: only the final map says which statements are needed, and [delta encoding](../spec/address-table.md#delta-encoding) makes each one's length a property of its predecessor, so lengths cannot be known before the statements are.

```rust
/// Everything the flush in progress has taken, and so must state.
struct Dirty {
    ranges: IntervalSet<Key>,      // merged on insert: overlapping or touching ranges fuse
    records: HashMap<Id, Record>,  // per touched id, what the fragment map cannot say
}
```

Its ranges come from every source:

1. **the header's**, [taken whole](address-table-operations.md#the-header) at the start of every flush — the header is rewritten every flush, so its content is this flush's to state as much as any new write;
2. the fold's, description defragmentation's, and every relocated survivor's;
3. the page rewrite's table victims', and the rotating window's.

Its records say how each touched id's size and existence changed, and whether a page the flush retires held its size statement or tombstone; the cut turns them into `Size` and `Tombstone` statements, or into the sizing bit of a content statement, by [the rules the mutation functions give](address-table-operations.md#what-the-cut-states-for-a-touched-id).

**Deriving is one pass over the merged ranges**, and it merges across sources for free:

```rust
/// Every statement this flush writes, from the fragment map as it finally stands.
/// Each pending fragment falls in exactly one run, so it is stated exactly once.
fn derive(dirty: &Dirty) -> Vec<Statement> {
    let mut out = Vec::new();
    for id in dirty.ids_in_key_order() {
        // One statement per maximal run of pending fragments that state alike: zeros
        // with zeros, bytes with bytes that sit contiguously in one data page, and
        // inline bytes with inline bytes up to the `Inline` threshold.
        for run in pending_runs(id, dirty.ranges.of(id)) { out.push(run.statement()); }
        // What the fragment map cannot say, merged in at its own key: a `Size` where
        // no content statement ends at the size, or a `Tombstone`. Which content
        // statement is sizing is decided once the fillers are known too.
        out.insert_sorted(size_statement(id, &dirty.records[id], &out));
    }
    out
}
```

Two fragments from different sources — the flush's own write and a survivor that `pack` placed right after it, say — become one `Ref` when they are adjacent both in the allocation and in the page.
Inline fragments need no such luck, since the cut copies every inline payload into its new table page anyway: adjacent ones merge into one `Inline` whatever their origin.

**The cut then lays the statements out in three steps, and every page but one comes out full:**

1. **The header first, with the hottest statements it can hold.**
   When every statement fits, every statement goes there and no leaf is written, which is the steady state of every small file.
2. **The rest into leaves, in key order**, each leaf closed when the next statement does not fit, so that every leaf but the last is full to within one statement.
3. **The one page that still has room — the last leaf, or the header when there is no leaf — takes the fillers**: whole table victims that fit, then [the rotating window](#the-rotating-window), which can stop at any byte and so fills the room exactly.

A leaf therefore exists only when the header is full, so the last leaf's content could never have fit in the header instead, and no layout needs fewer pages.
The one page that can end up more than a statement short — well under `θ` — is the one the fillers could not fill because nothing was left to take.

**The header keeps the hottest statements, not the tail of the key order.**
The header is [the write buffer](flush.md#the-header-as-write-buffer): a statement that stays in it is rewritten for free every flush, so an allocation the application keeps touching never leaves a statement to be shadowed in a leaf.
Filling the header from the end of the sorted statements would evict a hot statement whenever it happened to sort early, and each of its later updates would then leave garbage in a leaf.
Key order is right for the rest: the statements the header does not keep are cut into leaves in key order, which is what keeps leaves dense.
Evicting the coldest *key-contiguous run*, rather than the coldest statements wherever they sit, gets both at once — heat decides that a run goes, and key order decides which.

**Fillers cost only what they newly take.**
Both take more ranges into the dirty set, and the cut derives what they add; a range that was dirty already adds nothing.

**Subadditivity of the encoded length is what makes fitting them cheap.**
`|encode(A ∪ B)| ≤ |encode(A)| + |encode(B)|` for any two sets of statements that may share an epoch, because merging only ever brings each element's predecessor closer and a varint's length is monotone in the value it encodes.
The one condition is that the union be conflict-free — [each fragment stated once](#each-fragment-is-stated-once-per-epoch) — since two statements claiming the same byte would need the offset cursor to step backwards, which the encoding cannot express.
So a filler's length, measured on its own, is a safe upper bound on what it adds: filling to that bound under-fills rather than overflows, and a second pass over the merged statements recovers the difference.
(Subadditive is the word; convexity is a property of functions on a vector space, which a function of sets is not.)

Three details of the bookkeeping:

- Lengths are computed *with page boundaries*: each leaf's first statement pays its full-width id and offset, since both cursors reset at a page boundary; a leaf spends one delimiter byte on its empty child list; and a statement that does not fit starts the next leaf.
- The header's capacity is what remains after the file-header fields and its child list, and the child list depends on how many leaves this flush adds and unlinks, so step 1 works against an estimate and is corrected once: if the list turns out longer, the header gives up its coldest statements to the leaves.
- A child list that no longer fits the header at all is not a case to optimize, but [the depth change](flush.md#the-shape-of-the-tree) the tree already provides: the whole list moves into one fresh interior page, which the header references instead.

**Binding comes last.**
Only when every statement's page is known does the cut [bind](address-table-operations.md#bindstmt-page-run) each one: a slab slot, its fragments — each pending fragment turning into the `Bytes` or `Zero` fragment that names it — its pins, and its framing, charged to its page with an inline payload per byte.
A statement that states the size becomes its id's size statement then.

#### Each fragment is stated once per epoch

**Everything a flush writes shares one epoch, so no two of its statements may claim the same byte — and deriving the statements from the final fragment map is what guarantees it.**
Each pending fragment falls in exactly one run and each run becomes exactly one statement, whatever order the sources took their ranges in and however those ranges overlapped.

Were each source to create its statements as it went instead, a later source taking fragments from a statement an earlier one had already created would leave both claiming the same bytes, and a flush that assembles its output from several sources would do so in many ways:

- evacuation moving a survivor that the same flush overwrites, or that the header is carrying forward;
- the rotating window passing over a fragment that some other source had already restated;
- a page rewrite restating a `Ref` whose data page a later evacuation moves;
- a description defragmentation rewrite taking bytes from a statement the fold had just created.

Each would need a rule of its own, and the page-rewrite case would need the earlier statement rewritten after the fact.
With statements derived at the cut, a later source just takes the fragments, and the earlier source's claim on them is simply gone.

**Chunks, by contrast, are produced eagerly**, when a source takes their fragments, **and `pack` fixes where their bytes go.**
Both are safe because no source ever takes over bytes that another source of the same flush is writing, and nothing after `pack` moves a byte it placed:

- evacuation takes only fragments pointing into pages older than this flush, and a flush's own pages can never be its victims;
- page rewrites, the header, and the rotating window take fragments in place and never move bytes at all;
- description defragmentation's rewrites are chosen before `pack`, and [its age filter](#where-it-is-called-and-how-it-is-executed) keeps them off anything the flush is writing.

The one thing after `pack` that reads the layout is the derivation, which is exactly where it should: it is what merges adjacent chunks that `pack` placed contiguously into one `Ref`.

Two things follow.
Coverage *releases* happen when a source takes a range, so victim selection sees the state the flush will leave with no provisional bookkeeping of its own; only the *charges* wait for locations — [at placement for bytes, and at the cut for framing](liveness.md#coverage).
And the fragment map now changes before the commit, so a flush that fails must unwind or [poison the session](../spec/durability.md#headroom), which the reference implementation's poisoning already covers.

## The budget

The design has one throttle — a per-flush **maintenance budget** in pages beyond the flush's own content — and one queue of offers competing for it, ranked by `bytes reclaimed / bytes written`.
One further parameter, [the churn floor `λ`](#the-churn-floor-is-a-parameter-not-an-identity), says when an offer is not worth taking at all.

Two sources compete for that budget, and they are one mechanism applied to two kinds of page:

1. **Evacuation** relocates a data victim's survivors, and reclaims dead data bytes.
2. **The page rewrite** restates a table victim's live content, and reclaims dead statement bytes.

The other two mechanisms stay out of the ranking, each for a reason of its own.
**The rotating window** takes only the room the victims leave, because what it buys — density — is worth too little per byte to compete.
**Description defragmentation** pays down a different debt: it reclaims almost no space, and what it retires is description, measured in statements and fragment-map memory rather than in file bytes, so it draws on [a reserved share](#where-it-is-called-and-how-it-is-executed) instead of competing.

Description defragmentation is worth naming as a mechanism of its own even so, because it is the only one that pays down [description fragmentation](#the-high-level-picture) and the only one that shrinks the in-memory fragment map.
An allocation stippled with `[data] [zeros] [data] …` patches costs statements forever until someone rewrites the region as one `Ref` — and the [`Zero` usage note](../spec/address-table.md#statement-types) explicitly permits rewriting such a stipple as one `Ref` over a range whose gaps the consolidator fills with actual zeros, which is what makes the rewrite legal.
Those bytes are being written anyway, so zeroing them costs nothing beyond the `memset`.

### The structures behind the ranking

Every source that starts from a *page* is ranked from **bucket queues**, which is the structure that makes "give me a good victim" `O(1)` instead of `O(log P)`.

```rust
struct Buckets {
    lists: [Vec<PageNumber>; B],    // B is small; 16 is plenty
    at: HashMap<PageNumber, (u8, u32)>,   // which bucket, and which slot in it
}
```

A page's bucket is `coverage[p] * B / capacity`, so moving a page is: swap-remove it from its old list, push it onto the new one, and fix the `at` entry of whatever the swap moved.
Three array writes and two map updates, no comparisons, no rebalancing — which matters because coverage changes on **every** fragment created or destroyed, not once per flush.

**Each page kind gets its own instance.**
One shared instance would cost the `O(1)` the structure exists for: data pages outnumber table pages by roughly the ratio of their contents, so `lowest_non_empty()` on a mixed structure would keep handing back data pages and the table side would scan past them.
Both can use the same `B`, since the page rewrite is the table side's primary mechanism and wants the same resolution evacuation does.
Header pages belong in neither: a header is rewritten every flush, so it is never a victim, and its content is accounted for by being [taken and stated again by every flush](#one-dirty-set-and-why-statements-are-derived-last) rather than by its coverage.

The rotating window needs no bucket structure at all, being driven by its cursor, and neither does the defragmentation it discovers.

**Why bucketing is enough.**
A bucket queue answers "roughly the sparsest" rather than "the sparsest", and the ranking is a heuristic whose inputs are estimates anyway.
What it must not do is *miss* a good victim, and it cannot: a page in the sparsest non-empty bucket is within `1/B` of the true minimum live fraction.

### The budget loop

A flush and its consolidation run as one straight line:

```rust
fn flush(journal: Journal, budget: Budget) {
    // 1. Take: the header's content whole, then everything the fold changed. Every
    //    source from here on takes fragments and marks their range dirty, and none
    //    states anything; coverage falls as they take, so everything below sees the
    //    state this flush will leave. Description defragmentation's share is spent
    //    here as well: the best candidates earlier walks found, re-checked against
    //    the state the fold left, become chunks of the flush's own.
    let mut dirty = Dirty::new();
    take_header(&mut dirty);
    let fold = fold(journal, &mut dirty);
    let rewrites = defrag.choose(budget.defrag_share, &mut dirty);

    // 2. Data pages: the flush's own chunks in key order, with victims riding along
    //    wherever a page has room its own content cannot use. See `pack`.
    let mut data = pack(merge_by_key(fold.chunks, rewrites), &mut elastic);

    // 3. Budgeted pages, one at a time, to whichever page kind reclaims more per
    //    byte — and only a page the offer fills, since a page opened for a sliver
    //    would cost a whole page write for next to nothing.
    for _ in 0..budget.pages {
        let Some(offer) = [evacuation.offer_page(), page_rewrite.offer_page()]
                              .into_iter().flatten()
                              .max_by_key(|o| o.reclaimed / o.written) else { break };
        if !offer.holds_tail() {        // compaction mode's tail passes both floors
            if offer.reclaimed < budget.lambda * offer.written { break; }   // the churn floor
            if offer.fill() < 1.0 - THETA { break; }
        }
        offer.take(&mut data, &mut dirty);
    }

    // 4. Address-table pages: derive the statements from the dirty set and cut them.
    //    The hottest stay in the header, the rest go to leaves in key order, and the
    //    last page's room goes to table victims that fit and then to the rotating
    //    window, whose walk also finds the next description defragmentation
    //    candidates. Binding comes last.
    let table = cut(dirty, &mut page_rewrite, &mut rotating_window);

    // 5. Unlink emptied pages, write, fsync, commit; then truncate a reusable tail.
    #[cfg(debug_assertions)] fill_observer.record(&data, &table);
    commit(data, table);
}
```

**The two page kinds are combined, not traded off — but they do compete.**
Their victims fill one plan, and what they compete for is the one budget, a page at a time; each source's offers decline as it takes more, so one greedy pass over the two is the right amount of machinery.

**Free filling happens inside steps 2 and 4, and step 3 is the only place a page is opened for consolidation's sake.**
It opens one only when the offer fills it to `1 − θ`, which is what keeps a victim too many from costing a nearly empty page; only an offer holding [compaction mode's tail](#compaction-mode) is exempt.
An offer is what the page would hold: whole victims first, by best fit, and then — if that leaves more than `θ` empty — one more victim with a survivor cut at the page boundary, exactly as `pack` cuts the flush's own chunk.
A cut's tail leads the next page, which is therefore opened whatever its own offer, so the loop cuts nothing on its last page, and every chain of cuts it starts ends within the budget.
The flush's data pages form one such chain: the last page `pack` returns may cut a victim's survivor too whenever the loop goes on to open another, so only the flush's very last data page can close short.
Description defragmentation's reserved share is spent in step 1, before anything is packed, so its rewrites are packed in step 2 exactly as the flush's own chunks are — on a flush whose only data they are, too.

**Debug builds observe how full the pages come out.**

```rust
#[cfg(debug_assertions)]
#[derive(Default)]
struct FillObserver {
    header: Fill,        // the header pages
    table:  Fill,        // every other address-table page: leaves and interior pages
    data:   Fill,        // data pages
}

#[cfg(debug_assertions)]
#[derive(Default)]
struct Fill {
    pages:   u64,        // written this session
    content: u64,        // their `content_size`s summed; average fill is content / pages
    lowest:  u16,        // the lowest `content_size` written by the latest flush
}
```

It measures fill *at write time* — the packer's quality — which is a different number from the file's live fraction, the consolidator's quality, which the budget throttle reads.
`record` also asserts what packing promises: every data page of a flush but its last at least `1 − θ` full, and every leaf but the last full to within the length of the statement that did not fit.
The header's fill is observed but not asserted, since a small file's header is legitimately almost empty.

#### The churn floor is a parameter, not an identity

The floor is a parameter `λ` — space freed per byte written — and its price is explicit.
Cleaning pages at live fraction `u` costs `u / (1 − u)` bytes written per byte of space freed, so a total write amplification of `1 / (1 − u)`: 2× at `u = 1/2`, 5× at `0.8`, 10× at `0.9`.

**But `λ` is the wrong knob for a target file fill.**
An average fill of 80–90 % does not mean cleaning pages that are 80–90 % live, and implementing it that way would buy the last few percent at five to ten times the write traffic.
It means the distribution of live fractions is bimodal enough that most pages sit near full while the garbage concentrates in a few nearly empty ones — which is what [the age term](#scoring-a-data-page) is for, and the whole reason LFS's cost-benefit cleaner beats plain greedy.

The two knobs therefore do different jobs, and only one of them is a floor:

- **`λ` is a churn floor.** It stops the cleaner grinding when there is nothing worth cleaning, and it belongs *low* — near 1, i.e. near `u = 1/2` — precisely so that it is not what decides the file's fill.
- **The per-flush budget is the throttle**, and it is what converges the file on a target fill `τ`: measure `live_bytes / (pages · C)` after each commit and move the budget up while it sits below `τ`, down while above; the [consolidator state](consolidator-state.md#the-budget) carries the budget it has reached from one session to the next.

That also sharpens [the bound this buys](#the-bound-this-buys): `live_size / τ` is reached by spending budget, and the floor's only job is to stop spending it on pages that cannot repay.

**The known failure mode of the single ranked queue.**
Ratio-greedy ranking starves **costs that current ratios cannot see**, and one such cost remains in this design: description debt on a cold allocation — one that will be read forever and never written again — never looks urgent, because nothing about it is changing.
It is taken out of the ranking rather than weighted inside it: [the rotating walk](#description-defragmentation-rides-the-rotating-window) discovers it, and a reserved share of the budget pays it.
A table page whose live statements are scattered across the id space is not a second such cost, because the page rewrite selects by sparsity alone, to which scattering is invisible.

**That is not a verdict against age terms.**
Age can do two different jobs, and only one of them is a starvation guard.
In [data-page scoring](#scoring-a-data-page) it is a *prediction*: a page written recently will likely get sparser by itself, so cleaning it now wastes the writes that relocate survivors about to die.
That job is exactly right there, and the same prediction applies to defragmentation — a range patched recently will likely be patched again, which would waste the rewrite — which is why [defragmentation candidates are scored with age](#where-it-is-called-and-how-it-is-executed).
What age would be wrong for is the other job, guaranteeing that a cost the ratio never sees is eventually paid: a weight large enough to force that is a tuning parameter standing in for a rotation, which guarantees it outright.

### The complexity this incurs

Strictly logarithmic work *per flush* is unattainable, because a flush must at least record what it changed.
The attainable form, and what this design achieves:

- `O((k + s) · log F)` in-memory work for a flush dirtying `k` ranges and rewriting `s` statements under the budget;
- `O(1)` victim selection per candidate;
- page writes bounded by `dirty data pages + c + depth` for budget `c`;
- with the common small flush writing **no** address-table page beyond the header.

## Two forms of consolidation

Consolidation runs inside every flush in two forms, which differ in [the price of the sink](#victims-are-pulled-one-at-a-time) and, following from that, in whether they are traded off against anything at all:

- **Free filling.** Victims, description defragmentation's remaining candidates, and parts of victims too big to take whole ride in the room of pages that are being written anyway — the flush's own data pages, and the header or the last leaf — so they cost no page write and compete with nothing.
  It happens inside [packing](#packing-in-id-order-with-look-ahead) and inside [the cut](#one-dirty-set-and-why-statements-are-derived-last), and it runs to exhaustion.
- **Budgeted pages.** Beyond that, pages that exist only to reclaim, offered one at a time and opened only when the offer fills them.
  These compete, under [the budget and the churn floor](#the-budget-loop).

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

### Victims are pulled one at a time

**A victim is taken only when all of its survivors have somewhere to go in this flush.**
A victim is all-or-nothing, since relocating only some of a page's survivors reclaims nothing, so the victim is the unit of choice and the room is the only constraint.
In free filling that means the page's room holds all of them; on a budgeted page, one survivor may be [cut at the boundary](#slack-is-cheaper-than-a-split--up-to-a-point), its tail leading the next page — never on the budget's last page, where the tail would have nowhere to go.
Free filling makes one exception once nothing whole fits: [moving part of a victim](#packing-in-id-order-with-look-ahead) reclaims nothing yet, but in a page that is written anyway it costs nothing either, and it leaves the victim sparser.

A marginal victim frees `(1 − u_v) · C` bytes and writes `u_v · C`.
The `1 − u_v` is not the victim's empty fraction claimed as a gain; that space is unusable already and would be released regardless.
It is what remains after paying for the survivors: all `C` of the victim's bytes come back, and `u_v · C` of them are spent again at once, because the survivors take up as much space in their new page as they did in the victim.
The two-epoch quarantine delays the release and the consumption alike, so it changes the timing and nothing else.

**Sparsest first is exactly right**, since the marginal ratio `(1 − u_v) / u_v` falls as `u_v` rises, and the churn floor then reads `u_v ≤ 1 / (1 + λ)`.

**A free sink changes the arithmetic, not the order.**
A survivor landing in room that a page being written anyway would otherwise waste consumes nothing, so the victim's net gain is the whole `C` and its ratio `1 / u_v`, above 1 for every `u_v`: free filling takes any victim that fits, and the churn floor does not apply to it.
That is the whole of the distinction between [the two forms](#two-forms-of-consolidation).

```rust
impl Evacuation {
    /// The victim whose survivors fit `room` in page `filling`, evacuated, or None: in
    /// compaction mode the tail if it is a data page above `filling` and fits, otherwise
    /// the best-scoring sampled victim that fits. A victim that would overshoot is passed
    /// over for one that fits, never cut.
    fn take_fitting(&mut self, room: u32, filling: PageNumber) -> Option<Survivors> {
        // `tail()` is the highest non-journal live page, and None outside compaction mode.
        let tail = compaction.tail().filter(|&p| is_data(p) && p > filling);
        if let Some(top) = tail.filter(|&p| coverage[p] <= room) {
            return Some(self.evacuate(top));
        }
        let bucket = self.buckets.lowest_non_empty()?;
        let v = bucket.sample(K).filter(|p| coverage[p] <= room).max_by_key(|p| score(p))?;
        Some(self.evacuate(v))       // its survivors taken; see "Each fragment is stated once"
    }

    /// What one budgeted page would hold and reclaim, without taking anything: whole
    /// victims by best fit — the tail first, in compaction mode, whatever its fill — the
    /// churn floor applied to each but the tail, then, unless this is the budget's last
    /// page, one more victim cut at the boundary if the page would otherwise close more
    /// than θ empty.
    fn offer_page(&self) -> Option<Offer> { … }
}
```

A caveat on the arithmetic rather than on the policy: `coverage` counts [aliased bytes once per claimant](liveness.md#coverage), so `u_v` over-states both what an evacuation writes and what it reclaims, and the ratio above is a lower bound wherever aliasing is present.

## Victim selection for address-table pages

**Table pages are drained by the page rewrite, victim by victim, as data pages are evacuated**, and the rotating window restates a key range for density's and defragmentation's sake in whatever room is left; [why the work divides that way](#why-address-table-pages-have-two-mechanisms) is above.

### The page rewrite

Victims come from the address-table buckets — sparsest first, `K` sampled and scored as [data pages are](#scoring-a-data-page), or the tail first [in compaction mode](#compaction-mode) — and a victim is taken only where all of its restatements fit: in the room of the cut's last page, which costs nothing, or on a budgeted leaf.
Compaction mode's tail is the exception: it is taken whatever its size, and restatements that do not fit one leaf spill into the cut's next.
Executing one is [`rewrite_page`](address-table-operations.md#rewrite_pagevictim-dirty): decode the victim, take the fragments its live statements still own so that the cut states them again, and record any size statement or tombstone that lived there for the cut to state again.

Its cost is those restatements' encoded bytes, estimated from the victim's coverage while it is only being offered, and known exactly once the cut has derived them — possibly less, since they merge with whatever else the flush states nearby; its benefit is the whole victim.
So it is priced exactly as evacuation is, `(C − written) / written`, and the two compete in [the budget loop](#the-budget-loop) on equal terms.
[Coverage charges an inline payload per byte to the table page that carries it](liveness.md#coverage), so the estimate includes payloads without special care — which matters, since a page of 64-byte inlines is ~90 % payload and would look ten times sparser than it is if only framing counted.

A victim with children — an interior page — is rewritten the same way, and its `child_ref`s move with it; see [unlinking](#unlinking-an-emptied-page).

### The rotating window

```rust
/// One step of the rotation. Takes live fragments from the cursor on until `room`
/// bytes of encoding are used, walks on for description defragmentation's sake until
/// `WALK` fragments have been seen, and returns the candidates found.
fn rotating_window(room: u32, dirty: &mut Dirty) -> Vec<DefragCandidate> {
    let start = cursor;
    let mut stop = None;                           // where the taken range ends
    let mut encoded = 0;
    let mut defrag = KadanePerId::new();           // resets at every id boundary
    for (key, fragment) in fragments.range_wrapping(start).take(WALK) {
        defrag.push(key, fragment);                // weights as in `defrag_candidates`
        cursor = key.next();
        if stop.is_some() { continue; }
        // A pending fragment is stated already: it adds nothing to what the
        // window costs.
        if fragment.statement().is_none() { continue; }
        encoded += re_encoded_size(fragment);      // against the previous restatement
        if encoded > room { stop = Some(key); }
    }
    rewrite_key_range(start, stop.unwrap_or(cursor), dirty);
    defrag.finish()
}
```

**`room` is an argument because the room is whatever the cut leaves**, which varies from flush to flush; and the function returns what its walk found rather than a score, since nothing chooses between windows — there is only the one.
The walk meets fragments the flush has already taken, whose statements the cut has not bound yet, so it weighs those by estimate; that is harmless, since a candidate's gain and age are [recomputed when it is chosen](#where-it-is-called-and-how-it-is-executed).

**The cursor is a fragment-map key**, `(Id, AllocationOffset)`, so a window is a plain range of the structure it walks, with no special case at either end.
An allocation whose live statements alone outgrow the room is simply resumed mid-allocation by the next window, where a cursor keyed by id alone would never get past it.
Size statements need no position in the window: [`rewrite_key_range`](address-table-operations.md#rewrite_key_rangelo-hi-dirty) states an id's size again only when its size statement's page is one the window restates from, which is a question about pages rather than about how much of the id the window covered.

**A new session resumes the rotation where the last one left it, or else somewhere new.**
The cursor lives in memory, so the [consolidator state](consolidator-state.md#the-rotating-windows-position) carries it from one session to the next.
Without a usable state, a session that started it at the beginning of the key space every time would, over many short sessions — a command-line tool that a script invokes over and over, say — only ever reach the low ids.
So open then seeds it at the first statement of one table page, chosen by the governing header's CRC modulo the number of table pages: deterministic for a given file, so tests reproduce, and spread evenly over pages across sessions, which is spread evenly over statements to within how full the pages are.

### Unlinking an emptied page

**Nothing waits for a load.**
With framing, inline payloads, and child references all charged per byte, a table page whose last live content is restated elsewhere reaches zero coverage in the same flush, and [the two-generation quarantine](../spec/durability.md#the-reuse-rule) does the rest: fallback at the next commit, writable one commit after that.
Its dead bytes then sit in it until it is overwritten, which is a statement about the bytes and not about the page's availability.

One step stands between zero coverage and reuse, and it is structural rather than an accounting overhang: **an emptied table page is still named by its parent**, so it stays reachable from the header, and [I2](../spec/durability.md#the-two-invariants) forbids overwriting it until a flush has also unlinked it.
When the parent is the header, the flush that emptied the page unlinks it for free, since the header is written last.
But when the cut's own fillers empty a leaf `B` whose parent is an interior page `I`, unlinking `B` would take a fresh copy of `I`, and a new child reference in a header the cut may already have filled to the byte, so `B` stays linked — superseded, only not yet reusable — until the next flush takes it as a free victim before its cut.

The reference implementation writes shallow trees — the header is the only interior page until a file has accrued [on the order of a thousand leaves](flush.md#the-shape-of-the-tree) — but it must not rely on that, since another writer may shape the tree differently.
So:

- **The page table records each table page's parent**, four bytes a page, which [the tree property](../spec/address-table.md#physical-format) makes a single page number.
- **Unlinking rewrites the parent** without the child's reference.
  At depth 1 the parent is the header, rewritten anyway; deeper, the parent is rewritten like any other table page, its other children carried over, and so is *its* parent, up to the header — the `depth − 1` path copy of a [structural update](flush.md#the-shape-of-the-tree).
- **A child reference is charged to its parent's coverage**, and released when the child is unlinked.
  Without that charge, a parent whose statements had all moved elsewhere would reach zero coverage while still holding the only reference to live children, and reusing it would orphan them — silently, since nothing would reach their statements at the next load.
- **A page rewritten while it has children carries them along**: into the header if its child list has room, which also flattens the tree, and otherwise into the page's replacement, with each moved child's parent entry updated.

Data pages need none of this, having no parent.

The one correctness obligation on any rewrite is [stating the size again](address-table-operations.md#stating-the-size-again--the-one-correctness-obligation).

## Finding the referrers of a data page

**This is an unresolved gap**, and it is worth stating plainly rather than leaving implicit.

Rewriting an address-table page is self-describing — decode the victim, and its statements name the `(id, offset)` keys to look up — which is what lets [the page rewrite](#the-page-rewrite) drain table pages directly, and why address-table pages need nothing of what follows.
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
- **Make data-page consolidation allocation-driven**, the way defragmentation already is: start from an id whose bytes are spread over sparse pages rather than from the page.
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

## Outer fragmentation

**Placement decides whether holes form, truncation returns what reaches the end, and a compaction mode moves what never would.**

### Lowest page first

**Reusable pages are handed out lowest first rather than in the order they became reusable, and the file grows only when none is left.**
Lowest first keeps new content away from the end of the file, so pages that die there stay dead and the tail drains of its own accord; a first-in-first-out pool would refill the tail as readily as any hole.

The cost is small.
The pool is an ordered set — a `BTreeSet<PageNumber>`, or a bitmap with a cursor on its lowest set bit — rather than a queue, and [the quarantine rotation](flush.md#where-the-pages-come-from) is unchanged.
It is an ordered set rather than a min-heap because it is used from both ends: allocation takes the lowest page, and truncation removes the highest ones, which a min-heap could only discard lazily, once they surfaced as its minimum.
Lowest first does give up the contiguity that a run from the end of the file would offer a large write, but contiguity buys nothing: a `Ref`'s payload is [bounded by one page's content](../spec/address-table.md#bounds), so no `Ref` spans pages, and adjacent pages need exactly as many statements as scattered ones.
Journal pages come from the same pool, so the journal, too, starts in the lowest hole rather than at the end.

### Truncation

After a commit, the file may shrink to end right after its highest live or fallback page — the [truncation rule](../spec/durability.md#the-truncation-rule).
It is safe for the same reason reuse is: no header that recovery could choose reaches past that point, and a power cut that loses the truncation only leaves the file longer than necessary.
The journal's last page does not count while it is empty, since a reader treats [a journal page past the end of the file](../spec/file-format.md#the-journal-pointer) as empty too.
The new end is always a page boundary, never inside the last page's padding: the file system would rewrite the block holding that page's end — zeroing the rest of it, and on some file systems again when the file grows — and a power cut can corrupt a block while it is being written.

Truncating is a cheap metadata operation, but shrinking and regrowing the file on every flush of a workload that oscillates is not, so the reference implementation truncates only once the reusable tail exceeds a threshold, and at close and open.
At close, only fallback pages can keep the file from ending at its last page of content; once [the `fsync` at open](../spec/durability.md#recovery--loading-a-kladde-file) has retired the older world, they go too.

### Compaction mode

Placement and truncation return the tail only if the pages there die by themselves, and cold, full pages never do.
So while **holes** — pages below the highest live one that are reusable now, not in [quarantine](flush.md#where-the-pages-come-from) — exceed a share `h` of the file, the highest non-journal live page is offered to [the budget loop](#the-budget-loop) ahead of every other victim, whatever its fill: evacuated if it is a data page, rewritten if it is a table page, with lowest-first placement landing its content in holes.

**The mode needs no hysteresis**, because switching in and out of it costs nothing.
A tail page once moved stays moved, so leaving the mode loses no progress; and near `h`, where compaction lowers the share of holes and ordinary cleaning raises it again — every sparse page it frees below the tail is a new hole — a mode that alternates flush by flush merely splits the budget between the two debts.

**An offer holding the tail passes the churn floor and the fill floor whatever else it holds, and a table tail is taken even when its restatements need more than one leaf, so it is the budget alone that paces the mode.**
The floors weigh what a page returns to the pool, and a tail victim returns more: a whole page to the file system.
For a data page the exemption waives nothing the churn floor would catch, since moving it at fill `u` writes `u · C`, a ratio of `1 / u` that passes a floor near 1 even for a full page; the fill floor it can fail, when nothing else fills the page it moves into.
A table page's restatements can take more than its coverage, since fragments that shadowing split need a statement each and restated statements lose their neighbours' delta encoding, so a nearly full table page fails the churn floor as well, and may not fit one budgeted leaf.
A mass free makes one the tail as a matter of course: the flush that states the frees, and the one after it, cannot reuse the pages the frees release yet, so their leaves — the tombstones, and whatever they state `Inline` — extend the file.
Were the tail held to the floors, the mode would stop at the first such page, and the budget loop with it, until the rotating window had drained the page a few statements a flush.

The return is realised only once every page above the victim has gone too, which is why the mode works strictly from the top down; journal pages are skipped, since the next flush moves the journal to the lowest hole by itself.

**The tail moves only while a page that is reusable now lies below it.**
Right after a mass free, the pages it released sit in quarantine for two commits, so the lowest reusable page can lie above the tail, or there can be none; content moved then lands in pages the file grows by, which become the next tail, and the same content moves again.
Holes count only reusable pages for a related reason: the pages the last commits released are working space, which the next flushes reuse by themselves, and counting them would keep a file whose flushes rewrite much of it in the mode for good.

**Free filling takes part in the mode too**, since a tail victim riding in a page the flush writes anyway costs nothing at all: while the mode lasts, each source's [`take_fitting`](#victims-are-pulled-one-at-a-time) offers the highest non-journal live page ahead of the sparsest, whenever that page is of its kind, lies above the page being filled, and its content fits the room.
The tail wins that comparison because holes are what the file has too many of: freeing one more page for reuse gains little, while every tail page moved brings a truncation closer.

Finding the highest live page needs no structure of its own: a cached index, lowered past reusable pages as the tail empties and raised when a page above it is written, passes each page once per change of state, which is `O(1)` amortised.

## Pacing

Each flush consumes at least one reusable page, often a few, so consolidation must reclaim at least that many on average.

The situation where no good victim exists is the situation where pages are largely full — i.e. there is little garbage and the file is close to its live size — so "cleaning cannot keep up" and "the file is honestly this large" coincide, which is the benign coincidence.

The one obligation this leaves is [headroom](../spec/durability.md#headroom), which is a requirement rather than a policy.

## The bound this buys

Garbage accrues in proportion to write traffic and drains in proportion to consolidation effort, so the steady state is set by the budget: enforcing "consolidate until live fraction ≥ τ" bounds the file at `live_size / τ` for a chosen τ, at the price of the corresponding cleaning work.
The bound is controllable and, in relative terms, independent of file size.

## Constants still to be chosen

- The live-fraction target τ, and the feedback rule that moves [the budget](#the-churn-floor-is-a-parameter-not-an-identity) toward it.
- The churn floor λ, near 1 and not the knob that sets τ.
- Defragmentation's price `μ` per byte written, and its reserved share of the budget.
- The per-flush page budget's starting value and its bounds.
- The age weighting, in data-page scoring and in defragmentation's ranking.
- The look-ahead depth `L` and the fill floor `θ` in [packing](#packing-in-id-order-with-look-ahead) — the first sets how much slack survives in the common case, the second bounds it in every case.
- The rotating walk's length `WALK`, in fragments per flush.
- The bucket count, 16 for each page kind.
- The hole share `h` above which [compaction mode](#compaction-mode) runs, and the reusable tail that triggers truncation.
- The page size, 4 KiB versus 16 KiB, to be measured on macOS/iOS and Linux — a per-file header property either way.
