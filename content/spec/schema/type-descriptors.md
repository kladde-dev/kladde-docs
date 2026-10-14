---
title: Type descriptors
---

The language-neutral model of how a value type encodes into the [sequence](../sequence.md), and what its bytes mean.

**Status:** settled and implemented.
This document is normative.

## Terminology

- **Type descriptor** (*descriptor*): a node describing one value type's encoding.
- **Descriptor table**: an ordered array of descriptors.
  A descriptor refers to another by its **index** in this table — a table-local, otherwise arbitrary integer.
  Indices are an artifact of one serialization; a type's fingerprint does not depend on them.
- **Type graph**: the directed graph whose vertices are descriptors and whose edges are the references one descriptor makes to others.
  It may contain cycles — recursive and mutually recursive types are legal, within [the rules of nesting](#rules-of-nesting).

## The kinds

A descriptor is one of nine kinds: **Primitive**, **Struct**, **Enum**, **Array**, **Sequence**, **Map**, **Text**, **Opaque**, and **Fixed**.
Every type has **one encoding**, which a reader decodes front to back from the type's descriptor, [below](#encodings).

### Primitive

A scalar, identified by a **primitive code**.

| code | type | encoding | fixed form |
| --- | --- | --- | --- |
| 0 | `u8` | one byte | the same byte |
| 1 | `u16` | unsigned LEB128 | 2 bytes, little-endian |
| 2 | `u32` | unsigned LEB128 | 4 bytes, little-endian |
| 3 | `u64` | unsigned LEB128 | 8 bytes, little-endian |
| 4 | `i8` | one byte, two's complement | the same byte |
| 5 | `i16` | zigzag, then LEB128 | 2 bytes, little-endian two's complement |
| 6 | `i32` | zigzag, then LEB128 | 4 bytes, little-endian two's complement |
| 7 | `i64` | zigzag, then LEB128 | 8 bytes, little-endian two's complement |
| 8 | `f32` | 4 bytes, little-endian IEEE-754 | the same bytes |
| 9 | `f64` | 8 bytes, little-endian IEEE-754 | the same bytes |
| 10 | `bool` | one byte, `0x00` false, `0x01` true | the same byte |
| 11 | `char` | UTF-8, 1 to 4 bytes | the Unicode scalar value, 4 bytes, little-endian |

Zigzag maps `0, −1, 1, −2, …` to `0, 1, 2, 3, …`, so that a number of small magnitude takes one byte whatever its sign.
The *fixed form* is what the type's values take inside a [`Fixed`](#fixed) value.

The primitive code doubles as the descriptor's kind tag: codes `0..=127` are reserved for primitives, so a Primitive descriptor is a **single byte** with no further content.

#### Reserved primitive codes

Fixed here so later additions cannot conflict.
An implementation need not accept them until a corresponding type exists and can be tested, and must refuse a table that uses one it does not implement.

| code | type |
| --- | --- |
| 12 | `u128` |
| 13 | `i128` |
| 14 | `f16`, IEEE-754 binary16 |
| 15 | `bf16`, bfloat16 |
| 16–19 | `u24`, `u40`, `u48`, `u56` |
| 20–23 | `i24`, `i40`, `i48`, `i56` |

### Struct

An ordered, fixed set of named fields, encoded back to back — no discriminant, no header.

Carries:

- `name`: UTF-8, **diagnostics only**; a struct's own name is not fingerprinted.
- `fields`: an ordered list of `(field_name, type_reference)`.

Field **order** is significant: it is the order of the encodings.
Each field **name** is significant.
A positional (tuple) field's name is the decimal rendering of its position — `"0"`, `"1"`, … — so the model stays uniform.

### Enum

A discriminated union: a discriminant, followed by the value that the variant it selects holds, its **payload**.

Carries:

- `name`: UTF-8, diagnostics only.
- `discriminant_width`: the bytes the discriminant takes in the **fixed form** — 1, 2, 4, or 8; every discriminant value must fit in it.
  The encoding itself writes the discriminant as a varint.
- `variants`: each `(discriminant_value, variant_name, payload)`, canonically ordered by ascending `discriminant_value`; discriminant values are distinct, and `payload` is a reference.

Each discriminant value, each variant name, and each variant's payload type is significant.
Variant *declaration* order is **not**: a value's encoding is selected by the stored discriminant, not by a variant's position, so reordering variants in the source without changing their discriminants changes neither the encoding nor the fingerprint.

**Every variant holds exactly one value.**
A variant that holds nothing holds the empty Struct, which encodes to no bytes; a variant that holds several fields holds a Struct of them; and a variant that holds one value of any type holds that type directly.
So in Rust, a unit variant `Nil` holds the empty Struct, a tuple variant `Element(Element)` or `Text(String)` holds its field's type, a struct variant `Point { x: f32, y: f32 }` holds a Struct of its fields named after the variant, and a tuple variant of several fields `Pair(u8, u8)` a Struct of positional fields.

*Why one value, always.*
Languages disagree about what a variant holds: Rust has unit, tuple and struct variants, while a C++ `std::variant`, a C++ class hierarchy, Swift's enums with associated values and TypeScript's discriminated unions all hold exactly one value per alternative.
Describing a variant by one payload lets every one of them map to the same descriptor with no choice left to the programmer: Rust's `Element(Element)` and a struct variant with `Element`'s fields describe the same bytes and get the same fingerprint, as a struct's own name already does not matter.
And holding *always* one value, rather than at most one, keeps every variant the same shape: a valueless variant, a C++ alternative that is an empty struct and Rust's `()` all hold the empty Struct, and need no rule to say they agree.

An `Option<T>` is an enum, `None` with discriminant 0 holding the empty Struct, and `Some` with discriminant 1 holding a `T`.

### Array

`len` values of one element type, back to back, with no count.

Carries `element` (a reference) and `len`.
An Array is deliberately *not* interchangeable with a Struct of `len` identically-typed positional fields: the encodings coincide, but the kind tags differ and therefore so do the fingerprints.
The kind exists to avoid spelling out a large `len` as that many fields.

### Sequence

Any number of values of one element type: their count, then the values.

Carries `element` (a reference).
A vector is a Sequence; a map is a [Map](#map).

### Map

Any number of entries, each a key and a value: their count, then each entry's key and value.

Carries `key` and `value` (references).
The entries are in the order the writing library keeps them; the format neither sorts them nor checks that keys are distinct, which is the library's business, as is how it finds a key in memory.

*Why a kind of its own.*
A Map encodes exactly as a Sequence of two-field Structs would, but every language has a native map that a reader can decode one into, and a tool can render one as a JSON object rather than as an array of pairs.
It also leaves nothing to agree on: a Sequence of Structs would need every implementation to name the two fields alike, or the same map would get two fingerprints.

### Text

UTF-8 text, ended by the byte `0xFF`.
Carries nothing.

*Why a terminator rather than a count.*
UTF-8 never contains `0xFF`, so no byte of text needs escaping, and an edit of text is then one splice, where a count would add a second record — and a second place where the chain of spans is cut — to every keystroke.
A decoder finds the end by scanning for the terminator, which it does while it checks the UTF-8 anyway; text below 128 bytes takes the byte a count would have taken, and longer text saves the count's further bytes.
Only text can have a terminator, since a terminator must be a byte the content cannot hold, so bytes and sequences of any other type keep their count.

### Opaque

A type whose internal structure the format does **not** decompose — a black box identified nominally.

Used for types with hand-written encodings that no structural descriptor fits: externally serialized payloads, say.

Carries:

- `library_name`: the package, module, or crate defining the type.
  Language-neutral: not necessarily a Rust crate.
- `type_name`: UTF-8.
- `version`: a `(major, minor, patch)` triple.
- `fixed_size`: the number of bytes every value takes, or none, in which case each value is prefixed with its length.
- `parameters`: an ordered list of type references, for generic parameters.

Only the **compatibility component** of `version` is fingerprinted, and it has two parts, both of which are necessary:

- a **stability flag** — true when `major > 0`, false when `major == 0`;
- the **leading nonzero component** — `major` if `major > 0`, otherwise `minor`.

Without the stability flag, `0.1.z` and `1.y.z` would both reduce to `1` and collide, even though crossing `0.x → 1.x` is a breaking change.
Patch-level changes, and minor-level changes above `0.x`, are thereby treated as encoding-compatible.

This tuple is the same identity that semantic versioning already relies on, and it carries the same risk: it trusts the author to bump the major version whenever the encoding changes.
A structural descriptor needs no such trust, which is a standing argument for preferring structural descriptors wherever a type admits one, and reserving Opaque for encodings that genuinely are not decomposable.
Whether a fixed size or a length prefix per type is enough, or whether some opaque types want both modes, is open.

### Fixed

The values of a wrapped type, each encoded at the same size: its **fixed form**.

Carries `target` (a reference).
`Fixed(T)` is a type of its own, with the values of `T`: it lays out everything inside `T` at its largest, so every field lies at a constant offset whatever the value, and a container of them finds its `i`th element by multiplication.
It applies to all of `T` and not to what wraps it: `Fixed(Option<u32>)` takes five bytes for every value, while `Option<Fixed<u32>>` takes one for `None` and five for `Some`.

## Encodings

| kind | encoding |
| --- | --- |
| Primitive | as [its code](#primitive) gives |
| Struct | its fields' encodings, concatenated, in declaration order |
| Enum | the discriminant as unsigned LEB128, then the selected variant's payload's encoding |
| Array | `len` element encodings |
| Sequence | the number of elements as unsigned LEB128, then the element encodings |
| Map | the number of entries as unsigned LEB128, then each entry's key's encoding and its value's |
| Text | the UTF-8 bytes, then `0xFF` |
| Opaque | `fixed_size` bytes, or, without a fixed size, the number of bytes as unsigned LEB128 and then the bytes |
| Fixed | the target's fixed form, below |

**The fixed form** of a type is:

| kind | fixed form |
| --- | --- |
| Primitive | the fixed form of [its code](#primitive) |
| Struct | its fields' fixed forms, concatenated |
| Enum | the discriminant in `discriminant_width` bytes, little-endian, then the variant's payload's fixed form, then zeros up to the size of the largest payload's |
| Array | `len` element fixed forms |
| Opaque | its encoding, if it has a fixed size |
| Fixed | the target's fixed form |

Sequence, Map, Text, and an Opaque without a fixed size have **no** fixed form, and neither has any type that holds one of them, or itself.

**The canonical encoding** is the only one a writer may produce, and a reader refuses any other: varints are minimal, text is valid UTF-8, discriminants name variants, integers lie in range of their type — a `u16`'s varint at most `2^16 − 1`, an `i16`'s zigzag likewise — `bool`s are 0 or 1, a `char`'s fixed form is a Unicode scalar value, and an enum's padding in the fixed form is zeros.
So a value has exactly one encoding, and the [sequence](../sequence.md#what-a-valid-sequence-is) is a function of the root value.

### A worked example

With `struct Point { x: f32, label: Text, n: u32 }`, the point `(1.5, "ab", 300)` encodes as

```
00 00 C0 3F      x: 1.5
61 62 FF         label: "ab", and its terminator
AC 02            n: 300 as LEB128
```

and `Fixed(Option<u32>)` holding `Some(300)` as `01 2C 01 00 00`: the discriminant in one byte, then 300 in four, little-endian; `None` is `00 00 00 00 00`.

## Fixed-size types

**A type whose every value encodes to the same number of bytes is fixed-size**, and the descriptors say which types are, so a reader computes it as a writer does:

- `u8`, `i8`, `bool`, `f32` and `f64` are, with sizes 1, 1, 1, 4 and 8; the other primitives are varints or UTF-8, and are not;
- a Struct is if all its fields are, with the sum of their sizes;
- an Enum is if every discriminant is below 128, so that its varint takes one byte, and every variant's payload is fixed-size with the same size: its size is 1 plus that size;
- an Array is if its element is, with `len` times the element's size;
- an Opaque is if it declares a fixed size;
- a `Fixed` always is, with the size of its target's fixed form;
- Sequence, Map and Text never are, nor is a type that holds itself.

A fixed-size type's fixed form is its encoding byte for byte, except where an enum's `discriminant_width` exceeds the one byte its encoding spends on the discriminant: a writer may set a wider width so that the fixed form keeps its size when variants are added.

## Rules of nesting

A table must keep two rules, which a reader checks when it decodes one:

- **A `Fixed` holds a type that has a fixed form.**
- **A type reaches itself only through an Enum, a Sequence or a Map**, whose discriminant or count can end the recursion, and never through Structs, Arrays and `Fixed` alone, whose values would be infinite.
  A tree whose nodes hold their children inline is a Struct holding a Sequence of itself; a list is an Enum one of whose variants holds itself.
  An Opaque's parameters are not containment: an Opaque says nothing of how its value holds them, so a type may reach itself through one.
  The rule asks only that recursion *can* end; an Enum whose every variant holds itself has no finite value, and is legal but useless.

A decoder of a recursive type nests as deeply as the file says, so it limits its depth, or does not recurse, and refuses a file beyond its limit rather than overflow its stack.

## References and the root

A **reference** is an index into the descriptor table.
Every field type, payload, `element`, `key`, `value`, `target`, and parameter is a reference.

By convention the descriptor at **index 0 is the root** — the type actually stored in the file.

*Why the model is enough for evolution.*
Every encoding is decodable from the descriptors alone, so a reader can skip, or read, a writer's values by the **writer's** graph, even when its own types differ.
See [Evolution](evolution.md).
