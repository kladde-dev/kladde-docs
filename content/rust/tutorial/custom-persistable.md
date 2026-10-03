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
    const SLOTTED_SIZE: Option<usize>;
    const PACKED_SIZE: Option<usize>;
    type RootEncoding: Encoding;

    fn encoded_size<E: Encoding>(&self) -> usize;
    fn encode<E: Encoding>(&self, out: &mut Vec<u8>);
    fn decode<B: ReadBackend<Pointer = P>, E: Encoding>(
        backend: &mut B, input: &mut Input<'_>,
    ) -> Result<Self, Error>;

    fn prepare<B: WriteBackend<Pointer = P>>(&mut self, backend: &B) -> Result<(), Error> {
        Ok(())
    }
    fn free<B: WriteBackend<Pointer = P>>(&mut self, backend: &B) -> Result<(), Error> {
        Ok(())
    }
}
```

Plus a `Guard` type and a `guard()` method for the mutation side, and `describe_local` for the schema.

**Your type has two encodings**, and every method that touches bytes takes the one it means as a type parameter `E`: `Slotted`, a fixed encoding that takes the same number of bytes for every value, or `Packed`, as many bytes as the value needs.
A value stands in a slotted place — a field of an ordinary struct, an element of a `PersistableVec` — or in a packed one, an element of a `PackedPersistableVec` and everything inside it.
For a type whose bytes are always the same length, the two are one, and you can ignore `E`.

**`SLOTTED_SIZE`** is how many bytes your fixed encoding takes, the slot a slotted place reserves for it.
For a scalar, its own width.
For a type that owns a separate allocation, the size of its pointer — not the size of its contents.
It is a constant because it is what makes the offsets of slotted fields statically computable.
A type with no fixed encoding at all says `None`, and stands only in packed places.

**`PACKED_SIZE`** is the size of your packed encoding if every value's is the same, and `None` otherwise.

**`RootEncoding`** is `Slotted` if your type has a fixed encoding, and `Packed` if it does not: what a root of your type holds.

**`encode`** appends your value's bytes in encoding `E`, and **`encoded_size`** says how many that is.
**`decode`** reads them back from an `Input`, a cursor over the bytes of the allocation that holds your value, advancing past them; it fails with `Error::Corrupt` on bytes no value encodes to.

**`prepare` takes `&mut self`**, which surprises people.
A value that owns an allocation may not have one yet — a `PersistableVec` built by `from_iter` holds real content but no pointer, because nothing gave it a backend — and `prepare` is where that allocation happens the first time, before anything encodes the value, so it must record the new pointer in `self`.

**`decode` takes `&mut B`**, not `&B`.
Loads are sequential, so a single exclusive borrow reborrowed down the recursion suffices, and it lets the read path follow your pointers into other allocations through a real cursor.

**`free`** releases whatever your value owns; the default, for a type that owns nothing, does nothing.

The trait provides `store` and `load` on top: `store` prepares a value and writes its encoding with one record, and `load` reads an allocation's bytes and decodes from them.

## What to import

One crate. `kladde` re-exports everything a hand-written impl names, so you never add `kladde-persist` yourself:

```toml
[dependencies]
kladde = { git = "https://github.com/kladde-dev/kladde-rs" }
```

If you are writing a *library* on top of `kladde-persist` and have no reason to pull the facade in, depend on `kladde-persist` directly and point the macro at it instead — see [the derive macro's path resolution](../derive-macro.md#path-resolution).

## A complete example

A fixed-size type owning no allocation of its own.
This is everything the trait requires: the sizes, a guard, the two halves of the round trip, and the descriptor.

<!-- kladde-example: name=rgb file=src/lib.rs deps=kladde -->
```rust
use kladde::{
    write_encoded, Encoding, Error, Field, Guard, Input, Persistable, Place, PointerRepr,
    ReadBackend, SchemaBuilder, Slottable, Slotted, TypeDescriptor, WriteBackend,
};

pub struct Rgb {
    r: u8,
    g: u8,
    b: u8,
}

pub struct RgbGuard<'s, B: WriteBackend, E: Encoding> {
    inner: &'s mut Rgb,
    backend: &'s B,
    place: Place<'s, B, E>,
}

impl<'s, B: WriteBackend, E: Encoding> RgbGuard<'s, B, E> {
    pub fn set(&mut self, value: Rgb) -> Result<(), Error> {
        write_encoded(self.backend, &self.place, 3, &[value.r, value.g, value.b])?;
        *self.inner = value;
        Ok(())
    }
}

