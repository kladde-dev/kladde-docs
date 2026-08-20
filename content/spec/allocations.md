---
title: Allocation model
---

The heap layer: what an allocation is, how it is named, and how one allocation refers to another.

**Status:** the model is settled; the byte encoding is TBD.

## Allocations

The snapshot is a heap.
It is partitioned into **allocations** — contiguous byte ranges holding application data — and **gaps**, the free space between them.

An allocation has:

- a **stable id**, assigned once and never changed;
- an **address**, its current offset in the file, which may change over the file's lifetime;
- a **size** in bytes;
- a **sizedness**: whether it may be resized at all.

Nothing else.
An allocation is an untyped byte range; what its bytes mean is a question for the [schema](schema/) layer, and the heap never asks it.

## Stable ids, and why pointers are not addresses

A pointer stored inside an allocation is serialized as the **stable id** of the allocation it points at — never as an address.

This is the single most consequential decision in the format, and it is what makes compaction cheap.
When an allocation moves, the bytes of every pointer that targets it are unchanged, because those bytes name an id, not a location.
The mover updates one entry in an in-memory `id → address` table and copies the bytes; it never has to find, interpret, or rewrite a pointer.
It does not even need to know which of an allocation's bytes *are* pointers, which means it can relocate allocations whose contents it cannot parse.

The cost is one level of indirection on every dereference, and the need to reconstruct the `id → address` table when a file is opened.
The table is not stored as a separate structure; each allocation carries its own id in its metadata, so opening a file rebuilds the table by scanning live allocations.

An implementation is required to preserve ids across a fold and across compaction.
It is *not* required to assign them in any particular way, or to reuse them in any particular order.

### Sizedness

Every id records whether the allocation it names is **fixed-size** or **resizable**.

This distinction exists because it is placement-relevant.
Fixed-size allocations are minted in bulk at a handful of distinct sizes, so they form dense size classes worth indexing.
Resizable ones scatter across many sizes and would have to move again on the next growth, so a placement policy will generally treat them differently.

The specification requires that sizedness be recoverable from the id, and that a resizable allocation never be silently converted in place — a sizedness conversion mints a *new* id and copies the content.
It does not require any particular encoding; kladde-rust packs the flag into the id's low bit, but that is an implementation choice.

## Ownership

Every allocation has exactly **one owning pointer**.
This is an invariant of the format, not a convention: the value graph reachable from the root is a tree of ownership, and a tool may rely on that when walking it.

Non-owning references — pointers that may target an interior offset of an allocation rather than its start, and that may alias — are **reserved but not specified**.
They interact with liveness in ways that are not yet settled: a reference must not outlive the allocation it points into, and nothing currently enforces that.
**TBD.**

Reclamation follows ownership.
Dropping or overwriting an owning value releases the allocation it owns, recursively, before the pointer to it becomes unreachable.
The ordering matters for crash consistency: nothing is freed until whatever superseded it is already durable.
See [Journal](journal.md#ordering).

## Content semantics

A reader of an allocation **must not assume anything** about the contents of any part of it that has not been explicitly written.

Concretely: after an allocation is created, its bytes are unspecified.
After a resize, only the first `min(old_size, new_size)` bytes are preserved; everything beyond that is unspecified.
An implementation may leave whatever was physically there, may zero it, or may leave it undefined, and a file is conforming either way.

This rule is what licenses most of the interesting optimizations in the fold — an implementation is free to elide a shrink immediately followed by a growth, because the bytes in the re-grown region were unspecified in the first place.
It also means a tool must never infer meaning from bytes outside the region a schema accounts for.

## Free space

Gaps are not represented on disk.
The set of gaps is derivable from the set of live allocations, which is derivable from the per-allocation id tags, so nothing needs to record them.

An implementation's in-memory index over free space — whatever structure it uses to answer "where does this allocation go?" — is entirely its own business.
See [kladde-rust's heap design](../rust/design/heap/) for one such structure.

## Compaction

**Compaction is not part of this specification.**

Whether an implementation compacts, when it compacts, and which allocations it chooses to move are unconstrained.
The reason this is safe is the stable-id design: a compaction step moves bytes and updates an in-memory table, and produces a file that is indistinguishable from one that happened to be laid out that way from the start.

Two implementations with wildly different compaction strategies therefore produce files that are equally valid and mutually readable.
An implementation with no compaction at all is conforming; it will simply accumulate fragmentation.

The one thing the specification does require is that compaction be **externally invisible**: it must not change any allocation's id, size, or content, and it must not be observable in the file except as a different arrangement of the same allocations.

## Byte encoding

**TBD.** Still to be pinned down:

- the per-allocation metadata layout (the id tag, and whether size is stored or derived);
- the pointer encoding, including its width and its null representation;
- whether ids are dense small integers or something sparser;
- alignment requirements, if any.

kladde-rust currently uses a 32-bit pointer with a nonzero niche for `None` and the sizedness flag in the low bit, but neither the width nor the layout is fixed by this document yet.
The width in particular should probably be a per-file property rather than a format-wide constant.
