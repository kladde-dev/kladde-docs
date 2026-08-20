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
        &mut self,
        backend: &B,
        location: Location<P, B::Size>,
    );

    fn load<B: ReadBackend<Pointer = P>>(
        backend: &mut B,
        location: Location<P, B::Size>,
    ) -> Self;
}
```

Plus a `Guard` type and a `guard()` method for the mutation side.

**`INLINE_SIZE`** is how many bytes your value occupies *inline*, in whatever allocation contains it.
For a scalar, its own width.
For a type that owns a separate allocation, the size of its fixed header — not the size of its contents.
This must be a constant, because it is what makes sibling fields' offsets statically computable.

**`Location`** is where your value's inline bytes live: an anchor pointer plus a byte offset within it.
It is threaded down the same chain that carries the backend.

**`store` takes `&mut self`**, which surprises people.
The reason: a value that owns an allocation may not have one yet.
A `PersistableVec` built by `from_iter` holds real content but no pointer, because nothing gave it a backend.
`store` is where that allocation happens the first time — and it must record the new pointer in `self`, or the caller's copy stays permanently out of sync and any later guard on it is broken.

**`load` takes `&mut B`**, not `&B`.
Loads are sequential, so a single exclusive borrow reborrowed down the recursion suffices, and it lets the read path hand out a real seekable cursor.
It also means the borrow checker forbids loading while any guard is alive, which is a free correctness property.

## A minimal example

A fixed-size type with no allocation of its own:

```rust
struct Rgb { r: u8, g: u8, b: u8 }

impl<P: PointerRepr> Persistable<P> for Rgb {
    const INLINE_SIZE: usize = 3;

    fn store<B: WriteBackend<Pointer = P>>(
        &mut self, backend: &B, location: Location<P, B::Size>,
    ) {
        backend.write(location.anchor, location.offset, &[self.r, self.g, self.b]);
    }

    fn load<B: ReadBackend<Pointer = P>>(
        backend: &mut B, location: Location<P, B::Size>,
    ) -> Self {
        let mut buf = [0u8; 3];
        backend.read_at(location.anchor, location.offset)
            .read_exact(&mut buf)
            .expect("read rgb");
        Rgb { r: buf[0], g: buf[1], b: buf[2] }
    }
}
```

## Owning an allocation

If your type owns content, it holds a pointer and its inline representation is a fixed header.
The pattern:

- keep an `Option<UniquePointerResizable>` in the type — `None` until first stored;
- in `store`, allocate if `None`, resize if the content grew, write the content, then write the header;
- **order those writes** so the header — which is what makes the content reachable — is written *last*.

That last point is not stylistic.
It is the [prefix-replay discipline](../../spec/journal.md#ordering): a crash between the content write and the header write must leave a valid, if stale, state.
Publishing the header first would leave a pointer to uninitialised space.

The rule generalizes to *prepare new state → one publishing write → clean up what it replaced*.

## Your descriptor

A hand-written implementation must also declare its [type descriptor](../../spec/schema/type-descriptors.md), and the rule is easy to get wrong:

> Declare the descriptor that matches **the bytes you actually read and write**, not the shape of your Rust type.

`Rgb` above writes three consecutive `u8`s, so its descriptor is a Struct with three `u8` fields — even though the implementation is hand-written.
A struct with a non-persisted cache field declares the struct *without* that field.

Declare `Opaque` only when your representation genuinely is not decomposable into fields and variants — a length-prefixed blob, an externally serialized payload.
"Hand-written" and "opaque" are different axes, and conflating them makes your type needlessly invisible to [tooling](../../spec/tooling.md).

## Freeing

If your type owns an allocation, it must release it when it is dropped or overwritten.

This mechanism — a type-driven recursive free hook — is **designed but not implemented**.
Until it lands, replacing or dropping an owning value orphans its allocation.
That is harmless against the current in-memory backend and will not be once there is a real file.

See [Freeing](../design/persistence/freeing.md).

## The obligations you are taking on

A hand-written implementation is trusted, not checked.
You are promising:

- `INLINE_SIZE` matches what `store` writes and `load` reads;
- `store` and `load` are exact inverses;
- writes within one mutation are ordered so every prefix is valid;
- every allocation you take is eventually released;
- your declared descriptor matches your actual bytes.

None of these are enforced by the compiler.
This is the same bargain `std`'s collections make with `unsafe`, and the reason the derive macro is the right default.
