---
title: The built-in containers
---

`kladde-types` provides seven durable containers: a vector, its packed variant, a string, a small string and a small vector, a hash map, and a blob.
They are hand-implemented against the persistence layer rather than derived, the same way `std`'s collections hand-write raw pointer manipulation internally.

## A default library, not a privileged one

`kladde-types` is not part of the machinery, and nothing depends on it.
It is a collection of types most applications turn out to want, offered so that you do not have to write them yourself.

Everything in it is built on the same public `Persistable`/`Guard` surface available to any crate.
There is no internal kladde magic here that a third-party library — or your own application — could not use equally well, and third-party libraries of general-purpose durable types are welcome.

## `PersistableVec<T>`

A growable sequence.
`T` must be `Slottable` — have a fixed size on file, which every type but a small string or vector does — and nothing more: no `Clone`, no `Serialize`.

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
let first = v.remove(0)?;   // yours now, allocations and all
v.push(first)?;             // so it moves without copying its bytes
v.delete(0)?;               // removes and frees
```

Reading needs no guard: `PersistableVec<T>` dereferences to `[T]`, so `len`, indexing and `iter` work on the in-memory elements.

**Layout.** A pointer inline, plus a separate allocation holding a dense array of fixed-size element slots — the same shape as `Vec<T>`'s in memory.
The length is that allocation's size divided by the element size, so it is never stored separately.

**Cost.** A push grows the allocation and writes the new slot, a pop shrinks it, and an insertion or removal in the middle is one splice, which shifts the tail exactly as `Vec` does.

**Removal.** `remove` and `pop` hand the element back with everything it owns, so you can store it elsewhere; `delete` and `clear` free it.
An element you take out and then drop leaks its allocations in the file, the way a value passed to `std::mem::forget` leaks its memory.

## `PackedPersistableVec<T>`

A growable sequence whose elements take as many bytes as their values need, with the same methods as `PersistableVec`.

<!-- kladde-example: name=containers file=src/packed.rs
before:
  use kladde::{Kladde, Persistable};
  use kladde_types::PackedPersistableVec;
  #[derive(Persistable)]
  enum Step {
      Close,
      Line { x: f32, y: f32 },
      Curve { x1: f32, y1: f32, x2: f32, y2: f32, x: f32, y: f32 },
  }
  fn demo(path: &mut Kladde<PackedPersistableVec<Step>>) -> kladde::Result<()> {
after:
  Ok(())
  }
-->
```rust
let mut steps = path.guard();
steps.push(Step::Line { x: 1.0, y: 2.0 })?;   // 9 bytes, where a slot would take 25
steps.push(Step::Close)?;                      // 1 byte
steps.get_mut(1).unwrap().set(Step::Line { x: 0.0, y: 0.0 })?; // grows, and moves what follows
```

**Layout.** A pointer inline, plus a separate allocation holding the elements back to back: an enum takes only its current variant rather than its largest, an integer is a varint, and a `char` is its UTF-8 bytes.

**Cost.** An element whose bytes change length — an enum switching to a larger variant, an integer crossing a power of 128 — is spliced in, which moves every element behind it, where a `PersistableVec` would write in place.
The vector keeps every element's offset in memory to find it, which a slotted vector computes.
Use it where the elements' sizes differ widely, such as path segments, and for elements that have no fixed size at all.

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

`replace_range` edits the middle, as `String::replace_range` does.
It is a thin wrapper around a byte vector, which is more interesting than it sounds.
Plain `String` deliberately has **no** `Persistable` implementation, and that absence is the enforcement mechanism: a struct field typed `String` simply fails to compile, rather than silently persisting nothing.
`PersistableString` exists because a durable string needs a field of its own to remember which allocation holds its bytes, and `String` has nowhere to put one.

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

## Small strings and vectors

`SmallPersistableString` and `SmallPersistableVec<T>` keep their content inline, inside the value that holds them, while it takes at most 128 bytes, and move it to an allocation of their own beyond that — for the many short strings and lists of a document, which would otherwise each own an allocation.

<!-- kladde-example: name=containers file=src/small.rs
before:
  use kladde::{Kladde, Persistable};
  use kladde_types::{PackedPersistableVec, SmallPersistableString, SmallPersistableVec};
  #[derive(Persistable)]
  #[kladde(packed_only)]
  struct Shape {
      id: SmallPersistableString,
      classes: SmallPersistableVec<SmallPersistableString>,
  }
  fn demo(shapes: &mut Kladde<PackedPersistableVec<Shape>>) -> kladde::Result<()> {
after:
  Ok(())
  }
-->
```rust
let mut guard = shapes.guard();
guard.push(Shape { id: "logo".into(), classes: SmallPersistableVec::new() })?;
let mut shape = guard.get_mut(0).unwrap();
shape.id_mut().push_str("-dark")?;                 // still inline
shape.classes_mut().push("brand".into())?;         // inline too, string and all
assert!(shape.classes.is_inline());
```

They read and mutate like `PersistableString` and `PackedPersistableVec`, and a small vector holds any `T`, small strings included.

**Layout.** A one-byte length, then the content itself; or, once the content outgrew 128 bytes, a marker and a pointer to the allocation that holds it.
Content moves back inline only once it shrinks below 64 bytes, so that editing it around one threshold does not allocate and free at every edit.

**Where they can stand.** A small value has no fixed size, so it stands only inside a packed container — a `PackedPersistableVec`, another small vector — or as the root; a struct holding one is marked [`#[kladde(packed_only)]`](deriving.md#small-strings-and-slotted-fields).
A `PersistableVec` of small strings does not compile, and the error points to the packed containers.

## `PersistableHashMap<K, V>`

A key-value map.
`K: Eq + Hash + Slottable`, `V: Slottable`.

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
let ada = PersistableString::from("ada");
contacts.insert(PersistableString::from("ada"), contact)?;
contacts.get_mut(&ada).unwrap().email_mut().set("ada@example.com")?;
contacts.delete(&ada)?;     // removes and frees; `remove` hands the value back
```

Reading — `get`, `contains_key`, `iter`, `len` — needs no guard.
Inserting under a key that is already present overwrites its value in place and returns the old one, as `HashMap::insert` does.

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

Tuples of up to twelve components are persistable, laid out like a tuple struct, and their guard's `parts()` hands out a guard per component.
There is no durable `Option` or `Box` as such — a derived enum covers `Option`'s role.

## Choosing

| you have | use |
| --- | --- |
| a sequence | `PersistableVec<T>` |
| a sequence of values of very different sizes | `PackedPersistableVec<T>` |
| text | `PersistableString` |
| many short strings or lists, inside packed values | `SmallPersistableString`, `SmallPersistableVec<T>` |
| a lookup table | `PersistableHashMap<K, V>` |
| a few values that belong together | a tuple, or a derived struct |
| your own struct or enum | [`#[derive(Persistable)]`](deriving.md) |
| a foreign type you cannot change | `PersistableBlob<T>`, reluctantly |
| a foreign type whose layout you know | [a hand-written impl](custom-persistable.md) |
