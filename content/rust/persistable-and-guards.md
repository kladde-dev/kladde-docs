---
title: Persistable and guards
---

The central traits, why mutation needs a guard, and why it returns a `Result`.

## `Persistable`

```rust
pub trait Persistable<P: PointerRepr = Pointer>: Sized {
    const SLOTTED_SIZE: Option<usize>;
    const PACKED_SIZE: Option<usize>;
    type RootEncoding: Encoding;

    type Guard<'s, B: WriteBackend<Pointer = P>, E: Encoding>: Guard<Persistable = Self, Backend = B>;
    fn guard<'s, B: WriteBackend<Pointer = P>, E: Encoding>(
        &'s mut self, backend: &'s B, place: Place<'s, B, E>,
    ) -> Self::Guard<'s, B, E>;

    fn encoded_size<E: Encoding>(&self) -> usize;
    fn encode<E: Encoding>(&self, out: &mut Vec<u8>);
    fn decode<B: ReadBackend<Pointer = P>, E: Encoding>(
        backend: &mut B, input: &mut Input<'_>,
    ) -> Result<Self, Error>;
    fn prepare<B: WriteBackend<Pointer = P>>(&mut self, backend: &B) -> Result<(), Error> { Ok(()) }
    fn free<B: WriteBackend<Pointer = P>>(&mut self, backend: &B) -> Result<(), Error> { Ok(()) }

    fn describe_local(builder: &mut SchemaBuilder) -> TypeDescriptor;

    // provided: store, load, to_bytes, describe, schema, fingerprint
}
```

A `Persistable` is a plain, in-memory value that *has the right shape* to be persisted, but is not itself tied to any backend.
Reading it is ordinary reading.

### Two encodings, chosen by a type parameter

