---
title: The Move operation
---

**Status: the record is [specified](../spec/journal.md#move); how a flush states it is a sketch.**

`Move(src_id, src_offset, len, dst_id, dst_offset)` relocates a range from one allocation to another, or within one, **without copying the bytes on disk**, and leaves the vacated source range reading as zero.

Recorded because it changes what the storage layer must support, and because it is the reason the address table can [decline copy-on-write clones](../spec/address-table.md#design-directions).

## It needs no new address-table statement

The flush emits a `Ref(dst_id, dst_offset, len, address)` pointing at the bytes where they already lie, and denies the source's claim in the same epoch; the old `Ref(src_id, src_offset, len, address)` is shadowed by that denial, loses its fragments, and dies.

So the address-table format is untouched, and `Move` is a [journal record](../spec/journal.md#record-kinds) and an API operation rather than an address-table addition.

## It keeps the byte-referenced-once invariant

Which is the point.
That invariant forbids two *live* `Ref`s over one byte, and a move **transfers rather than shares**: the new claim and the old claim's denial land in the same epoch, so the two are never live together.

Data-page coverage therefore stays a plain counter — the source fragment's destruction subtracts the length, the destination fragment's creation adds it back.

One ordering caution: **add before subtracting**, or net the two, so coverage never transiently reads 0 for a page whose last live extent is being moved, which a flush that frees pages part-way through could otherwise hand to the free list.

## What it buys

Rope- and B-tree-like structures shuffle ranges between neighbouring nodes constantly, and without `Move` every one of those is a byte copy.
With it, a split or merge that donates a suffix costs one `Ref` and one `Shrink`.

This is what lets the address table decline copy-on-write clones: cloning would then be needed only for genuine simultaneous sharing — snapshots and deduplication — and not for moving data between neighbours.

## Settled by the record's semantics

- **Which moves to allow: any.**
  The record moves any range, and the cost difference between a tail move and a head or interior move is the caller's: closing the gap is a separate `Splice`, and it is that splice, not the move, that shifts every later offset in the source.
- **What becomes of the source range: it reads as zero, and the source keeps its size.**
  So the flush states it as `Zero(src_id, src_offset, len)`, or, when a `Resize` shrinking the source to `src_offset` follows, the two fold into one `Shrink(src_id, src_offset)` — the cheapest case, and the one a suffix donation produces.
- **Overlapping self-moves zero only `source \ destination`.**
  That is also what the address table needs: the new `Ref` and the denial cannot both name the overlap in one epoch, per [no conflicts within each epoch](../spec/address-table.md#no-conflicts-within-each-epoch).

## Open questions

- **Folding.**
  A move changes content in *two* ids at once, and may grow the destination.
  The rules for annihilating a move against a later free, resize, or overwrite of either id are not written.
- **Cleaner heuristics.**
  [Consolidation](../impl/consolidation.md#victim-selection-for-data-pages)'s cost-benefit victim selection assumes a data page's residents are the content that one flush wrote together.
  After moves, residents belong to ids that never wrote them, so the age signal gets noisier.
  That assumption is documented as degrading gracefully, so this is a note rather than an objection.
