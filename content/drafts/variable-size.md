---
title: Variable-size values
---

**Status: a proposal, not adopted.**
It would add a second encoding for every type to the [schema](../spec/schema/), and a choice of encoding wherever a value is laid out.
Nothing in the file layers below the schema would change, and nothing in memory either.

**Let whoever lays out a value — a container for its elements, a struct for a field — choose between today's fixed-size *slot* and a *packed* encoding that marks its own end, and let everything laid out inside the value follow that choice unless it says otherwise.**
In a packed layout, an enum takes only the bytes of its current variant, with no padding up to the largest one, and an integer wider than a byte is a LEB128 varint.
The choice changes only the file: in memory, a `u16` stays a `u16`, whichever way it is laid out.
The slot stays the default and keeps every offset static; packing is opt-in, and the schema records it.
Files that use no packing are unchanged, byte for byte and fingerprint for fingerprint.

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

**Two existing mechanisms reach part of this, and neither reaches the padding between elements.**

- A [`Zero` statement](../spec/address-table.md#statement-types) describes a range of zeros without data bytes, and the address table already names "data types with unbalanced variant payloads" as a use for it.
  But a statement costs on the order of ten bytes of address table, more than the padding between two elements of a vector, so stating padding as `Zero` would grow the file rather than shrink it.
- A narrower discriminant, which the specification allows (1, 2, 4 or 8 bytes) and kladde-rs picks by the largest discriminant value, saves on the tag and nothing on the padding.

## The proposal

### Two encodings per type

**Every type has a slotted encoding, which is today's, and a packed one, which a reader who knows the type and the mode decodes front to back, learning where the value ends as it goes.**
They differ by kind:

| kind | slotted | packed |
| --- | --- | --- |
| `u8`, `i8`, `bool` | one byte | the same byte |
| `u16`, `u32`, `u64` | fixed width, little-endian | unsigned LEB128: 1–3, 1–5 and 1–10 bytes |
| `i16`, `i32`, `i64` | fixed width, two's complement | zigzag, then LEB128: 1–3, 1–5 and 1–10 bytes |
| `char` | the scalar value in 4 bytes, little-endian | UTF-8: 1–4 bytes |
| `f32`, `f64` | IEEE-754, little-endian | the same bytes |
| Struct | its fields' encodings, concatenated | the same |
| Enum | the discriminant at its width, then the variant's fields | the discriminant as unsigned LEB128, then the variant's fields |
| Array (reserved) | its elements, concatenated | the same |

**In a slotted layout, a value occupies its type's slot size, with zeros after its encoding; in a packed layout, it occupies exactly its encoding.**
The slot size is the largest the value's encoding can be, so a type has a slot exactly when that is bounded; for a type laid out slotted throughout, it is today's inline size, and its slot is today's layout.
The zeros keep files deterministic, as [content semantics](../spec/allocations.md#content-semantics) does for unwritten bytes.
A struct, a float or a byte is encoded the same in both modes, and a struct differs only by what its fields do; the packed mode changes enums, integers and `char`s.
A decimal encoding of floats, which would shrink them too, is [left for later](#left-for-later-floats-as-decimals).

A varint must be minimal — no trailing `0x80` groups — so that equal values have equal bytes, which the journal and every comparison of files rely on, and it must decode to a value within its type's range, or the reader refuses the file.
`u8` and `i8` keep their byte, since no varint is shorter than one byte and LEB128 would spend two on half their values.

A `char` is UTF-8 rather than a varint of its scalar value, so that a packed sequence of `char`s is byte for byte the UTF-8 text a `PersistableString` holds, and a tool can show either as text.
LEB128 would be a byte shorter for U+0800 to U+3FFF and for everything past U+FFFF, kana and emoji among them, which is too little to give that up.
UTF-8's own rules make it canonical: only the shortest form, and no surrogates, which a reader refuses like a varint out of range.

A packed encoding is not always the shorter one.
A `u16` of $2^{14}$ or more takes three bytes packed against two slotted, a `u32` of $2^{28}$ or more five against four, so packing integers pays only where their values are mostly small, which counts, lengths, and indices usually are.
And it is not the slotted encoding with its zeros dropped: converting a value from one mode to the other means encoding it again, from the value.
Nothing relies on the conversion being cheaper than that.
A reader always knows the mode from the schema, and kladde-rs stores a value by encoding it from memory anyway, while the allocations a value owns, which move between containers without copying, are laid out by those containers and not by the value.

A type whose encoding can differ in size from one value to the next, in the mode it is laid out in, is *variable* there.
In a packed layout, an enum whose variants differ in size is variable, and so is any integer wider than a byte and any `char`; a struct is variable if a field is.
In a slotted layout, nothing is: a value always occupies its slot.

### Who chooses, and how the choice nests

**The mode is chosen where a value is laid out, and a value's fields inherit it, so packing a value packs everything inline in it unless a field declares otherwise.**
Every place a value is laid out — a struct's field, a variant's field, an array's element, a container's element — has a mode.
A place takes the mode of the value it sits in, unless its declaration fixes one: packed, or slotted.
The places at the top take theirs from whoever owns the bytes: the root value is slotted, and an allocation's content is laid out in the mode its owning container chooses — slotted for today's vector and hash map, packed for a packed vector.
Inheritance stops at an allocation boundary, since what a value owns is laid out by its own container: a packed struct holding a `PersistableVec` packs the vector's header, but not its elements.

So in `Packed(T)` with `struct T { x: MyEnum }`, the enum `x` is packed: the struct is laid out packed, and `x` inherits that.
Had `T` declared `x` slotted, it would stay slotted inside the packed `T`, occupying its slot whichever variant it holds.

The trade-off decides in which direction a declaration is useful.
A slot buys in-place mutation of any value at a static offset, at the price of padding and fixed-width integers; a packed layout buys compactness, at the price of moving whatever follows a value whose size changes.

- **A packed element that keeps a field slotted** spares the element a size change when that field changes: a counter that is incremented often, or an enum whose variant switches often.
- **A slotted value that packs a field** keeps a static offset for itself, and its slot absorbs the field's changes in size.
  The slot is then sized for the field's largest packed encoding, which for an integer is larger than its fixed width: a slotted struct with a packed `u16` field reserves three bytes for it.
- **The slots of a hash map**, which [reuses a tombstoned slot for a new entry](../rust/containers.md), have to stay slotted, since a new entry must fit any old one's slot.

**Inheritance is chosen over the alternative, in which a mode would apply only to the value it is declared for, and its fields would keep the modes their own declarations give them.**
Under that rule, `Packed(T)` for a struct would be byte for byte the same as `T`, since a struct has neither padding nor integers of its own, and packing a composite value would need every type inside it to declare its fields packed in advance.
Inheritance lets a container pack values whose types know nothing of it, and leaves to the types only the exceptions.

### In the schema

**Two wrapper descriptor kinds, `Packed(T)` and `Slotted(T)`, fix a place's mode, and a plain reference inherits it.**
A struct field of type `Packed(PathSegment)` is a packed field; an array of `Packed(PathSegment)` is an array of packed elements; a field `Slotted(u32)` inside a packed struct stays slotted.
Each wrapper would take a new kind tag in the [canonical encoding](../spec/schema/canonical-encoding.md#descriptor-encoding), with one reference as its content.

The wrappers leave every existing descriptor, encoding and [fingerprint](../spec/schema/fingerprints.md) as it is.
A file without them has no packed place, so every plain reference in it resolves to slotted, as it always has.
A mode bit on every reference would carry the same information for explicit modes, but would change the encoding, and so the fingerprint, of every type that has a field, and would still need a third value for inheritance.

A file that uses either wrapper raises the minimum reader version, as [versioning](../spec/file-format.md#versioning) requires of anything an older reader would misread: it could not decode the descriptor table, let alone the values.

## What a mutation costs

The cost depends on whether the mutation changes the size of the value it touches in its layout.

### The size stays the same

**A mutation that keeps a value's size is a `Write` in place, exactly as today.**
That covers every mutation of a slotted value, and in a packed one every write to a float, a byte, a `bool`, or a field of the current variant that keeps its size: moving a point, or recoloring a shape without changing how its paint is expressed.
A field sits at its value's start plus the sizes of the fields before it, and those are static up to the first variable one; a field inside a packed enum adds the discriminant's encoded size, which only a change of variant can change.

An integer in a packed layout keeps its size until its value crosses a power of 128: setting a `u32` from 100 to 200 makes it two bytes, and the mutation becomes a size change.
That is the price of varints, and the reason to keep a field that is mutated often slotted inside a packed element.

### The size changes

**A mutation that changes a value's size travels outward until something absorbs it: a slot around the value, or else the allocation that contains it.**
The new encoding replaces the old one, and whatever follows the value moves up or down.

- **Inside a slot.**
  If some value between the mutated one and its allocation is laid out slotted, that slot's padding absorbs the change.
  The mutation rewrites the slot from the mutated value to the end of its encoding as one `Write`, and nothing outside the slot moves.
- **Up to the allocation.**
  If every value on the way is packed, the change reaches the allocation, and the mutation is one `Splice` there: the old bytes out, the new ones in, and the tail shifted.
  No pointer has to be rewritten, because a pointer names an [id](../spec/allocations.md), not a position.
  The `Splice` moves no data bytes on file, but the address table restates every statement of the allocation behind the splice point, `O(fragments behind it)` [in table bytes](../spec/address-table.md#design-directions).
  On an allocation that the current journal created, it [costs nothing](../impl/flush.md#what-falls-out-unasked).

Either way the mutation is one record, so it is atomic on its own.
A value that owns allocations is replaced as today, which the [ordering discipline](../spec/journal.md#ordering) demands: the new value's allocations are stored before the record that publishes them, and the old ones are freed after it.
A container that moves elements with `Copy` or `Move` records can do so only between places of the same mode; between modes, it encodes them again.

### In memory

**Offsets that are compile-time constants today become runtime sums wherever a variable value precedes them.**
A field after a variable field sits at an offset that depends on that field's current value, which the in-memory value supplies, since every encoded size is computable from a value and its mode without touching the file.
A packed vector needs the offset of every element to hand out a guard for one, so it keeps an index of element offsets alongside its elements.
As an array of prefix sums, a size change or an insertion updates the entries behind it in `O(n)`, as the memmove of an insertion into `Vec` already costs; a Fenwick tree would make a size change `O(log n)`.
Reads do not change at all: they go to the native in-memory values, which have no offsets and no varints.

## Why it fits kladde

**Kladde [reads in bulk and searches nothing on disk](../impl/index.md#the-shape-of-an-implementation), and that is what makes packed layouts cheap here.**
A format that looked values up on disk would need offsets on disk — an offset table per packed vector, or a stride — to find element `i` without decoding the elements before it.
Kladde needs offsets only in memory, where they are built during the load that decodes everything anyway.
That load is already [sequential](../rust/persistable-and-guards.md), one field after another, so decoding a packed value front to back costs it nothing, and neither does decoding a varint into the integer the application declared.
A packed vector's length needs no field either, just as a slotted vector's does not: the load decodes elements until the allocation ends.

## What else it touches

### Containers

**A packed vector is a new container, or a mode of the existing vector, and the hash map stays as it is.**
A packed vector holds its elements back to back in packed layout, with the offset index above; a push is one `Write` past the end, as for a slotted vector, and an insertion, removal, or size change in the middle is one `Splice`.
The hash map's slots would stay slotted, since an insertion that reuses a tombstoned slot needs every slot to hold any value.

### Evolution

**Packed layouts keep the writer's layout readable, but turn the per-type read plan into a per-type parse.**
Under [evolution](../spec/schema/evolution.md#what-is-settled), a reader reconciles the writer's types with its own once, and then reads every value at offsets the writer's descriptors determine.
For slotted layouts that stays as it is.
For packed ones, the writer's offsets depend on each value, but the writer's descriptors and modes still determine them, so the plan becomes a small program that decodes the writer's encoding, run during the sequential load.
A change of mode is a change of layout like any other: a reader whose type packs a field the writer slotted reads the writer's fixed-width integer and stores a varint on the next write.
Mutating a value in place at the writer's layout, the *retain* mode, gets harder, since its offsets would have to be computed from the writer's descriptors at every mutation; the *upgrade* mode, which rewrites a value in the reader's layout, would be the natural default for packed values.

### Tooling

**A packed value can be walked from its descriptors alone, which is more than a tool can do with a container today.**
Every packed encoding marks its own end, so a level-3 [tool](../spec/tooling.md) decodes it without knowing the application, once it has resolved each place's mode from the wrappers and from inheritance.
It does not by itself solve [the container problem](../spec/tooling.md#the-container-problem), since a container's elements still sit behind a pointer the schema does not describe, but a packed sequence is the shape that option 1 there would describe.

### kladde-rs

The Rust side would need:

- a mode passed to `store`, `load`, and a new `encoded_size(&self, mode)`, and a slot size on `Persistable` in place of today's `INLINE_SIZE`, absent for an unbounded type;
- both encodings for every scalar, so that an application keeps declaring `u16` or `i64` and the mode decides only the bytes on file;
- `#[kladde(packed)]` and `#[kladde(slotted)]` attributes on fields, and derived offsets that are computed at runtime after the first variable field;
- guards that know the mode they write in, and report a change in their value's size to the parent that keeps the offset index, which the parent's borrow of the child makes possible;
- the packed vector.

## Stages

**The proposal can land in two stages, each worth having on its own.**

1. **Packed vector elements.**
   A new container lays out its elements packed, enums without padding, integers as varints and `char`s as UTF-8 throughout each element, and keeps their offsets in memory.
   The format does not change while the container is described as `Opaque`, since how an opaque container lays out its content is its own business, and no element type has to change, since inheritance packs them whole.
   This stage covers the largest cases, sequences of heterogeneous values such as path data, attribute lists and child nodes.
2. **Packed and slotted fields**, with the `Packed` and `Slotted` descriptor kinds, the raised minimum reader version, runtime offsets in derived types, and size changes that travel up through several levels.
   The descriptor kinds belong to this stage rather than a later one: a derived type whose fields change their layout must declare it, or its descriptor would no longer match its bytes.

## Left for later: floats as decimals

**Not part of this proposal, and recorded for a later round: packing a float as the shortest decimal that reads back as it — its significant digits and a power of ten — would shrink the numbers of SVG drawings from 4 bytes to between 1.5 and 2.8, where binary schemes that encode one float at a time come out larger than a plain `f32`.**
Under the proposal as it stands, `f32` and `f64` keep their IEEE bytes in both modes.
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

**Encodings of one float at a time, the kind a packed layout needs, are rare, and the decimal ones among them come from columnar databases.**

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
  A float would become variable in packed layouts, so moving a point, an in-place `Write` under the proposal as it stands, would change the point's size whenever its digits change in number.
  A field that is moved often could stay slotted, as a frequently changed integer can.
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

- **Should `Packed(T)` and `T` be one descriptor when `T` encodes the same in both modes?**
  A type of floats, bytes and `bool`s alone does, so wrapping it changes its fingerprint without changing a byte.
- **Pointers.**
  The [pointer encoding](../spec/allocations.md#pointer-encoding) is still undecided; in a packed layout, an allocation id could be a varint as well, which [recycling the lowest ids first](../impl/id-recycling.md) keeps small.
- **Unbounded inline types.**
  A string stored inline as a length and its bytes would have no slot and could only be laid out packed, which would save the allocation that every `PersistableString` holds today — a large saving for documents with many short strings, such as identifiers and attribute values.
  Whether a type without a slot is allowed, and how a struct containing one is described, is not decided.
- **Opaque types.**
  An opaque type declares a fixed inline size; whether a packed place may hold one, and whether an opaque type may have a packed encoding of its own that a reader who cannot decode it could still skip, is open.
- **The rope and `Move`.**
  A [rope](../rust/containers.md) would make insertions into a long packed vector cheap, and [`Move`](move-op.md) would let a rope's nodes shift packed ranges between them without copying, since all of a rope's nodes would share one mode; how the two fit together is not worked out.
- **What it saves.**
  The proposal's own numbers are arithmetic, not measurements; only the floats' [left for later](#left-for-later-floats-as-decimals) are measured.
  A realistic workload should compare slotted with packed layouts for heterogeneous sequences, in file size, journal bytes, bytes written per byte the application writes, and flush time, before anything here is adopted.
