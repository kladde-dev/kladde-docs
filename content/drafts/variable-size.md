---
title: Variable-size values
---

**Status: a proposal, not adopted.**
It would add a second encoding for every type to the [schema](../spec/schema/), and a choice of encoding wherever a value is laid out.
Nothing in the file layers below the schema would change.

**Let every value have a *packed* encoding that marks its own end, and let whoever lays out a value — a container for its elements, a struct for a field — choose between that packed encoding and today's fixed-size *slot*.**
A slot already is a packed value followed by zero padding: an enum's discriminant, then its variant's fields, then zeros up to the size of the largest variant.
The proposal lets a layout drop the padding.
The slot stays the default and keeps every offset static; packing is opt-in where a value is laid out, and the schema records it.
For a type with no variable part the two encodings coincide, so every existing type and file is unchanged.
The same mechanism admits integers whose encoding grows with their value, such as LEB128 varints, as primitives of their own.

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
- A narrower discriminant, which the specification already allows (1, 2, 4 or 8 bytes), saves on the tag and nothing on the padding.

## The proposal

### Two encodings per type

**Every type gets a packed encoding that a reader who knows the type can decode front to back, learning where the value ends as it goes.**
It is defined by kind:

| kind | packed encoding | marks its end by |
| --- | --- | --- |
| Primitive, fixed width | its bytes | its width |
| Primitive, variable width (new, below) | its bytes | its own encoding, e.g. a continuation bit |
| Struct | its fields' encodings, concatenated, each in the mode its field declares | its last field |
| Enum | the discriminant, then the selected variant's fields, concatenated | the variant's last field |
| Array (reserved) | its elements, concatenated | the count |

