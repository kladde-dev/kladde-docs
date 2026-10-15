---
title: kladde-rs
---

The Rust implementation of kladde: one of the two reference implementations of the [specification](../spec/), with [kladde-cpp](../cpp/) in C++.

Everything here is **specific to Rust** and should be expected to differ in a port.
The [file format](../spec/) and the [algorithms](../impl/) are shared; the trait shapes, the mutation-capture mechanism, the macros, and the memory layout are not.

## Two audiences

**If you want to use kladde in an application**, start with the [tutorial](tutorial/).
It assumes you know Rust and nothing about kladde, and works up from a first program to writing your own durable types.

**If you want to work on kladde**, or port it, read the [specification](../spec/) and [implementation notes](../impl/) first — they carry the parts that are not about Rust — and come back here for the rest.

## What it is

A Rust workspace providing durable data structures: containers and derived types whose mutations are recorded durably as they happen.

<!-- kladde-example: name=notes file=src/main.rs mode=run deps=kladde,kladde-types
before:
  fn main() -> kladde::Result<()> {
after:
  Ok(())
  }
-->
```rust
use kladde::Kladde;
use kladde::Durable;
use kladde_types::DurableVec;

#[derive(Durable)]
struct Notes {
    title: String,
    lines: DurableVec<String>,
}

let mut notes = Kladde::new(Notes {
    title: String::from("today"),
    lines: DurableVec::new(),
});

notes.get_mut().lines_mut().push(String::from("wrote a doc"))?;
```

The `push` updates the in-memory vector and appends the change to the journal before it returns.
There is no save call.
`Kladde::new` keeps its file in memory, which suits examples and tests; `Kladde::create` and `Kladde::open` do the same on a real file.

## The central problem

Rust has no way to intercept a field assignment.

Every comparable system relies on exactly such a hook: Python has `__setattr__`, Smalltalk has message dispatch, Swift has property wrappers.
Those systems make persistence syntactically invisible — `self.foo = bar` just works — because the language hands them a place to stand.

Rust hands them nothing.
So kladde-rs makes the recording point **explicit and unforgeable** instead: the only mutating API is the one that records, and it is reached through a `Mut`.

Everything idiosyncratic about this layer follows from that one constraint, and it is the single largest thing a port should expect to redesign.
A Python implementation should use property hooks and have no `Mut`s at all.

## The documents

| document | contents |
| --- | --- |
| [Crate layout](crates.md) | the workspace, the dependency direction, and where the seam falls |
| [Durable and Mut](durable-and-mut.md) | the central traits, regions, `Fixed`, why mutation needs a `Mut`, and why it returns a `Result` |
| [Containers](containers.md) | how the built-in containers are encoded and indexed, and cuts |
| [The derive macro](derive-macro.md) | what it generates, fixed forms, enums in place of trait objects, and why `Mut`s are generated per type |
| [Transactions](transactions.md) | the RAII types, and what happens on unwind |
| [Schema binding](schema-binding.md) | how a Rust type declares its descriptor, and the check at open |
| [The store](store.md) | the Rust-specific half of the storage layer: interior mutability, I/O, and the in-memory structures |
| [Tutorial](tutorial/) | for application authors |

## Status

A prototype of the whole design exists; nothing is frozen.
Where the implementation departs from the [reference algorithms](../impl/), or found them unclear, its [implementation notes](../impl/implementation-notes.md) say so.

| area | status |
| --- | --- |
| Schema descriptors, encoding, fingerprints, rules of nesting | implemented and specified |
| The encoding of values; schema binding to Rust types; fingerprint check at open | implemented |
| Containers, derive macro, `Mut`s, regions, `Fixed` | implemented |
| File format, span table, journal, recovery | implemented, with a span table of [two levels](../impl/implementation-notes.md) |
| Flush, span policy and consolidation | implemented |
| Transactions, batches, and cuts | implemented; cuts on the vector only |
| The private chain | written and read, but nothing kept in it |
| Schema evolution | designed, not built |
