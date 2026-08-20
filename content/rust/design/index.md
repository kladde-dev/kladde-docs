---
title: Design
---

How kladde-rust is put together, and why.

These documents are for people working on the implementation, or porting it.
They are self-contained: they assume no familiarity with the code, and they explain the problems before the solutions.

## Reading order

The parts stack, and the stack is the reading order.

1. **[Architecture](#the-stack)** — this page: the parts, their roles, and what depends on what.
2. **[The heap](heap/)** — allocations, addresses, placement, and compaction.
   Type-agnostic; the largest and most algorithmically interesting part.
3. **[The journal](journal/)** — durability, and what a flush is allowed to do.
4. **[Persistence](persistence/)** — pointers, `Persistable`, guards, containers, the derive macro.
   This is the Rust-specific layer.
5. **[Schema](schema/)** — the Rust binding of the language-independent descriptor model.

## The stack

```
        ┌─────────────────────────────────────────────┐
        │  application code                           │
        └───────────────────┬─────────────────────────┘
                            │
        ┌───────────────────▼─────────────────────────┐
        │  kladde           root value, flush policy  │
        └───────────────────┬─────────────────────────┘
                            │
   ┌────────────────────────┼──────────────────┬──────────────┐
   │                        │                  │              │
┌──▼───────────┐  ┌─────────▼────────┐  ┌──────▼──────┐  ┌────▼─────────┐
│ kladde-types │  │  kladde-derive   │  │kladde-schema│  │kladde-varint │
│ containers   │  │  #[derive]       │  │ descriptors │  │  LEB128      │
└──┬───────────┘  └─────────┬────────┘  └──────┬──────┘  └────▲─────────┘
   │                        │                  └──────────────┘
   └────────────┬───────────┘
                │
   ┌────────────▼────────────────────────────────┐
   │  kladde-persist                             │
   │  Persistable, Guard, Location, PointerRepr  │
   └────────────┬────────────────────────────────┘
                │
   ┌────────────▼────────────────────────────────┐
   │  kladde-heap                                │
   │  Pointer, RelocatableHeap, Storage,         │
   │  Backend / ReadBackend / WriteBackend       │
   └─────────────────────────────────────────────┘
```

Dependencies point strictly downward.

## The parts

### `kladde-heap` — the relocatable persistent heap

Owns the address space.
It knows which allocation lives where, where the free space is, and how to squeeze it out.
It is **type-agnostic**: it has no notion of serialization, schema, or any kladde-specific concept, and depends on none of the other crates.

That independence is deliberate.
A relocatable persistent heap over stable ids is useful well beyond this library, and keeping the boundary clean means the layer above can be replaced without touching it — which is exactly what a port to another language would do.

It contains:

- **`Pointer`** — the stable id, with sizedness in a spare bit.
- **`RelocatableHeap`** — the geometry trait: allocate, free, resize, look up, and propose one bounded compaction step at a time.
- **`GainGreedyHeap`** — the implementation, with placement and incremental compaction.
- **`Storage`** — an unstructured, resizable, seekable byte interface.
- **`Backend`/`ReadBackend`/`WriteBackend`** — the trait split that composes a heap with a storage.

See [The heap](heap/) and [The journal](journal/).

### `kladde-persist` — the serialization layer

Where types meet bytes.
`Persistable`, its `Guard` counterpart, `Location`, and the on-file pointer encoding.

Knows about the heap; knows nothing about any particular container or derived type.

See [Persistence](persistence/).

### `kladde-schema` — type descriptors

The Rust implementation of the [language-independent schema model](../../spec/schema/): the descriptor types, the canonical encoding, and the fingerprint computation.

Depends only on `kladde-varint`.
It has no connection to the heap or to `Persistable` — deliberately, because it must be portable and testable on its own.
The binding between a Rust type and its descriptor lives one layer up.

See [Schema](schema/).

### `kladde-types` and `kladde-derive` — the type vocabulary

The built-in containers, and the macro that turns user types into backed ones.
Both build on `kladde-persist`.

See [Containers](persistence/containers.md) and [The derive macro](persistence/derive-macro.md).

### `kladde` — the entry point

The crate application code depends on: opening files, the root value, flush policy, and the default backend.

## The seam

For anyone porting kladde to another language, one boundary matters more than the rest.

**Below `kladde-persist`, the design is language-neutral.**
The heap, the journal, the placement and compaction algorithms, and the schema model all translate more or less directly.
They are shaped by the file format, not by Rust.

**At and above `kladde-persist`, the design is Rust-specific and should be redone.**
`Persistable`/`Guard` exists because Rust cannot intercept a field assignment.
The `&self` write / `&mut self` read split exists because Rust's borrow checker can then enforce "no reading stale data mid-write" for free.
`INLINE_SIZE` as an associated constant exists because Rust can compute field offsets at compile time.

A Python port should keep the heap almost verbatim and throw away the guards entirely.

## Cross-cutting concerns

Three things do not belong to any single part.

**Crash consistency** is a property of the journal and the fold, but every container must uphold the [ordering discipline](journal/crash-consistency.md) for it to hold.

**Freeing** is designed as a type-driven recursive hook, so it spans the persistence layer and the heap.
See [Freeing](persistence/freeing.md).

**Schema evolution** touches the load path, the mutation path, and the fold, because a value read at a foreign layout must not then be mutated at native offsets.
See [the specification](../../spec/schema/evolution.md).

## Current state

| part | state |
| --- | --- |
| Heap geometry, placement | implemented |
| Incremental compaction | implemented, measured, tuned |
| Storage and backends | implemented (in-memory only) |
| Journal | **known broken**, redesign documented |
| `Persistable`, guards, `Location` | implemented |
| Containers | implemented |
| Derive macro | implemented for non-generic types |
| Schema descriptors, encoding, fingerprints | implemented |
| Schema binding to Rust types | implemented |
| Real file storage | not started |
| Freeing | not started |
| Schema evolution | not started |
