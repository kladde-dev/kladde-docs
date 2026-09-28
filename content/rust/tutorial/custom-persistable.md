---
title: Writing a custom Persistable
---

*Advanced.*
Most types should be [derived](deriving.md) or built from the [containers](containers.md).
Reach for a hand-written implementation when neither fits.

## When you need one

Three cases.

**A type with non-persisted fields** — a struct carrying a cache or a handle that should not be stored.
The derive macro has no attribute for this yet, so you write an implementation that reads and writes only the fields you want.

**A layout you control precisely** — you know your type's bytes should be, say, a `u32` length followed by a payload, and you want that exact layout rather than what the derive would produce.

**A container** — something with its own allocation and a non-trivial internal structure.
This is what the built-in containers do, and it is the hardest case.

If your type is a foreign one you cannot change, you probably want [`PersistableBlob<T>`](containers.md#persistableblobt) instead, and to accept its costs.

## The trait

```rust
pub trait Persistable<P: PointerRepr = Pointer>: Sized {
    const INLINE_SIZE: usize;

    fn store<B: WriteBackend<Pointer = P>>(
        &mut self, backend: &B, location: Location<P, B::Size>,
    ) -> Result<(), Error>;

    fn load<B: ReadBackend<Pointer = P>>(
        backend: &mut B, location: Location<P, B::Size>,
    ) -> Result<Self, Error>;

    fn free<B: WriteBackend<Pointer = P>>(&mut self, backend: &B) -> Result<(), Error> {
        Ok(())
    }
}
```

Plus a `Guard` type and a `guard()` method for the mutation side, and `describe_local` for the schema.

**`INLINE_SIZE`** is how many bytes your value occupies *inline*, in whatever allocation contains it.
For a scalar, its own width.
For a type that owns a separate allocation, the size of its fixed header — not the size of its contents.
This must be a constant, because it is what makes sibling fields' offsets statically computable.

**`Location`** is where your value's inline bytes live: an anchor pointer plus a byte offset within it.
It is threaded down the same chain that carries the backend.

**`store` takes `&mut self`**, which surprises people.
A value that owns an allocation may not have one yet — a `PersistableVec` built by `from_iter` holds real content but no pointer, because nothing gave it a backend — and `store` is where that allocation happens the first time, so it must record the new pointer in `self`.

**`load` takes `&mut B`**, not `&B`.
Loads are sequential, so a single exclusive borrow reborrowed down the recursion suffices, and it lets the read path hand out a real seekable cursor.

**`free`** releases whatever your value owns; the default, for a type that owns nothing, does nothing.

## What to import

One crate. `kladde` re-exports everything a hand-written impl names, so you never add `kladde-persist` yourself:

```toml
[dependencies]
kladde = { git = "https://github.com/kladde-dev/kladde-rs" }
```

If you are writing a *library* on top of `kladde-persist` and have no reason to pull the facade in, depend on `kladde-persist` directly and point the macro at it instead — see [the derive macro's path resolution](../derive-macro.md#path-resolution).

## A complete example

A fixed-size type owning no allocation of its own.
This is everything the trait requires: the inline size, a guard, the two halves of the round trip, and the descriptor.

<!-- kladde-example: name=rgb file=src/lib.rs deps=kladde -->
```rust
use kladde::{
    Error, Field, Guard, Location, Persistable, PointerRepr, ReadBackend, SchemaBuilder,
    TypeDescriptor, WriteBackend,
};
use std::io::Read;

pub struct Rgb {
    r: u8,
    g: u8,
    b: u8,
}

pub struct RgbGuard<'s, B: WriteBackend> {
    inner: &'s mut Rgb,
    backend: &'s B,
    location: Location<B::Pointer, B::Size>,
}

impl<'s, B: WriteBackend> RgbGuard<'s, B> {
    pub fn set(&mut self, mut value: Rgb) -> Result<(), Error> {
        <Rgb as Persistable<B::Pointer>>::store(&mut value, self.backend, self.location)?;
        *self.inner = value;
        Ok(())
    }
}

impl<'s, B: WriteBackend> Guard for RgbGuard<'s, B> {
    type Persistable = Rgb;
    type Backend = B;
    fn as_persistable(&self) -> &Rgb {
        self.inner
    }
    fn as_persistable_mut(&mut self) -> &mut Rgb {
        self.inner
    }
    fn backend(&self) -> &B {
        self.backend
    }
}

impl<P: PointerRepr> Persistable<P> for Rgb {
    const INLINE_SIZE: usize = 3;

    type Guard<'s, B: WriteBackend<Pointer = P>>
        = RgbGuard<'s, B>
    where
        B: 's;

    fn guard<'s, B: WriteBackend<Pointer = P>>(
        &'s mut self,
        backend: &'s B,
        location: Location<P, B::Size>,
    ) -> RgbGuard<'s, B> {
        RgbGuard { inner: self, backend, location }
    }

    fn store<B: WriteBackend<Pointer = P>>(
        &mut self,
        backend: &B,
        location: Location<P, B::Size>,
    ) -> Result<(), Error> {
        backend.write(location.anchor, location.offset, &[self.r, self.g, self.b])
    }

    fn load<B: ReadBackend<Pointer = P>>(
        backend: &mut B,
        location: Location<P, B::Size>,
    ) -> Result<Self, Error> {
        let mut buf = [0u8; 3];
        backend.read_at(location.anchor, location.offset)?.read_exact(&mut buf)?;
        Ok(Rgb { r: buf[0], g: buf[1], b: buf[2] })
    }

    fn describe_local(builder: &mut SchemaBuilder) -> TypeDescriptor {
        TypeDescriptor::Struct {
            name: "Rgb".into(),
            fields: ["r", "g", "b"]
                .into_iter()
                .map(|name| Field {
                    name: name.into(),
                    ty: <u8 as Persistable<P>>::describe(builder),
                })
                .collect(),
        }
    }
}
```

Four things worth noting.

`INLINE_SIZE` is 3 because `store` writes exactly three bytes.
Every offset computed by a containing struct depends on that number being right.

**The guard is a separate type**, generated for you by the derive macro but written out here.
It holds the value, the backend and the location, and its mutating methods do both halves in order: record the bytes, then update the in-memory value, so that a failed append leaves the value as it was.

**`describe_local` declares what the bytes are, not what the Rust type is.**
`Rgb` writes three consecutive `u8`s, so it declares a `Struct` of three `u8` fields — even though the implementation is hand-written.
Declare `Opaque` only when the representation genuinely is not decomposable; see [Your descriptor](#your-descriptor).

The impl is generic over `P: PointerRepr`, so `Rgb` works at any pointer width.
A type that *holds* a pointer is written for one `P`.

## Owning an allocation

If your type owns content, it holds an `Option<UniquePointer>` — `None` until first stored — and its inline representation is that pointer.
The pattern:

- in `store`, allocate if `None` — `backend.alloc(size)` — and write the content;
- write the pointer itself *last*, since it is what makes the content reachable;
- in `free`, free what the content owns, then the allocation itself;
- for a whole-value `set` on its guard, call `kladde::replace`, which stores the new value and then frees the old one, in one transaction.

That ordering is not stylistic.
It is the [ordering discipline](../../spec/journal.md#ordering): a crash between the content write and the pointer write must leave a valid, if stale, state, and publishing the pointer first would leave it pointing at content that was never written.
The rule generalizes to *prepare new state → one publishing write → clean up what it replaced*.

## Your descriptor

A hand-written implementation must also declare its [type descriptor](../../spec/schema/type-descriptors.md), and the rule is easy to get wrong:

> Declare the descriptor that matches **the bytes you actually read and write**, not the shape of your Rust type.

A struct with a non-persisted cache field declares the struct *without* that field.
Declare `Opaque` only when your representation genuinely is not decomposable into fields and variants — a length-prefixed blob, an externally serialized payload.
"Hand-written" and "opaque" are different axes, and conflating them makes your type needlessly invisible to [tooling](../../spec/tooling.md).

## The obligations you are taking on

A hand-written implementation is trusted, not checked.
You are promising:

- `INLINE_SIZE` matches what `store` writes and `load` reads;
- `store` and `load` are exact inverses;
- writes within one mutation are ordered so every prefix is valid;
- `free` releases exactly what the value owns;
- your declared descriptor matches your actual bytes.

None of these are enforced by the compiler.
This is the same bargain `std`'s collections make with `unsafe`, and the reason the derive macro is the right default.
