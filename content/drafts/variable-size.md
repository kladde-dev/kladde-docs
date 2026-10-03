---
title: Variable-size values
---

**Status: a proposal, not adopted.**
It would give every type a packed encoding beside its fixed one, admit types that have only a packed encoding, and add a choice of encoding wherever a value is laid out.
Nothing in the file layers below the schema would change, and nothing in memory either.

**Separate two questions that a value's slot answers together today: how many bytes the value's encoding takes, and how much room the place that holds it reserves.**
A type's encoding is *fixed-size* or *variable-size*; a place is *slotted*, holding a type's fixed encoding, the same size for every value, or *packed*, holding its packed encoding, as many bytes as the value needs.
Most types have both encodings: in the packed one, an enum takes only its current variant, an integer wider than a byte is a LEB128 varint, and a `char` is UTF-8.
Some types have only the packed one, and so can be laid out only packed: `SmallString` and `SmallVec`, which keep content of up to a threshold, such as 127 bytes, inline, and longer content in an allocation of their own.
A slotted value is slotted throughout; a packed value packs everything inline in it, unless a place inside it declares itself slotted.
The choice changes only the file: in memory, a `u16` stays a `u16`, and a `SmallString` holds its text however it is laid out.
The slot stays the default; files that use no packing are unchanged, byte for byte and fingerprint for fingerprint.

## The problem

