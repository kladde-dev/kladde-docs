---
title: Implementation
---

The reference algorithms and data structures: what an implementation must maintain in memory to satisfy the [specification](../spec/), and how it maintains it.

Everything here is **language-agnostic**.
Pseudocode is written in a Rust-like notation, because `enum`s and pattern matching make the cases concrete and easy to reason about, but nothing here depends on Rust.
A Python, C++, or Java implementation should make substantially the same choices — including the choice of a B-tree here and a hash map there, and the choice of integer widths, both of which follow from the [bounds the specification states](../spec/address-table.md#bounds).

What does *not* belong here is anything a different language would do differently: trait shapes or class hierarchies, memory layout, alignment, the mutation-capture mechanism.
Those decisions are documented in the language-specific documentations (currently only [kladde-rust](../rust/)).

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

A kladde file is currently **read in bulk and never searched on disk**.

That single decision is what keeps this design far simpler than a database engine, and it is worth stating first because almost every other choice follows from it:

- On-file representations are optimized for compactness and cheap incremental edits, never for random access.
- There is no on-disk search tree, no B-tree page splitting, no path copying.
- The address table's [tree of child references](../spec/address-table.md#physical-format) is a *directory*, not a search structure: nothing ever descends it by key.
- Epochs are consulted exactly once, during the bulk read, and never again at run time.

What this costs is that opening a file is `O(live bytes)` and that the whole file image is resident.
What it buys is that every read is an in-memory lookup and every flush writes only deltas.

A future feature called *[[segments]]* will address the cost of bulk reads by allowing application authors to manually declare a subtree of their persisted data structure as being loaded and resolved lazily.

## The four structures

A loaded file is a mirror of its pages plus four indexes over them.

### The mirror

An implementation reads pages into its own buffers with ordinary file reads, rather than mapping the file.
Two rules keep that from meaning "the whole file, forever".

**Mirror only pages that are reachable.**
Reachability is known while the address table is being parsed — every child reference and every `Ref` address names a page — so the set can be accumulated during that parse and nothing else need ever be read.
An implementation that finds this inconvenient may instead load each page lazily, the first time something reads from it, which arrives at the same set by a different route.

Today this saves little, since a reasonably consolidated file is mostly reachable.
It matters once [segments](../drafts/segments.md) arrive: each segment will live in its own subset of pages, and there is no way to guarantee that a segment's pages are contiguous in the file, so "load one segment" will mean exactly "mirror the reachable pages of that segment".

**Drop the data-page mirror once loading is done.**
After the load, application reads are served by the values' own in-memory representations and never touch the file, so a `Bytes` fragment's `(page, offset)` is dereferenced only when a flush relocates those bytes or a `Copy`/`Splice` reads them — both rare, and both able to afford a fresh read.

**Address-table pages stay resident**, for two reasons that data pages do not share: `Inline` payloads live *inside* them, so a fragment resolving to an inline value dereferences into a table page rather than a data page; and a [page rewrite](address-table-operations.md) has to decode its victim.
They are also small — on the order of 8 MB for a million allocations.

*Why not `mmap`.*
Mapping the file would let the operating system's page cache serve as the mirror, with pointer-dereference access and no second copy.
It is rejected for now because **I/O errors arrive as `SIGBUS` rather than as an error value**: a page that cannot be faulted in — a failing disk, or the file truncated by something else — takes the process down, and there is no portable way to catch it.
Keeping error handling explicit is worth the extra copy.
Should that trade ever be revisited, note also that the mapping must be read-only, since kladde writes with ordinary writes and `fsync` and mixing the two is only coherent by grace of the platform's unified page cache.

*Reading through a cursor.*
Handing a data type a `Read + Seek` cursor rather than a byte slice keeps `Persistable::load` from depending on the bytes being contiguous or resident — which is what makes everything above an implementation detail rather than a format one.
Over the mirror, such a cursor costs no syscalls at all.

### The indexes

1. **The fragment map** — the resolved content view, keyed by `(id, offset)`. Everything else hangs off it.
2. **The statement slab** — one record per *live* statement: where its encoding lives, and how many reasons it has to stay.
3. **The allocation map** — per-id metadata: size, anchor, and two counters that no per-page number could replace: how much description this allocation costs, and how many statements still name its id.
4. **The page table** — per-page kind, epoch, and live-byte counter, bucketed for `O(1)` victim selection.

Plus two derived structures rebuilt at open and never persisted: the **id allocator** and the **eviction clock**.

Content is never copied out of the mirror while the mirror holds it.
Both `Ref` and `Inline` content have a file address — an `Inline` payload lives inside an address-table page, but it is bytes at a known address all the same — so a resolved fragment names a location rather than owning bytes, and a range that resolves to zero needs no bytes at all.

See [In-memory state](in-memory-state.md).
