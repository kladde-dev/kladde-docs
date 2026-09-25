---
title: kladde-rust
---

The Rust implementation of kladde, and the reference implementation of the [specification](../spec/).

Everything here is **specific to Rust** and should be expected to differ in a port.
The [file format](../spec/) and the [algorithms](../impl/) are shared; the trait shapes, the mutation-capture mechanism, the macros, and the memory layout are not.

## Two audiences

**If you want to use kladde in an application**, start with the [tutorial](tutorial/).
It assumes you know Rust and nothing about kladde, and works up from a first program to writing your own persistable types.

**If you want to work on kladde**, or port it, read the [specification](../spec/) and [implementation notes](../impl/) first — they carry the parts that are not about Rust — and come back here for the rest.

## What it is

A Rust workspace providing backed data structures: containers and derived types whose mutations are recorded durably as they happen.

<!-- kladde-example: name=notes file=src/main.rs mode=run deps=kladde,kladde-types
before:
  fn main() -> kladde::Result<()> {
after:
  Ok(())
  }
-->
```rust
use kladde::Kladde;
use kladde::Persistable;
use kladde_types::{PersistableString, PersistableVec};

#[derive(Persistable)]
struct Notes {
    title: PersistableString,
    lines: PersistableVec<PersistableString>,
}

let mut notes = Kladde::new(Notes {
    title: PersistableString::from("today"),
    lines: PersistableVec::new(),
});

notes.guard().lines_mut().push(PersistableString::from("wrote a doc"))?;
```

The `push` updates the in-memory vector and appends the change to the journal before it returns.
There is no save call.
`Kladde::new` keeps its file in memory, which suits examples and tests; `Kladde::create` and `Kladde::open` do the same on a real file.

## The central problem

Rust has no way to intercept a field assignment.

Every comparable system relies on exactly such a hook: Python has `__setattr__`, Smalltalk has message dispatch, Swift has property wrappers.
Those systems make persistence syntactically invisible — `self.foo = bar` just works — because the language hands them a place to stand.

Rust hands them nothing.
So kladde-rust makes the recording point **explicit and unforgeable** instead: the only mutating API is the one that records, and it is reached through a `Guard`.

Everything idiosyncratic about this layer follows from that one constraint, and it is the single largest thing a port should expect to redesign.
A Python implementation should use property hooks and have no guards at all.

## The documents

| document | contents |
| --- | --- |
| [Crate layout](crates.md) | the workspace, the dependency direction, and where the seam falls |
| [Pointers](pointers.md) | the two handle types, and the ownership discipline they enforce |
| [Persistable and guards](persistable-and-guards.md) | the central traits, why mutation needs a guard, and why it returns a `Result` |
| [Containers](containers.md) | how the built-in containers are laid out and why |
| [The derive macro](derive-macro.md) | what it generates, and why guards are generated per type |
| [Freeing](freeing.md) | the `free` hook, and where ownership ends |
| [Transactions](transactions.md) | the RAII types, and what happens on unwind |
| [Schema binding](schema-binding.md) | how a Rust type declares its descriptor, and the check at open |
| [The store](store.md) | the Rust-specific half of the storage layer: interior mutability, I/O, and the in-memory structures |
| [Tutorial](tutorial/) | for application authors |

## Status

A first prototype of the whole design exists; nothing is frozen.
Where the implementation departs from the [implementation notes](../impl/), or found them unclear, `implementation-notes.md` in the kladde-rust repository says so.

| area | status |
| --- | --- |
| Schema descriptors, encoding, fingerprints | implemented and specified |
| Schema binding to Rust types, fingerprint check at open | implemented |
| Containers, derive macro, guards | implemented |
| File format, address table, journal, recovery | implemented |
| Flush and consolidation | implemented |
| Transactions and batches | implemented, without the unwind behaviour |
| Freeing | implemented as a type-driven hook |
| Schema evolution | designed, not built |
