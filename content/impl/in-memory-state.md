---
title: In-memory state
---

The structures a loaded kladde file is held in, what each is for, and why each is the shape it is.

**The store keeps a span table, a rope, the page table, a reverse index, and a span id allocator.**
Together they take about 32 bytes per span, besides the resident table pages, and none of them grows with the number of values the application holds: positions of values are kept by the values' containers, [Values and their positions](values.md) says how.

Integer widths below follow from the [bounds the specification states](../spec/span-table.md#bounds), and are given because they are the same in any language.

## 1. The span table

An array indexed by span id, one record per span a chain reaches:

```rust
struct Span {
    content: Content,     // what the span shows
    len: u32,             // its length in bytes, never 0
    next: u32,            // the span after it in its chain, or 0
    entry_page: u32,      // the table page that holds its live entry
}

enum Content {
    Ref { page: u32, offset: u16 },   // bytes of a data page
    Inline { offset: u16 },           // its payload, inside its entry in `entry_page`
    Zero,                             // zeros
}
```

That is about 16 bytes per span.
**An array, because span ids are dense integers** that the [allocator](#5-the-span-id-allocator) hands out lowest first, so every lookup is an index rather than a hash.
An id no chain reaches has an empty record, and is free.

**The in-memory span table is the on-file span table resolved**: it holds, for every reached span, what its newest entry says, and nothing about older entries.
Content is never copied out of the pages while they are resident: a `Ref` names its bytes in a data page, and an `Inline` names its payload inside its own entry, which lies in a resident table page.

## 2. The rope

**The rope is a counted balanced tree over the sequence's spans in order**, each item a span id and its length, each node the total length below it.
It finds the span that holds a position, splits one, and cuts and inserts runs of spans, each in `O(log k)` for `k` spans, which is what the flush needs to map the journal's positions to spans, and what the decode follows to read the sequence in order.

A counted B-tree with leaves of 32 entries is the recommended shape: about 11 bytes per span in its leaves at a typical fill, a few percent more in interior nodes, and 4 bytes per span in the span table for the leaf that holds it, so about 15 bytes in all; and a lookup descends three levels for any realistic file, each a scan of a node of one or two cache lines.
An implicit treap, a randomized binary tree whose nodes know their parent and the total length below them, does every operation with two primitives, a split at a position and a join of two trees, and those are exactly what [the fold](flush.md#the-fold) needs as well, so one arena type can serve the rope, the fold's piece table and a transaction's cuts.
It costs a node of about 40 bytes per span, and `O(log k)` expected steps, about `2 ln k − 3` levels on average, each likely a cache miss; that shows only once memory per span matters, 640 MB against the B-tree's 240 for 16 million spans.

The schema's and the private state's chains are short and written only by flushes, so a list of span ids serves them.

## 3. The page table

For every page: its state, and for a live page its kind, epoch, and live-byte counter (**coverage**).

**Coverage counts what the page holds that a reached span relies on**: for a data page, the bytes reached spans show in it; for a table page, the bytes of the live entries in it, inline payloads included, and of its child references.
It is a 32-bit counter rather than a page-sized one, because a page's live bytes exceed its capacity when several spans show the same bytes, which is legal and [bounded](../spec/span-table.md#bounds) precisely so that 32 bits suffice.
A page whose coverage reaches zero is reusable once the next commits have passed it through the [reuse rule's](../spec/durability.md#the-reuse-rule) quarantine — a table page only once it is also unlinked from its parent ([unlinking](consolidation.md#unlinking-an-emptied-page)).

**A page's record holds what its state has, and nothing else**: an enum over the states of [the specification](../spec/durability.md#page-states).
A page a flush takes is *filling*, which the specification counts as reusable: it has a kind and coverage but no epoch, since its epoch is the flush's, and no place among the victims.
The commit turns every page it wrote into a live one.
Journal pages are a state of their own, although the specification counts them live, since the journal keeps what they hold.

Live pages are bucketed by live fraction — a handful of buckets suffices — which makes victim selection `O(1)` rather than a priority queue's `O(log P)`, with `O(1)` bucket moves as counters change.
`Data` and non-header `Table` pages are bucketed [separately](consolidation.md#the-structures-behind-the-ranking), and header pages in neither, since a header is rewritten every flush and so is never a victim.

An ordered set tracks reusable pages, handing out the lowest first, which keeps the file's live pages low, where truncation can follow ([where the pages come from](flush.md#where-the-pages-come-from)).

## 4. The reverse index

**For each data page, the reverse index lists the spans that show bytes in it**, so that [evacuation](consolidation.md#evacuation) finds what a victim holds in `O(1)` per span.
A data page is opaque bytes with no ids in it, so decoding it tells nothing; but span ids are stable, and a span's address changes only when the span is stated with a new one, so the list is exact and cheap to keep: a span is added to its page's list when it is stated with a `Ref` there, and removed when it is restated elsewhere, merged away, or no longer reached.
Keeping each span's place in its page's list in its span record makes both `O(1)`.

## 5. The span id allocator

**The allocator hands out the lowest free id, and runs of consecutive free ids where a flush states consecutive spans**, from a bitmap of free ids with a summary bit per word, which finds either in a few scans of words.
Low, consecutive ids are what keep id deltas short and successors implicit in the [entry encoding](../spec/span-table.md#delta-encoding).
A free list threaded through the span table's empty records would cost no memory of its own, but would hand ids out in the order they were freed, which scatters them.

An id is free the moment its span is no longer reached, and may be handed out again in the same flush: the [reach rule](../spec/span-table.md#rules-for-writers) has the new span stated in the epoch it becomes reached, so its entry is newer than any old entry of the same id.

## Liveness is one bit

**An entry is live exactly while it is the span table's entry for a span the rope, or a short chain, holds**, so liveness needs no counter at all: no pins, no count of mentions, no size or tombstone to keep alive.
A dead entry survives as bytes in its page until a rewrite of that page drops it, and the page's coverage stopped counting it the moment it died.

## Heat

**Each span carries its heat: the epoch of the flush that last wrote its bytes**, or the number of flushes since.
The flush ranks the header's entries by it ([the header as write buffer](flush.md#the-header-as-write-buffer)), and the rotating walk merges only spans that have gone cold ([merging spans](consolidation.md#merging-spans-rides-the-rotating-walk)).
It is content age, where a page's epoch is location age: restating a span re-stamps its entry's epoch, but not its heat.

## During a flush

A flush adds transient state of its own, all dropped when it commits:

- **the fold's piece table**, over the sequence as the journal leaves it ([the fold](flush.md#the-fold));
- **the new bytes**, of new spans and of runs that have no span yet, in a scratch buffer, each with whether its entry will hold them inline;
- **the stated set**: the spans that get a new entry in this flush, and which of them go to the header.

Coverage releases happen as the flush changes spans, so victim selection sees the state the flush will leave; charges wait for locations: at placement for bytes, and at the cut for entries.
A flush that fails must therefore unwind or [poison the session](../spec/durability.md#headroom).

## Epochs belong to pages, not to entries

An entry inherits the epoch of the page it sits in, so restating an entry re-stamps it.
The header is rewritten every flush, so every entry in the header carries the current epoch, and two epochs can disagree about a span only between the header and a leaf, or between two leaves.

## Memory for very large files

The span table and the rope take about 32 bytes per span together.
A cold gigabyte in spans of a page needs about 8 MB of them; a gigabyte edited everywhere at once, in short spans, would need far more, which is an open question, and what [segments](../spec/sequence.md#partial-loading-later) would bound.
