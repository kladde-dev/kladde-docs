---
title: kladde-rust
---

The Rust implementation of kladde, and the reference implementation of the [specification](../spec/).

## Two audiences

**If you want to use kladde in an application**, start with the [tutorial](tutorial/).
It assumes you know Rust and nothing about kladde, and it works up from a first program to writing your own persistable types.

**If you want to work on kladde**, or port it to another language, start with the [design documents](design/).
They go top-down: the [architecture overview](design/) first, then a document per part covering the problem it solves, the algorithm, and the data structures.

## What it is

A Rust workspace providing backed data structures: containers and derived types whose mutations are recorded durably as they happen.

```rust
use kladde::Kladde;
use kladde_types::{Persistable, PersistableString, PersistableVec};

#[derive(Persistable)]
struct Notes {
    title: PersistableString,
    lines: PersistableVec<PersistableString>,
}

let mut notes = Kladde::new(Notes {
    title: PersistableString::from("today"),
    lines: PersistableVec::new(),
});

notes.guard().lines_mut().push(PersistableString::from("wrote a doc"));
```

The `push` updates the in-memory vector and records the change.
There is no save call.

## The shape of the implementation

Rust has no way to intercept a field assignment — no `__setattr__`, no property hooks, no message dispatch.
Every other system in this space relies on exactly such a hook to make persistence syntactically invisible.

So kladde-rust compensates with an explicit **guard**: a short-lived RAII wrapper, obtained from a value plus a backend, through which mutations are made.
`notes.guard().lines_mut()` walks from the root to the field being changed, carrying the backend and the field's location along with it.
The guard is what other languages get for free from their object systems.

This is the single largest thing that a port should expect to redesign.
A Python implementation should use property hooks and have no guards at all; the layer beneath — the heap, the journal, the schema — should port far more directly.
See [Architecture](design/) for exactly where that seam falls.

## Crate map

| crate | role |
| --- | --- |
| `kladde-heap` | the relocatable heap: pointers, allocations, placement, compaction, the journal, and the storage backends. Type-agnostic; knows nothing about serialization. |
| `kladde-persist` | the serialization layer: `Persistable`, `Location`, the pointer byte encoding, and the typed conveniences on top of a backend. |
| `kladde-schema` | type descriptors, their canonical encoding, and fingerprints. Depends on nothing but `kladde-varint`. |
| `kladde-varint` | LEB128 varints. |
| `kladde-types` | the built-in containers: vector, hash map, string, blob. The default collection, not a layer — it uses only the public API any library could. |
| `kladde-derive` | the `#[derive(Persistable)]` macro. |
| `kladde` | the application-facing entry point: opening files, the root value, the default backend. |

The dependency direction is strictly downward, and `kladde-heap` deliberately depends on none of the others — it is usable on its own as a relocatable persistent heap, independently of anything kladde-specific.

## Status

Not feature-complete, and the parts are at very different stages.

| area | status |
| --- | --- |
| Heap, placement, incremental compaction | implemented and measured |
| Backends, storage abstraction | implemented |
| Schema descriptors, encoding, fingerprints | implemented and specified |
| Containers, derive macro, guards | implemented against an in-memory backend |
| Journal | **known broken** — see [Journal semantics](design/journal/semantics.md) |
| Real file storage | not started |
| Crash consistency | designed, not built |
| Schema evolution | designed, not built |
| Freeing / reclamation | designed, not built |

The design documents describe the system as intended, and mark where the implementation currently falls short.
