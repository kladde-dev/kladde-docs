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

- list every type in the file, with its kind, fields, variants, and inline size;
- compute and display each type's [fingerprint](schema/fingerprints.md);
- identify the root type;
- report which types are Opaque, and their declaring library and version — which is exactly the list of things a reader would need an implementation of;
- diff two files' schemas, or a file's schema against an application's.

This is what makes "why won't my application open this file?" a diagnosable question rather than a guess.

## Level 3 — structural export

Requires: additionally the ability to walk the value graph from the root.
Requires no application knowledge, but is **inherently partial**.

A tool at this level can render the file as text — JSON, or something more structured — by starting at the root type and recursing.
Primitives decode by their code, structs by their fields, enums by their discriminant.

It stops at two boundaries:

**Opaque types.**
By definition their content is not decomposable.
A tool can report identity, version, and inline size, and can dump the bytes, but cannot interpret them.
This includes the built-in container types, which is a significant limitation — see below.

**Pointers.**
A tool can follow the ownership graph only if it can tell which bytes are pointers.
Today that knowledge lives in the language bindings, not in the schema, which is why the [Pointer descriptor kind](schema/type-descriptors.md#pointer-reserved) is reserved.

## The container problem

The most significant gap between this design intent and what is achievable today is that the **built-in containers are Opaque**.

A vector is currently described as an opaque type with a library name, a version, and an inline header size.
So a level-3 tool can see *that* a field holds a vector, but not its length, not its element layout, and not how to reach its contents — even though a vector is one of the most structurally regular things in the file.

Two possible resolutions, neither decided:

1. **Give containers structural descriptors.**
   A vector's representation really is describable — a header plus a dense array of fixed-size element slots — and the reserved [Array](schema/type-descriptors.md#array-reserved) and Pointer kinds exist partly for this.
   This would make containers fully walkable by any tool, at the cost of freezing more of their layout into the format.
2. **Register container layouts out of band**, so a tool can be taught about the standard containers without them ceasing to be Opaque in the file.
   Cheaper, but it means a tool needs a table of known types and is no longer purely self-describing.

**TBD.**
This decision determines how useful generic tooling can actually be, and it is worth making before the format is frozen: option 1 changes the descriptors that files contain, so it cannot be retrofitted silently.

## Cross-implementation testing

Tools are also the natural vehicle for testing the cross-language contract.
A conformance suite would consist of files written by each implementation, plus the expected level-2 and level-3 output for each, so that every implementation can be checked against every other's output without either needing to link the other.

See [Conformance](conformance.md).
