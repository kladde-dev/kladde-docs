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
| 129 | Enum | `string(name) byte(discriminant_width) varint(variant_count)`, then per variant `varint(discriminant_value) string(variant_name) reference(payload)` |
| 130 | Opaque | `string(library_name) string(type_name) varint(major) varint(minor) varint(patch) varint(size_mode) varint(param_count)`, then per parameter `reference(type)` |
| 131 | Array | `reference(element) varint(len)` |
| 132 | Map | `reference(key) reference(value)` |
| 133 | Sequence | `reference(element)` |
| 134 | Text | nothing |
| 135 | Fixed | `reference(target)` |

An Opaque's `size_mode` is `0` for a value prefixed with its length, and one more than the size for a type with a fixed size: `n + 1` for `n` bytes.
Tags `136..=255` are unassigned and reserved for future kinds, and a reader refuses a table that uses one.

A decoded table must also keep the [rules of nesting](type-descriptors.md#rules-of-nesting), and every enum's discriminant values must fit its `discriminant_width`; a reader refuses one that does not.

**A reader refuses every table that is not the canonical encoding of the table it decodes to**: a varint longer than its value needs, enum variants out of ascending discriminant order, two variants with one discriminant, or bytes after the last descriptor.
So a table that decodes re-encodes to its own bytes, and two tables that decode alike are byte-identical, which is what lets [fingerprints](fingerprints.md) and the file's schema chain be compared as bytes.

## Canonical order

The order in which sub-elements are emitted is fixed, so that the encoding — and therefore the [fingerprint](fingerprints.md) — is fully determined.

**Struct fields** are emitted in **declaration order**, which is their on-disk layout order.
A field's position determines its byte offset, so this order is part of the representation and is never re-sorted.

**Enum variants** are emitted in **ascending discriminant value**.
A variant's position does not affect any value's layout, so declaration order is discarded in favour of this canonical order.
Two enums differing only in the source order of their variants encode identically.

**Opaque parameters** keep their given order, being positional type arguments; a Map emits its key before its value, and an Array, a Sequence and a `Fixed` have one reference each.

## Table encoding

`varint(descriptor_count)`, followed by each descriptor in index order, root first.

This is the canonical on-file encoding of a schema, and the content of a file's [schema chain](../sequence.md#three-chains).
A table holds at least one descriptor, every reference lies inside it, and no byte follows its last descriptor.

A table of a vector of text, `Sequence(Text)`, is the four bytes `02 85 01 86`: two descriptors, a Sequence (133) of descriptor 1, and Text (134).

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
