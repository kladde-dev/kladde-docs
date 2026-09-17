---
title: The relocatable heap
---

**Superseded.** Replaced by [pages and the address table](../spec/address-table.md).

A partition of a flat address space over stable ids, which could close its own gaps one bounded step at a time.
Implemented, measured, and tuned; the description below is of a system that no longer exists.

## What it was

The snapshot was a **heap**: a partition of `[0, end)` into allocations — contiguous byte ranges, each tagged with the stable id naming it — and **gaps**, the free space between them.
Gaps were not represented on disk at all: the set of gaps is derivable from the set of live allocations, which is derivable from the per-allocation id tags, so nothing needed to record them.
Opening a file rebuilt the `id → address` table by **scanning** live allocations.

An allocation therefore had an **address** in addition to its id and size — its current offset in the file, which could change over the file's lifetime.

## The trait

```rust
pub trait RelocatableHeap {
    type Id: AllocationId;
    type Address: Word;
    type Size: Word + Into<Self::Address>;

    fn alloc(&mut self, id: Self::Id, size: Self::Size) -> Result<Self::Address, HeapError>;
    fn free(&mut self, id: Self::Id) -> Result<(), HeapError>;
    fn resize(&mut self, id: Self::Id, new_size: Self::Size)
        -> Result<Relocation<Self::Address, Self::Size>, HeapError>;

    fn lookup(&self, id: Self::Id) -> Option<(Self::Address, Self::Size)>;
    fn len(&self) -> Self::Address;
    fn live_bytes(&self) -> Self::Address;
    fn iter(&self) -> impl Iterator<Item = (Self::Id, Self::Address, Self::Size)> + '_;

    fn propose_compaction_step(&self, budget: Self::Address) -> Option<Step<Self::Address>>;
    fn commit_compaction_step(&mut self, step: Step<Self::Address>);
}
```

### The heap owned the id table

The naive division of labour would put free-space management in an "allocator" and the `id → address` table in the layer above.
That is wrong, and the reason was **compaction**: choosing a good move requires simultaneous, index-grade access to *both* the geometry — where are the gaps, what is adjacent to what — and the id mapping.
Split them and the compactor has to interrogate its caller in a loop, or keep a shadow copy that can drift.

> The component that decides where things go should own that table rather than shadow it.

A bad id was therefore the *heap's* error, which it would not be for a free-space-only allocator.

**This argument survives the supersession**, in changed form: the [fragment map](../impl/in-memory-state.md#1-the-fragment-map) is still the single structure that both resolution and consolidation read, for the same reason.

### Ids came from outside

The heap never minted ids; the caller did.
Id minting is coupled to *persistence* — an id has to be a stable identity from the moment anything might serialize it — whereas placement is coupled to geometry.

### A step was a byte range, not an allocation

```rust
pub struct Step<A> { pub from: A, pub to: A, pub len: A }
```

Deliberately id-free, so that one step could describe a whole **run** of neighbouring allocations sliding together — the natural unit for I/O, since the fixed per-operation cost is paid once per run rather than once per allocation.
`to < from` always, so a source/destination overlap is a downward move and can be done with a forward copy.

### `budget` was a ranking input, not a cap

`propose_compaction_step` could return a step costing **more** than the budget when no worthwhile cheaper step existed.
Otherwise a heap whose only useful move is one large slide would claim quiescence and never compact at all.

**This idea survives** in the [consolidation budget](../impl/consolidation.md#pacing), for the same reason.

## Sizedness

Every id recorded whether the allocation it named was **fixed-size** or **resizable**, carried in the low bit of the raw pointer value:

```
raw = (counter << 1) | fixed_bit
```

The counter pool was **shared** between the two sizednesses, so the counter alone was unique and ids stayed dense at roughly twice the counter — which mattered for any table indexing by id.
A high bit would have been the alternative and is much worse: it doubles the apparent id range and is fatal to density.

It was **placement-relevant**, which is why it had to be visible to the heap: fixed-size allocations are minted in bulk at a handful of distinct sizes, so they form dense size classes worth indexing, while resizable ones scatter across many sizes and would have to move again on the next growth.

Two consequences followed:

- a sizedness conversion could not re-tag in place; it had to mint a **new** id, copy the content, and free the old one — which is why the journal carried a `Convert(old_id, new_id, size)` record;
- the Rust API needed two owned handle types, `UniquePointerResizable` and `UniquePointerFixedSize`, differing only in which operations they permitted, as a typestate.

**Why it is gone.** The distinction existed so that neighbours of a fixed-size allocation could rely on it not moving.
Under the page-oriented design nothing is adjacent to anything — every reshape is a statement edit — so it buys nothing.
The `Convert` record, the second handle type, and the low pointer bit all went with it.

## Errors and composition

```rust
pub enum HeapError {
    OutOfMemory,   // no free range large enough, and the space cannot extend
    UnknownId,     // freed, or never allocated
    DuplicateId,   // alloc with an id that is already live
}
```

`OutOfMemory` arose only when the address width was exhausted, not when the heap was merely full, because extending the address space at `end` was always available and always correct.

A concrete backend was a `Composed(Storage, RelocatableHeap, id pool)`: the heap owned geometry and the table; the backend owned id minting, storage I/O, and the journal.
There was no second in-memory table anywhere.

A marker trait `IncrementallyCompactableHeap` gated the *user-facing* compaction controls — the methods themselves were defaulted on the base trait so a shared flush path could call them without a bound, with a non-compacting heap simply proposing nothing.
Compare `ExactSizeIterator`, which marks `size_hint` as meaning something.

## Placement

**Placement was compaction with the copy pre-paid.**

An evacuation moves an existing allocation into the lowest gap that fits it, paying `size` bytes of copying.
A placement puts a *new* allocation into the lowest gap that fits it, and the bytes were going to be written regardless.
So placement was free compaction, using the same index and the same query: `lowest_gap_fitting(min_len) -> Option<(Address, Size)>`, answered in `O(log n)`.

Best-fit by **address**, not by size: filling from the bottom keeps the compact prefix growing, whereas filling the tightest gap wherever it happens to be scatters live bytes above the frontier and leaves work for later.

**Deferred placement.**
The journaled backend deferred placement to flush time rather than assigning an address when the allocation was minted, because a flush knows the *whole* batch of pending allocations and can sort them **first-fit-decreasing** — serving the large ones while the large gaps are still intact.
Sorting also made the layout independent of hash-map iteration order, which would otherwise make the layout, and therefore how much work compaction later had to do, unreproducible from run to run.

**Both of these survive the supersession**, in the [flush's placement phase](../impl/flush.md#phase-b--placement): first-fit-decreasing over the whole batch, and the same caveat that it becomes best-effort over the ready set once dependencies block.

## `resize` could ask for two moves

```rust
pub enum Relocation<A, S> {
    None,
    Single { old: A, new: A },
    Double { first: (A, A), then: (A, A), then_len: S },
}
```

`Double` existed for the *lift*, described in [Incremental compaction](incremental-compaction.md#remedy-2-the-lift): a shrinking allocation is moved up and a replacement is moved down into the space it vacated.
The replacement's destination *is* the mover's own old address, so the two copies had to happen in the order given, and `then_len` was carried because the second move is of a *different* allocation, one the caller never asked about and whose size it therefore could not look up.
