---
title: Tooling
---

What a generic tool may assume about a kladde file it did not write, and how far it can get without knowing the application's data structures.

**Status:** design intent.
No tools exist yet.

## The premise

A kladde file is self-describing in two independent ways.
It describes its **storage**: the [address table](address-table.md) is reachable from the header and names every allocation, so the file's structure is recoverable without any application knowledge at all — and without a scan, since everything is rooted.
And it describes its **types**: the embedded [descriptor table](schema/) says how every non-opaque value in the file is laid out.

Together these mean a tool can be useful at three levels, each requiring strictly more than the last.

## Level 1 — storage analysis

Requires: the [page](file-format.md), [address-table](address-table.md) and [allocation](allocations.md) layers.
Requires **no** schema and no application knowledge.

A tool at this level can:

- enumerate every live allocation with its id and size, and every fragment of its content with the page it lives in;
- compute total live bytes, file length, and therefore **garbage**;
- report the per-page live fraction, and how much of the file is superseded pages awaiting reuse;
- report journal length and how far the committed state lags behind it;
- **consolidate** the file into a tight new one, and truncate it.

Rewriting a file from an external tool is safe for exactly the reason it is safe internally: it changes no id, no size, and no content, so it is invisible to any reader.
This makes a standalone `kladde compact` genuinely implementable, which is unusual for a format of this kind.

What it cannot do is tell you what any of it *means* — which allocation holds what, or whether a large one is a container's backing array or a single opaque blob.

## Level 2 — schema inspection

Requires: additionally the [descriptor table](schema/canonical-encoding.md).
Still requires no application knowledge.

A tool at this level can:

- list every type in the file, with its kind, fields, variants, and slot size, or that it has none;
- compute and display each type's [fingerprint](schema/fingerprints.md);
- identify the root type;
- report which types are Opaque, and their declaring library and version — which is exactly the list of things a reader would need an implementation of;
- diff two files' schemas, or a file's schema against an application's.

This is what makes "why won't my application open this file?" a diagnosable question rather than a guess.

## Level 3 — structural export

Requires: additionally the ability to walk the value graph from the root.
Requires no application knowledge, but is **inherently partial**.

A tool at this level can render the file as text — JSON, or something more structured — by starting at the root type and recursing.
Primitives decode by their code, structs by their fields, enums by their discriminant, a small value by its tag; every packed encoding marks its own end, so a tool decodes it once it has resolved each [place's](schema/type-descriptors.md#places) encoding from the wrappers and from inheritance.
A [pointer](schema/type-descriptors.md#pointer) says what its allocation holds, so a tool follows it into the allocation and reads its content — a vector's elements, a string's text, a hash map's slots.

It stops at one boundary:

**Opaque types.**
By definition their content is not decomposable.
A tool can report identity, version, and inline size, and can dump the bytes, but cannot interpret them, and cannot follow a pointer an opaque type holds.

## Containers

**Containers describe their structure, so a tool walks them without knowing the library that defines them.**
A vector is a `Pointer` to a `Sequence` of its elements, a string a `Pointer` to a `Packed` `Sequence` of `char`s, a hash map a `Pointer` to a `Sequence` of slots, each a liveness flag, a key and a value.
Their layouts are therefore part of the format, and changing one changes the descriptors that files contain; that is the price of a file that describes itself.
The alternative, registering container layouts out of band so that a tool knows the standard containers while they stay opaque in the file, would have needed a tool to carry a table of known types, and would not reach a third party's containers at all.

A container that serializes its content with an external format, such as the reference implementation's blob, stays opaque.

**What a tool still cannot tell is what a library makes of a structure** — that a string is kept normalized, or that a hash map's slot whose flag is clear holds no entry.
A way to annotate a structural descriptor with the library that defines it is a [draft](../drafts/library-annotations.md).

## Cross-implementation testing

Tools are also the natural vehicle for testing the cross-language contract.
A conformance suite would consist of files written by each implementation, plus the expected level-2 and level-3 output for each, so that every implementation can be checked against every other's output without either needing to link the other.

See [Conformance](conformance.md).
