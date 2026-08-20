---
title: Persistable and guards
---

The central traits, and why mutation needs a guard.

## `Persistable`

```rust
pub trait Persistable<P: PointerRepr = Pointer>: Sized {
    const INLINE_SIZE: usize;

    type Guard<'s, B>: Guard<Persistable = Self, Backend = B>;
    fn guard<'s, B: WriteBackend<Pointer = P>>(
        &'s mut self, backend: &'s B, location: Location<P, B::Size>,
    ) -> Self::Guard<'s, B>;

    fn store<B: WriteBackend<Pointer = P>>(
        &mut self, backend: &B, location: Location<P, B::Size>,
    );
    fn load<B: ReadBackend<Pointer = P>>(
        backend: &mut B, location: Location<P, B::Size>,
    ) -> Self;
}
```

A `Persistable` is a plain, in-memory value that *has the right shape* to be persisted, but is not itself tied to any backend.
Reading it is ordinary reading.

### `INLINE_SIZE`

Every type has a fixed-size **inline representation** — what a containing struct reserves for it, regardless of how much variable-length content it may own elsewhere:

- a scalar: its own bytes;
- a derived struct: the sum of its fields';
- a derived enum: a discriminant plus the largest variant;
- an owning type: a fixed header, *not* the size of its content.

Having *some* fixed inline size is what makes sibling fields' offsets statically computable, and computing them at compile time is what makes field access free.

This is also the biggest thing a port has to think about.
In a language without compile-time constant evaluation, offsets have to be computed some other way — most likely once per type at startup, which is fine but reshapes the code.

### `Location`

```rust
struct Location<P, S> { anchor: P, offset: S }
```

Where a value's inline bytes live: the nearest ancestor that owns a real allocation, plus a byte offset within it.

A deeply nested leaf does not know or care what type its ancestor's allocation was created as, which is why the anchor is the type-erased `Pointer`.
`Location` is threaded down the same reborrow chain that carries the backend.

### Why `store` takes `&mut self`

The surprising signature, and the reason is real.

A value that owns an allocation may not have one yet.
A vector built by `from_iter` holds real content but no pointer, because nothing gave it a backend.
`store` is where that allocation happens the first time.

If `store` took `&self`, it could still allocate and write correctly — but the caller's copy would stay stuck believing it has no allocation, breaking any guard obtained from it afterwards.
Containers keep the value they are storing alive for further mutation, so this is not hypothetical.

### Why `load` takes `&mut B`

Loads are **sequential** — one field or element after another — so a single exclusive borrow reborrowed down the recursion suffices.
That lets the read path hand out a real seekable cursor with no interior mutability at all.

As a free side effect, the borrow checker forbids loading while any guard is alive, since a guard holds a shared borrow.
Automatic "do not read stale data mid-write."

## Guards

```rust
pub trait Guard {
    type Persistable: Persistable;
    type Backend;
    fn as_persistable(&self) -> &Self::Persistable;
    fn as_persistable_mut(&mut self) -> &mut Self::Persistable;
    fn backend(&self) -> &Self::Backend;
}
```

A guard borrows both the value and a backend for a lifetime — exactly the relationship `MutexGuard` has to `Mutex`, and the naming is deliberate.
It is an RAII token proving exclusive, recording access.

Every mutating method on a guard does two things: it mutates the in-memory value, and it emits the records that mutation requires.
**Mutating methods never return anything for the caller to separately apply.**
Persistence is not optional or forgettable by construction.

Non-mutating access uses the plain value; guards implement `Deref`, so read-only methods stay available inside a mutating context.

### Guards are generated per type

Not one shared generic wrapper, and the reason is Rust's orphan rules.

A single `Guard<T, B>` defined in the persistence crate would be a *foreign* type from any downstream crate's point of view, and Rust forbids inherent `impl` blocks on foreign types outright, regardless of what their parameters are filled with.
So a library author could not write ordinary methods on their own guard.

Per-type generation means `MyTypeGuard` is local to the crate that declared `MyType`, and ordinary `impl` blocks just work.

### The write/read asymmetry

`WriteBackend`'s methods take `&self`; `ReadBackend`'s take `&mut self`.

**Write is `&self`** because a parent guard holds `&B` and hands each nested field guard the *same* `&B` by reborrow.
That is what lets sibling guards coexist.
The interior mutability this requires lives in the concrete backend, never in the heap.

**Read is `&mut self`** for the reason above: sequential access, real cursors, and the free staleness guarantee.

The two traits are also split like `Read`/`Write` in `std`: `ReadBackend` has *no* write or allocation surface at all, so `&mut impl ReadBackend` genuinely cannot mutate.
That is what enforces the journaled backend's phase separation — a shared `&Backend` could not, because it would still carry `&self` write methods.

## The chain, end to end

```rust
notes.guard()              // NotesGuard  — anchor = root allocation, offset 0
     .lines_mut()          // PersistableVecGuard — same anchor, offset 4
     .push(value);         // mutates memory, emits records
```

Each `_mut()` reborrows the same backend and extends the location by that field's static offset.
The caller supplies a backend exactly once, at the outermost guard.

## What this costs

Honest accounting.

- **Verbosity.** `x.guard().field_mut().set(v)` where other languages write `x.field = v`.
- **Borrow contention.** A guard borrows the root, so nothing else can touch the structure while it lives.
  Usually right, occasionally annoying.
- **No partial borrows.** You cannot hold guards on two different fields at once, because each reborrows the root.

The first is inherent to Rust.
The second is a feature disguised as a cost — it is what makes the [auto-flush safety argument](../journal/crash-consistency.md#why-an-auto-flush-cannot-tear-a-mutation) work.
The third is a real limitation with no current answer.
