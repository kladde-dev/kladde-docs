---
title: The built-in containers
---

`kladde-types` provides four backed containers.
They are hand-implemented against the persistence layer rather than derived, the same way `std`'s collections hand-write raw pointer manipulation internally.

## A default library, not a privileged one

`kladde-types` is not part of the machinery, and nothing depends on it.
It is a collection of types most applications turn out to want, offered so that you do not have to write them yourself.

Everything in it is built on the same public `Persistable`/`Guard` surface available to any crate.
There is no internal kladde magic here that a third-party library — or your own application — could not use equally well, and third-party libraries of general-purpose backed types are welcome.

## `PersistableVec<T>`

A growable sequence.
`T` must be `Persistable`, and nothing more — no `Clone`, no `Serialize`.

<!-- kladde-example: name=containers file=src/vec.rs deps=kladde,kladde-types
before:
  use kladde::{Kladde, Persistable};
  use kladde_types::{PersistableString, PersistableVec};
  #[derive(Persistable)]
  struct Journal {
      entries: PersistableVec<PersistableString>,
  }
  fn demo(journal: &mut Kladde<Journal>) -> kladde::Result<()> {
after:
  Ok(())
  }
-->
```rust
let mut guard = journal.guard();
let mut v = guard.entries_mut();
v.push(PersistableString::from("a"))?;
v.push(PersistableString::from("b"))?;
let first = v.remove(0)?;   // yours now, allocations and all
v.push(first)?;             // so it moves without copying its bytes
v.delete(0)?;               // removes and frees
```

**Layout.** A pointer inline, plus a separate allocation holding a dense array of fixed-size element slots — the same shape as `Vec<T>`'s in memory.
The length is that allocation's size divided by the element size, so it is never stored separately.

**Cost.** A push is one write, a pop one resize, and a removal from the middle one splice, which shifts the tail exactly as `Vec` does.

**Removal.** `remove` and `pop` hand the element back with everything it owns, so you can store it elsewhere; `delete` and `clear` free it.
An element you take out and then drop leaks its allocations in the file, the way a value passed to `std::mem::forget` leaks its memory.

## `PersistableString`

Growable text.

<!-- kladde-example: name=containers file=src/string.rs
before:
  use kladde::{Kladde, Persistable};
  use kladde_types::PersistableString;
  #[derive(Persistable)]
  struct Journal {
      owner: PersistableString,
  }
  fn demo(journal: &mut Kladde<Journal>) -> kladde::Result<()> {
  let mut guard = journal.guard();
after:
  Ok(())
  }
-->
```rust
let mut owner = guard.owner_mut();
owner.set("ada")?;
owner.push_str(" lovelace")?;
```

It is a thin wrapper around `PersistableVec<u8>`, which is more interesting than it sounds.
Plain `String` deliberately has **no** `Persistable` implementation, and that absence is the enforcement mechanism: a struct field typed `String` simply fails to compile, rather than silently persisting nothing.
`PersistableString` exists because a backed string needs a field of its own to remember which allocation holds its bytes, and `String` has nowhere to put one.

So this does not compile, and that is the point:

<!-- kladde-example: name=plain-string-is-rejected file=src/lib.rs mode=compile_fail deps=kladde
before:
  use kladde::Persistable;
-->
```rust
#[derive(Persistable)]
struct Journal {
    owner: String,  // the trait bound `String: Persistable<_>` is not satisfied
}
```

## `PersistableHashMap<K, V>`

A key-value map.
`K: Eq + Hash + Persistable`, `V: Persistable`.

<!-- kladde-example: name=containers file=src/map.rs
before:
  use kladde::{Kladde, Persistable};
  use kladde_types::{PersistableHashMap, PersistableString};
  #[derive(Persistable)]
  struct Contact {
      email: PersistableString,
  }
  #[derive(Persistable)]
  struct Book {
      contacts: PersistableHashMap<PersistableString, Contact>,
  }
  fn demo(book: &mut Kladde<Book>, contact: Contact) -> kladde::Result<()> {
  let mut guard = book.guard();
after:
  Ok(())
  }
-->
```rust
let mut contacts = guard.contacts_mut();
contacts.insert(PersistableString::from("ada"), contact)?;
```

**Layout.** An array of fixed-size slots, each a one-byte liveness tag followed by a `(K, V)` pair.
The on-disk form does *not* support key lookup at all — that is what the in-memory map is for — so it needs no hashing structure on disk.

**Removal is tombstoning**: the tag is cleared in place and nothing else moves, and a later insertion reuses the slot.
This is what lets each key be stored exactly once in memory, and is why `K: Clone` is not required.

**Keys are immutable.**
No guard is ever handed out for a key; `Persistable` on `K` governs only how its bytes are read and written.

## `PersistableBlob<T>`

An escape hatch for foreign types.

<!-- kladde-example: name=containers file=src/blob.rs deps=kladde-types[serde],serde
before:
  use kladde::Persistable;
  use kladde_types::PersistableBlob;
  use serde::{Deserialize, Serialize};
  #[derive(Default, Serialize, Deserialize)]
  struct WindowGeometry {
      width: u32,
      height: u32,
  }
-->
```rust
#[derive(Persistable)]
struct Config {
    window: PersistableBlob<WindowGeometry>,  // a foreign serde type
}
```

Wraps any `T: Serialize + DeserializeOwned + Default` as an opaque, serialized payload with its own allocation.
Use it when a type is not yours to change and cannot get its own `Persistable` implementation.

**It is deliberately the worst option.**
A blob is rewritten in full on every change, which forfeits kladde's central advantage; it is opaque to [tooling](../../spec/tooling.md); and it drags in a serialization dependency.
Prefer a derived type or a hand-written implementation whenever you can.
It is gated behind `kladde-types`' `serde` feature — the only thing in the workspace that needs `serde` at all.

## What is missing

A **rope**, for large text with efficient middle-insertion, is designed but not built.

There is no backed `Option`, `Box`, or tuple as such — a derived enum covers `Option`'s role, and a derived struct covers a tuple's.

## Choosing

| you have | use |
| --- | --- |
| a sequence | `PersistableVec<T>` |
| text | `PersistableString` |
| a lookup table | `PersistableHashMap<K, V>` |
| your own struct or enum | [`#[derive(Persistable)]`](deriving.md) |
| a foreign type you cannot change | `PersistableBlob<T>`, reluctantly |
| a foreign type whose layout you know | [a hand-written impl](custom-persistable.md) |