Every type has the two encodings the [specification](../spec/schema/type-descriptors.md#encodings) defines — a fixed one, the same number of bytes for every value, and a packed one, as many bytes as the value needs — and every method that touches bytes takes the one it means as a type parameter `E`:

- `Slotted`, the fixed encoding, which a slotted place holds;
- `Packed`, the packed encoding, which a packed place holds.

`Encoding` is a sealed trait with these two implementations and a constant `PACKED`, so a method generic over `E` branches on a constant, which the compiler folds: the code for slotted places has static offsets and fixed-width writes, with no test of the encoding at run time.

### `SLOTTED_SIZE`, `PACKED_SIZE`, and `RootEncoding`

**`SLOTTED_SIZE` is the size of the type's fixed encoding — the slot a slotted place reserves for it — or `None` if it has none.**

- a scalar: its own width;
- a derived struct: the sum of its fields';
- a derived enum: a discriminant of 1, 2, 4 or 8 bytes plus the largest variant;
- an owning container: its pointer, *not* the size of its content;
- a small value: `None`, since a fixed encoding would reserve its whole inline capacity.

A constant slot size is what makes the offsets of slotted fields statically computable, and computing them at compile time is what makes field access free.

**`PACKED_SIZE` is the size of the packed encoding, if every value's is the same** — `Some` exactly for a fixed-size type, whose two encodings are one: floats, bytes, `bool`, and structs of them.

**`RootEncoding` is the encoding of a place that leaves the choice to the type**, such as a root: `Slotted` if the type has a fixed encoding, `Packed` if it does not.
It is a type rather than a constant because it chooses the type of the root's guard, which a constant cannot.
The derive joins its fields' with `Encoding::Join`, `Slotted` only if all are.

### `Slottable`

```rust
pub trait Slottable<P: PointerRepr = Pointer>: Persistable<P> {
    const SLOT_SIZE: usize = /* SLOTTED_SIZE, unwrapped */;
}
```

A type with a fixed encoding, and so one that may stand in a slotted place.
It is what a slotted container such as `PersistableVec` requires of its elements, and a field declared `#[kladde(slotted)]` of its type, so that a type without a fixed encoding there fails to compile at `cargo check`, with a message that points to the packed containers.
The derive implements it for every type whose fields all have a fixed encoding; see [The derive macro](derive-macro.md#slotted-fields-and-packed-only-types).

### `Place`

```rust
pub struct Place<'a, B: WriteBackend, E: Encoding> { /* a Link, and E */ }
```

Where a guard's value lives, which encoding the place holds, and whom to tell when the value's size changes.
`Slotted::at(location)` and `Packed::at(location)` make one for a fixed location, which is all an application ever builds; composite guards build the places of their children.

A place holds a **`Link`**: either a fixed `Location` — an anchor allocation and an offset — with nobody to tell, or a reference to the parent's **`Node`** and the child's index among the parent's children.
The node is what a parent that keeps track of its children implements: where each child starts, and what a child's change of size requires of it.

```rust
pub trait Node<B: WriteBackend> {
    fn location_of(&self, owner: &Link<'_, B>, index: usize) -> Location<B::Pointer, B::Size>;
    fn resized(&self, owner: &Link<'_, B>, backend: &B, index: usize, old: usize, new: usize)
        -> Result<(), Error>;
}
```

A composite guard's node is a `FieldOffsets` it keeps beside the value, a packed vector's the offsets it keeps in the value, a small vector's its tag and offsets.
The methods take the owner's link, so that a node can live in the value rather than in its guard, and they take `&self`, with the offsets in `Cell`s, so that a child's guard can hold a shared reference to the node while it holds a mutable one to the child — the two are disjoint fields.
[Packed values](../impl/packed-values.md#finding-a-value-ask-the-parent) explains why a value finds itself through its parent rather than holding a location.

### Storing: `prepare`, then `encode`

**A value is stored by preparing it and then encoding it into one buffer, which the provided `store` writes with one record.**
`prepare` creates and fills every allocation the value owns but does not have yet, recursively: a vector built by `from_iter` holds real content but no pointer, because nothing gave it a backend, and its pointer's packed encoding depends on the id its allocation gets.
`encode` then appends the value's encoding `E` to a buffer, and `encoded_size` says how long it is; both write pointers as they stand, which is why `prepare` comes first.

`prepare` takes `&mut self` because the value learns its pointers there.
If it took `&self`, it could still allocate and write correctly — but the caller's copy would stay stuck believing it has no allocation, breaking any guard obtained from it afterwards.

`replace`, the whole-value `set` of nearly every guard, does all of it in one transaction: it prepares the new value, writes its encoding over the old one — in place if the size stays, as a `Splice` its ancestors hear about if not — and frees what the old value owned.

### Loading: `decode`

`decode` reads a value from an `Input`, a cursor over the bytes of the allocation that holds it, and advances past it, following pointers into other allocations through the backend.
The provided `load` reads an allocation's bytes once and decodes from them.
Every read checks its bounds, and `decode` refuses bytes that no value encodes to — an overlong varint, UTF-8 that is not well-formed — with `Error::Corrupt` rather than a panic.

`decode` takes `&mut B`: loads are **sequential**, one field or element after another, so a single exclusive borrow reborrowed down the recursion suffices, and the read path reads through a real cursor with no interior mutability at all.
As a free side effect, the borrow checker forbids loading while any guard is alive, since a guard holds a shared borrow.

### `free`

Releases the allocations a value owns, recursively; see [Freeing](freeing.md).

## Guards

```rust
pub trait Guard {
    type Persistable;
    type Backend;
    fn as_persistable(&self) -> &Self::Persistable;
    fn as_persistable_mut(&mut self) -> &mut Self::Persistable;
    fn backend(&self) -> &Self::Backend;
}
```

A guard borrows both the value and a backend for a lifetime — exactly the relationship `MutexGuard` has to `Mutex`, and the naming is deliberate.
It is an RAII token proving exclusive, recording access.

Every mutating method on a guard does two things: it emits the records the mutation requires, and it mutates the in-memory value.
**Mutating methods never return anything for the caller to separately apply.**
Persistence is not optional or forgettable by construction.

Non-mutating access uses the plain value; guards implement `Deref`, so read-only methods stay available inside a mutating context.

### A guard is generic over the encoding of its place

`MyTypeGuard<'s, B, E = Slotted>` is the guard of a value in a place of encoding `E`, and the compiler builds a slotted and a packed guard from the one type.
The slotted guard of a value at a fixed location keeps static offsets and fixed-width writes, as if packing did not exist; what it carries beyond the value, the backend and the place is room for its fields' offsets, which it fills only if its place is a link, since a slotted value inside a packed one sits behind siblings that can grow.
Testing which kind of place it holds, whenever it hands out a field's guard, is the one cost the possibility of packing adds to a program that never packs.

A guard in a packed place writes a value whose size changes as a `Splice` and reports the change through its place, so that every value around it, and every sibling's guard, keeps its bearings; see [What a mutation costs](../impl/packed-values.md#what-a-mutation-costs).

### Mutations return a `Result`

A mutation is durable once it returns, which it can promise only because it appends to the journal before returning — and an append is I/O, which can fail.
So every mutating method returns `Result<_, Error>`, and the order inside it is fixed: **record first, then mutate memory.**
A mutation that fails leaves the in-memory value as it was, and the file at worst holding an allocation that nothing reaches yet, which the [ordering discipline](../spec/journal.md#ordering) permits.
The nodes a change of size passes through keep the same order, recording before they update their offsets, so a failure leaves them matching the value too.

A failed `fsync` [ends the session](../spec/durability.md#fsync-failure-is-fatal), so after one the store is **poisoned**: every later call returns `Error::Poisoned`, and the application drops the `Kladde` and reopens the file, which then holds every mutation that returned `Ok`.

### Guards are generated per type

Not one shared generic wrapper, and the reason is Rust's orphan rules.

A single `Guard<T, B>` defined in the persistence crate would be a *foreign* type from any downstream crate's point of view, and Rust forbids inherent `impl` blocks on foreign types outright, regardless of what their parameters are filled with.
So a library author could not write ordinary methods on their own guard.

Per-type generation means `MyTypeGuard` is local to the crate that declared `MyType`, and ordinary `impl` blocks just work.

### The write/read asymmetry

`WriteBackend`'s methods take `&self`; `ReadBackend`'s take `&mut self`.

**Write is `&self`** because a parent guard holds `&B` and hands each nested field guard the *same* `&B` by reborrow, which is what lets sibling guards coexist.
The interior mutability this requires lives in the concrete backend; see [The store](store.md#interior-mutability).

**Read is `&mut self`** for the reason above: sequential access, real cursors, and the free staleness guarantee.

The two traits are also split like `Read`/`Write` in `std`: `ReadBackend` has *no* write or allocation surface at all, so `&mut impl ReadBackend` genuinely cannot mutate.

## Why an automatic flush cannot tear a mutation

The store flushes by itself once its journal outgrows its budget, and it does so [from inside the mutating call](../impl/transactions-and-batches.md#checkpoint-versus-commit) that pushed it over — while a guard is alive.
That is safe for two reasons, one per layer.

**The flush touches no `Persistable` value.**
It folds journal records into pages; it never reads or writes an in-memory value, so the guard's borrow of the root is irrelevant to it, and the store's own interior borrow is released before the flush takes it again.

**A flush lands only on a transaction boundary.**
It runs after an append, and an append is a whole transaction — a bare operation, a committed transaction, or a batch's ready run — so every state a flush commits is one the [ordering discipline](../spec/journal.md#ordering) already requires to be valid.

## The chain, end to end

```rust
notes.guard()              // NotesGuard, Slotted — at the root allocation, offset 0
     .lines_mut()          // PersistableVecGuard — the same allocation, offset 4
     .push(value)?;        // appends the record, then mutates memory
```

Each `_mut()` reborrows the same backend and hands the field a place: in a slotted value at a fixed location, the location advanced by the field's static offset; anywhere else, a link to the guard's field offsets.
The caller supplies a backend exactly once, at the outermost guard.

## What this costs

- **Verbosity.** `x.guard().field_mut().set(v)?` where other languages write `x.field = v`.
- **Borrow contention.** A guard borrows the root, so nothing else can touch the structure while it lives.
- **Partial borrows only through `parts()`.** Each `_mut()` reborrows the whole guard, so holding the guards of two fields at once takes a `parts()`, which splits the value into its fields; two elements of one vector cannot be held at once at all.

The first is inherent to Rust.
The third is a real limitation with no current answer for containers.
