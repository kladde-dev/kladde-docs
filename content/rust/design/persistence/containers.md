---
title: Containers
---

How the built-in containers are laid out, and the design choices behind each.

The containers are **hand-implemented** against `Persistable`/`Guard`/the owned handles, not generated.
This is the same division `std` makes: its collections hand-write raw pointer manipulation internally, and the derive macro targets application-level types composed from already-persistable fields.

## `PersistableVec<T>`

**Inline:** a fixed-size header.
**Content:** one allocation holding a dense array of fixed-size element slots.

The layout deliberately mirrors `Vec<T>`'s in memory: growing resizes the content allocation, and element `i` is at `i × T::INLINE_SIZE`.

`T` needs only `Persistable` — no `Clone`, no `Serialize`.
That falls out of storing a fixed-size inline representation rather than going through a serializer, and it is worth noting because it is unusual: a guard method serializes a value into the record while only *borrowing* it, rather than moving a copy into a semantic operation that must outlive the call.

**Ordering.** A push that grows must resize and write the element *before* bumping the stored length, because the length is what makes the element reachable.
See [Crash consistency](../journal/crash-consistency.md#the-ordering-discipline).

### The chunked variant

A chunked layout was prototyped to validate that the backend trait surface supports layouts kladde may eventually want:

- **Empty** — the inline pointer is null.
- **Small** (`len ≤ CHUNK_LEN`) — the head is a **resizable** allocation holding exactly the elements.
- **Linked** (`len > CHUNK_LEN`) — the head is a **fixed-size** allocation, the first chunk of a linked list.

Which layout is in use is discovered by asking the backend for the head's **sizedness** — no bit is stolen from the pointer.
Chunk layout keeps data first so promoting Small to Linked never moves element bytes; only trailer bytes are added.

This exercises the sizedness conversion and the "no data move" property, which is what it was built for.
It is a prototype, not the shipping vector.

## `PersistableString`

A thin wrapper around `PersistableVec<u8>`.

The interesting part is why it exists at all.
Plain `String` deliberately has **no** `Persistable` implementation, and that absence is the enforcement mechanism: a struct field typed `String` fails to compile rather than silently persisting nothing.

`String` cannot implement the trait because it has nowhere to keep the pointer to its own content allocation.
`PersistableString` is a library struct with room for one.

## `PersistableHashMap<K, V>`

**Inline:** a fixed-size header.
**Content:** an array of fixed-size slots, each a one-byte liveness tag followed by a `(K, V)` pair.

The design turns on one observation: **the on-disk form does not need to support lookup.**
That is what the in-memory map is for.
So the content is a flat slot array with no hashing structure at all.

**Removal is tombstoning** — clear the tag in place, move nothing.
Two consequences:

- No "which key is at slot *n*" reverse lookup is needed, so each key is stored **exactly once** in memory, in a single map from key to slot-and-value.
  Hence `K: Clone` is not required, which is unusual and genuinely useful.
- The array's length becomes a **capacity** — the highest slot ever used — rather than a live count.

That second point is a real limitation: sustained insert/remove churn grows the array without bound until a pass reclaims tombstoned slots.
**That pass does not exist.**
It is the most concrete missing piece in the container layer.

**Keys are immutable.** No guard is ever handed out for a key, so `Persistable` on `K` governs only how its bytes are read and written.

## `PersistableBlob<T>`

Wraps any `T: Serialize + DeserializeOwned + Default` as an opaque payload with its own allocation.

The escape hatch for foreign types that can get neither their own wrapper nor a derive.
It is deliberately the least attractive option: rewritten in full on every change, opaque to [tooling](../../../spec/tooling.md), and the only thing in the workspace that needs a serialization dependency at all — hence the feature gate.

Two constructors, and the difference is about leaks:

- `new(value, backend)` allocates **eagerly**, so it always has an allocation to reuse and never leaks by forgetting one;
- `default()` stays **lazy**, holding no allocation, paired with the invariant that its value is `T::default()`.

The same laziness argument applies to `PersistableVec::new()`.

## What is missing

**A rope**, for large text with efficient middle insertion.
Designed in outline, not built.
It is the container that would most benefit from the [fold's zero-I/O splice](../journal/fold-and-schedule.md#what-falls-out-unasked).

**Tombstone reclamation** for the hash map, as above.

**Destructors.** No container currently frees the allocations it owns when dropped or overwritten.
See [Freeing](freeing.md).
