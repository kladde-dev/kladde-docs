---
title: Implementation
---

The reference algorithms and data structures: what an implementation must maintain in memory to satisfy the [specification](../spec/), and how it maintains it.

Everything here is **language-agnostic**.
Pseudocode is written in a Rust-like notation, because `enum`s and pattern matching make the cases concrete and easy to reason about, but nothing here depends on Rust.
A Python, C++, or Java implementation should make substantially the same choices — including the choice of a B-tree here and a hash map there, and the choice of integer widths, both of which follow from the [bounds the specification states](../spec/address-table.md#bounds).

What does *not* belong here is anything a different language would do differently: trait shapes, memory layout, alignment, the mutation-capture mechanism.
Those are in [kladde-rust](../rust/).

## The documents

| document | contents |
| --- | --- |
| [In-memory state](in-memory-state.md) | the four structures a loaded file is held in, and why each is the shape it is |
| [Address-table operations](address-table-operations.md) | pseudocode for every operation on that state: load, read, write, resize, free, recycle, evict, consolidate |
| [Liveness accounting](liveness.md) | pins, coverage, and what makes a statement or a page reclaimable |
| [Id recycling](id-recycling.md) | which recyclable id to hand out next, and why the order matters |
| [Write-phase state](write-phase-state.md) | what is tracked between flushes, and why it is derived from the journal |
| [The flush](flush.md) | folding a journal segment into pages: piece tables, hoisting, scheduling, and what gets written |
| [Consolidation](consolidation.md) | reclaiming garbage: victim selection, the three candidate kinds, and the budget |
| [Transactions and batches](transactions-and-batches.md) | grouping operations into transactions, and the buffering state machine |
| [Related work](related-work.md) | where the design comes from, and where it parts company |

## The shape of an implementation

A kladde file is **read in bulk and never searched on disk**.

That single decision is what keeps this design far simpler than a database engine, and it is worth stating first because almost every other choice follows from it:

- On-file representations are optimized for compactness and cheap incremental edits, never for random access.
- There is no on-disk search tree, no B-tree page splitting, no path copying.
- The address table's [tree of child references](../spec/address-table.md#physical-format) is a *directory*, not a search structure: nothing ever descends it by key.
- Epochs are consulted exactly once, during the bulk read, and never again at run time.

What this costs is that opening a file is `O(live bytes)` and that the whole file image is resident.
What it buys is that every read is an in-memory lookup and every flush writes only deltas.

## The four structures

A loaded file is the page images plus four indexes over them:

1. **The fragment map** — the resolved content view, keyed by `(id, offset)`. Everything else hangs off it.
2. **The statement slab** — one record per *live* statement: where its encoding lives, and how many reasons it has to stay.
3. **The allocation map** — per-id metadata: size, anchor, and the counters that drive reclamation.
4. **The page table** — per-page kind, epoch, and live-byte counter, bucketed for `O(1)` victim selection.

Plus two derived structures rebuilt at open and never persisted: the **id allocator** and the **eviction clock**.

Content is never copied out of the page images.
Both `Ref` and `Inline` content have a file address — an `Inline` payload lives inside an address-table page, but it is bytes at a known address all the same — so every resolved fragment points into the in-memory copy of the file, and `Undefined` needs no bytes at all.

See [In-memory state](in-memory-state.md).
