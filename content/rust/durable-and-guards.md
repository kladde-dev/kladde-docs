---
title: Durable and guards
---

The central traits, why mutation needs a guard, and why it returns a `Result`.

**A data type encodes, decodes, and mutates its region; it never allocates, frees, or knows a position.**

## `Durable`

```rust
pub trait Durable: Sized {
    /// `Some(n)` if every value encodes to `n` bytes.
    const FIXED_SIZE: Option<usize>;

    fn encoded_len(&self) -> usize;
    fn encode(&self, out: &mut Encoder<'_>);
    fn decode(input: &mut Decoder<'_>) -> Result<Self, Error>;

    type Guard<'r, B: Backend + 'r>: Guard where Self: 'r;
    fn guard<'r, B: Backend + 'r>(&'r mut self, region: Region<'r, B>) -> Self::Guard<'r, B>;

    fn describe_local(builder: &mut SchemaBuilder) -> TypeDescriptor;
}

/// A type with a fixed form, which `Fixed<Self>` holds.
#[diagnostic::on_unimplemented(message = "`{Self}` has no fixed form", /* ... */)]
pub trait Bounded: Durable {
    const FIXED_FORM_SIZE: usize;
    fn encode_fixed(&self, out: &mut Encoder<'_>);
    fn decode_fixed(input: &mut Decoder<'_>) -> Result<Self, Error>;
}

pub struct Fixed<T: Bounded>(pub T);
```

A `Durable` is a plain, in-memory value that *has the right shape* to be persisted, but is not itself tied to any backend.
Reading it is ordinary reading.
Besides `describe_local`, the trait offers `to_bytes`, `from_bytes`, `describe`, `schema` and `fingerprint`, with defaults.

### `FIXED_SIZE`

