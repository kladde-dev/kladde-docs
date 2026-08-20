---
title: The heap
---

`kladde-heap` owns the address space: which allocation lives where, where the free space is, and how to squeeze it out.

It is entirely type-agnostic.
It has no notion of serialization, schema, or any kladde-specific concept, and it depends on none of the other crates.

## The documents

| document | contents |
| --- | --- |
| [The relocatable heap](relocatable-heap.md) | the problem, the trait, and why the heap owns the `id → address` table |
| [Placement](placement.md) | where a new allocation goes |
| [Incremental compaction](compaction.md) | the potential function, the greedy policy, and why it converges |
| [The evacuation index](evacuation-index.md) | the augmented B+ tree that answers "what is the best move?" in `O(log n)` |
| [Pathologies](pathologies.md) | adversarial workloads, and the two mechanisms that bound them |

## The one fixed constraint

Kladde's pointers are **stable ids**, not addresses.
So an in-memory table mapping `id → (address, size)` must exist no matter what.

Everything in this part follows from taking that table as given and asking what the cleanest memory manager around it looks like.
The answer, in one sentence: **the component that decides where things go should own that table rather than shadow it.**
A compactor needs index-grade access to both the geometry and the id mapping, and splitting them across two components means one of them keeps a copy that can drift.

## What the heap must do

Maintain a partition of `[0, end)` into allocations — each tagged with the stable id naming it — and gaps.
Support:

- `alloc(id, size) → address`, `free(id)`, `resize(id, size)` — foreground operations, `O(log n)` CPU, no byte movement except when a resize forces relocation;
- `lookup(id) → (address, size)` — the point query everything else depends on;
- **one bounded compaction step** — `budget → step`, callable arbitrarily often, `O(log n)` CPU *including finding the step*, such that repeated calls converge to a compact heap.

That last requirement is the demanding one, and it is what the rest of this part is about.

## No stop-the-world

There is never a full-compaction phase.
A complete compaction is just the bounded step in a loop that something is allowed to interrupt.

This is a hard requirement rather than a nicety.
Kladde is meant to sit under interactive applications, and a multi-second pause while a large heap is rearranged is not acceptable.
It also forces a genuinely useful property: because every step is independently valid, the heap is always in a consistent state, so a crash mid-compaction costs at most one step.

The cost is that the compactor cannot use any algorithm that needs a global pass.
It must *find* its next move in logarithmic time, which is a much stronger requirement than being able to *execute* it in logarithmic time — and it is the reason the [evacuation index](evacuation-index.md) exists.

## Why compaction is safe

A compaction step moves bytes and updates the `id → address` table.
It changes no id, no size, and no content.

So it is invisible to everything above: no pointer is rewritten, no serialized byte changes, and the heap does not need to know which of an allocation's bytes are pointers.
It can therefore relocate allocations whose contents it cannot parse — which is the whole reason the stable-id design was chosen.

This is also why [the format does not specify compaction at all](../../../spec/allocations.md#compaction): two implementations with completely different strategies produce equally valid, mutually readable files.
