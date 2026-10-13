---
title: Containers
---

How kladde-types realises the [built-in containers](../impl/values.md#the-built-in-containers), and the choices that are Rust's.

The containers are **hand-implemented** against `Durable`, `Guard` and `Region`, not generated, since each keeps an index of its own in memory.
Everything they use is public, so a third-party library of durable types can do anything they do.

## `DurableVec<T>`

**Encoding:** a [Sequence](../spec/schema/type-descriptors.md#sequence): the count as a varint, then the elements.
**In memory:** a `Vec<T>`, and for a `T` whose `FIXED_SIZE` is `None`, a Fenwick tree of the elements' encoded sizes in 32-bit cells, behind a cached total; for a fixed-size `T`, element `i` starts at `i` times its size after the count, and the vector keeps nothing more.

The tree appends and pops in `O(log m)`, answers where element `i` starts in `O(log m)`, and is built again in `O(m)` after an insertion, a removal or a reordering elsewhere, beside the `O(m)` move of the `Vec`'s elements.
It lies in cells and implements `Node`, so the element guards that `parts`-like accessors make together, or a future `split_at_mut`, can look their starts up through it while sharing it.

- **`push` and `insert`** splice the element in with `splice_value` and rewrite the count, in one transaction.
- **`pop`, `remove` and `delete`** splice it out and rewrite the count; `remove` and `pop` return the value, and `delete` drops it.
- **`move_element(from, to)`** records one `Move`, which writes no element's bytes.
- **`get_mut(i)`** makes the element's region, at the start the tree gives, and the element's guard; a change of the element's size reaches the tree through the region.
- **`set`** replaces the whole vector.
- **`cut(i)`, `insert_cut` and `push_cut`** move an element between vectors as [a cut and a paste](../impl/values.md#moving-a-value-between-containers), below.

`T` needs only `Durable` — no `Clone`, no `Serialize` — since a guard method encodes a value into the record while only borrowing it.
The vector reads through `Deref<Target = [T]>`, so a slice's whole read API works on it.

The tree's field takes 40 bytes even where it is empty or the elements are fixed-size: 24 for its `Vec` and 16 for its cached total and the count's varint length, which it adds when it answers for guards made together.
Neither number needs a field — the total can live in the tree's allocation, and the count's length is the vector's to pass in — which would leave the 24 bytes of an empty `Vec`, or 8 with a thin one.

## `DurableString`

**Encoding:** [Text](../spec/schema/type-descriptors.md#text): UTF-8, then `0xFF`.
**In memory:** a `String`, which it dereferences to as `str`.

`replace_range`, `push_str`, `insert_str`, `truncate` and `set` are each one splice, since text has no count to rewrite; `set` records a `Write` where the new text has the old one's length.

Plain `String` has **no** `Durable` implementation, by choice: a field's type says that it is durable, and its guard is where its recorded mutations live, so a field typed `String` fails to compile and points the author to `DurableString`.

## `DurableMap<K, V>`

**Encoding:** a Sequence of entries, each a Struct named `Entry` of a field `key` and a field `value`, in the map's order.
**In memory:** an index map — the entries in a `Vec<(K, V)>`, in their order on file, and a hash table of their indices, its own, with open addressing over 32-bit positions, probed linearly and at most half full — and for variable-size entries a Fenwick tree of their sizes.

- **`insert`** appends a new key, or splices a new value over an existing key's, returning the old one.
- **`remove` and `delete`** swap: the last entry moves into the removed one's place by a `Move`, the removed entry is spliced out, and the count goes down, all in one transaction; only the moved entry's index changes in memory.
- **`get_mut`** hands out a guard for a value, never for a key: keys are immutable.

Each key is stored once, in the entries, so `K: Clone` is not required.

## `DurableBlob<T>`

**Encoding:** an [Opaque](../spec/schema/type-descriptors.md#opaque) without a fixed size: the serde payload, prefixed with its length.
Wraps any `T: Serialize + DeserializeOwned + Default`.

The escape hatch for foreign types that can get neither their own wrapper nor a derive, and deliberately the least attractive option: rewritten in full on every change, opaque to [tooling](../spec/tooling.md), and the only thing in the workspace that needs a serialization dependency — hence the feature gate.
Its in-place edit is a closure, `update(|value| ...)`, rather than a handle that persists when dropped, since `Drop` cannot report an error.

**It rewrites its whole payload on every change, where it could splice only what changed.**
It keeps its last serialization, so it could find the changed span by a scan for the common prefix and suffix, and splice it in, which costs a few span entries wherever it lands; editing one nested field of a large blob would then write little.
How little depends on the serialization: postcard's varints and length prefixes shift everything after a value that changes size, so the saving depends on the workload.
If that is not done, the blob need keep only its serialization's length rather than the serialization itself.

## Cuts

**A container's `cut(i)` removes element `i` as `remove(i)` does, and returns a `Cut<T, B>`**: the in-memory value, and the cut's number in the store's open transaction, which the `Cut` holds open, nested in any the application opened, for as long as it lives.

- `push_cut` and `insert_cut` take a `Cut<T, B>`, record a `Paste` instead of the value's encoding, and move the value into memory.
  They are methods of their own, rather than `push` taking either a value or a cut, since one method taking either would need a conversion trait whose two impls Rust's coherence rules make fragile.
- A `Cut` dereferences to its value but gives no mutable access, since its bytes must stay the value's encoding until they are pasted.
- It is neither `Copy` nor `Clone`, and typed by its value's type, so it is pasted at most once, and a `Cut<Fixed<Shape>>` cannot go where a `Shape` goes.
- It borrows the store, `B = &Store`, not the guard it came from, so the application can drop the source's guard and make the destination's while it holds the cut.
- Dropping it unpasted ends the transaction it holds open, which removes the value for good; `commit` does the same and returns a `Result`.
  A drop cannot return the failure of its append, so it poisons the store instead, and the next call reports it, rather than panicking in `drop`.

kladde-rs implements cuts on the vector only; the map, fields, `Option` and enums neither make nor take them yet, though any guard method that splices a value out could return one.

## What is missing

**A rope**, for large text with cheap lookups by position in memory.
The file already takes splices in long text at constant cost; what a `String` costs is the `O(n)` shift of its in-memory bytes.

**`split_at_mut`** on the vector, for guards on two runs of elements at once.
The tree already lies in cells and answers any element's start, so it would serve as the two runs' index as it is; it was left out only because nothing measured needs it.
