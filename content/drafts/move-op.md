---
title: The Move operation
---

**Status: sketch, not specified.**

`Move(src_id, src_offset, len, dst_id, dst_offset)` relocates a range from one allocation to another, or within one, **without copying the bytes on disk**.

Recorded because it changes what the storage layer must support, and because it is the reason the address table can [decline copy-on-write clones](../spec/address-table.md#design-directions).

## It needs no new address-table statement

The flush emits a `Ref(dst_id, dst_offset, len, address)` pointing at the bytes where they already lie, and denies the source's claim in the same epoch; the old `Ref(src_id, src_offset, len, address)` is shadowed by that denial, loses its fragments, and dies.

So the on-disk format is untouched, and `Move` is a [journal record](../spec/journal.md#record-kinds) and an API operation rather than a format addition.

## It keeps the byte-referenced-once invariant

Which is the point.
That invariant forbids two *live* `Ref`s over one byte, and a move **transfers rather than shares**: the new claim and the old claim's denial land in the same epoch, so the two are never live together.

Data-page coverage therefore stays a plain counter — the source fragment's destruction subtracts the length, the destination fragment's creation adds it back.

One ordering caution: **add before subtracting**, or net the two, so coverage never transiently reads 0 for a page whose last live extent is being moved, which a flush that frees pages part-way through could otherwise hand to the free list.

## What it buys

Rope- and B-tree-like structures shuffle ranges between neighbouring nodes constantly, and without `Move` every one of those is a byte copy.
With it, a split or merge that donates a suffix costs one `Ref` and one `Shrink`.

This is what lets the address table decline copy-on-write clones: cloning would then be needed only for genuine simultaneous sharing — snapshots and deduplication — and not for moving data between neighbours.

## Open questions, none of them settled

- **Which moves to allow.**
  A tail move is the cheap case, since a suffix donation leaves the source needing only a `Shrink(src_id, src_offset)` and shifts no offsets.
  A head or interior move is where the cost sits, because closing the gap shifts every later offset in the source.
  Restricting the record to head and tail moves may be worth it; it has not been decided.
- **What becomes of the source range.** Three candidates, not interchangeable:
  - `Undefined(src_id, src_offset, len)` — one statement, no shifting, but the source keeps its size and carries a hole until defragmentation pays it down.
  - `Shrink(src_id, src_offset)` — available for a tail move only, and then the cheapest of the three.
  - a splice closing the gap — `O(fragments behind the splice point)` restated statements, which [the address table](../spec/address-table.md#design-directions) identifies as this design's weak spot.

  Allowing all three and letting the caller's shape pick is the likely answer, but the choice interacts with the previous question and has not been worked through.
- **Overlapping self-moves.**
  With `src_id == dst_id` and overlapping ranges, the new `Ref` and the denial cannot both name the overlap in one epoch, per [no conflicts within each epoch](../spec/address-table.md#no-conflicts-within-each-epoch), so the denial must cover `source \ destination` only.
- **Folding.**
  A move is a content operation with a geometry side effect on *two* ids, so it touches two ids' state at once.
  The rules for annihilating a move against a later free, resize, or overwrite of either id are not written.
- **Cleaner heuristics.**
  [Consolidation](../impl/consolidation.md#victim-selection-for-data-pages)'s cost-benefit victim selection assumes a data page's residents are the content that one flush wrote together.
  After moves, residents belong to ids that never wrote them, so the age signal gets noisier.
  That assumption is documented as degrading gracefully, so this is a note rather than an objection.
