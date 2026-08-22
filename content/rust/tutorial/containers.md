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

One exception exists today: [generic tooling](../../spec/tooling.md) will initially carry built-in knowledge of the opaque types this crate declares, so that a tool can render a vector or a map without knowing the library that wrote it.
That is a stopgap rather than a privilege.
The intended replacement is a mechanism by which *any* opaque type can optionally describe itself to tooling, at which point `kladde-types` becomes ordinary in that respect too.

## `PersistableVec<T>`

A growable sequence.
`T` must be `Persistable`, and nothing more — no `Clone`, no `Serialize`.

```rust
let mut v = journal.guard().entries_mut();
v.push(PersistableString::from("a"));
v.push(PersistableString::from("b"));
```

**Layout.** A fixed-size inline header plus a separate allocation holding a dense array of fixed-size element slots — the same shape as `Vec<T>`'s in memory.
Growing resizes that allocation.

**Cost.** A push that does not grow is one write.
A push that grows is a resize plus a write.
Removal from the middle shifts the tail, exactly as `Vec` does.

## `PersistableString`

Growable text.

```rust
guard.owner_mut().set(PersistableString::from("ada"));
```

It is a thin wrapper around `PersistableVec<u8>`, which is more interesting than it sounds.
Plain `String` deliberately has **no** `Persistable` implementation, and that absence is the enforcement mechanism: a struct field typed `String` simply fails to compile, rather than silently persisting nothing.
`PersistableString` exists because a backed string needs a field of its own to remember which allocation holds its bytes, and `String` has nowhere to put one.

## `PersistableHashMap<K, V>`

A key-value map.
`K: Eq + Hash + Persistable`, `V: Persistable`.

```rust
let mut contacts = guard.contacts_mut();
contacts.insert(PersistableString::from("ada"), contact);
```

**Layout.** An array of fixed-size slots, each a one-byte liveness tag followed by a `(K, V)` pair.
The on-disk form does *not* support key lookup at all — that is what the in-memory map is for — so it needs no hashing structure on disk.

**Removal is tombstoning**: the tag is cleared in place and nothing else moves.
This is what lets each key be stored exactly once in memory, and is why `K: Clone` is not required.

**The trade-off:** the on-disk array's length is a *capacity* — the highest slot ever used — not a live count.
Sustained insert/remove churn grows it without bound until a compaction pass reclaims tombstoned slots.
That pass does not exist yet.

**Keys are immutable.**
No guard is ever handed out for a key; `Persistable` on `K` governs only how its bytes are read and written.

## `PersistableBlob<T>`

An escape hatch for foreign types.

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

Gated behind `kladde-types`' `serde` feature — it is the only thing in the workspace that needs `serde` at all.

Two constructors, and the difference matters for leaks:

- `PersistableBlob::new(value, backend)` allocates eagerly, so it always has an allocation to reuse on the next store.
- `PersistableBlob::default()` stays lazy, holding no allocation, paired with the invariant that its value is `T::default()`.

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
