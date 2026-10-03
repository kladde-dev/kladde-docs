---
title: Containers
---

How the built-in containers are laid out, and the design choices behind each.

The containers are **hand-implemented** against `Persistable`/`Guard`/`UniquePointer`, not generated.
This is the same division `std` makes: its collections hand-write raw pointer manipulation internally, and the derive macro targets application-level types composed from already-persistable fields.

Every container but the blob **describes its structure** in the schema, with a [`Pointer`](../spec/schema/type-descriptors.md#pointer) to what its allocation holds, so that a [tool](../spec/tooling.md#containers) walks it without knowing kladde-types.
Its pointer takes the encoding of the place it sits in: 4 bytes in a slotted place, a varint of its id in a packed one, `0` for none in either.

## `PersistableVec<T>`

**Inline:** the content allocation's pointer, null while there is none.
**Content:** one allocation holding the elements in their fixed encodings, element `i` at `i × T::SLOT_SIZE`.
**Descriptor:** `Pointer(Sequence(T))`.

**The length is not stored.**
It is the content allocation's size divided by the element size, since the store already knows every allocation's size.
That halves the inline footprint and removes a field that could drift out of step with the allocation — and it makes a push **one record**: a `Write` past the end [grows its allocation](../spec/journal.md#every-record-but-free-brings-its-allocation-into-existence), so writing the new element and publishing the new length are the same atomic step.
The price is that a zero-sized element type is unrepresentable, and `PersistableVec` refuses one.

A push costs one `Write`, a pop one `Resize`, and a removal from the middle one `Splice`, which the [fold](../impl/flush.md#what-falls-out-unasked) turns into a statement edit when the vector was written in the same flush.

`T` needs `Slottable` — no `Clone`, no `Serialize` — since the elements take fixed slots.
That falls out of storing a fixed-size encoding rather than going through a serializer: a guard method encodes a value into the record while only *borrowing* it.

**Removal transfers ownership.**
`remove` and `pop` return the element with its allocations intact, so it can be pushed elsewhere; `delete` removes and [frees](freeing.md) it.

## `PackedPersistableVec<T>`

**Inline:** the content allocation's pointer.
**Content:** the elements in their packed encodings, back to back, each as long as its value needs.
**Descriptor:** `Pointer(Packed(Sequence(T)))`.

The same interface as `PersistableVec`, for any `T`, packed-only types included, and a different trade.
An enum element takes its current variant rather than its largest, integers are varints, and a `char` is UTF-8; but an element whose encoding changes size moves every element behind it, a `Splice` where a slotted vector writes in place.

**The offsets live in memory.**
The vector keeps each element's offset beside its elements, as prefix sums with one more entry than there are elements, built while the content is decoded.
A size change or an insertion updates the entries behind it in `O(n)`, as the memmove of the insertion itself costs; the offsets are `Cell`s, so that an element's guard can shift them through the shared reference its place holds while the element itself is borrowed mutably.
Its elements' guards are in packed places, linked to those offsets, so an element whose size changes splices its new encoding in and has the vector shift the offsets behind it; the vector's own encoding, its pointer, stays as it is.

## `PersistableString`

**Inline:** the content allocation's pointer.
**Content:** the text in UTF-8.
**Descriptor:** `Pointer(Packed(Sequence(char)))` — a packed `char` is UTF-8, so the descriptor fits the content byte for byte, and tells a tool it is text.

In memory, a thin wrapper around a byte vector.
Edits are byte-range splices at `char` boundaries: `set`, `push_str` and `replace_range`, each one record.

The interesting part is why it exists at all.
Plain `String` deliberately has **no** `Persistable` implementation, and that absence is the enforcement mechanism: a struct field typed `String` fails to compile rather than silently persisting nothing.
`String` cannot implement the trait because it has nowhere to keep the pointer to its own content allocation; `PersistableString` is a library struct with room for one.

A string and a `PackedPersistableVec<char>` share a descriptor and a fingerprint, as the schema's [guiding principle](../spec/schema/index.md#the-guiding-principle) intends for byte-identical representations, although one keeps text in memory and the other four-byte `char`s.

## Small values

**`SmallPersistableString` and `SmallPersistableVec<T>` keep up to 128 bytes of content inline, in the value that holds them, and move it to an allocation of their own once it grows.**
They are for the many short strings and lists of a document — an element's `id`, its attributes — which would otherwise each own an allocation, with its entry in the allocation map and its statements in the address table.

**Encoding:** a one-byte size tag, then the content in that many bytes, or the tag 255 and a varint pointer to the allocation the content spilled into.
**Descriptor:** `Small(Sequence(char), Pointer(Packed(Sequence(char))))` for the string, `Small(Sequence(T), Pointer(Packed(Sequence(T))))` for the vector: spilled, each is exactly a `PersistableString` or a `PackedPersistableVec<T>`, with the same bytes as inline, so moving the content never encodes it again.

**They have no fixed encoding**, since one would reserve the whole inline capacity in every value, so they implement no `Slottable` and stand only in packed places: inside an element of a packed vector, a field of a struct inside one, another small vector, or a packed root.
A struct that holds one is marked [`#[kladde(packed_only)]`](derive-macro.md#slotted-fields-and-packed-only-types).

**Content moves with hysteresis**: it spills once it takes more than `SPILL_ABOVE`, 128 bytes, and folds back only once it takes fewer than `FOLD_BELOW`, 64, so that edits around one threshold do not allocate and free at every crossing; a new value is inline if its content takes at most 128 bytes.
The thresholds are this crate's policy, not part of the encoding.

**An element can move the vector around it.**
A small vector's elements are in packed places linked to the vector's tag and offsets, which live in the value as `Cell`s; when an element changes size, the vector rewrites its tag, or spills or folds its content if the change crosses a threshold, and reports its own change of size to its parent.
The element is borrowed while this happens, so the vector moves the bytes already on file with a `Copy` record rather than encoding the content again, as it does when its own guard crosses a threshold; the element's guard keeps working after the move, since it asks for its location at every write.
[Small values](../impl/packed-values.md#small-values) has the algorithm.

In memory, a small string is a `String` and a small vector a `Vec` with its offsets, plus the handle of their allocation while they have one: *small* describes the file, not the memory.

## `PersistableHashMap<K, V>`

**Inline:** the slot array's pointer.
**Content:** an array of fixed-size slots, each a one-byte liveness flag followed by a key and a value in their fixed encodings.
**Descriptor:** `Pointer(Sequence(Slot))`, where `Slot` is a struct of `live: bool`, `key: K` and `value: V`.

The design turns on one observation: **the on-disk form does not need to support lookup.**
That is what the in-memory map is for, so the content is a flat slot array with no hashing structure at all.

**Removal is tombstoning** — clear the flag in place, move nothing — and an insertion reuses the lowest tombstoned slot before it grows the array.
Two consequences:

- No "which key is at slot *n*" reverse lookup is needed, so each key is stored **exactly once** in memory, in a single map from key to slot-and-value.
  Hence `K: Clone` is not required.
- The array's length is a **capacity**, the highest slot in use, rather than a live count; reusing tombstones keeps it at the high-water mark of live entries rather than of entries ever inserted.

Reusing a slot needs slots of equal size, so `K` and `V` must be `Slottable`; a map that stopped reusing slots could pack its entries as a packed vector does.

**Keys are immutable.** No guard is ever handed out for a key, so `Persistable` on `K` governs only how its bytes are read and written.

## `PersistableBlob<T>`

Wraps any `T: Serialize + DeserializeOwned + Default` as an opaque payload with its own allocation.

The escape hatch for foreign types that can get neither their own wrapper nor a derive.
It is deliberately the least attractive option: rewritten in full on every change, opaque to [tooling](../spec/tooling.md), and the only thing in the workspace that needs a serialization dependency at all — hence the feature gate.
Its descriptor is `Opaque`, the same for every `T`, and since an opaque type keeps its inline bytes in either place, its pointer keeps its 4 bytes in a packed place too.

## What is missing

**A rope**, for large text with efficient middle insertion.
Designed in outline, not built.
It is the container that would most benefit from the [fold's zero-I/O splice](../impl/flush.md#what-falls-out-unasked), and from packed layouts, whose insertions into a long sequence it would make cheap.
