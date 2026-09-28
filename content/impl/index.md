---
title: Implementation
---

The reference algorithms and data structures: what an implementation must maintain in memory to satisfy the [specification](../spec/), and how it maintains it.

Everything here is **language-agnostic**.
Pseudocode is written in a Rust-like notation, because `enum`s and pattern matching make the cases concrete and easy to reason about, but nothing here depends on Rust.
For implementations in other languages, it is recommended but not required to make substantially the same choices for, e.g., integer widths or fundamental data structures like B-trees or hash maps, both of which follow from the [bounds the specification states](../spec/address-table.md#bounds).

What does *not* belong here is anything a different language would do differently: trait shapes or class hierarchies, memory layout, alignment, the mutation-capture mechanism.
Those decisions are documented in the language-specific documentations (currently only [kladde-rs](../rust/)).

## The documents

| document | contents |
| --- | --- |
| [In-memory state](in-memory-state.md) | the four structures a loaded file is held in, and why each is the shape it is |
| [Address-table operations](address-table-operations.md) | pseudocode for every operation on that state: load, read, write, resize, free, recycle, evict, consolidate |
| [Liveness accounting](liveness.md) | pins, coverage, and what makes a statement or a page reclaimable |
| [Id recycling](id-recycling.md) | which recyclable id to hand out next, and why the order matters |
| [Write-phase state](write-phase-state.md) | what is tracked between flushes, and why it is derived from the journal |
| [The flush](flush.md) | folding the journal into pages: piece tables, hoisting, scheduling, and what gets written |
| [Consolidation](consolidation.md) | reclaiming garbage: victim selection, the three candidate kinds, and the budget |
| [Consolidator state](consolidator-state.md) | what consolidation carries from one session to the next, and how it checks that it is still current |
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

**Mirror only pages that are reachable**, where reachable means *named by a surviving fragment* — not merely named by some `Ref` that is physically present.
The two differ by every `Ref` that lost its probes to a newer statement, and the difference is not small in a file that has been written to for a while.
So the set is accumulated during [resolution](address-table-operations.md#what-falls-out-of-emission), from the `Bytes` fragments the sweep emits, rather than while decoding statements.
It might be tempting to instead load each page lazily, the first time something reads from it.
The decisive objection is not that a page could be missing — one can always be fetched — but that the resident set would stop being a property of the *file* and become a property of the *application*: nothing obliges a `Persistable` to read anything, so a partial loader, whether a sensor app that mocks the subtree it never touches or a tool that only compacts, would leave the storage layer on a path chosen by application code and exercised only when consolidation happens to relocate a page nobody loaded.
Eager loading also turns scattered reads interleaved with `Persistable::load` into one sequential pass over a set that is known in advance, and validates every reachable page's CRC at open rather than at first touch — which may be long after the last backup that could have helped.

Nothing can read a page that only dead `Ref`s name, which is what makes ignoring it safe rather than merely economical: a dead statement wins no probe at any epoch the file can be recovered to, so no read at any recoverable state resolves into that page.
The page table agrees by construction, since such a page's [coverage](liveness.md#coverage) is zero and it is therefore already reusable.

Today this saves little, since a reasonably consolidated file is mostly reachable.
It matters once [segments](../drafts/segments.md) arrive: each segment will live in its own subset of pages, and there is no way to guarantee that a segment's pages are contiguous in the file, so "load one segment" will mean exactly "mirror the reachable pages of that segment".

**Drop the data-page mirror once loading is done.**
After the load, application reads are served by the values' own in-memory representations and never touch the file, so a `Bytes` fragment's `(page, offset)` is dereferenced only when a flush relocates those bytes or a `Copy`/`Splice` reads them — both rare, and both able to afford a fresh read.

**Address-table pages stay resident by default**, for a reason data pages do not share: `Inline` payloads live *inside* them, and [both table-side consolidation mechanisms](consolidation.md#why-address-table-pages-have-two-mechanisms) copy those payloads on every consolidating flush — the [page rewrite](address-table-operations.md#rewrite_pagevictim-dirty), which also decodes its victim, and the rotating window.
They are also small — on the order of 8 MB for a million allocations.
Nothing depends on it, though: only the header must stay, being the write buffer, and dropping the leaves after load, as data pages are dropped, costs one read per table victim and one per table page whose inline payload the window restates.

*Why not `mmap`.*
Mapping the file would let the operating system's page cache serve as the mirror, with pointer-dereference access and no second copy.
It is rejected for now because **I/O errors arrive as `SIGBUS` rather than as an error value**: a page that cannot be faulted in — a failing disk, or the file truncated by something else — takes the process down, and there is no portable way to catch it.
Keeping error handling explicit is worth the extra copy.
Should that trade ever be revisited, note also that the mapping must be read-only, since kladde writes with ordinary writes and `fsync` and mixing the two is only coherent by grace of the platform's unified page cache.

*Reading through a cursor.*
When a data type reads its allocations during `Persistable::load`, the reference implementation hands it an iterator over shared byte slices (fragments) rather than an assembled owned byte slice to avoid unnecessary copies.
Over the mirror, such a cursor costs no syscalls at all.

### The indexes

1. **The fragment map** — the resolved content view, keyed by `(id, offset)`. Everything else hangs off it.
2. **The statement slab** — one record per *live* statement: where its encoding lives, and how many reasons it has to stay.
3. **The allocation map** — per-id metadata: size, anchor, when the application last wrote it, and two counters that no per-page number could replace: how much description this allocation costs, and how many statements still name its id.
4. **The page table** — per-page kind, epoch, and live-byte counter, bucketed for `O(1)` victim selection.

Plus two derived structures rebuilt at open and never persisted: the **id allocator** and the **eviction clock**.
A flush adds transient state of its own — the fragments it has taken but not yet stated, and the dirty set it states them from — which it drops when it commits; see [During a flush](in-memory-state.md#during-a-flush).

Content is never copied out of the mirror while the mirror holds it.
Both `Ref` and `Inline` content have a file address — an `Inline` payload lives inside an address-table page, but it is bytes at a known address all the same — so a resolved fragment names a location rather than owning bytes, and a range that resolves to zero needs no bytes at all.

See [In-memory state](in-memory-state.md).
