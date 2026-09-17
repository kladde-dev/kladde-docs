---
title: Id recycling
---

Which recyclable id to hand out next, and why the order is worth choosing deliberately.

The [specification](../spec/allocations.md#id-recycling) fixes only that an id becomes reusable as soon as a tombstone for it is committed, and leaves the order free.
The order still matters, for a reason that is not the obvious one.

## The policy

> **Recycle lowest-id-first, taking ids whose tombstone is already dead first, and never hold a recyclable id back in favour of a fresh one.**

What decides this is **delta encoding**, not tombstones.
Statements are sorted by `(id, offset)` and `id_delta` is a varint, so what matters is how tightly the *live* id set is packed: `id_delta` costs a byte more for every factor of 128 by which the set is spread out, and it costs that once per id per page — which in the many-small-allocations regime this design targets is once per statement.
Against that, a stale tombstone is two bytes once per id.

Lowest-free-first keeps the live set packed, and needs only a min-heap over the recyclable set or a bitmap scanned for its lowest set bit.

## The refinements

**Prefer ids whose tombstone is already dead.**
That is exactly the `mentions == 1` case, and it is free to identify because the recyclable-set entry's tombstone reference is then already absent.
Recycling one of those costs nothing at all: no anchor is carried into the new incarnation, nothing older is left to deny, and no tombstone is frozen.

**`mentions` is a weak signal pointing the opposite way from the intuition.**
Recycling an id freezes the `mentions == 1` path permanently, since the new incarnation's own statements hold the count above 1 from then on.
So recycling an id at `mentions == 2` discards progress that was one page-clean from completing, while recycling one at `mentions == 50` discards almost nothing — an argument for taking *high*-`mentions` ids first.
At two bytes apiece this is a tiebreak at most, and it should lose to density wherever the two disagree.

**Do not hand out fresh ids while recyclable ones wait.**
It consumes the 32-bit id space at the rate of allocations *ever made* rather than concurrently live, and it sparsifies the live set — paying per statement to save per id.
The [tombstone sweep](liveness.md#a-possible-extension-not-adopted) is the better answer to the same worry, and reaches tombstones this one cannot.

## What an earlier draft got wrong

Gating recycling on `mentions == 0` — waiting for every statement naming the id to be physically dropped — coupled id availability to the cleaning of unrelated pages.
One dead seven-byte statement in a page that is never chosen as a victim could hold an id out of circulation for the life of the file, and a workload that churns allocations would leak ids at the rate its cold pages resist cleaning.

Recycling now waits only on a committed tombstone, so the stake is two bytes of tombstone framing rather than an id.
