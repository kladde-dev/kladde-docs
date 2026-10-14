---
title: Containers
---

How kladde-rs realises the [built-in containers](../impl/values.md#the-built-in-containers), and the choices that are Rust's.

Text and vectors of fixed-size elements are the standard `String` and `Vec<T>`, durable in kladde-persist, since they need nothing in memory beyond what the standard types hold.
`DurableVec<T>`, `DurableMap<K, V>` and `DurableBlob<T>` live in kladde-types, since each keeps an index or a serialization of its own in memory.
All of them are **hand-implemented** against `Durable`, `Mut` and `Region`, not generated.
Everything they use is public, so a third-party library of durable types can do anything they do.

## `Vec<T>` and `DurableVec<T>`

**`Vec<T>` is durable when `T` is fixed-size, and `DurableVec<T>` for every `T`**; `DurableVec<T>` is the type for elements whose size depends on their value.
Both have the same encoding and the same schema, a [Sequence](../spec/schema/type-descriptors.md#sequence): the count as a varint, then the elements.

**`Vec<T>`** finds element `i` at `i` times the element's size after the count, so the standard vector holds everything it needs.
A `T` whose `FIXED_SIZE` is `None` is refused at compile time by a const assertion in `Vec<T>`'s `Durable` implementation, whose message names `DurableVec<T>`.
Its `Mut` is `VecMut`, with `set`, `insert`, `push`, `remove`, `pop`, `clear` and `get_mut`; every insertion and removal is a splice of the element and a rewrite of the count, in one transaction.

**`DurableVec<T>`** keeps, in memory, a `Vec<T>`, and for a `T` whose `FIXED_SIZE` is `None`, a Fenwick tree of the elements' encoded sizes in 32-bit cells, behind a cached total; for a fixed-size `T`, element `i` starts at `i` times its size after the count, and it keeps no tree.
It adds to `Vec<T>`'s methods those that a `Vec` has no room for: `delete`, `move_element`, and cuts.

The tree appends and pops in `O(log m)`, answers where element `i` starts in `O(log m)`, and is built again in `O(m)` after an insertion, a removal or a reordering elsewhere, beside the `O(m)` move of the `Vec`'s elements.
It lies in cells and implements `Node`, so the element `Mut`s that `parts`-like accessors make together, or a future `split_at_mut`, can look their starts up through it while sharing it.

- **`push` and `insert`** splice the element in with `splice_value` and rewrite the count, in one transaction.
- **`pop`, `remove` and `delete`** splice it out and rewrite the count; `remove` and `pop` return the value, and `delete` drops it.
- **`move_element(from, to)`** records one `Move`, which writes no element's bytes.
- **`get_mut(i)`** makes the element's region, at the start the tree gives, and the element's `Mut`; a change of the element's size reaches the tree through the region.
- **`set`** replaces the whole vector.
- **`cut(i)`, `insert_cut` and `push_cut`** move an element between vectors as [a cut and a paste](../impl/values.md#moving-a-value-between-containers), below.

`T` needs only `Durable` — no `Clone`, no `Serialize` — since a `Mut` method encodes a value into the record while only borrowing it.
The vector reads through `Deref<Target = [T]>`, so a slice's whole read API works on it.

The tree's field takes 40 bytes even where it is empty or the elements are fixed-size: 24 for its `Vec` and 16 for its cached total and the count's varint length, which it adds when it answers for `Mut`s made together.
Neither number needs a field — the total can live in the tree's allocation, and the count's length is the vector's to pass in — which would leave the 24 bytes of an empty `Vec`, or 8 with a thin one.

## `String`

**Encoding:** [Text](../spec/schema/type-descriptors.md#text): UTF-8, then `0xFF`.
**In memory:** the `String` itself.

Its `Mut` is `StringMut`, whose `replace_range`, `push_str`, `insert_str`, `truncate` and `set` are each at most one splice, since text has no count to rewrite.
**`set` records only what changed**: one splice of the bytes between the longest prefix and the longest suffix that the old and the new text share, or nothing if they are equal.
So code that sets a whole text at every change, such as a path's data rewritten at every step of a drag, records a few bytes rather than the whole text, at the cost of one pass over both texts.
The comparison is public as `kladde_persist::splice_changed`, for hand-written `Mut`s of other values that are replaced whole.

Text is UTF-8; a byte buffer is a `Vec<u8>`.

## `DurableMap<K, V>`

**Encoding:** a [Map](../spec/schema/type-descriptors.md#map), a descriptor kind of its own with a key type and a value type: the count as a varint, then each entry's key and value, in the map's order.
**In memory:** an index map — the entries in a `Vec<(K, V)>`, in their order on file, and a hash table of their indices, its own, with open addressing over 32-bit positions, probed linearly and at most half full — and for variable-size entries a Fenwick tree of their sizes.

- **`insert`** appends a new key, or splices a new value over an existing key's, returning the old one.
- **`remove` and `delete`** swap: the last entry moves into the removed one's place by a `Move`, the removed entry is spliced out, and the count goes down, all in one transaction; only the moved entry's index changes in memory.
- **`get_mut`** hands out a `Mut` for a value, never for a key: keys are immutable.

Each key is stored once, in the entries, so `K: Clone` is not required.

## `DurableBlob<T>`

**Encoding:** an [Opaque](../spec/schema/type-descriptors.md#opaque) without a fixed size: the serde payload, prefixed with its length.
Wraps any `T: Serialize + DeserializeOwned + Default`.

The escape hatch for foreign types that can get neither their own wrapper nor a derive, and deliberately the least attractive option: serialized again in full on every change, opaque to [tooling](../spec/tooling.md), and the only thing in the workspace that needs a serialization dependency — hence the feature gate.
Its in-place edit is a closure, `update(|value| ...)`, rather than a handle that persists when dropped, since `Drop` cannot report an error.

**`set` and `update` record only the bytes of the serialization that changed.**
The blob keeps its last serialization, compares the new one with it, and splices in the range between their common prefix and suffix with `splice_changed`; where the length changes, the rewritten length prefix goes in the same transaction.
Editing one nested field of a large blob then writes little, though how little depends on the serialization: postcard's varints and length prefixes put a changed byte near the front of everything behind a value that changes size, so the one range that holds every change can reach far.

## Cuts

**A container's `cut(i)` removes element `i` as `remove(i)` does, and returns a `Cut<T, B>`**: the in-memory value, and the store's handle on the removed bytes.
Cutting is an ordinary removal, recorded in a transaction of its own; it holds no transaction open.

**A cut is weak: it lives as long as the journal that made it.**
A paste while that journal lasts writes none of the value's bytes, since it records a `Paste`; a paste after a flush has folded that journal encodes the value again, as an ordinary splice would.
Either way the sequence ends up the same, so a `Cut` may be held across transactions and flushes, and `Cut::is_alive()` tells whether a paste would still write none of its bytes.

- `push_cut` and `insert_cut` take a `Cut<T, B>`, record the paste, and move the value into memory.
  They are methods of their own, rather than `push` taking either a value or a cut, since one method taking either would need a conversion trait whose two impls Rust's coherence rules make fragile.
- A `Cut` dereferences to its value but gives no mutable access, since its bytes must stay the value's encoding until they are pasted.
- It is neither `Copy` nor `Clone`, and typed by its value's type, so it is pasted at most once, and a `Cut<Fixed<Shape>>` cannot go where a `Shape` goes.
- It borrows the store, `B = &Store`, not the `Mut` it came from, so the application can drop the source's `Mut` and make the destination's while it holds the cut.
- Dropping it unpasted drops the value, which the container removed when it cut it; nothing is recorded, so a drop has no failure to report.

kladde-rs implements cuts on `DurableVec` only; `Vec`, the map, fields, `Option` and enums neither make nor take them yet, though any `Mut` method that splices a value out could return one.

## What is missing

**A rope**, for large text with cheap lookups by position in memory.
The file already takes splices in long text at constant cost; what a `String` costs is the `O(n)` shift of its in-memory bytes.

**`split_at_mut`** on the vector, for `Mut`s of two runs of elements at once.
The tree already lies in cells and answers any element's start, so it would serve as the two runs' index as it is; it was left out only because nothing measured needs it.
