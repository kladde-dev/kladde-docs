---
title: The built-in containers
---

`kladde-types` provides four durable containers.
They are hand-implemented against the persistence layer rather than derived, since each keeps an index of its own in memory.
Every container's content lies inline in the file's one sequence of bytes, wherever its value lies, so a short list costs no more on file than its elements and its count.

## A default library, not a privileged one

`kladde-types` is not part of the machinery, and nothing depends on it.
It is a collection of types most applications turn out to want, offered so that you do not have to write them yourself.

Everything in it is built on the same public `Persistable`/`Guard` surface available to any crate.
There is no internal kladde magic here that a third-party library — or your own application — could not use equally well, and third-party libraries of general-purpose durable types are welcome.

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
v.insert(0, PersistableString::from("b"))?;
v.get_mut(1).unwrap().push_str("c")?;  // the guard of one element
let first = v.remove(0)?;   // yours now, as a plain value
v.push(first)?;             // encoded again, at the end
let moved = v.cut(0)?;      // removed, its bytes kept for a paste
v.push_cut(moved)?;         // pasted at the end, without writing its bytes again
v.move_element(0, 1)?;      // reordered, without writing an element's bytes
v.delete(0)?;               // removed and dropped
```

Reading needs no guard: `PersistableVec<T>` dereferences to `[T]`, so `len`, indexing and `iter` work on the in-memory elements.

**Encoding.** The number of elements as a varint, then the elements, back to back.
In memory, a `Vec<T>`, and for an element type whose size depends on its value, a Fenwick tree of the elements' sizes, four bytes per element, so that the guard of element `i` finds where it starts in `O(log m)`.
A vector of fixed-size elements — `f32`, a struct of them, or [`Fixed<T>`](deriving.md#fixed-size-values) — finds element `i` by multiplication and keeps nothing more.

**Cost.** A push, an insertion and a removal are each a splice of the element plus a rewrite of the count, in one transaction; a change of an element's size is a splice inside it; and `move_element` writes no element's bytes at all.
None of them rewrites the elements behind it: the file records where its bytes lie as linked runs, so bytes that shift are not restated.

**Moving.** `remove` and `pop` hand the element back as a plain value; pushing it elsewhere encodes it again.
`cut` removes it too, but keeps its bytes as a *cut*, which `push_cut` or `insert_cut` pastes into any vector of the same element type without writing them again: the move costs a few entries in the file however large the element is.
A cut holds a transaction open until it is pasted, so paste it right away; dropping it unpasted removes the element for good.
`delete` and `clear` remove and drop.

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

On file, the text's UTF-8 bytes and then the byte `0xFF`, which UTF-8 never contains; in memory, a `String`, which it dereferences to.
Text states no count, so every edit — `push_str`, `insert_str`, `replace_range`, `truncate` — is one splice of the file's sequence, wherever it lands and however long the text is.

Plain `String` deliberately has **no** `Persistable` implementation, and neither have `Vec` and `HashMap`: a field's type says that it is durable, and its guard is where its recorded mutations live.
A struct field typed `String` fails to compile, and the error points you to the durable type.

So this does not compile, and that is the point:

<!-- kladde-example: name=plain-string-is-rejected file=src/lib.rs mode=compile_fail deps=kladde
before:
  use kladde::Persistable;
-->
```rust
#[derive(Persistable)]
struct Journal {
    owner: String,  // the trait bound `String: Persistable` is not satisfied
}
```

## `PersistableMap<K, V>`

A key-value map that keeps its entries in insertion order.
`K: Eq + Hash + Persistable`, `V: Persistable`.

<!-- kladde-example: name=containers file=src/map.rs
before:
  use kladde::{Kladde, Persistable};
  use kladde_types::{PersistableMap, PersistableString};
  #[derive(Persistable)]
  struct Contact {
      email: PersistableString,
  }
  #[derive(Persistable)]
  struct Book {
      contacts: PersistableMap<PersistableString, Contact>,
  }
  fn demo(book: &mut Kladde<Book>, contact: Contact) -> kladde::Result<()> {
  let mut guard = book.guard();
after:
  Ok(())
  }
-->
```rust
let mut contacts = guard.contacts_mut();
let ada = PersistableString::from("ada");
contacts.insert(PersistableString::from("ada"), contact)?;
contacts.get_mut(&ada).unwrap().email_mut().set("ada@example.com")?;
contacts.delete(&ada)?;     // removes and drops; `remove` hands the value back
```

Reading — `get`, `contains_key`, `iter`, `len` — needs no guard.
Inserting under a key that is already present replaces its value and returns the old one, as `HashMap::insert` does.

**Encoding.** The number of entries, then each entry's key and value, in the map's order.
The file does *not* support key lookup at all — that is what the in-memory map is for — so it needs no hashing structure on file.
In memory, the entries sit in a vector, in their order on file, beside a hash table of their indices, so each key is stored exactly once and `K: Clone` is not required.

**Removal swaps**: the last entry moves into the removed one's place, without writing its bytes, so the file stays dense, and an insertion appends.

**Keys are immutable.**
No guard is ever handed out for a key; `Persistable` on `K` governs only how its bytes are encoded.

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

Wraps any `T: Serialize + DeserializeOwned + Default` as an opaque, serialized payload, prefixed with its length.
Use it when a type is not yours to change and cannot get its own `Persistable` implementation.

**It is deliberately the worst option.**
A blob is rewritten in full on every change, which forfeits kladde's central advantage; it is opaque to [tooling](../../spec/tooling.md); and it drags in a serialization dependency.
Prefer a derived type or a hand-written implementation whenever you can.
It is gated behind `kladde-types`' `serde` feature — the only thing in the workspace that needs `serde` at all.

## What is missing

A **rope**, for large text with efficient middle-insertion, is designed but not built.

Tuples of up to twelve components are persistable, encoded like a tuple struct, and their guard's `parts()` hands out a guard per component.
`Option<T>` is persistable as an enum, and its guard offers `set`, `take` and `as_mut`; `Box<T>` is persisted as the `T` it holds, which is how a recursive type gets a size in Rust.

## Choosing

| you have | use |
| --- | --- |
| a sequence | `PersistableVec<T>` |
| text | `PersistableString` |
| a lookup table | `PersistableMap<K, V>` |
| a value that changes often and should keep its size | [`Fixed<T>`](deriving.md#fixed-size-values) |
| a few values that belong together | a tuple, or a derived struct |
| your own struct or enum | [`#[derive(Persistable)]`](deriving.md) |
| a foreign type you cannot change | `PersistableBlob<T>`, reluctantly |
| a foreign type whose layout you know | [a hand-written impl](custom-persistable.md) |
