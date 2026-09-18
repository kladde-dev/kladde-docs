---
title: Whole-allocation table entries
---

**Superseded.** Replaced by [per-range statements](../spec/address-table.md#statement-types).

The first address-table design under copy-on-write pages: one statement per allocation, carrying its size and the locations of all its chunks.
It was worked out in full — including the escalation mechanisms below — before per-range statements made all of it unnecessary.

## What it was

Two statement kinds:

- `entry(id) = (size, chunk list)` — the allocation's size plus the lengths and locations of all its chunks, extent-compressed;
- `tombstone(id)` — the id is unallocated.

A statement in a page with a higher epoch shadowed any statement about the same id in a page with a lower epoch.

**Chunks** were the unit of content: an allocation was a sequence of chunks, each of arbitrary size up to `MAX_PAGE_CONTENT`, each contained entirely in one page, with logical offsets implied by the lengths before them rather than stored.
A chunk could reference a reserved **null page** to state that its range was uninitialized.

That chunk model is the direct ancestor of today's `Ref` and `Zero` statements, and two of its rules [survive unchanged](../impl/flush.md#cutting-content-across-pages): the writer prefers maximal pieces, and whoever rewrites bytes may re-cut them.
The free-re-cutting argument — that consolidation should pack pages the way stock is cut rather than the way bins are packed — was developed here.

## Why per-chunk statements were rejected first

An earlier version tried a per-allocation `shape` statement plus independent per-chunk statements, so that updating one chunk of a large allocation would restate only that chunk's location.

That founders on **key stability** once chunking is flexible: with consolidation free to split and merge chunks, and splices free to insert them, a chunk's *index* within its allocation is not a stable name, and a statement keyed by `(id, chunk index)` can be silently re-aimed by an unrelated edit earlier in the same allocation.

Whole-entry statements had no sub-keys to destabilize, and bought three simplifications outright: a resize is just a new entry, re-allocating a freed id is just a new entry, and the reader never assembles an allocation from statements of mixed epochs.

**The resolution, which came later:** key statements by **byte offset** rather than by chunk index.
A byte offset *is* stable under overwrites and resizes — the operations that dominate — which is what makes per-range statements viable where per-chunk statements were not.
The price is that a middle splice shifts the offsets of everything behind it, which is [recorded as a known cost](../spec/address-table.md#design-directions) rather than a blocker.

## The mechanisms it needed, and no longer does

### Extents

Chunk locations were encoded as extents wherever chunks happened to be contiguous: a run of full-size chunks in consecutive pages was one `(start_page, page_count)` pair, and any other chunk a `(length, page, offset)` triple.

The reasoning behind this is worth keeping.
File systems have given both answers historically: FFS and ext2 used fixed-depth radix trees of block pointers, while every modern system — ext4, XFS, NTFS, btrfs — moved to **extents**, precisely because real files are mostly contiguous, escalating to a per-file extent tree only when fragmentation forces it.
Kladde took the extent position, with a cheaper escalation than a tree, because it never searches the structure on disk.

Per-range statements make this moot: each statement carries its own address, and a run of contiguous content is simply one statement with a large `size`.

### Spilling

If an entry's encoding outgrew a threshold — say a quarter page — the entry **spilled**: its statement in the shared table page shrank to `(size, continuation page numbers)`, and the chunk list moved to dedicated pages owned by that entry alone and rewritten together with it.

The obvious question is why this was needed at all — why an oversized chunk list could not simply split across ordinary table pages with no extra machinery.
Under per-chunk statements it could; under whole-entry statements it could not, because **the statement is the unit of shadowing**, so it must be replaceable as one atom, and fragments of one entry spread over shared pages would need stable sub-keys to be shadowed individually — precisely the key-stability problem that ruled per-chunk statements out.

Spilling kept the statement *logically* whole while letting its bytes exceed a page.
Its cost was stated rather than hidden: a huge, badly fragmented allocation rewrites its spill pages on every touch.

Per-range statements remove the need entirely: a large allocation's statements split across pages naturally, because no single statement ever needs to span one.

### A separate index

The **index** was a small rooted structure that made scanning unnecessary: a list of `(page_number, epoch)` for every live `AddressTable` page, held in pages of its own kind, whose page numbers were in the header, and rewritten in full every flush.

Sizing, to justify "small": at ~8 bytes per entry, a million allocations occupy ≈ 8 MB ≈ 2000 table pages; at ≈ 6 bytes per index entry that is ≈ 12 KB ≈ 3 index pages per flush.
If the table grew to where the full index rewrite hurt, the index would get a second level and only the changed leaves would be rewritten — a two-level copy-on-write tree.
That was the honest boundary of the "no trees" stance: trees are unnecessary for *lookup* here, but a bounded incremental commit of a large directory is legitimately what they are for.

Because the index was rewritten atomically with the commit, membership in it was authoritative: an address-table page not listed was dead, whatever its bytes said.
Resolution never depended on the *absence* of an unreferenced page, only on the listed set and their epochs — which is what kept the scheme free of the reclamation-ordering subtleties a scan-based design has to solve.

**What replaced it** is the [child-reference tree](../spec/address-table.md#physical-format) inside the address-table pages themselves, rooted at the header page.
The `Index` page kind is gone; the escalation story survives as [the shape of the tree](../impl/flush.md#the-shape-of-the-tree), with the same "escalate then and not before" reasoning and the same observation that it is a directory rather than a search structure.
Since epochs live in the pages' own framing, the `(page, epoch)` pairing the index carried is no longer needed either.

### Tombstone lifetime

A `tombstone(id)` had to exist as long as some **indexed** address-table page carried an `entry(id)` at a lower epoch *and* no indexed page carried one at a higher epoch — in that second case the id had been re-allocated and the newer entry already shadowed both.

Because the index committed atomically, the lifetime rule was purely logical, with none of the durable-invalidation lag a scan-based design needs: a tombstone became droppable in exactly the commit that removed or consolidated away the last lower-epoch page carrying an entry for the id.

The modern rule is [the same idea expressed in pins](../impl/liveness.md#tombstones), and the underlying question — "is any older mention still physically present?" — is the same one, now answered by the `mentions` counter.

## What else survived

Beyond the chunking rules, the [coverage accounting](../impl/liveness.md#coverage) was designed here and carried over essentially unchanged: coverage is in-memory only, maintained `O(1)` per shadowed statement, rebuilt for free at open during the bulk read, and never persisted.

So was the split of consolidation into data-page victim selection by live fraction and address-table consolidation by **id range** rather than page identity — including the observation that rewriting a window of statements sorted and adjacent re-establishes delta-encoding density, so that density is restored by cleaning rather than maintained by an invariant.
