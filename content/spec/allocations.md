---
title: Allocations
---

What an allocation is, how it is named, how one refers to another, and what its bytes mean.

## Allocations

An **allocation** is a byte range that application data lives in.
It has:

- a **stable id**, assigned once and never changed;
- a **size** in bytes;
- **content**, resolved from the [address table](address-table.md).

Nothing else.
An allocation is an untyped byte range; what its bytes mean is a question for the [schema](schema/) layer, and the storage layer never asks it.

An allocation is **not contiguous on file**.
Its content is described by whatever `Ref`, `Inline` and `Zero` statements currently win its offsets, and those may point into any number of data pages, in any order, with gaps that resolve to zero and occupy no data pages at all.

*Why not contiguous.*
Contiguity would have to be maintained against every write, which under copy-on-write means relocating an allocation whenever a byte in its middle changes.
Giving it up costs a lookup per read — served in memory — and buys a flush that writes only what changed.

A **zero-sized allocation** is the natural endpoint of the rules rather than a special case: no content statements at all, and an on-file existence consisting of a single `Grow(id, 0)`.
Nothing may assume "at least one content statement": even a large allocation can have none, if it is entirely uninitialized.

There is no distinction between resizable and fixed-size allocations.
*Why not:* it existed so that neighbours of a fixed-size allocation could rely on it not moving, and in this design nothing is adjacent to anything — every reshape is a statement edit.

## Stable ids, and why pointers are not addresses

A pointer stored inside an allocation is serialized as the **stable id** of the allocation it points at — never as an address.

This is the most consequential decision in the format, and it is what makes relocation cheap.
When an allocation's bytes move, the bytes of every pointer that targets it are unchanged, because those bytes name an id, not a location.
The mover writes new address-table statements and copies the bytes; it never has to find, interpret, or rewrite a pointer.
It does not even need to know which of an allocation's bytes *are* pointers, which means it can relocate allocations whose contents it cannot parse.

The cost is one level of indirection on every dereference, and the need to reconstruct the id-to-content mapping when a file is opened.
That reconstruction is the bulk read: the address table is resolved once at open into an in-memory index, and epochs are never consulted again at run time.

An implementation is required to preserve ids across a flush and across consolidation.
It is *not* required to assign them in any particular way, or to reuse them in any particular order — though [the reference implementation's order](../impl/id-recycling.md) is chosen for good reasons.

### Id recycling

An id becomes reusable as soon as a `Tombstone` for it is committed.
It does **not** have to wait for the tombstone to die or for the old incarnation's statements to be physically removed.

This is safe because the tombstone matches every probe and outranks every statement below it: the new incarnation's statements are written above it, so the old incarnation can never win a probe, no matter how much of it survives physically.

One case needs no tombstone at all: freeing and re-allocating an id **within a single flush**.
A tombstone and the new incarnation's statements would make contradicting existence claims in one epoch, which the [no-conflicts rule](address-table.md#no-conflicts-within-each-epoch) forbids.
The flush must instead emit statements that fully cover the new extent, which denies the old incarnation on its own — a bare `Zero(id, 0, n)` plus a `Shrink(id, n)` if the new allocation is smaller than the old one.

### Pointer encoding

How a pointer is represented **inside an allocation's bytes** is a separate question from the [address table](address-table.md), and is **TBD**:

- the width — the table fixes ids at 32 bit, but whether the in-allocation representation is always four bytes, or a per-file property, is not decided;
- the **null representation**, and whether it is a reserved id value or a separate tag;
- alignment requirements, if any.

The reference implementation uses a 32-bit value with zero reserved for null, which gives an optional pointer the null niche for free in memory as well as on file.
That is [an implementation choice](../rust/pointers.md#why-exactly-one-copyable-type) until this section says otherwise.

## Ownership

Every allocation has exactly **one owning pointer**.
This is an invariant of the format, not a convention: the value graph reachable from the root is a tree of ownership, and a tool may rely on that when walking it.

Non-owning references — pointers that may target an interior offset of an allocation rather than its start, and that may alias — are **reserved but not specified**.
They interact with liveness in ways that are not settled: a reference must not outlive the allocation it points into, and nothing currently enforces that.
**TBD.**

Reclamation follows ownership.
Dropping or overwriting an owning value releases the allocation it owns, recursively, before the pointer to it becomes unreachable.
The ordering matters for crash consistency: nothing is freed until whatever superseded it is already published.
See [Journal](journal.md#ordering).

## Content semantics

Every byte of an allocation that has not been explicitly written **reads as zero**.

Concretely: after an allocation is created, its bytes are zero.
After a resize, the first `min(old_size, new_size)` bytes are preserved and everything beyond reads as zero.
Every conforming implementation returns the same bytes for the same file; there is no latitude here.

Zero costs nothing to store — a range that resolves to zero by default occupies no data bytes — so this is a constraint on what a *reader* returns rather than on what a writer must write.
What it does constrain is the flush: a shrink immediately followed by a growth may no longer be elided outright, because the re-grown region must read as zero rather than as whatever survived.
In practice the shrink is [emitted regardless](../impl/liveness.md#emission-when-a-resize-must-write-a-statement) and the re-grown range then resolves through it, so the statements on disk are unchanged; what is lost is the narrow case where the fold could previously emit nothing at all.

A consequence worth naming for data-type authors: a type whose zero bit-pattern is its natural default — an empty string, a null pointer, a false flag, a zero integer — needs no initialisation write at all.
A vector of a thousand empty strings is one `Grow`.

## Free space

Free space is not represented on disk, and neither is liveness.

A page is live if it is reachable from a committed header; content within a page is live if some current statement still refers to it.
Both are derived, and neither is recorded.
See [page states](durability.md#page-states).

An implementation's in-memory accounting of how much of each page is still relied upon — what drives its consolidation policy — is entirely its own business, and nothing about it is persisted.
See [Liveness accounting](../impl/liveness.md).

## Reclamation is not part of this specification

Whether an implementation reclaims garbage, when it does so, and which pages it chooses are unconstrained.

This is safe for the same reason relocation is: a consolidation step rewrites content into fresh pages and emits address-table statements describing where it went, producing a file indistinguishable from one that happened to be laid out that way from the start.

Two implementations with wildly different policies therefore produce files that are equally valid and mutually readable.
An implementation that never consolidates is conforming; it will simply accumulate garbage until it runs out of space, which is a quality-of-implementation failure rather than a correctness one.

The one thing that *is* required is that reclamation be **externally invisible**: it must not change any allocation's id, size, or content.