**A slot is the packed encoding followed by zeros up to the type's slot size.**
The slot size is the largest packed size any value of the type can have, so a type has a slot exactly when its packed size is bounded.
Today's inline size is the slot size, and today's layout of a value is its slot, so a reader can always decode a slotted value by reading its packed prefix and skipping the rest.
Requiring the padding to read as zero keeps files deterministic, as [content semantics](../spec/allocations.md#content-semantics) does for unwritten bytes, and makes the step from a slot to a packed value and back a matter of dropping or appending zeros.

A type whose packed size can differ from one value to the next is *variable*.
An enum whose variants differ in size is variable; so is a struct with a variable field laid out packed, and so is a variable-width primitive.
A type with no variable part has one packed size, equal to its slot size, and nothing about it changes.

### The place that lays a value out chooses

**Whether a value is laid out packed or in a slot is decided where it is laid out — a struct for each field, a container for its elements — not by its type.**
The trade-off belongs there.
A slot buys in-place mutation of any value at a static offset, at the price of the padding; a packed layout buys compactness, at the price of moving whatever follows a value whose size changes.
Which one wins depends on how the values around it are used: path segments that are read far more often than they change shape want to be packed, and the slots of a hash map, which [reuses a tombstoned slot for a new entry](../rust/containers.md), must stay slots.
So one type may be packed in one place and slotted in another.

### In the schema

**The choice is recorded by a new descriptor kind, `Packed(T)`, which describes `T` in its packed encoding, rather than by a mode bit on every reference.**
A struct field of type `Packed(PathSegment)` is a packed field; an array of `Packed(PathSegment)` is an array of packed elements.
A `Packed` descriptor would take a new kind tag in the [canonical encoding](../spec/schema/canonical-encoding.md#descriptor-encoding), and its content is one reference.

A bit on every reference would carry the same information, and would change the encoding, and therefore the [fingerprint](../spec/schema/fingerprints.md), of every type that has a field.
The wrapper leaves every existing descriptor, encoding and fingerprint as it is, and appears only where a layout actually packs.
`Packed(T)` for a `T` with no variable part describes the same bytes as `T`; whether the two should then be one descriptor, so that they fingerprint alike, is an [open question](#open-questions).

### Variable-width primitives

**Two new primitive codes would describe integers by their value rather than their range: an unsigned LEB128 integer of up to 64 bits, and a signed one, zigzag-encoded.**
Their packed size is 1 to 10 bytes and their slot size 10.
The encoding must be minimal — no trailing `0x80` groups — so that equal values have equal bytes, which the journal and every comparison of files rely on.
A new primitive code raises the minimum reader version, as [versioning](../spec/file-format.md#versioning) already requires, since an older reader would not know where the value ends.

## What a mutation costs

The cost depends on whether the mutation changes the packed size of the value it touches.

### The size stays the same

**A mutation that keeps a value's size is a `Write` in place, exactly as today.**
Most mutations are of this kind: moving a point, recoloring a shape without changing how its paint is expressed, or any write to a field of fixed size.
A field inside a packed enum sits at the value's start, plus the discriminant, plus a static offset within its variant, because the layout of a variant is fixed once the discriminant is known.
Only the value's own start can be dynamic.

### The size changes

**A mutation that changes a value's size travels outward until something absorbs it: a slot around the value, or else the allocation that contains it.**
The new packed bytes replace the old ones, and whatever follows the value moves up or down.

- **Inside a slot.**
  If some value between the mutated one and its allocation is laid out in a slot, that slot's padding absorbs the change.
  The mutation rewrites the slot from the mutated value to the end of its content as one `Write`, and nothing outside the slot moves.
- **Up to the allocation.**
  If every value on the way is packed, the change reaches the allocation, and the mutation is one `Splice` there: the old bytes out, the new ones in, and the tail shifted.
  No pointer has to be rewritten, because a pointer names an [id](../spec/allocations.md), not a position.
  The `Splice` moves no data bytes on file, but the address table restates every statement of the allocation behind the splice point, `O(fragments behind it)` [in table bytes](../spec/address-table.md#design-directions).
  On an allocation that the current journal created, it [costs nothing](../impl/flush.md#what-falls-out-unasked).

Either way the mutation is one record, so it is atomic on its own.
A value that owns allocations is replaced as today, which the [ordering discipline](../spec/journal.md#ordering) demands: the new value's allocations are stored before the record that publishes them, and the old ones are freed after it.

### In memory

**Offsets that are compile-time constants today become runtime sums wherever a packed variable value precedes them.**
A field after a packed variable field sits at an offset that depends on that field's current value, which the in-memory value supplies, since every packed size is computable from a value without touching the file.
A packed vector needs the offset of every element to hand out a guard for one, so it keeps an index of element offsets alongside its elements.
As an array of prefix sums, a size change or an insertion updates the entries behind it in `O(n)`, as the memmove of an insertion into `Vec` already costs; a Fenwick tree would make a size change `O(log n)`.
Reads do not change at all: they go to the native in-memory values, which have no offsets.

## Why it fits kladde

**Kladde [reads in bulk and searches nothing on disk](../impl/index.md#the-shape-of-an-implementation), and that is what makes packed layouts cheap here.**
A format that looked values up on disk would need offsets on disk — an offset table per packed vector, or a stride — to find element `i` without decoding the elements before it.
Kladde needs offsets only in memory, where they are built during the load that decodes everything anyway.
That load is already [sequential](../rust/persistable-and-guards.md), one field after another, so decoding a packed value front to back costs it nothing.
A packed vector's length needs no field either, just as a slotted vector's does not: the load decodes elements until the allocation ends.

## What else it touches

### Containers

**A packed vector is a new container, or a mode of the existing vector, and the hash map stays as it is.**
A packed vector holds its elements back to back, with the offset index above; a push is one `Write` past the end, as for a slotted vector, and an insertion, removal, or size change in the middle is one `Splice`.
The hash map's slots would stay slots, since an insertion that reuses a tombstoned slot needs every slot to hold any value.

### Evolution

**Packed layouts keep the writer's layout readable, but turn the per-type read plan into a per-type parse.**
Under [evolution](../spec/schema/evolution.md#what-is-settled), a reader reconciles the writer's types with its own once, and then reads every value at offsets the writer's descriptors determine.
For slotted layouts that stays as it is.
For packed ones, the writer's offsets depend on each value, but the writer's descriptors still determine them, so the plan becomes a small program that decodes the writer's encoding, run during the sequential load.
Mutating a value in place at the writer's layout, the *retain* mode, gets harder, since its offsets would have to be computed from the writer's descriptors at every mutation; the *upgrade* mode, which rewrites a value in the reader's layout, would be the natural default for packed values.

### Tooling

**A packed value can be walked from its descriptors alone, which is more than a tool can do with a container today.**
Every packed encoding marks its own end, so a level-3 [tool](../spec/tooling.md) decodes it without knowing the application.
It does not by itself solve [the container problem](../spec/tooling.md#the-container-problem), since a container's elements still sit behind a pointer the schema does not describe, but a packed sequence is the shape that option 1 there would describe.

### kladde-rs

The Rust side would need:

- a slot size on `Persistable` in place of today's `INLINE_SIZE`, absent for an unbounded type, plus `packed_size(&self)` and packed versions of `store` and `load`;
- a wrapper `Packed<T>` that mirrors the descriptor, or a `#[kladde(packed)]` attribute on a field, and derived offsets that are computed at runtime after the first packed variable field;
- guards that report a change in their value's packed size to the parent that keeps the offset index, which the parent's borrow of the child makes possible;
- the packed vector, and newtypes for the two varint primitives, leaving `u32` and `u64` fixed.

## Stages

**The proposal can land in three stages, each worth having on its own.**

1. **Packed vector elements.**
   A new container packs its elements and keeps their offsets in memory.
   The format does not change while the container is described as `Opaque`, since how an opaque container lays out its content is its own business; the element types do not change either, because a variant's layout past its discriminant is the same packed or slotted.
   This stage covers the largest cases, sequences of heterogeneous values such as path data, attribute lists and child nodes.
2. **Variable-width primitives**, with their two primitive codes and the raised minimum reader version.
3. **Packed fields**, with the `Packed` descriptor kind, runtime offsets in derived types, and size changes that travel up through several levels.

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

- **Should `Packed(T)` and `T` be one descriptor when `T` has no variable part?**
  They describe the same bytes, so a type that gains packing for an invariant field would otherwise change its fingerprint for nothing.
- **Unbounded inline types.**
  A string stored inline as a length and its bytes would have no slot and could only be laid out packed, which would save the allocation that every `PersistableString` holds today — a large saving for documents with many short strings, such as identifiers and attribute values.
  Whether a type without a slot is allowed, and how a struct containing one is described, is not decided.
- **Opaque types.**
  An opaque type declares a fixed inline size; whether one should be allowed to be variable, and how a reader that cannot decode it would then find its end, is open.
- **The rope and `Move`.**
  A [rope](../rust/containers.md) would make insertions into a long packed vector cheap, and [`Move`](move-op.md) would let a rope's nodes shift packed ranges between them without copying; how the two fit together is not worked out.
- **What it saves.**
  None of the numbers above are measurements.
  A realistic workload should compare slotted with packed layouts for heterogeneous sequences, in file size, journal bytes, bytes written per byte the application writes, and flush time, before anything here is adopted.
