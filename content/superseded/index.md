---
title: Superseded
---

Designs that were worked out, sometimes built and measured, and then replaced.

They are kept for the reasoning rather than for the result.
Each of them was a reasonable answer to the constraints as they stood, and understanding *which* constraint changed is usually more instructive than the replacement design on its own — and it is the cheapest defence against re-proposing one of them.

Nothing here describes current behaviour.
Where a document says "kladde does X", read "kladde did X".

## What was replaced, and by what

| superseded design | replaced by | what changed |
| --- | --- | --- |
| [The relocatable heap](relocatable-heap.md) — allocations as contiguous ranges in a flat address space, an `id → address` table, and sizedness as a placement hint | [Pages and the address table](../spec/address-table.md) | the move to copy-on-write pages: nothing is adjacent to anything any more, so contiguity, placement-by-address, and sizedness all stop meaning anything |
| [Incremental compaction](incremental-compaction.md) — a potential function over byte addresses, frontier slides, and an augmented B+ tree to find the best move in `O(log n)` | [Consolidation](../impl/consolidation.md) | same cause: there is no address ordering left to descend, so victim selection becomes the log-structured live-fraction rule instead |
| [The in-place flush](in-place-flush.md) — conflict detection, write-ahead copies, read hoisting for restartability | [The copy-on-write protocol](../spec/durability.md) | a flush no longer overwrites anything a recoverable state depends on, so there is nothing to protect against and nothing to restart from |
| [Whole-allocation table entries](whole-entry-address-table.md) — one statement per allocation, chunk lists, extents, spilling, and a separate index of table pages | [Statements](../spec/address-table.md#statement-types) | keying by byte offset instead of by allocation made per-range statements viable, which removed the need for every mechanism in the left column |

## A note on measurement

The compaction work in particular was implemented, benchmarked across several orders of magnitude of heap size, and tuned.
Those numbers describe a system that no longer exists, but the *methodology* notes in it — especially about build-to-build timing drift swamping the effects being measured — apply to anything measured later, and are the reason that document keeps its numbers rather than being summarised away.