**Every type has one fixed inline size, and an enum's is its discriminant plus its largest variant, so every value of an enum pays for the largest variant.**
That is [what makes offsets static](../spec/schema/type-descriptors.md#inline-size), and with them field access, element access by index, and the reading of a writer's layout under [evolution](../spec/schema/evolution.md).
Rust pays the same padding in memory, so the in-memory representation loses nothing it would not lose anyway.
The file and the journal do lose: a store writes the whole slot, padding included, and the padding then occupies data pages like any other bytes.

Take SVG path data, a sequence of segments of ten kinds, as a typical case:

```rust
enum PathSegment {
    MoveTo { abs: bool, x: f32, y: f32 },
    LineTo { abs: bool, x: f32, y: f32 },
    HorizontalLineTo { abs: bool, x: f32 },
    VerticalLineTo { abs: bool, y: f32 },
    CurveTo { abs: bool, x1: f32, y1: f32, x2: f32, y2: f32, x: f32, y: f32 },
    SmoothCurveTo { abs: bool, x2: f32, y2: f32, x: f32, y: f32 },
    Quadratic { abs: bool, x1: f32, y1: f32, x: f32, y: f32 },
    SmoothQuadratic { abs: bool, x: f32, y: f32 },
    EllipticalArc { abs: bool, rx: f32, ry: f32, x_axis_rotation: f32,
                    large_arc: bool, sweep: bool, x: f32, y: f32 },
    ClosePath { abs: bool },
}
```

With a one-byte discriminant, its slot is 26 bytes, the size of `CurveTo`.
Its variants need 2 bytes (`ClosePath`), 6 (`HorizontalLineTo`, `VerticalLineTo`), 10 (`MoveTo`, `LineTo`, `SmoothQuadratic`), 18 (`SmoothCurveTo`, `Quadratic`), 24 (`EllipticalArc`) and 26 (`CurveTo`).
A path that is mostly curves wastes little, but the outline of an icon, `M L L L Z`, takes 130 bytes in slots where its content is 42.
An element tree has the same shape one level up: a node that is either an element or a short run of text pays for the element in both cases.

Integers are the same problem in a different form.
A count, a length, or an index is stored at the width its largest possible value needs, 4 or 8 bytes, while nearly every value written fits in one.

**Short strings are the problem in a third form: every `PersistableString` owns an allocation, however short its content.**
kladde-svg's model of an SVG drawing stores each attribute value it keeps as text, an `id` above all, in a string of its own:

| drawing | allocations | strings | of them `id`s | mean length of an `id` |
| --- | --- | --- | --- | --- |
| tiger | 1,453 | 486 | 482 | 5.4 bytes |
| coat of arms | 2,007 | 664 | 609 | 7.5 bytes |
| world map | 8,542 | 3,240 | 2,528 | 7.6 bytes |

So a third to two fifths of the allocations hold a few bytes each, and all but two of the 4,390 strings are at most 127 bytes long.
On file, such an allocation is cheap already, since content below the [`Inline` threshold](../impl/flush.md#the-inline-threshold) lives in its statement, inside pages a flush writes anyway; what remains is the pointer in the parent and the statement's framing.
In memory, it costs an entry in the allocation map, a statement in the slab, and a fragment in the fragment map, as [in-memory state](../impl/in-memory-state.md) describes them, and every change to it is journaled against an allocation of its own.

**Two existing mechanisms reach part of this, and neither reaches the padding between elements.**

- A [`Zero` statement](../spec/address-table.md#statement-types) describes a range of zeros without data bytes, and the address table already names "data types with unbalanced variant payloads" as a use for it.
  But a statement costs on the order of ten bytes of address table, more than the padding between two elements of a vector, so stating padding as `Zero` would grow the file rather than shrink it.
- A narrower discriminant, which the specification allows (1, 2, 4 or 8 bytes) and kladde-rs picks by the largest discriminant value, saves on the tag and nothing on the padding.

## The proposal

### Encodings: fixed and packed

**Every type has a packed encoding, which a reader who knows the type decodes front to back, learning where the value ends as it goes, and most types also have a fixed encoding, which takes the same number of bytes for every value.**
They differ by kind:

| kind | fixed encoding | packed encoding |
| --- | --- | --- |
| `u8`, `i8`, `bool` | one byte | the same byte |
| `u16`, `u32`, `u64` | fixed width, little-endian | unsigned LEB128: 1–3, 1–5 and 1–10 bytes |
| `i16`, `i32`, `i64` | fixed width, two's complement | zigzag, then LEB128: 1–3, 1–5 and 1–10 bytes |
| `char` | the scalar value in 4 bytes, little-endian | UTF-8: 1–4 bytes |
| `f32`, `f64` | IEEE-754, little-endian | the same bytes |
| Struct | its fields' fixed encodings, concatenated | its fields' encodings in their places, concatenated |
| Enum | the discriminant at its width, the variant's fields, and zeros up to the largest variant | the discriminant as unsigned LEB128, then the variant's fields |
| `SmallString`, `SmallVec<T>` (new, below) | none | a length and the content inline, or a marker and a pointer |
| Array (reserved) | its elements' fixed encodings, concatenated | its elements' encodings, concatenated |

**A type is *fixed-size* if its packed encoding takes the same number of bytes for every value, and then its two encodings are one; it is *variable-size* otherwise.**
Floats, bytes, `bool`s, and structs of them are fixed-size.
A variable-size type is *slottable* if it has a fixed encoding — every integer wider than a byte, every `char`, every enum, and every struct of such fields — and *packed-only* if it has none.
A struct or an enum has a fixed encoding exactly when the types of all its fields do, so a type with a packed-only field inline is packed-only as well.
The fixed encoding of a type is today's layout of it, so a type that existing files contain encodes there as it always has.

The packed encoding obeys three rules of canonical form, so that a value has one encoding and a reader can refuse anything else.
A varint must be minimal — no trailing `0x80` groups — and decode to a value within its type's range.
`u8` and `i8` keep their byte, since no varint is shorter than one byte and LEB128 would spend two on half their values.
A `char` is UTF-8 rather than a varint of its scalar value, so that a packed sequence of `char`s is byte for byte the UTF-8 text a string holds, and a tool can show either as text; LEB128 would be a byte shorter for U+0800 to U+3FFF and past U+FFFF, kana and emoji among them, which is too little to give that up, and UTF-8's own rules make it canonical: only the shortest form, and no surrogates.

A packed encoding is not always the shorter one.
A `u16` of $2^{14}$ or more takes three bytes packed against two fixed, a `u32` of $2^{28}$ or more five against four, so packing integers pays only where their values are mostly small, which counts, lengths, and indices usually are.
And it is not the fixed encoding with its zeros dropped: converting a value from one encoding to the other means encoding it again, from the value.
Nothing relies on the conversion being cheaper than that.
A reader always knows from the schema which encoding a place holds, and kladde-rs stores a value by encoding it from memory anyway, while the allocations a value owns, which move between containers without copying, are laid out by those containers and not by the value.

A decimal encoding of floats, which would shrink them too, is [left for later](#left-for-later-floats-as-decimals).

### Small strings and vectors

**`SmallString` and `SmallVec<T>` keep their content inline while it takes at most a threshold of bytes, and in an allocation of their own once it takes more.**
Their packed encoding starts with a LEB128 header: the content's length in bytes if it is inline, followed by the content; or the threshold plus one if it is not, followed by a pointer to the allocation that holds it, whose size is the content's length.
A `SmallVec`'s content is its elements in their packed encodings, the same bytes inline and in an allocation, so moving the content between the two changes no byte of it.

With a threshold of 127, the header of inline content is one byte, and a value takes 1 to 128 bytes inline, or 6 spilled: two bytes of header and a four-byte pointer.
The threshold is part of the type, and so of its descriptor.
Whether content is inline is a function of the value alone — inline exactly when it fits — so that a value has one encoding, and a reader refuses content inline that would not fit, or spilled that would.

In memory, a `SmallString` is text, and a `SmallVec` a vector, plus the handle of their allocation while they have one; *small* describes the file, not the memory.
A spilled value owns its allocation, as a `PersistableString` does, and frees it when it is replaced or deleted; an inline one owns nothing.

**The small types have no fixed encoding, since one would reserve their threshold.**
A fixed `SmallString` with a threshold of 127 would take 128 bytes in every slot, while the `id`s above average 5 to 8; a struct that held one inline in a slotted place would reserve that room in every value, which is the footgun the small types must not set.
So they are packed-only, and the rules below keep them, and every type that holds one inline, out of slotted places altogether.

### Who chooses, and how the choice nests

**The encoding is chosen where a value is laid out, and the places inside a value inherit it, so packing a value packs everything inline in it, unless a place inside declares itself slotted.**
Every place a value is laid out in — a struct's field, a variant's field, an array's element, a container's element — is slotted or packed.
A place takes the choice of the value it sits in, unless its declaration fixes one.
The places at the top take theirs from whoever owns the bytes: an allocation's content is laid out as its owning container chooses — slotted for today's vector and hash map, packed for a packed vector and for a small type's content — and the root value is slotted if its type has a fixed encoding, as every root is today, and packed otherwise.
Inheritance stops at an allocation boundary, since what a value owns is laid out by its own container: a packed struct holding a `PersistableVec` packs the vector's header, but not its elements.

**A slotted value is slotted throughout: no place inside it may be packed.**
A packed place inside a slotted value would never save a byte.
The slot reserves the largest encoding its content can take, and a packed encoding's largest is never smaller than the fixed one — three bytes for a `u16` against two, the threshold for a small type — so the slot could only grow, and a change of size inside it would rewrite the slot's tail where a fixed field is written in place.
The same rule is what keeps the small types out of slots: a packed-only type can stand only where every value around it, up to its allocation, is packed.

**A packed value may declare places inside it slotted**, and that is the only exception the declarations make.
A slotted place inside a packed value holds a fixed encoding, so its value never changes size, which suits a field that changes often: a counter, which as a varint would change size at every power of 128, or an enum whose variant switches often.

So in `Packed(T)` with `struct T { x: MyEnum }`, the enum `x` is packed: the struct is laid out packed, and `x` inherits that.
Had `T` declared `x` slotted, it would stay slotted inside the packed `T`, occupying its slot whichever variant it holds.
And `struct T { id: SmallString }` is packed-only: a packed vector can hold it, today's vector cannot.

**Inheritance is chosen over the alternative, in which a choice would apply only to the value it is declared for, and its fields would keep the choices their own declarations give them.**
Under that rule, `Packed(T)` for a struct would be byte for byte the same as `T`, since a struct has neither padding nor integers of its own, and packing a composite value would need every type inside it to declare its fields packed in advance.
Inheritance lets a container pack values whose types know nothing of it, and leaves to the types only the exceptions.

### In the schema

**Two wrapper descriptor kinds, `Packed(T)` and `Slotted(T)`, fix a place's choice, a plain reference inherits it, and a new kind, `Small(T, threshold)`, describes the small types.**
An element of a container described as `Packed(Attr)` is packed; a field `Slotted(u32)` inside a packed struct stays slotted.
`Small(T, threshold)` is a sequence of `T` in packed encodings, inline while it takes at most `threshold` bytes and in an owned allocation otherwise; a `SmallVec<T>` declares `Small(T, 127)`, and a `SmallString` `Small(u8, 127)`.
Each kind would take a new tag in the [canonical encoding](../spec/schema/canonical-encoding.md#descriptor-encoding): the wrappers with one reference as their content, `Small` with a reference and a varint.

A descriptor table must keep the rules of nesting: a `Packed` wrapper, and a reference to a packed-only type, may stand only where no value around them is slotted, and a reader refuses a table that breaks either rule.
The root's reference is wrapped in `Packed` where the root is packed.

The new kinds leave every existing descriptor, encoding and [fingerprint](../spec/schema/fingerprints.md) as it is.
A file without them has no packed place, so every plain reference in it resolves to slotted, as it always has.
A bit on every reference would carry the same information for explicit choices, but would change the encoding, and so the fingerprint, of every type that has a field, and would still need a third value for inheritance.
A file that uses any of them raises the minimum reader version, as [versioning](../spec/file-format.md#versioning) requires of anything an older reader would misread: it could not decode the descriptor table, let alone the values.

## What a mutation costs

The cost depends on whether the mutation changes the size of the value it touches.

### The size stays the same

**A mutation that keeps a value's size is a `Write` in place, exactly as today.**
That covers every mutation inside a slotted value, a change of an enum's variant included, which rewrites its slot.
In a packed value, it covers every write to a float, a byte, a `bool`, a slotted place, or a field of the current variant that keeps its size: moving a point, recoloring a shape without changing how its paint is expressed, or editing a short string without changing its length.
A field sits at its value's start plus the sizes of the fields before it, and those are static up to the first variable one; a field inside a packed enum adds the discriminant's encoded size, which only a change of variant can change.

An integer in a packed place keeps its size until its value crosses a power of 128: setting a `u32` from 100 to 200 makes it two bytes, and the mutation becomes a size change.
That is the price of varints, and the reason to declare a field that is mutated often slotted.

### The size changes

**A mutation that changes a value's size is one `Splice` on the allocation that contains the value: the old encoding out, the new one in, and whatever follows shifted.**
Only a value in a packed place can change size, and every value around a packed place, up to its allocation, is packed as well, so the change reaches the allocation, where nothing would absorb it.
No pointer has to be rewritten, because a pointer names an [id](../spec/allocations.md), not a position.
The `Splice` moves no data bytes on file, but the address table restates every statement of the allocation behind the splice point, `O(fragments behind it)` [in table bytes](../spec/address-table.md#design-directions).
On an allocation that the current journal created, it [costs nothing](../impl/flush.md#what-falls-out-unasked).

**A small type whose content crosses its threshold moves the content between its parent and an allocation of its own, in one transaction.**
Spilling writes the content to a new allocation, then splices the pointer in where the content was; folding back splices the content in where the pointer was, then frees the allocation: prepare, publish, clean up, as the [ordering discipline](../spec/journal.md#ordering) demands.
Since the content's bytes are the same in both places, a [`Move`](move-op.md) could hand them over without writing them again.
A value edited back and forth across its threshold allocates and frees each time it crosses it; with a threshold of 127, the `id`s above would never come near it.

A value that owns allocations is replaced as today: the new value's allocations are stored before the record that publishes them, and the old ones are freed after it.
A container that moves elements with `Copy` or `Move` records can do so only between places with the same choice; between a packed place and a slotted one, it encodes them again.

### In memory

**Offsets that are compile-time constants today become runtime sums wherever a variable-size value precedes them.**
A field after a variable-size field sits at an offset that depends on that field's current value, which the in-memory value supplies, since every encoded size is computable from a value and its place without touching the file.
A packed vector needs the offset of every element to hand out a guard for one, so it keeps an index of element offsets alongside its elements.
As an array of prefix sums, a size change or an insertion updates the entries behind it in `O(n)`, as the memmove of an insertion into `Vec` already costs; a Fenwick tree would make a size change `O(log n)`.
Reads do not change at all: they go to the native in-memory values, which have no offsets and no varints.

## Why it fits kladde

**Kladde [reads in bulk and searches nothing on disk](../impl/index.md#the-shape-of-an-implementation), and that is what makes packed layouts cheap here.**
A format that looked values up on disk would need offsets on disk — an offset table per packed vector, or a stride — to find element `i` without decoding the elements before it.
Kladde needs offsets only in memory, where they are built during the load that decodes everything anyway.
That load is already [sequential](../rust/persistable-and-guards.md), one field after another, so decoding a packed value front to back costs it nothing, and neither does decoding a varint into the integer the application declared, or reading a small string's content from beside its header rather than from an allocation.
A packed vector's length needs no field either, just as a slotted vector's does not: the load decodes elements until the allocation ends.

## What else it touches

### Containers

**Today's vector and hash map stay slotted, so their elements must have fixed encodings, and the packed vector and the small types hold the rest.**
A packed vector holds its elements back to back in their packed encodings, with the offset index above; a push is one `Write` past the end, as for a slotted vector, and an insertion, removal, or size change in the middle is one `Splice`.
A `PersistableVec` of a packed-only type does not compile, nor does a `PersistableHashMap` with `SmallString` keys or values.
The hash map's slots have to stay slotted for as long as an insertion reuses a tombstoned slot, since a new entry must fit any old entry's slot; a map that stopped reusing slots could pack its entries as a packed vector does.

### Evolution

**Packed layouts keep the writer's layout readable, but turn the per-type read plan into a per-type parse.**
Under [evolution](../spec/schema/evolution.md#what-is-settled), a reader reconciles the writer's types with its own once, and then reads every value at offsets the writer's descriptors determine.
For slotted layouts that stays as it is.
For packed ones, the writer's offsets depend on each value, but the writer's descriptors, and the choices they record, still determine them, so the plan becomes a small program that decodes the writer's encoding, run during the sequential load.
A changed choice is a changed layout like any other: a reader whose type declares a field slotted that the writer packed reads the writer's varint and stores a fixed-width integer on the next write.
Mutating a value in place at the writer's layout, the *retain* mode, gets harder, since its offsets would have to be computed from the writer's descriptors at every mutation; the *upgrade* mode, which rewrites a value in the reader's layout, would be the natural default for packed values.

### Tooling

**A packed value can be walked from its descriptors alone, which is more than a tool can do with a container today.**
Every packed encoding marks its own end, so a level-3 [tool](../spec/tooling.md) decodes it without knowing the application, once it has resolved each place's choice from the wrappers and from inheritance.
A `Small` descriptor goes further than any descriptor today: it says where the content of the allocation that a spilled value owns lies and how it is laid out, so a tool can follow that pointer.
That is a start on [the container problem](../spec/tooling.md#the-container-problem), whose vectors and maps still sit behind pointers the schema does not describe, and a packed sequence is the shape that option 1 there would describe.

### kladde-rs

The Rust side would need:

- a choice of encoding passed to `store`, `load`, and a new `encoded_size(&self, packed)`;
- a fixed size on `Persistable` in place of today's `INLINE_SIZE`, absent for a packed-only type, derived for structs and enums from their fields, and checked at compile time by every slotted container and slotted field;
- both encodings for every scalar, so that an application keeps declaring `u16` or `i64` and the place decides only the bytes on file;
- a `#[kladde(slotted)]` attribute on fields, the only declaration a derived type needs, since a field can be packed only by inheriting it, and derived offsets that are computed at runtime after the first variable-size field;
- guards that know which encoding they write, and report a change in their value's size to the parent that keeps the offset index, which the parent's borrow of the child makes possible;
- the packed vector, `SmallString`, `SmallVec`, and a packed root for a packed-only root type.

## Stages

**The proposal can land in two stages, each worth having on its own.**

1. **Packed vector elements.**
   A new container lays out its elements packed throughout — enums without padding, integers as varints, `char`s as UTF-8 — and keeps their offsets in memory.
   The format does not change while the container is described as `Opaque`, since how an opaque container lays out its content is its own business, and no element type has to change, since inheritance packs them whole.
   This stage covers the largest cases of padding, sequences of heterogeneous values such as path data, attribute lists and child nodes.
2. **The schema's part**: the `Packed`, `Slotted` and `Small` descriptor kinds and the raised minimum reader version, and with them slotted places inside packed values, the small types, and packed roots.
   These belong together because each changes the bytes of types that describe themselves structurally, whose descriptors would otherwise no longer match their bytes.
   It is the stage that removes the allocations of short strings, the `id`s of the table above.

## Left for later: floats as decimals

**Not part of this proposal, and recorded for a later round: packing a float as the shortest decimal that reads back as it — its significant digits and a power of ten — would shrink the numbers of SVG drawings from 4 bytes to between 1.5 and 2.8, where binary schemes that encode one float at a time come out larger than a plain `f32`.**
Under the proposal as it stands, `f32` and `f64` keep their IEEE bytes in both encodings.
In memory they would stay what the application declares in any case; only the bytes on file would change, as they do for integers.

### What it would save

**On kladde-svg's corpus, the shortest decimal takes 1.54 to 2.78 bytes per number, and the two binary schemes 2.64 to 4.89, more than four bytes on every drawing.**
The numbers are every `f32` that kladde-svg's model stores for the drawings: coordinates, lengths, transforms, opacities and view boxes.
They were measured with a throwaway program, not kept, on three drawings and on resvg's test suite, whose files are small and whose numbers are mostly integers.

| bytes per number | tiger | coat of arms | world map | resvg's tests |
| --- | --- | --- | --- | --- |
| numbers | 11,001 | 22,422 | 168,554 | 34,072, in 1,800 files |
| share that are integers | 19 % | 9 % | 2 % | 90 % |
| fixed `f32` | 4.00 | 4.00 | 4.00 | 4.00 |
| byte-reversed bits, then a varint | 4.31 | 4.57 | 4.83 | 2.64 |
| a tag, then `f16` where exact, else `f32` | 4.39 | 4.64 | 4.89 | 2.84 |
| shortest decimal | 2.12 | 2.78 | 2.38 | 1.54 |

The decimal encoding measured is one LEB128 varint holding `zigzag(digits) × 16 + (exponent + 8)` for a power of ten from $10^{-8}$ to $10^{7}$, and otherwise an escape followed by the four IEEE bytes, five bytes in all.
On the world map, 4.5 % of the numbers take one byte, 55 % two, 38 % three, and 2 % four or five.
The byte-reversed variant is what Go's `encoding/gob` does: reversing the bytes puts a float's trailing zero mantissa bits first, where a varint drops them.
The tagged variant follows CBOR's preferred serialization, with zero in the tag alone.

**The binary schemes lose because SVG's numbers are short decimals, whose binary mantissas are full.**
A float's trailing mantissa bits are zero only for integers and fractions with a power of two below them, such as halves and quarters; `12.35` or `0.1` fill all 23 bits.
On resvg's tests, nine tenths of whose numbers are integers, the binary schemes do save, but less than the decimal one does.
The decimal encoding keeps what the source wrote, and that is short whenever the source's text was.

### What others do

**Encodings of one float at a time, the kind a packed place needs, are rare, and the decimal ones among them come from columnar databases.**

- *Go's gob* reverses a float's bytes before writing it as an integer; 17.0 takes three bytes.
- *CBOR's preferred serialization* ([RFC 8949](https://www.rfc-editor.org/rfc/rfc8949)) writes a float at the shortest of half, single and double precision that holds it exactly; *Amazon Ion* writes zero in no bytes, and other floats in four or eight.
- *SQLite* writes a `REAL` without a fractional part as an integer of one to eight bytes, which is the decimal encoding restricted to a power of ten of one.
- *Ion's decimal type* stores a coefficient and an exponent, each of variable length.
- *BtrBlocks*' pseudodecimal encoding (SIGMOD 2023) splits a double into its significant digits and a power of ten, with exceptions for those that do not split, and *ALP* (SIGMOD 2024, used in DuckDB) finds the doubles that are short decimals by scaling them to integers.
  Both compress columns rather than single values, but the split itself would work one value at a time.

**Compressors of float sequences reach further, and would not fit, since each value's encoding depends on the values before it.**
*Gorilla* (VLDB 2015) writes each value as its XOR with the one before; *Chimp* (VLDB 2022) and *Elf* (VLDB 2023) refine it, Elf by erasing the mantissa bits beyond a value's decimal precision first.
*FPC* predicts each value from earlier ones, *zfp* and *fpzip* compress arrays of scientific data, and Parquet's byte-stream split regroups a column's bytes for a general-purpose compressor.
Under any of them, changing one number would change the encoding of the numbers after it, so a mutation that kladde makes as one `Write` would rewrite a run of values.
They belong with [compressing data pages](#compressing-data-pages), which could use them, rather than with packing.

### What adopting it would take

- **A canonical rule.**
  The specification would have to fix which digits a float is written with: the shortest decimal that reads back as the float, the closest such if there are several, and how a tie between two equally close ones breaks, as the Ryū algorithm and its ports do in most languages.
- **Escapes** for what has no short decimal: infinities, NaNs with their payloads, negative zero, and exponents outside the encoding's range.
  The prototype above wrote negative zero as zero, which a real encoding must not.
- **Time.**
  A store would compute a float's shortest decimal, and a load would parse one back with correct rounding; for the world map's 169,000 numbers, that should be milliseconds against the 78 ms its open takes now.
- **No gain for computed numbers.**
  A coordinate that a rotation or a scaling produced has up to nine significant digits and takes the escape, five bytes, one more than an `f32`.
- **More size changes.**
  A float would become variable-size, so moving a point, an in-place `Write` under the proposal as it stands, would change the point's size whenever its digits change in number.
  A field that is moved often could be declared slotted, as a frequently changed integer can.
- **Tuning.**
  The varint's layout above is a first guess; giving integers a one-byte form, or fitting the exponent's range to where SVG's numbers lie, would likely save more.

**It might also settle whether an application should declare `f32` or `f64`.**
The shortest decimal of the `f64` that `12.35` parses to is `12.35`, as it is for the `f32`, so for numbers that came from decimal text, an `f64` should take about as many bytes on file as an `f32`, and its extra precision would cost space only where it is used.
That has not been measured.

## Alternatives

**Four alternatives save part of the same space with less change, and the proposal is preferred because none of them removes the padding while values stay typed and walkable.**
Compressing pages is orthogonal and could be combined with it.

### A durable `Box` for large variants

A library type that owns an allocation would let a rare, large variant hold a pointer instead of its payload, shrinking the slot of every value to the size of the next largest variant.
It would need no change to the format, since it would be a container like any other.
But every boxed value would be an allocation of its own, with its statements in the address table and its entry in the allocation map, which would cost more than the padding it saves unless the variant were both large and rare.
It would suit an element kind with a dozen attributes that occurs once per document; it would be wrong for path segments.

### Struct of arrays, in the application

A path could be two vectors, one byte per segment kind and four bytes per coordinate, with no padding at all.
It would need no change to kladde, and nothing would be more compact.
But the values would no longer be typed in the schema, so a tool would see two arrays of numbers; every edit would span two vectors and so need a transaction to stay consistent; and every application would hand-write what the type system could derive.

### Compressing data pages

Padding is zeros, and zeros compress to almost nothing, so compressed data pages would make padding nearly free on disk while types and offsets stay exactly as they are.
But it would change the page framing, make consolidation account for pages whose compressed size differs from their content, and add compression to every flush.
The journal would keep its padding unless its transactions were compressed too, and so would every byte written while the journal is folded.
It is orthogonal to packing and would combine with it.

### `Zero` statements for padding

The address table can already state a range as zeros without writing them, but at on the order of ten bytes per statement it would cost more than the padding between two vector elements.
It pays only for long runs of zeros, which padding between small elements is not.

## Open questions

- **Should `Packed(T)` and `T` be one descriptor when `T` is fixed-size?**
  A type of floats, bytes and `bool`s alone encodes the same in both places, so wrapping it changes its fingerprint without changing a byte.
- **Small thresholds.**
  127 is the largest threshold whose inline header takes one byte, and suits `id`s; whether the threshold should be a parameter of each small type, and whether a small type with a small threshold — say 15 bytes, as small-string types in memory often use — should have a fixed encoding after all, so that a slotted struct could hold one, is open.
- **Crossing back and forth.**
  Content inline exactly when it fits gives a value one encoding, but makes a value edited across its threshold allocate and free at every crossing; spilling above the threshold and folding back only well below it would stop that, at the price of two encodings for one value, which a reader would then have to accept.
- **Text in the schema.**
  `Small(u8, 127)` says that a `SmallString` holds bytes, not text; whether the schema should know that they are UTF-8 is the question it leaves open for `PersistableString` too, which it describes as `Opaque`.
- **Pointers.**
  The [pointer encoding](../spec/allocations.md#pointer-encoding) is still undecided; in a packed place, an allocation id could be a varint as well, which [recycling the lowest ids first](../impl/id-recycling.md) keeps small.
- **Opaque types.**
  An opaque type declares a fixed inline size, so a packed place can hold one as a fixed-size value; whether an opaque type may have a packed encoding of its own, which a reader who cannot decode it could still skip, is open.
- **The rope and `Move`.**
  A [rope](../rust/containers.md) would make insertions into a long packed vector cheap, and [`Move`](move-op.md) would let a rope's nodes shift packed ranges between them without copying, since all of a rope's nodes would share one choice; how the two fit together is not worked out.
- **What it saves.**
  The proposal's numbers are arithmetic and counts, not measurements of savings; only the floats' [left for later](#left-for-later-floats-as-decimals) are measured.
  A realistic workload should compare slotted with packed layouts, and small types with strings that own allocations, in file size, journal bytes, bytes written per byte the application writes, memory, and flush time, before anything here is adopted.
