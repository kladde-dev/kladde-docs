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

## The kinds

A descriptor is one of four kinds — **Primitive**, **Struct**, **Enum**, or **Opaque** — plus **Array**, reserved for a future revision.

### Primitive

A fixed-width scalar with a fixed byte encoding, identified by a **primitive code**.

| code | type | width | encoding |
| --- | --- | --- | --- |
| 0 | `u8` | 1 | little-endian |
| 1 | `u16` | 2 | little-endian |
| 2 | `u32` | 4 | little-endian |
| 3 | `u64` | 8 | little-endian |
| 4 | `i8` | 1 | little-endian two's-complement |
| 5 | `i16` | 2 | little-endian two's-complement |
| 6 | `i32` | 4 | little-endian two's-complement |
| 7 | `i64` | 8 | little-endian two's-complement |
| 8 | `f32` | 4 | little-endian IEEE-754 |
| 9 | `f64` | 8 | little-endian IEEE-754 |
| 10 | `bool` | 1 | `0x00` false, `0x01` true |
| 11 | `char` | 4 | little-endian Unicode scalar value |

The primitive code doubles as the descriptor's kind tag: codes `0..=127` are reserved for primitives, so a Primitive descriptor is a **single byte** with no further content.
Its inline width is fixed by its code.

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

### Struct

An ordered, fixed set of named fields laid out consecutively — no discriminant, no header.

Carries:

- `name`: UTF-8, **diagnostics only**; a struct's own name is not fingerprinted.
- `fields`: an ordered list of `(field_name, type_reference)`.

Field **order** is significant: it is the on-disk order, and a field's position determines its byte offset.
Each field **name** is significant.
A positional (tuple) field's name is the decimal rendering of its position — `"0"`, `"1"`, … — so the model stays uniform.

### Enum

A discriminated union: a discriminant, followed by the fields of the selected variant.

Carries:

- `name`: UTF-8, diagnostics only.
- `discriminant_width`: bytes occupied by the discriminant — 1, 2, 4, or 8.
- `variants`: each `(discriminant_value, variant_name, fields)`, canonically ordered by ascending `discriminant_value`.

Each discriminant value, each variant name, and each variant's field list is significant.
Variant *declaration* order is **not**: a value's layout is selected by the stored discriminant, not by a variant's position, so reordering variants in the source without changing their discriminants changes neither the representation nor the fingerprint.

### Opaque

A type whose internal structure the format does **not** decompose — a black box identified nominally.

Used for types with hand-written representations that are not a plain field sum: length-prefixed blobs, externally serialized payloads, built-in container types.

Carries:

- `library_name`: the package, module, or crate defining the type.
  Language-neutral: not necessarily a Rust crate.
- `type_name`: UTF-8.
- `version`: a `(major, minor, patch)` triple.
- `inline_size`: the fixed number of bytes the type occupies **inline**.
  Required because it is not structurally derivable — a type that stores its payload out of line occupies its fixed header here.
- `parameters`: an ordered list of type references, for generic parameters.

Only the **compatibility component** of `version` is fingerprinted, and it has two parts, both of which are necessary:

- a **stability flag** — true when `major > 0`, false when `major == 0`;
- the **leading nonzero component** — `major` if `major > 0`, otherwise `minor`.

Without the stability flag, `0.1.z` and `1.y.z` would both reduce to `1` and collide, even though crossing `0.x → 1.x` is a breaking change.
Patch-level changes, and minor-level changes above `0.x`, are thereby treated as representation-compatible.

This tuple is the same identity that semantic versioning already relies on, and it carries the same risk: it trusts the author to bump the major version whenever the representation changes.
A structural descriptor needs no such trust, which is a standing argument for preferring structural descriptors wherever a type's layout admits one, and reserving Opaque for representations that genuinely are not decomposable.

### Array (reserved)

A fixed-length homogeneous sequence: `count` consecutive values of one element type, no header, no discriminant.

Carries `element` (a reference) and `count`.
Inline width is `count × element.inline_size`.

**Reserved, not yet implemented.**
An Array is deliberately *not* interchangeable with a Struct of `count` identically-typed positional fields: the byte layouts coincide, but the kind tags differ and therefore so do the fingerprints.
The kind exists precisely to avoid the descriptor blow-up of spelling out a large `count` as that many fields.

### Pointer (reserved)

A kind tag is reserved for describing an owning pointer, so that a schema-driven tool could follow the ownership graph without help from any language binding.

Content and semantics are **TBD**.
It is not needed until a language-independent garbage collector or leak-detection tool exists; the language bindings currently walk ownership using their own type knowledge.
See [Tooling](../tooling.md).

## References and the root

A **reference** is an index into the descriptor table.
Every `type`, `element`, and parameter field is a reference.

By convention the descriptor at **index 0 is the root** — the type actually stored in the file.

## Inline size

Every descriptor determines a fixed **inline size**, the number of bytes a value of that type occupies within its parent:

- a Primitive's is fixed by its code;
- a Struct's is the sum of its fields';
- an Enum's is `discriminant_width` plus the largest variant's field sum;
- an Array's is `count × element`;
- an Opaque's is stored explicitly, because it is not derivable.

That the inline size is *always* derivable from the type graph is what makes evolution tractable: a reader can compute the **writer's** offsets from the **writer's** graph, even when its own sizes differ.
See [Evolution](evolution.md).
