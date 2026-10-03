---
title: Allocations
---

What an allocation is, how it is named, how one refers to another, and what its bytes mean.

## Allocations

An **allocation** is a byte range that application data lives in.
It has:

- a **stable id**, assigned once, never changed, and never zero — which leaves zero available to encode a null pointer;
- a **size** in bytes;
- **content**, resolved from the [address table](address-table.md).

Nothing else.
An allocation is an untyped byte range; what its bytes mean is a question for the [schema](schema/) layer, and the storage layer never asks it.

An allocation is **not necessarily contiguous on file**.
Its content is described by whatever `Ref`, `Inline` and `Zero` statements currently win its offsets, and those may point into any number of data pages, in any order, with gaps that resolve to zero and occupy no data pages at all.

*Why not contiguous.*
Contiguity would have to be maintained against every write, which under copy-on-write means relocating an allocation whenever a byte in its middle changes.
Giving it up costs a lookup per read — served in memory, and paid only while loading or when a flush relocates bytes, since application reads go to the loaded values rather than to the file — and buys a flush that writes only what changed.

A **zero-sized allocation** is the natural endpoint of the rules rather than a special case: no content statements at all, and an on-file existence consisting of a single statement such as `Grow(id, 0)`.
Nothing may assume "at least one content statement": even a large allocation can have none, if it is entirely left to its default content of all-zero bytes.

There is no distinction between resizable and fixed-size allocations.
*Why not:* it existed in a [superseded design](../superseded/relocatable-heap.md) so that neighbours of a fixed-size allocation could rely on it not moving, and in this design nothing is adjacent to anything — every reshape is a statement edit.

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
The flush must instead emit statements that fully cover the new extent, which denies the old incarnation on its own — for example a bare `Zero(id, 0, n)` plus a `Shrink(id, n)` if the new allocation is smaller than the old one.

### Pointer encoding

**A pointer inside an allocation's bytes is the id it points at, and the schema says where the pointers are.**
A [`Pointer` descriptor](schema/type-descriptors.md#pointer) describes one: in a slotted place, the id in 4 bytes, little-endian; in a packed place, the unsigned LEB128 varint of the id, 1 to 5 bytes.
Zero, which is never an id, is the null pointer in either.
Pointers need no alignment.

The varint form is short because ids are dense: they are 32 bits, and a file's ids are about as many as its allocations, since an implementation [recycles](../impl/id-recycling.md) them, so a file of fewer than 16,384 allocations needs at most two bytes for any of its pointers.
A pointer changes size only when it changes at all: when a container first gets an allocation, gives it up, or is replaced.

A pointer inside an [opaque](schema/type-descriptors.md#opaque) type is encoded as that type's library decides, and is invisible to a tool.

## Ownership

In the current spec, every allocation has exactly **one owning pointer**.
This is an invariant of the format, not a convention: the value graph reachable from the root is a tree of ownership, and a tool may rely on that when walking it.

Reclamation follows ownership.
Dropping or overwriting an owning value releases the allocation it owns, recursively, in the same transaction as the write that superseded the pointer and after it.
Both halves matter.
*After*, because a [journal prefix](journal.md#ordering) that freed an allocation while something still pointed at it would be dangling; *in the same transaction*, because replay truncates at transaction boundaries, so no recovered state is ever left holding an allocation that is unreachable and unfreed.

A future design that allows aliasing for specific pointers is **TBD**.
Ideas include defining an `AliasingPointer` type in the schema that is completely separate from the normal owning pointer type, a `Reference` type that allows aliases to owning pointers but must not outlive them, or a combination: an `OwnedSharedPointer` that owns the allocation but may have additional `Reference`s while it is alive.
Its design constraints are that aliasing should be somehow restricted to a subtree of the data structure, and that generic tooling should be able to assume that common `Opaque` types that the tool knows about use pointers that no *other* type aliases.

## Content semantics

Every byte of an allocation that has not been explicitly written **reads as zero**.

Concretely: after an allocation is created, its bytes are zero.
After a resize, the first `min(old_size, new_size)` bytes are preserved and everything beyond reads as zero.
Two conforming implementations therefore return the same bytes for the same allocation of the same file.

Zeroing uninitialized bytes of allocations costs nothing to store — a range that resolves to zero by default occupies no data bytes — so this is a constraint on what a *reader* returns rather than on what a writer must write.
What it does constrain is the flush: a shrink immediately followed by a growth may not be elided outright, because the re-grown region must read as zero rather than as whatever survived.
In practice the shrink is [emitted regardless](../impl/liveness.md#emission-when-a-resize-must-write-a-statement) and the re-grown range then resolves through it, so the statements on disk are unchanged.

A consequence worth naming for data-type authors: a type whose zero bit-pattern is its natural default — an empty string, a null pointer, a `false` flag, a zero integer — needs no initialisation write at all.
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