`Some(n)` if every value encodes to `n` bytes, as the [specification](../spec/schema/type-descriptors.md#fixed-size-types) has it, and `None` otherwise.
A vector decides by it whether to keep an index of where its elements start, and the derive by it whether a field after another has a constant offset.
It stays an `Option` on `Durable`, rather than a subtrait, because a vector branches on it at compile time, which a subtrait could support only with specialization.

**A `Box<T>`'s `FIXED_SIZE` is `None` whatever `T`'s**: a type that holds itself through a box would otherwise ask its own size of itself while the compiler evaluates it, which rustc refuses as a cycle (E0391) rather than reading it as "not fixed-size".
The derive cannot tell a recursive type from the tokens of one type, and a box is the one indirection among kladde's types that could pass a fixed size on, so answering `None` there cuts every cycle; a vector of boxes then keeps an index it could do without, and nothing on file changes.

### `encode` and `decode`

`encode` appends the value's encoding to an `Encoder`, and `decode` reads it from a `Decoder`; they differ because writing goes into one buffer, while reading crosses spans.
An `Encoder` appends to a buffer it borrows and offers varints, zigzag, and UTF-8 with its terminator; a value is encoded once per record, into the value's own buffer, which the store then copies into the record — a copy a store method taking an encoding closure would save.
A `Decoder` reads the sequence from the mirror through the rope, across span boundaries, without copying, refuses an encoding that is not canonical, and says where a decode failed.
`encoded_len` takes constant time for every type of the workspace: containers cache their total.

### `Bounded` and `Fixed<T>`

`Fixed<T>` holds `T`'s values in their [fixed form](../spec/schema/type-descriptors.md#fixed), which a subtrait, `Bounded`, states, so that no method panics for a type without one, and `Fixed<DurableString>` fails to compile where it is written, with a message that names the fix.
The scalars, `Fixed`, and the `Option`s, `Box`es and tuples of `Bounded` types are `Bounded`, and a derived type is if it is marked `#[kladde(bounded)]` ([the derive](derive-macro.md#fixed-forms)).
A `Box<T>` takes `T`'s fixed form, as it takes `T`'s encoding and descriptor, so boxing a field keeps `Fixed` of what holds it.
Its conditional impl never meets the cycle that its `FIXED_SIZE` cuts, since a recursive type is never `Bounded`; a recursive type marked `#[kladde(bounded)]` fails to compile, though with rustc's cycle error (E0391) rather than kladde's message.
A type with a fixed size and no fixed form is possible, and harmless, since nothing can wrap it in `Fixed`.

`FIXED_FORM_SIZE` is the bound on a value's size that `Fixed<T>` needs, so `Durable` declares no other maximum: `Fixed<T>` stores the fixed form, every integer at its full width, rather than `T`'s encoding padded to its longest, in which a field would move whenever a varint before it grew.
A fixed-size type's fixed form is its encoding byte for byte, except where an enum's integer `#[repr]` sets a discriminant wider than the one byte its encoding takes: `#[repr(u32)] enum Pinned { A, B }` has `FIXED_SIZE` 1 and `FIXED_FORM_SIZE` 4.

**The guard of a `Fixed<T>` replaces the value whole**, an overwrite of its fixed size, since a guard of one of `T`'s fields would have to write that field in its fixed form, which would take a second guard for every type.
That is visible to the caller: the guard offers `set(Fixed<T>)` and reading, and no guard of a field of `T`.

## Regions

```rust
pub trait Backend: WriteBackend + Copy {}   // any `WriteBackend` that is `Copy`, such as `&Store`
pub struct Region<'r, B> { /* the backend, and where the value starts and how long it is */ }
```

A region is the value's substring of the sequence, as [the reference algorithms](../impl/values.md#a-value-is-a-region) describe it: `len()`, `write(at, bytes)`, `splice(at, removed, bytes)`, `splice_value(at, removed, &value)`, `move_bytes(from, len, to)`, `paste(at, removed, cut)`, `transaction(f)`, and `child(at, len, node, index)`.
`Backend` is any `WriteBackend` that is `Copy`, so that a guard hands its backend to its children and to a cut by value.

A region's start is one of three things: the root's, an offset from an anchor region that is constant while the region lives, or *indexed* — looked up in its parent's `Node` at every write, for a region made together with its siblings.
When a region changes size, it tells the `Node` it was made from, which updates where the children behind it start, and its parent's region, which tells its own parent in turn; lengths and indexes sit in `Cell`s, so that regions made together, which share their parent by shared reference, can report through it.

`transaction(f)` poisons the store if `f` fails, since the regions around may have heard of a size change by then; a guard checks what can fail before it opens one.

A child made on its own borrows its parent region mutably, so the parent cannot write while the child exists, and no sibling can move it.

## Guards

```rust
pub trait Guard {
    type Durable;
    fn as_durable(&self) -> &Self::Durable;
}
```

A guard borrows the value and its region for a lifetime — exactly the relationship `MutexGuard` has to `Mutex`, and the naming is deliberate.
It is an RAII token proving exclusive, recording access.
A guard's `Durable` need not be `Self`, since a `Box<T>` uses `T`'s guard.

Every mutating method on a guard does two things: it records the mutation through its region, and it mutates the in-memory value.
**Mutating methods never return anything for the caller to separately apply.**
Persistence is not optional or forgettable by construction.

Non-mutating access uses the plain value; guards implement `Deref`, and not `DerefMut`, which would let `guard.field = value` compile and persist nothing.
Scalars, and anything whose guard offers only a whole-value `set`, use the generic `SetGuard`.

### Mutations return a `Result`

A mutation is durable once it returns, which it can promise only because it appends to the journal before returning — and an append is I/O, which can fail.
So every mutating method returns `Result<_, Error>`, and the order inside it is fixed: **record first, then mutate memory.**
A mutation that fails leaves the in-memory value as it was, and records nothing; a mutation of several records is one transaction, appended whole or not at all.

A failed `fsync` [ends the session](../spec/durability.md#fsync-failure-is-fatal), so after one the store is **poisoned**: every later call returns `Error::Poisoned`, and the application drops the `Kladde` and reopens the file, which then holds every mutation that returned `Ok`.

### Guards are generated per type

Not one shared generic wrapper, and the reason is Rust's orphan rules.
A single `Guard<T, B>` defined in the persistence crate would be a *foreign* type from any downstream crate's point of view, and Rust forbids inherent `impl` blocks on foreign types, regardless of what their parameters are filled with.
So a library author could not write ordinary methods on their own guard.
Per-type generation means `MyTypeGuard` is local to the crate that declared `MyType`, and ordinary `impl` blocks just work.

## Why an automatic flush cannot tear a mutation

The store flushes by itself whenever a mutation would not fit in what is left of its journal's budget, and it does so [from inside the mutating call](../impl/transactions-and-batches.md#checkpoint-versus-commit) that closed it — while a guard is alive.
That is safe for two reasons, one per layer.

**The flush touches no `Durable` value and moves no position.**
It folds journal records into pages; it never reads or writes an in-memory value, and positions in the sequence, which guards hold, mean the same after it.

**A flush lands only on a transaction boundary.**
It runs between appends, and every append is a whole transaction, so every state a flush commits is a [valid sequence](../spec/sequence.md#what-a-valid-sequence-is).

## The chain, end to end

```rust
notes.guard()              // NotesGuard  — the root's region: the whole sequence
     .lines_mut()          // DurableVecGuard — a child region, after the title
     .push(value)?;        // splices the element and the count, then mutates memory
```

Each `_mut()` makes a child region from its parent's, at the field's offset, and reborrows the same backend.
The caller supplies a backend exactly once, at the outermost guard.

## What this costs

- **Verbosity.** `x.guard().field_mut().set(v)?` where other languages write `x.field = v`.
- **Borrow contention.** A guard borrows the root, so nothing else can touch the structure while it lives.
- **Partial borrows only through `parts()`.** Guards on two fields at once come from `parts()`, which looks each field's start up at every write; there is no `split_at_mut` for a vector yet.
