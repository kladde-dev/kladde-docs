---
title: Specification
---

The normative, language-independent description of kladde.
This section defines everything that *all* implementations must agree on, and — just as importantly — marks the boundaries beyond which they are free to disagree.

## What this section is for

Two audiences:

- **Implementers** porting kladde to a new language.
  This section is the contract you must satisfy; the [kladde-rust design documents](../rust/design/) are a worked example of one way to satisfy it, not part of the contract.
- **Tool authors** writing something that reads kladde files without knowing the application that wrote them.
  This section tells you what you may assume.

## The layers

A kladde file is described by four layers, from the bytes upward.

| layer | defines | document |
| --- | --- | --- |
| **Container** | the file header, versioning, and how the snapshot and journal regions are found | [File format](file-format.md) |
| **Allocation** | the heap: allocations, their stable ids, how a pointer is stored, how free space is represented | [Allocation model](allocations.md) |
| **Journal** | the record format for durable mutation, framing, and crash recovery | [Journal](journal.md) |
| **Schema** | how a value type's byte layout is described, encoded, fingerprinted, and evolved | [Schema](schema/) |

Above these sits the application's own data, whose meaning kladde does not define.

## The compatibility contract

> A file written by any conforming implementation can be opened by any other conforming implementation, with no conversion step, as long as the opening application implements equivalent data structures.

"Equivalent data structures" is doing real work in that sentence.
Kladde guarantees that the *representation* round-trips: an implementation that reads a file will find the same allocations, the same pointer graph, and the same declared types it would have found had it written the file itself.
It does not guarantee that an application which has never heard of a type can do anything useful with values of that type.
See [Schema](schema/) for exactly where that line falls, and [Tooling](tooling.md) for what a type-ignorant reader can still do.

## What is fixed and what is free

Fixed by this specification, and therefore identical in every implementation:

- the file header and its version fields;
- the on-disk representation of an allocation and of a pointer;
- the journal record encoding and the rules for recovering a torn tail;
- the type-descriptor model, its canonical byte encoding, and the fingerprint computation.

Explicitly **not** fixed, and expected to vary:

- **placement policy** — where a new allocation goes;
- **compaction strategy**, or whether an implementation compacts at all;
- **when** a journal is folded into the snapshot, and how aggressively the fold optimizes;
- everything above the storage layer: the API shape, the mutation mechanism, the container implementations.

The test for whether something belongs in the "fixed" column is simple: *could two implementations disagree about it and still read each other's files?*
If yes, it stays free.

See [Conformance](conformance.md) for how this is meant to be verified.

## Versioning

The specification carries a version.
A file records both the version it was written with and the minimum version required to read it, so that an implementation can distinguish "written by something newer, but still readable" from "written by something newer that used a feature I do not have."

The details are in [File format](file-format.md#versioning).

## Status

**Draft.** No part of this specification is frozen.
The schema-description layer ([type descriptors](schema/type-descriptors.md), [canonical encoding](schema/canonical-encoding.md), [fingerprints](schema/fingerprints.md)) is the most settled — it is specified precisely enough to implement and has a reference implementation.
The container and journal layers are sketched, and the allocation layer is settled in model but not in byte encoding.