impl<'s, B: WriteBackend, E: Encoding> Guard for RgbGuard<'s, B, E> {
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

impl<P: PointerRepr> Slottable<P> for Rgb {}

impl<P: PointerRepr> Persistable<P> for Rgb {
    const SLOTTED_SIZE: Option<usize> = Some(3);
    const PACKED_SIZE: Option<usize> = Some(3);
    type RootEncoding = Slotted;

    type Guard<'s, B: WriteBackend<Pointer = P>, E: Encoding>
        = RgbGuard<'s, B, E>
    where
        B: 's;

    fn guard<'s, B: WriteBackend<Pointer = P>, E: Encoding>(
        &'s mut self,
        backend: &'s B,
        place: Place<'s, B, E>,
    ) -> RgbGuard<'s, B, E> {
        RgbGuard { inner: self, backend, place }
    }

    fn encoded_size<E: Encoding>(&self) -> usize {
        3
    }

    fn encode<E: Encoding>(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&[self.r, self.g, self.b]);
    }

    fn decode<B: ReadBackend<Pointer = P>, E: Encoding>(
        _backend: &mut B,
        input: &mut Input<'_>,
    ) -> Result<Self, Error> {
        let [r, g, b] = input.array()?;
        Ok(Rgb { r, g, b })
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

Five things worth noting.

The sizes are 3 because `encode` writes exactly three bytes in either encoding.
Every offset computed by a containing struct depends on that number being right.

**The guard is a separate type**, generated for you by the derive macro but written out here.
It holds the value, the backend and its place, and its mutating methods do both halves in order: record the bytes, then update the in-memory value, so that a failed append leaves the value as it was.

**The guard writes through `write_encoded`**, which writes the new bytes in place when they are as long as the old ones, and otherwise splices them in and tells the values around this one, in one transaction.
`Rgb`'s bytes never change length, but writing through it keeps the guard right for a type whose bytes do.
And the guard asks its place for its location at every write, rather than keeping one, since in a packed place a sibling that grows can move it.

**`describe_local` declares what the bytes are, not what the Rust type is.**
`Rgb` writes three consecutive `u8`s, so it declares a `Struct` of three `u8` fields — even though the implementation is hand-written.
Declare `Opaque` only when the representation genuinely is not decomposable; see [Your descriptor](#your-descriptor).

The impl is generic over `P: PointerRepr`, so `Rgb` works at any pointer width, and it implements `Slottable`, since it has a fixed encoding.
A type that *holds* a pointer is written for one `P`.

## Owning an allocation

If your type owns content, it holds an `Option<UniquePointer>` — `None` until first prepared — and its encoding is that pointer.
The pattern:

- in `prepare`, allocate if `None` — `backend.alloc(size)` — and write the content, before anything encodes the pointer;
- encode the pointer as the place's encoding asks: four bytes, little-endian, in a slotted place, and the varint of its id in a packed one, with zero for none in either;
- in `free`, free what the content owns, then the allocation itself;
- for a whole-value `set` on its guard, call `kladde::replace`, which prepares the new value, writes it, and then frees the old one, in one transaction.

That ordering is not stylistic.
It is the [ordering discipline](../../spec/journal.md#ordering): a crash between the content write and the pointer write must leave a valid, if stale, state, and publishing the pointer first would leave it pointing at content that was never written.
The rule generalizes to *prepare new state → one publishing write → clean up what it replaced*.

The easiest way to own content is often to hold a container that already does — a byte vector, a string — and delegate to it, as the built-in blob does.

## Your descriptor

A hand-written implementation must also declare its [type descriptor](../../spec/schema/type-descriptors.md), and the rule is easy to get wrong:

> Declare the descriptor that matches **the bytes you actually read and write**, not the shape of your Rust type.

A struct with a non-persisted cache field declares the struct *without* that field.
A type that owns an allocation declares a `Pointer` to what the allocation holds — a `Sequence` of elements, `Packed` if they are packed — so that a tool can follow it.
Declare `Opaque` only when your representation genuinely is not decomposable into the other kinds — an externally serialized payload, a format of your own.
"Hand-written" and "opaque" are different axes, and conflating them makes your type needlessly invisible to [tooling](../../spec/tooling.md).

## The obligations you are taking on

A hand-written implementation is trusted, not checked.
You are promising:

- `encoded_size` matches what `encode` writes, and `SLOTTED_SIZE` matches the fixed encoding;
- `encode` and `decode` are exact inverses, in both encodings;
- your packed encoding is [canonical](../../spec/schema/type-descriptors.md#canonical-form), and `decode` refuses one that is not;
- writes within one mutation are ordered so every prefix is valid;
- `free` releases exactly what the value owns;
- your declared descriptor matches your actual bytes.

None of these are enforced by the compiler.
This is the same bargain `std`'s collections make with `unsafe`, and the reason the derive macro is the right default.
