---
title: Type descriptors
---

The language-neutral model of how a value type lays out and interprets its bytes.

**Status:** settled and implemented.
This document is normative.

## Terminology

- **Type descriptor** (*descriptor*): a node describing one value type's on-disk representation.
- **Descriptor table**: an ordered array of descriptors.
  A descriptor refers to another by its **index** in this table — a table-local, otherwise arbitrary integer.
  Indices are an artifact of one serialization; a type's fingerprint does not depend on them.
- **Type graph**: the directed graph whose vertices are descriptors and whose edges are the references one descriptor makes to others.
  It may contain cycles — recursive and mutually recursive types are legal.
- **Place**: where a value is laid out — a struct's field, a variant's field, an element of a sequence, a small value's content, or the root.
  A place is **slotted** or **packed**, and holds the corresponding [encoding](#encodings) of its value.

## The kinds

A descriptor is one of nine kinds — **Primitive**, **Struct**, **Enum**, **Opaque**, **Pointer**, **Sequence**, **Packed**, **Slotted**, or **Small** — plus **Array**, reserved for a future revision.
The first five describe values; `Sequence` describes the content of an allocation or of a small value; `Packed` and `Slotted` declare which encoding a place holds; and `Small` describes a value that keeps short content inline.

### Primitive

A scalar with fixed and packed byte encodings, identified by a **primitive code**.

| code | type | fixed encoding | packed encoding |
| --- | --- | --- | --- |
| 0 | `u8` | 1 byte | the same byte |
| 1 | `u16` | 2 bytes, little-endian | unsigned LEB128, 1–3 bytes |
| 2 | `u32` | 4 bytes, little-endian | unsigned LEB128, 1–5 bytes |
| 3 | `u64` | 8 bytes, little-endian | unsigned LEB128, 1–10 bytes |
| 4 | `i8` | 1 byte, two's-complement | the same byte |
| 5 | `i16` | 2 bytes, little-endian two's-complement | zigzag, then unsigned LEB128, 1–3 bytes |
| 6 | `i32` | 4 bytes, little-endian two's-complement | zigzag, then unsigned LEB128, 1–5 bytes |
| 7 | `i64` | 8 bytes, little-endian two's-complement | zigzag, then unsigned LEB128, 1–10 bytes |
| 8 | `f32` | 4 bytes, little-endian IEEE-754 | the same bytes |
| 9 | `f64` | 8 bytes, little-endian IEEE-754 | the same bytes |
| 10 | `bool` | `0x00` false, `0x01` true | the same byte |
| 11 | `char` | 4 bytes, little-endian Unicode scalar value | UTF-8, 1–4 bytes |

Zigzag maps a signed integer onto an unsigned one so that small magnitudes stay small — 0, −1, 1, −2, 2 become 0, 1, 2, 3, 4 — and LEB128 then writes seven bits per byte, low groups first, with the high bit set on every byte but the last.
`u8` and `i8` keep their byte, since no varint is shorter than one byte and LEB128 would spend two on half their values.
A `char` is UTF-8 rather than a varint of its scalar value, so that a packed sequence of `char`s is byte for byte UTF-8 text.

The primitive code doubles as the descriptor's kind tag: codes `0..=127` are reserved for primitives, so a Primitive descriptor is a **single byte** with no further content.

#### Reserved primitive codes

Fixed here so later additions cannot conflict.
An implementation need not accept them until a corresponding type exists and can be tested.

| code | type | width | encoding |
| --- | --- | --- | --- |
| 12 | `u128` | 16 | little-endian |
| 13 | `i128` | 16 | little-endian two's-complement |
| 14 | `f16` | 2 | little-endian IEEE-754 binary16 |
| 15 | `bf16` | 2 | little-endian bfloat16 |
| 16–19 | `u24`, `u40`, `u48`, `u56` | 3, 5, 6, 7 | little-endian, full byte width, no packing |
| 20–23 | `i24`, `i40`, `i48`, `i56` | 3, 5, 6, 7 | little-endian two's-complement |

The fixed encodings are as listed; their packed encodings, to be fixed when the codes are, follow the rules above.

### Struct

An ordered, fixed set of named fields laid out consecutively — no discriminant, no header.

Carries:

- `name`: UTF-8, **diagnostics only**; a struct's own name is not fingerprinted.
- `fields`: an ordered list of `(field_name, type_reference)`.

Field **order** is significant: it is the on-disk order, and a field's position determines its byte offset.
Each field **name** is significant.
A positional (tuple) field's name is the decimal rendering of its position — `"0"`, `"1"`, … — so the model stays uniform.

Its fixed encoding is its fields' fixed encodings back to back; its packed encoding is its fields' encodings in their places, back to back.

### Enum

A discriminated union: a discriminant, followed by the fields of the selected variant.

Carries:

- `name`: UTF-8, diagnostics only.
- `discriminant_width`: bytes occupied by the discriminant in the fixed encoding — 1, 2, 4, or 8.
- `variants`: each `(discriminant_value, variant_name, fields)`, canonically ordered by ascending `discriminant_value`.

Each discriminant value, each variant name, and each variant's field list is significant.
Variant *declaration* order is **not**: a value's layout is selected by the stored discriminant, not by a variant's position, so reordering variants in the source without changing their discriminants changes neither the representation nor the fingerprint.

Its fixed encoding is the discriminant, little-endian at `discriminant_width`, then the selected variant's fields, then zeros up to the size of the largest variant, so that every value takes the same number of bytes; the zeros must read as zero.
Its packed encoding is the discriminant as an unsigned LEB128 varint, then the selected variant's fields, with no padding.

### Opaque

A type whose internal structure the format does **not** decompose — a black box identified nominally.

Used for types whose representations are not decomposable into the other kinds: an externally serialized payload, a length-prefixed format of the type's own.

Carries:

- `library_name`: the package, module, or crate defining the type.
  Language-neutral: not necessarily a Rust crate.
- `type_name`: UTF-8.
- `version`: a `(major, minor, patch)` triple.
- `inline_size`: the fixed number of bytes the type occupies **inline**, in a slotted and a packed place alike.
  Required because it is not structurally derivable — a type that stores its payload out of line occupies its fixed header here.
- `parameters`: an ordered list of type references, for generic parameters.

An opaque type has one encoding, its `inline_size` bytes, whatever place holds it, since the format cannot see inside it to pack it.

Only the **compatibility component** of `version` is fingerprinted, and it has two parts, both of which are necessary:

- a **stability flag** — true when `major > 0`, false when `major == 0`;
- the **leading nonzero component** — `major` if `major > 0`, otherwise `minor`.

Without the stability flag, `0.1.z` and `1.y.z` would both reduce to `1` and collide, even though crossing `0.x → 1.x` is a breaking change.
Patch-level changes, and minor-level changes above `0.x`, are thereby treated as representation-compatible.

This tuple is the same identity that semantic versioning already relies on, and it carries the same risk: it trusts the author to bump the major version whenever the representation changes.
A structural descriptor needs no such trust, which is a standing argument for preferring structural descriptors wherever a type's layout admits one, and reserving Opaque for representations that genuinely are not decomposable.

### Pointer

**An owning pointer to an allocation whose content is the referenced type.**

Carries `target`, a reference to the descriptor of what the allocation holds.
In a slotted place, a pointer is the allocation's id in 4 bytes, little-endian; in a packed place, the unsigned LEB128 varint of the id, 1 to 5 bytes.
Either way, `0` is the null pointer, which points to no allocation.
The [address table](../address-table.md#bounds) fixes ids at 32 bits, so a packed pointer whose varint decodes beyond `2^32 − 1` is invalid.

The content of the pointed-to allocation starts afresh: it is laid out slotted, unless `target` is a `Packed` wrapper, which lays it out packed.
A pointer must say what it points to, or two containers that differ only in what their allocations hold — a vector of `u32` and a vector of `u64` — would share a descriptor and a fingerprint, and a reader would open one as the other.

### Sequence

**Values of one type back to back, as many as fill the range that holds them.**

Carries `element`, a reference.
A sequence has no inline form and no count: it fills exactly the range it is given, which only an allocation a pointer points to, or a small value's content, gives it, and a reader decodes elements until that range ends.
Its elements are slotted or packed as the range is: an allocation is slotted unless its pointer's target is a `Packed` wrapper, and a small value's content is packed.

An element type whose encodings take no bytes cannot form a sequence, since its count could not be recovered.

### Packed

**What a pointer points to, laid out packed.**

Carries `target`, a reference.
`Packed(T)` may stand only as the target of a `Pointer`; it describes the same value as `T` and declares the allocation's content packed rather than slotted.
So a vector of `char`s laid out packed is `Pointer(Packed(Sequence(char)))`, whose allocation holds UTF-8 text.

### Slotted

**A place inside a packed value declared slotted.**

Carries `target`, a reference to a slottable type.
`Slotted(T)` describes the same value as `T`, and makes the place hold `T`'s fixed encoding although the value around it is packed: for a field that changes often, so that it never changes size, such as a counter whose varint would grow at every power of 128.
Inside a slotted value it changes nothing, since every place there is slotted already.

### Small

**A value that keeps short content inline, and its content in another form once it grows.**

Carries two references, `content` and `spilled`.
Its encoding is a **one-byte size tag**, then:

- for a tag from 0 to 254, a `content` in exactly that many bytes, packed;
- for the tag 255, a `spilled` value, packed.

`content` fills the tag's number of bytes exactly, as a sequence does, and a reader that decodes it must consume them all.
`spilled` says what replaces the inline form, and may be any type: typically a pointer to an allocation that holds the same content, so that the bytes inline and spilled are the same.
A small string is `Small(Sequence(char), Pointer(Packed(Sequence(char))))`, its text inline or in an allocation of its own.

**A small value has no fixed encoding**, since one would have to reserve its whole inline capacity in every value; it stands only in packed places.
When content moves between the two forms is the policy of the library that defines the type: the tag says which form a value takes, so a reader decodes either form, whatever policy wrote it, and the descriptor records no threshold.
That makes a small value the one exception to the [canonical form](#canonical-form): content short enough for either form may be held in either.

### Array (reserved)

A fixed-length homogeneous sequence: `count` consecutive values of one element type, no header, no discriminant.

Carries `element` (a reference) and `count`.
Its fixed encoding is `count` fixed element encodings, its packed encoding `count` packed ones.

**Reserved, not yet implemented.**
An Array is deliberately *not* interchangeable with a Struct of `count` identically-typed positional fields: the byte layouts coincide, but the kind tags differ and therefore so do the fingerprints.
The kind exists precisely to avoid the descriptor blow-up of spelling out a large `count` as that many fields.

## References and the root

A **reference** is an index into the descriptor table.
Every `type`, `element`, `target`, `content`, `spilled`, and parameter field is a reference.

By convention the descriptor at **index 0 is the root** — the type actually stored in the file.

## Encodings

**Every type has a packed encoding, which a reader who knows the type's descriptor decodes front to back, learning where the value ends as it goes, and most types also have a fixed encoding, which takes the same number of bytes for every value.**

| kind | fixed encoding | packed encoding |
| --- | --- | --- |
| Primitive | [per code](#primitive) | [per code](#primitive) |
| Struct | its fields' fixed encodings | its fields' encodings in their places |
| Enum | the discriminant at its width, the variant's fields, zeros up to the largest variant | the discriminant as a varint, then the variant's fields |
| Opaque | its `inline_size` bytes | the same bytes |
| Pointer | the id in 4 bytes, little-endian | the id as a varint |
| Slotted | `target`'s fixed encoding | `target`'s fixed encoding |
| Small | none | a size tag, then `content` or `spilled` |
| Array *(reserved)* | its elements' fixed encodings | its elements' packed encodings |

`Sequence` and `Packed` describe the content of a range rather than an inline value, and have the encoding of that content: a sequence's elements back to back, in the range's encoding.

### Fixed-size, slottable, packed-only

**A type is *slottable* if it has a fixed encoding, and *packed-only* if it has none.**
Every primitive, pointer, opaque type and `Slotted` wrapper is slottable, and every small value packed-only; a struct or an enum is slottable exactly when the types of all its fields are, so a type that holds a packed-only type inline is packed-only as well.

**A type is *fixed-size* if its packed encoding takes the same number of bytes for every value**, and then its two encodings are one: floats, bytes, `bool`s, opaque types, and structs of them.
A fixed-size type is always slottable; a variable-size type may be either, as a `u16` is slottable and a small value packed-only.

### Canonical form

The packed encodings obey three rules, so that a value has one encoding and a reader can refuse anything else:

- a varint must be minimal — no trailing `0x80` groups — and decode to a value within its type's range, a pointer's within 32 bits;
- a `char` must be well-formed UTF-8: its shortest form, and no surrogate;
- an enum's discriminant must be one of its variants'.

A [small value](#small) is the exception: content short enough for either form may be held in either.

A packed encoding is not always the shorter one: a `u16` of `2^14` or more takes three bytes packed against two fixed, a `u32` of `2^28` or more five against four, and a pointer of `2^28` or more five against four.

## Places

**The encoding is chosen where a value is laid out, and the places inside a value inherit it, so packing a value packs everything inline in it, unless a place inside declares itself slotted.**
A place takes the choice of the value it sits in, unless its declaration fixes one: a `Slotted` wrapper fixes slotted, and a `Packed` wrapper, as a pointer's target, fixes packed.

**Inheritance stops at pointers and at opaque types.**
What a pointer points to starts afresh, slotted unless the pointer's target is a `Packed` wrapper, while the pointer itself follows its place.
An opaque type lays out its parameters by its own rules, so each starts afresh, as a root does.

**A slotted value is slotted throughout, up to its pointers and opaque types.**
A packed place inside a slotted value would never save a byte, since the slot reserves the largest encoding its content can take and a packed encoding's largest is never smaller than the fixed one; so no place inside a slotted value is packed, and no packed-only type stands in one.

### The rules of nesting

A descriptor table must keep these rules, and a reader refuses a table that breaks any of them:

- a `Sequence` stands only as the target of a pointer, as the target of a `Packed` wrapper, or as a small value's `content`;
- a `Packed` wrapper stands only as the target of a pointer;
- neither a `Sequence` nor a `Packed` wrapper is the root;
- a `Slotted` wrapper's target is slottable;
- a packed-only type stands only where every value around it, up to the nearest pointer, is packed;
- no struct or enum contains itself through its fields alone, which would give it no finite encoding; through a pointer, it may.

A table whose types hold no `Packed` wrapper and no small value has no packed place at all, so every reference in it resolves to slotted.

### The root

**The root is slotted if its type is slottable, and packed otherwise.**
Its allocation holds the root value's encoding and nothing else, so a packed root's allocation is exactly as long as its encoding.

## Slot sizes

Every slottable type determines a **slot size**, the number of bytes its fixed encoding takes:

- a Primitive's is fixed by its code;
- a Struct's is the sum of its fields';
- an Enum's is `discriminant_width` plus the largest variant's field sum;
- a Pointer's is 4;
- a `Slotted` wrapper's is its target's;
- an Array's is `count × element`;
- an Opaque's is its `inline_size`, stored explicitly, because it is not derivable.

That the slot size is *always* derivable from the type graph is what makes evolution tractable for slotted values: a reader can compute the **writer's** offsets from the **writer's** graph, even when its own sizes differ.
For packed values, the writer's offsets depend on each value, but the writer's descriptors still determine how to find them, by decoding front to back.
See [Evolution](evolution.md).

## What was considered and declined

**A bit on every reference saying which encoding its place holds**, instead of the `Packed` and `Slotted` wrappers.
It would carry the same information, but change the encoding, and so the fingerprint, of every type that has a field; the wrappers leave every table without packed places as it is.

**Packing a value without inheritance**, so that each type declares its own fields packed in advance.
Packing a struct would then change none of its bytes, since a struct has neither padding nor integers of its own, and a container could not pack values whose types know nothing of it.

**`Zero` statements for the padding of slotted enums.**
The [address table](../address-table.md#statement-types) can state a range as zeros without writing them, but a statement costs on the order of ten bytes, more than the padding between two elements of a vector.

## Open questions

- **Should `Slotted(T)` be written where `T` is fixed-size?**
  It changes no byte, only the fingerprint, so a binding could leave it out, or the fingerprint could ignore it.
  The reference implementation writes it for every field declared slotted.
- **Small values with a fixed size.**
  Whether a type that keeps a few bytes inline with a fixed size, such as a string of at most 15 bytes in a 16-byte slot, deserves a kind of its own, so that a slotted struct could hold it, is open.
- **The hash map's layout.**
  Described structurally, a hash map shows its tombstoned slots to a tool as entries whose liveness flag is clear; whether it should keep that layout is open, and a change would change its descriptor.
