---
title: The built-in containers
---

Kladde has durable text, two vectors, a map and a blob.
Text and vectors of fixed-size elements are the standard `String` and `Vec<T>`, durable as they are, since they need nothing beyond what the standard types hold.
`kladde-types` provides the other three, `DurableVec<T>`, `DurableMap<K, V>` and `DurableBlob<T>`, since each keeps an index or a serialization of its own in memory.
Every container's content lies inline in the file's one sequence of bytes, wherever its value lies, so a short list costs no more on file than its elements and its count.

## A default library, not a privileged one

`kladde-types` is not part of the machinery, and nothing depends on it.
It is a collection of types most applications turn out to want, offered so that you do not have to write them yourself.

Everything in it is built on the same public `Durable`/`Mut` surface available to any crate.
There is no internal kladde magic here that a third-party library — or your own application — could not use equally well, and third-party libraries of general-purpose durable types are welcome.

## `String`

Growable text.

<!-- kladde-example: name=containers file=src/string.rs deps=kladde,kladde-types
before:
  use kladde::{Kladde, Durable};
  #[derive(Durable)]
  struct Journal {
      owner: String,
  }
  fn demo(journal: &mut Kladde<Journal>) -> kladde::Result<()> {
  let mut journal_mut = journal.get_mut();
after:
  Ok(())
  }
-->
```rust
let mut owner = journal_mut.owner_mut();
owner.set("ada")?;
owner.push_str(" lovelace")?;
```

On file, the text's UTF-8 bytes and then the byte `0xFF`, which UTF-8 never contains; in memory, the `String` itself.
Text states no count, so every edit — `push_str`, `insert_str`, `replace_range`, `truncate` — is one splice of the file's sequence, wherever it lands and however long the text is.

**`set` records only what changed**: it compares the new text with the old one and splices in the bytes between their common prefix and suffix, or nothing if they are equal.
So you may set a whole text at every change — a path's data rewritten at every step of a drag, say — and the file records a few bytes rather than the whole text.

Text is UTF-8; for a byte buffer, use a `Vec<u8>`.

## `Vec<T>` and `DurableVec<T>`

A growable sequence, in two types with the same encoding: **`Vec<T>` for elements of a fixed size, and `DurableVec<T>` for any.**

