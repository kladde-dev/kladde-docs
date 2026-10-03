---
title: Canonical encoding
---

The byte serialization of a [descriptor table](type-descriptors.md).

**Status:** settled and implemented.
This document is normative: two conforming implementations must produce **byte-identical** output for the same type graph.

## Encoding primitives

- `byte(b)` — a single octet.
- `varint(n)` — unsigned LEB128: seven bits per byte, little-endian groups, high bit is the continuation flag.
- `string(s)` — `varint(byte_length)` followed by the UTF-8 bytes of `s`.
- `reference(index)` — `varint(index)`.

## Descriptor encoding

Each descriptor begins with a single **kind-tag byte** that both selects the kind and partitions the tag space.

**`0..=127` — Primitive.**
The tag byte *is* the [primitive code](type-descriptors.md#primitive).
The descriptor has no further content.

**`128..=255` — a non-primitive kind**, followed by kind-specific content:

| tag | kind | content |
| --- | --- | --- |
| 128 | Struct | `string(name) varint(field_count)`, then per field `string(field_name) reference(type)` |
| 129 | Enum | `string(name) byte(discriminant_width) varint(variant_count)`, then per variant `varint(discriminant_value) string(variant_name) varint(field_count)`, then per field `string(field_name) reference(type)` |
| 130 | Opaque | `string(library_name) string(type_name) varint(major) varint(minor) varint(patch) varint(inline_size) varint(param_count)`, then per parameter `reference(type)` |
| 131 | Array *(reserved)* | `reference(element) varint(count)` |
| 132 | Pointer | `reference(target)` |
| 133 | Sequence | `reference(element)` |
| 134 | Packed | `reference(target)` |
| 135 | Slotted | `reference(target)` |
| 136 | Small | `reference(content) reference(spilled)` |

Tags `137..=255` are unassigned and reserved for future kinds.

A decoder applies the [rules of nesting](type-descriptors.md#the-rules-of-nesting) to every table it reads, and refuses one that breaks them.

## Canonical order

The order in which sub-elements are emitted is fixed, so that the encoding — and therefore the [fingerprint](fingerprints.md) — is fully determined.

**Struct fields** are emitted in **declaration order**, which is their on-disk layout order.
A field's position determines its byte offset, so this order is part of the representation and is never re-sorted.

**Enum variants** are emitted in **ascending discriminant value**.
A variant's position does not affect any value's layout, so declaration order is discarded in favour of this canonical order.
Two enums differing only in the source order of their variants encode identically.

**Opaque parameters** and an **Array's element** keep their given order, being positional type arguments.

**A Small's references** are emitted `content` first, then `spilled`.

## Table encoding

`varint(descriptor_count)`, followed by each descriptor in index order, root first.

This is the canonical on-file encoding of a schema.
Its placement, framing, alignment, and checksumming within a kladde file are a [container-layer](../file-format.md) matter.

## Worked example

The type `struct Point { x: i32, y: u8 }` as a two-entry table:

```
02                      table: 2 descriptors
  80                    [0] tag 128 = Struct
  05 50 6f 69 6e 74     name: "Point"
  02                    2 fields
    01 78  01           "x" -> reference 1
    01 79  02           "y" -> reference 2
```

...except that reference 2 does not exist in a two-entry table.
A real encoding of this type has three descriptors — `Point`, `i32`, `u8` — because primitives occupy table entries like anything else:

```
03                      table: 3 descriptors
  80                    [0] Struct
  05 50 6f 69 6e 74     "Point"
  02                    2 fields
    01 78  01           "x" -> [1]
    01 79  02           "y" -> [2]
  06                    [1] Primitive code 6 = i32
  00                    [2] Primitive code 0 = u8
```

Note that the table is free to place primitives anywhere and to duplicate or share them; the [fingerprint](fingerprints.md) is invariant under any such choice.
