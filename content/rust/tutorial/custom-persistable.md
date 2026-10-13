---
title: Writing a custom Persistable
---

*Advanced.*
Most types should be [derived](deriving.md) or built from the [containers](containers.md).
Reach for a hand-written implementation when neither fits.

## When you need one

Three cases.

**A type with non-persisted fields** — a struct carrying a cache or a handle that should not be stored.
The derive macro has no attribute for this yet, so you write an implementation that encodes only the fields you want.

**An encoding you control precisely** — you want your type's bytes to be exactly so, rather than what the derive would produce.

**A container** — something with a non-trivial internal structure, which keeps an index of where its children start.
This is what the built-in containers do, and it is the hardest case.

If your type is a foreign one you cannot change, you probably want [`PersistableBlob<T>`](containers.md#persistableblobt) instead, and to accept its costs.

## The trait

```rust
pub trait Persistable: Sized {
    const FIXED_SIZE: Option<usize>;

    fn encoded_len(&self) -> usize;
    fn encode(&self, out: &mut Encoder<'_>);
    fn decode(input: &mut Decoder<'_>) -> Result<Self, Error>;

    type Guard<'r, B: Backend + 'r>: Guard where Self: 'r;
    fn guard<'r, B: Backend + 'r>(&'r mut self, region: Region<'r, B>) -> Self::Guard<'r, B>;

    fn describe_local(builder: &mut SchemaBuilder) -> TypeDescriptor;
}
```

A value of your type is plain, in-memory data; the trait says how it is encoded into the file's one sequence of bytes, and how a guard mutates that encoding.

**`FIXED_SIZE`** is `Some(n)` if every value encodes to `n` bytes, and `None` if the size depends on the value.
A fixed size lets a containing struct find the fields after yours at constant offsets, and lets a vector of your type find its elements by multiplication and keep no index.

**`encoded_len`, `encode` and `decode`** are the encoding: how many bytes this value takes, writing them into an `Encoder`, and reading them back from a `Decoder`, which fails on bytes that no value encodes to.
They never see where the value lies: the encoder appends to a buffer, and the decoder reads front to back.

**`guard`** borrows the value together with its **region** — the substring of the sequence that holds its encoding — and returns the guard your mutating methods live on.
A region offers `write`, `splice`, `splice_value`, `move_bytes` and `paste`, all at offsets relative to the value's start and checked against its length, and `child` to make a region for a part of the value.

**`describe_local`** builds your type's [descriptor](../../spec/schema/type-descriptors.md), which must match the bytes.

## What to import

One crate. `kladde` re-exports everything a hand-written impl names, so you never add `kladde-persist` yourself:

```toml
[dependencies]
kladde = { git = "https://github.com/kladde-dev/kladde-rs" }
```

If you are writing a *library* on top of `kladde-persist` and have no reason to pull the facade in, depend on `kladde-persist` directly and point the macro at it instead — see [the derive macro's path resolution](../derive-macro.md#path-resolution).

## A complete example

A colour of three bytes, with a guard that sets the whole colour or only its red channel.

<!-- kladde-example: name=rgb file=src/lib.rs deps=kladde -->
```rust
use kladde::{
    Backend, Bounded, Decoder, Encoder, Error, Field, Guard, Persistable, Region, SchemaBuilder,
    TypeDescriptor,
};

#[derive(Clone, Copy)]
pub struct Rgb {
    r: u8,
    g: u8,
    b: u8,
}

pub struct RgbGuard<'r, B> {
    inner: &'r mut Rgb,
    region: Region<'r, B>,
}

impl<B: Backend> RgbGuard<'_, B> {
    /// Replaces the whole colour: three bytes, written in place.
    pub fn set(&mut self, value: Rgb) -> Result<(), Error> {
        self.region.write(0, &[value.r, value.g, value.b])?;
        *self.inner = value;
        Ok(())
    }

    /// Replaces the red channel alone: one byte.
    pub fn set_red(&mut self, r: u8) -> Result<(), Error> {
        self.region.write(0, &[r])?;
        self.inner.r = r;
        Ok(())
    }
}

impl<B> Guard for RgbGuard<'_, B> {
    type Persistable = Rgb;
    fn as_persistable(&self) -> &Rgb {
        self.inner
    }
}

impl<B> std::ops::Deref for RgbGuard<'_, B> {
    type Target = Rgb;
    fn deref(&self) -> &Rgb {
        self.inner
    }
}

impl Persistable for Rgb {
    const FIXED_SIZE: Option<usize> = Some(3);

    fn encoded_len(&self) -> usize {
        3
    }

    fn encode(&self, out: &mut Encoder<'_>) {
        out.bytes(&[self.r, self.g, self.b]);
    }

    fn decode(input: &mut Decoder<'_>) -> Result<Self, Error> {
        let [r, g, b] = input.array()?;
        Ok(Rgb { r, g, b })
    }

    type Guard<'r, B: Backend + 'r> = RgbGuard<'r, B>;

    fn guard<'r, B: Backend + 'r>(&'r mut self, region: Region<'r, B>) -> RgbGuard<'r, B> {
        RgbGuard { inner: self, region }
    }

    fn describe_local(builder: &mut SchemaBuilder) -> TypeDescriptor {
        TypeDescriptor::Struct {
            name: "Rgb".into(),
            fields: ["r", "g", "b"]
                .into_iter()
                .map(|name| Field {
                    name: name.into(),
                    ty: <u8 as Persistable>::describe(builder),
                })
                .collect(),
        }
    }
}

/// The fixed form, which `Fixed<Rgb>` holds: the same three bytes.
impl Bounded for Rgb {
    const FIXED_FORM_SIZE: usize = 3;

    fn encode_fixed(&self, out: &mut Encoder<'_>) {
        self.encode(out);
    }

    fn decode_fixed(input: &mut Decoder<'_>) -> Result<Self, Error> {
        Self::decode(input)
    }
}
```

Four things worth noting.

**`FIXED_SIZE` and `encoded_len` say 3 because `encode` writes exactly three bytes.**
Every offset computed by a containing struct depends on that number being right.

**The guard is a separate type**, generated for you by the derive macro but written out here.
It holds the value and its region, and its mutating methods do both halves in order: record the bytes, then update the in-memory value, so that a failed append leaves the value as it was.
`set_red` writes one byte at offset 0 of the region, which is the red channel's place in the encoding; the guard knows nothing about where the colour lies in the file.

**`describe_local` declares what the bytes are, not what the Rust type is.**
`Rgb` writes three consecutive `u8`s, so it declares a `Struct` of three `u8` fields — even though the implementation is hand-written.
Declare `Opaque` only when the encoding genuinely is not decomposable; see [Your descriptor](#your-descriptor).

**`Bounded` is optional**: it gives the type a fixed form, so that `Fixed<Rgb>` exists.
A fixed-size type's fixed form is usually its encoding byte for byte, as here; for a type with varints inside, it lays every integer out at its full width.

## A value whose size changes

If your type's encoding can change size, its whole-value `set` splices instead of writing: `region.splice(0, region.len(), &new.to_bytes())`, which records a write where the sizes happen to agree.
`kladde::replace(&mut value, new, &region)` does exactly that and then updates the value.
The region reports the change of size to the regions around it, which update their indexes, so nothing else needs doing.

A type with several parts, some of whose sizes change, hands each part a region of its own with `region.child(at, len, node, index)`, where `node` is the type's index of where its parts start, which the child's region updates when the child changes size; the vector of `kladde-types` is the example to read.
A mutation that changes two places — a container's element and its count, say — opens `region.transaction(|r| ...)`, so that both reach the journal together.

## Your descriptor

A hand-written implementation must also declare its [type descriptor](../../spec/schema/type-descriptors.md), and the rule is easy to get wrong:

> Declare the descriptor that matches **the bytes you actually encode**, not the shape of your Rust type.

A struct with a non-persisted cache field declares the struct *without* that field.
Declare `Opaque` only when your encoding genuinely is not decomposable into fields, variants and sequences — an externally serialized payload, say.
"Hand-written" and "opaque" are different axes, and conflating them makes your type needlessly invisible to [tooling](../../spec/tooling.md).

## The obligations you are taking on

A hand-written implementation is trusted, not checked.
You are promising:

- `FIXED_SIZE` and `encoded_len` match what `encode` writes;
- `encode` and `decode` are exact inverses, and the encoding is the [canonical one](../../spec/schema/type-descriptors.md#encodings) for your descriptor;
- every guard method leaves its region holding the value's encoding when its transaction ends;
- your declared descriptor matches your actual bytes.

The compiler enforces none of these, but the third is easy to test: flush, and compare the file's sequence with `to_bytes()` of the value in memory.