A `Vec<T>` is durable when every value of `T` encodes to the same number of bytes — `u8`, `f32`, a tuple or struct of them, or [`Fixed<T>`](deriving.md#fixed-size-values) — since then it finds element `i` by multiplication.
For any other `T`, such as `String`, using a `Vec<T>` as a durable value fails to compile, and the message names `DurableVec<T>`:

<!-- kladde-example: name=vec-of-text-is-rejected file=src/lib.rs mode=compile_fail deps=kladde
before:
  use kladde::Durable;
-->
```rust
#[derive(Durable)]
struct Journal {
    entries: Vec<String>,  // a `Vec<T>` is durable only for a fixed-size `T`
}
```

`DurableVec<T>` takes any `Durable` `T`, and nothing more — no `Clone`, no `Serialize` — and adds cutting and reordering:

<!-- kladde-example: name=containers file=src/vec.rs
before:
  use kladde::{Kladde, Durable};
  use kladde_types::DurableVec;
  #[derive(Durable)]
  struct Journal {
      entries: DurableVec<String>,
  }
  fn demo(journal: &mut Kladde<Journal>) -> kladde::Result<()> {
after:
  Ok(())
  }
-->
```rust
let mut journal_mut = journal.get_mut();
let mut v = journal_mut.entries_mut();
v.push(String::from("a"))?;
v.insert(0, String::from("b"))?;
v.get_mut(1).unwrap().push_str("c")?;  // the `Mut` of one element
let first = v.remove(0)?;   // yours now, as a plain value
v.push(first)?;             // encoded again, at the end
let moved = v.cut(0)?;      // removed, its bytes kept for a paste
v.push_cut(moved)?;         // pasted at the end, without writing its bytes again
v.move_element(0, 1)?;      // reordered, without writing an element's bytes
v.delete(0)?;               // removed and dropped
```

A `Vec<T>`'s `Mut` offers the methods they share: `set`, `insert`, `push`, `remove`, `pop`, `clear` and `get_mut`.
Reading needs no `Mut`: both dereference to `[T]`, so `len`, indexing and `iter` work on the in-memory elements.

**Encoding.** The number of elements as a varint, then the elements, back to back; a `Vec<T>` and a `DurableVec<T>` of the same elements have the same bytes and the same schema.
In memory, a `DurableVec<T>` of elements whose size depends on their value keeps a Fenwick tree of their sizes beside its `Vec<T>`, four bytes per element, so that the `Mut` of element `i` finds where it starts in `O(log m)`; for fixed-size elements it keeps no tree.

**Cost.** A push, an insertion and a removal are each a splice of the element plus a rewrite of the count, in one transaction; a change of an element's size is a splice inside it; and `move_element` writes no element's bytes at all.
None of them rewrites the elements behind it: the file records where its bytes lie as linked runs, so bytes that shift are not restated.

**Moving.** `remove` and `pop` hand the element back as a plain value; pushing it elsewhere encodes it again.
`cut` removes it too, but keeps its bytes as a *cut*, which `push_cut` or `insert_cut` pastes into any `DurableVec` of the same element type without writing them again: the move costs a few entries in the file however large the element is.
A cut lives as long as the journal that made it: you may hold it across transactions, and a paste after the next flush still works, but encodes the element again, as `push` would.
`Cut::is_alive()` tells you whether a paste would still write none of its bytes; dropping a cut unpasted drops the element.
`delete` and `clear` remove and drop.

## `DurableMap<K, V>`

A key-value map that keeps its entries in insertion order.
`K: Eq + Hash + Durable`, `V: Durable`.

<!-- kladde-example: name=containers file=src/map.rs
before:
  use kladde::{Kladde, Durable};
  use kladde_types::DurableMap;
  #[derive(Durable)]
  struct Contact {
      email: String,
  }
  #[derive(Durable)]
  struct Book {
      contacts: DurableMap<String, Contact>,
  }
  fn demo(book: &mut Kladde<Book>, contact: Contact) -> kladde::Result<()> {
  let mut book_mut = book.get_mut();
after:
  Ok(())
  }
-->
```rust
let mut contacts = book_mut.contacts_mut();
let ada = String::from("ada");
contacts.insert(String::from("ada"), contact)?;
contacts.get_mut(&ada).unwrap().email_mut().set("ada@example.com")?;
contacts.delete(&ada)?;     // removes and drops; `remove` hands the value back
```

Reading — `get`, `contains_key`, `iter`, `len` — needs no `Mut`.
Inserting under a key that is already present replaces its value and returns the old one, as `HashMap::insert` does.

**Encoding.** The number of entries, then each entry's key and value, in the map's order; the schema describes it as a map of its key and value types, so a tool can show it as one.
The file does *not* support key lookup at all — that is what the in-memory map is for — so it needs no hashing structure on file.
In memory, the entries sit in a vector, in their order on file, beside a hash table of their indices, so each key is stored exactly once and `K: Clone` is not required.

**Removal swaps**: the last entry moves into the removed one's place, without writing its bytes, so the file stays dense, and an insertion appends.

**Keys are immutable.**
No `Mut` is ever handed out for a key; `Durable` on `K` governs only how its bytes are encoded.

## `DurableBlob<T>`

An escape hatch for foreign types.

<!-- kladde-example: name=containers file=src/blob.rs deps=kladde-types[serde],serde
before:
  use kladde::Durable;
  use kladde_types::DurableBlob;
  use serde::{Deserialize, Serialize};
  #[derive(Default, Serialize, Deserialize)]
  struct WindowGeometry {
      width: u32,
      height: u32,
  }
-->
```rust
#[derive(Durable)]
struct Config {
    window: DurableBlob<WindowGeometry>,  // a foreign serde type
}
```

Wraps any `T: Serialize + DeserializeOwned + Default` as an opaque, serialized payload, prefixed with its length.
Use it when a type is not yours to change and cannot get its own `Durable` implementation.

**It is deliberately the worst option.**
A blob is serialized again in full on every change, which costs time in proportion to its size, though only the bytes of the serialization that changed are recorded; it is opaque to [tooling](../../spec/tooling.md); and it drags in a serialization dependency.
Prefer a derived type or a hand-written implementation whenever you can.
It is gated behind `kladde-types`' `serde` feature — the only thing in the workspace that needs `serde` at all.

## What is missing

A **rope**, for large text with efficient middle-insertion, is designed but not built.

Tuples of up to twelve components are durable, encoded like a tuple struct, and their `Mut`'s `parts()` hands out a `Mut` per component.
`Option<T>` is durable as an enum, and its `Mut` offers `set`, `take` and `as_mut`; `Box<T>` is persisted as the `T` it holds, which is how a recursive type gets a size in Rust.

## Choosing

| you have | use |
| --- | --- |
| a sequence of fixed-size values | `Vec<T>` |
| a sequence of anything else, or one you cut and paste from | `DurableVec<T>` |
| text | `String` |
| bytes | `Vec<u8>` |
| a lookup table | `DurableMap<K, V>` |
| a value that changes often and should keep its size | [`Fixed<T>`](deriving.md#fixed-size-values) |
| a few values that belong together | a tuple, or a derived struct |
| your own struct or enum | [`#[derive(Durable)]`](deriving.md) |
| a foreign type you cannot change | `DurableBlob<T>`, reluctantly |
| a foreign type whose layout you know | [a hand-written impl](custom-durable.md) |
