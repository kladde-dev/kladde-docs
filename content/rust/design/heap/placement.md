---
title: Placement
---

Where a new allocation goes.

## Placement is compaction with the copy pre-paid

The central observation: placing a new allocation is the same decision as an evacuation, minus the cost.

An evacuation moves an existing allocation into the lowest gap that fits it, paying `size` bytes of copying.
A placement puts a *new* allocation into the lowest gap that fits it, and the bytes were going to be written regardless.

So placement is free compaction, and it uses the same index and the same query: **lowest gap that fits**.
Choosing well at placement time is strictly cheaper than fixing it later.

## The query

```rust
fn lowest_gap_fitting(&self, min_len: Size) -> Option<(Address, Size)>
```

Answered in `O(log n)` by the [evacuation index](evacuation-index.md), which maintains gaps and allocations in one augmented tree.

Best-fit by *address*, not by size.
This is the same reasoning that makes [frontier slides](compaction.md#frontier-slide) the right compaction move: filling from the bottom keeps the compact prefix growing, whereas filling the tightest gap wherever it happens to be scatters live bytes above the frontier and leaves work for later.

## Size classes

Fixed-size allocations are minted in bulk at a handful of distinct sizes, so they form **dense size classes**.
Resizable ones scatter across many sizes.

This is why sizedness [rides on the id](relocatable-heap.md#ids-come-from-outside): the heap indexes fixed-size allocations as evacuation candidates because they pack predictably, while a resizable allocation that is moved will likely have to move again on its next growth.

The class also participates in the potential weighting; see [Pathologies](pathologies.md#remedy-1-weighting-by-class).

## Extending the address space

When no gap fits, the allocation goes at `end` and the file grows.

This is always available and always correct, so `OutOfMemory` arises only when the address width is exhausted, not when the heap is merely full.

## Deferred placement

The journaled backend defers placement to flush time rather than assigning an address when the allocation is minted.

The reason is that a flush knows the *whole* batch of pending allocations, so it can sort them **first-fit-decreasing** — serving the large ones while the large gaps are still intact, rather than letting a small one fragment a gap that a large one needed.

Sorting also makes the resulting layout independent of hash-map iteration order, which would otherwise make the layout — and therefore how much work compaction later has to do — unreproducible from run to run.

There is a subtlety here that interacts with the fold's freedom to reorder: FFD becomes best-effort over the set of allocations whose placement is not blocked by a dependency.
See [Fold and schedule](../journal/fold-and-schedule.md).

## What is not decided here

Placement policy is [explicitly free in the format](../../../spec/allocations.md#compaction).
Another implementation may use best-fit by size, a segregated free list, or a bump allocator with periodic wholesale compaction, and produce equally valid files.

The choice made here is driven by the fact that kladde compacts incrementally and continuously: a placement policy that cooperates with the compactor is worth more than one that is locally optimal.
