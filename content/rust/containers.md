---
title: Containers
---

How the built-in containers are laid out, and the design choices behind each.

The containers are **hand-implemented** against `Persistable`/`Guard`/`UniquePointer`, not generated.
This is the same division `std` makes: its collections hand-write raw pointer manipulation internally, and the derive macro targets application-level types composed from already-persistable fields.

## `PersistableVec<T>`

**Inline:** the content allocation's `Pointer`, all-zero while there is none.
**Content:** one allocation holding a dense array of fixed-size element slots, element `i` at `i × T::INLINE_SIZE`.

**The length is not stored.**
It is the content allocation's size divided by the element size, since the store already knows every allocation's size.
That halves the inline footprint and removes a field that could drift out of step with the allocation — and it makes a push **one record**: a `Write` past the end [grows its allocation](../spec/journal.md#every-record-but-free-brings-its-allocation-into-existence), so writing the new element and publishing the new length are the same atomic step.
The price is that a zero-sized element type is unrepresentable, and `PersistableVec` refuses one.

A push costs one `Write`, a pop one `Resize`, and a removal from the middle one `Splice`, which the [fold](../impl/flush.md#what-falls-out-unasked) turns into a statement edit when the vector was written in the same flush.

`T` needs only `Persistable` — no `Clone`, no `Serialize`.
That falls out of storing a fixed-size inline representation rather than going through a serializer: a guard method serializes a value into the record while only *borrowing* it.

**Removal transfers ownership.**
`remove` and `pop` return the element with its allocations intact, so it can be pushed elsewhere; `delete` removes and [frees](freeing.md) it.

## `PersistableString`

A thin wrapper around `PersistableVec<u8>`.

The interesting part is why it exists at all.
Plain `String` deliberately has **no** `Persistable` implementation, and that absence is the enforcement mechanism: a struct field typed `String` fails to compile rather than silently persisting nothing.
`String` cannot implement the trait because it has nowhere to keep the pointer to its own content allocation; `PersistableString` is a library struct with room for one.

## `PersistableHashMap<K, V>`

**Inline:** the slot array's `Pointer`.
**Content:** an array of fixed-size slots, each a one-byte liveness tag followed by a `(K, V)` pair.

The design turns on one observation: **the on-disk form does not need to support lookup.**
That is what the in-memory map is for, so the content is a flat slot array with no hashing structure at all.

**Removal is tombstoning** — clear the tag in place, move nothing — and an insertion reuses the lowest tombstoned slot before it grows the array.
Two consequences:

- No "which key is at slot *n*" reverse lookup is needed, so each key is stored **exactly once** in memory, in a single map from key to slot-and-value.
  Hence `K: Clone` is not required.
- The array's length is a **capacity**, the highest slot in use, rather than a live count; reusing tombstones keeps it at the high-water mark of live entries rather than of entries ever inserted.

**Keys are immutable.** No guard is ever handed out for a key, so `Persistable` on `K` governs only how its bytes are read and written.

## `PersistableBlob<T>`

Wraps any `T: Serialize + DeserializeOwned + Default` as an opaque payload with its own allocation.

The escape hatch for foreign types that can get neither their own wrapper nor a derive.
It is deliberately the least attractive option: rewritten in full on every change, opaque to [tooling](../spec/tooling.md), and the only thing in the workspace that needs a serialization dependency at all — hence the feature gate.

## What is missing

**A rope**, for large text with efficient middle insertion.
Designed in outline, not built.
It is the container that would most benefit from the [fold's zero-I/O splice](../impl/flush.md#what-falls-out-unasked).
