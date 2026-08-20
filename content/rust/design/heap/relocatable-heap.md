---
title: The relocatable heap
---

The core abstraction: a partition of the address space over stable ids, which can close its own gaps one bounded step at a time.

## The problem

A conventional allocator hands out addresses and forgets about them.
The caller holds the address, and the allocator's only job is to track free space.

Kladde cannot work that way, because its pointers are **stable ids**.
Something must map an id to its current address, and that something must be updated whenever an allocation moves.

The naive division of labour puts the free-space management in an "allocator" and the `id → address` table in the layer above.
That is wrong, and the reason is compaction: choosing a good compaction move requires simultaneous, index-grade access to *both* the geometry (where are the gaps? what is adjacent to what?) and the id mapping (which allocation is this, and what is its size?).
Split them and the compactor has to interrogate its caller in a loop, or keep a shadow copy that can drift.

So: **the heap owns the table.**
It manages the whole partition of `[0, end)` into allocations and gaps, keyed by id, and the layer above holds no geometry at all.

## The trait

```rust
pub trait RelocatableHeap {
    type Id: AllocationId;
    type Address: Word;
    type Size: Word + Into<Self::Address>;

    fn alloc(&mut self, id: Self::Id, size: Self::Size)
        -> Result<Self::Address, HeapError>;
    fn free(&mut self, id: Self::Id) -> Result<(), HeapError>;
    fn resize(&mut self, id: Self::Id, new_size: Self::Size)
        -> Result<Relocation<Self::Address, Self::Size>, HeapError>;

    fn lookup(&self, id: Self::Id) -> Option<(Self::Address, Self::Size)>;
    fn len(&self) -> Self::Address;
    fn live_bytes(&self) -> Self::Address;
    fn iter(&self) -> impl Iterator<Item = (Self::Id, Self::Address, Self::Size)> + '_;

    fn propose_compaction_step(&self, budget: Self::Address)
        -> Option<Step<Self::Address>>;
    fn commit_compaction_step(&mut self, step: Step<Self::Address>);
}
```

Four things about this are worth explaining.

### Ids come from outside

The heap never mints ids.
The caller does, and hands them in.

This matters because id minting is coupled to *persistence* — an id has to be a stable identity from the moment anything might serialize it, and recycling one has consequences for the journal — whereas placement is coupled to geometry.
Keeping them separate lets each side own what it actually constrains.

The heap asks an id exactly one question:

```rust
pub trait AllocationId: Copy + Eq + Hash {
    fn is_fixed_size(&self) -> bool;
}
```

Sizedness rides on the id rather than being stored per allocation, because it is the only per-allocation fact the heap has ever needed.
A separate metadata channel would carry this one bit and nothing else.

It is placement-relevant: fixed-size allocations are minted in bulk at a handful of sizes, so they form dense size classes worth indexing, while resizable ones scatter and would have to move again on the next growth.

### A step is a byte range, not an allocation

```rust
pub struct Step<A> {
    pub from: A,
    pub to: A,
    pub len: A,
}
```

Deliberately **id-free**.
A step is a contiguous byte range, which is what lets one step describe a whole **run** of neighbouring allocations sliding together.

That is the natural unit for I/O: the fixed per-operation cost is paid once per run rather than once per allocation.
Moving a single allocation is just the one-element case.

`to < from` always, so a source/destination overlap — which happens whenever the gap is smaller than the run — is a *downward* move, and can be done with a forward copy.

The caller copies the bytes and hands the step back to `commit_compaction_step`, which re-keys every allocation in the range.

### `budget` is a ranking input, not a cap

`propose_compaction_step` may return a step costing **more** than `budget` when no worthwhile cheaper step exists.

Otherwise a heap whose only useful move is one large slide would claim quiescence and never compact at all.
The caller can price the returned step itself and decide whether to run it, chunk it, or drop it.

### `resize` can ask for two moves

```rust
pub enum Relocation<A, S> {
    None,
    Single { old: A, new: A },
    Double { first: (A, A), then: (A, A), then_len: S },
}
```

`Double` exists for the *lift* — see [Pathologies](pathologies.md).
A shrinking allocation is moved up and a replacement is moved down into the space it vacated.
The replacement's destination *is* the mover's own old address, so the two copies must happen in the order given.

`then_len` is carried because the second move is of a *different* allocation, one the caller never asked about and whose size it therefore cannot look up.

## Errors

```rust
pub enum HeapError {
    OutOfMemory,   // no free range large enough, and the space cannot extend
    UnknownId,     // freed, or never allocated
    DuplicateId,   // alloc with an id that is already live
}
```

A bad id is the *heap's* error, which it would not be for a free-space-only allocator.
This is a direct consequence of the heap owning the table.

## The marker trait

```rust
pub trait IncrementallyCompactableHeap: RelocatableHeap {}
```

The compaction methods are defaulted on the base trait so a shared flush path can call them without a bound — a heap that does not compact proposes nothing, and the call is free.
The marker is what gates the *user-facing* compaction controls, which should not exist at all on a backend whose heap does not compact.

Compare `ExactSizeIterator`, which marks `size_hint` as meaning something.

## Composition

A concrete backend is a `Composed(Storage, RelocatableHeap, id pool)`.
The heap owns geometry and the table; the backend owns id minting, storage I/O, and the journal.
There is no second in-memory table anywhere.

The backend trait split — `Backend` for shared type carriers and id queries, `ReadBackend` for stored-byte reads, `WriteBackend` for writes and allocation — is described in [Persistence](../persistence/).
