---
title: Schema binding
---

The Rust binding of the [language-independent schema model](../spec/schema/).

**Status:** implemented.

## Two layers

The split is deliberate and worth preserving in a port.

**`kladde-schema` — the model.**
The descriptor types, the canonical encoding, the fingerprint computation, and a SHA-256 implementation.
Depends on nothing but the varint crate.

It has **no** connection to the heap, to `Persistable`, or to any Rust type system machinery.
That is what makes it portable and testable on its own: the conformance vectors for [encoding](../spec/schema/canonical-encoding.md) and [fingerprints](../spec/schema/fingerprints.md) exercise this layer and nothing else.

**The binding — where Rust types meet the model.**
A `Persistable` type declares its own descriptor, and the derive macro generates that declaration.

## The binding

A type contributes its descriptor by describing itself into a builder, which interns descriptors into a table and hands back references.

The important properties:

- **Recursion terminates.**
  A type that refers to itself must reserve its table slot before describing its fields, or the builder recurses forever.
- **Descriptors are deduplicated** by structure, so a type used in twenty places occupies one table entry.
- **The table is built once**, not per value.

From a type's descriptor and the reachable graph, the fingerprint is computed by the [white/grey/black traversal](../spec/schema/fingerprints.md#cycles).

## What a derived type declares

A struct declares a `Struct` descriptor with its fields in declaration order.
An enum declares an `Enum` with its variants canonically ordered by discriminant.

A hand-written implementation declares **whatever descriptor matches the bytes it actually reads and writes** — which is the rule that most often trips people up.
A hand-written implementation of a plain field-sum declares `Struct`, not `Opaque`.
"Hand-written" and "opaque" are different axes; see [the guiding principle](../spec/schema/index.md#the-guiding-principle).

The containers declare `Opaque`, carrying their library name, type name, version, inline size, and element type as a parameter.
Whether they *should* — as opposed to declaring structural descriptors so that tools can walk them — is [an open format question](../spec/tooling.md#the-container-problem).

## Where it is used, and where it is not

**Used today:** a type's fingerprint can be computed and compared, and a schema table can be serialized.
There is an example that dumps a type's schema.

**Not used yet:** nothing writes the table into a file, because there is no file.
Nothing checks a fingerprint on open, because nothing opens.
No resolution exists.

So the layer is implemented and specified, and entirely unwired.
Connecting it is part of the real-file work, and it should happen at the same time — a file format that ships without the fail-closed fingerprint check is a file format that corrupts data silently.

## Design invariants

Worth keeping in mind when changing anything here:

- **The fingerprint must not depend on table layout.**
  References are encoded structurally, so any renumbering or reordering produces the same hash.
  There is a test that permutes the table and asserts equality.
- **A type's own name is not fingerprinted**; its fields' and variants' names are.
- **Determinism over everything.**
  No hash-map iteration order, no addresses, nothing incidental may reach the hash.
- **Encoding and fingerprinting are separate functions** over the same model.
  They share the canonical order but not the code, because the fingerprint deliberately omits things the encoding includes.

## Missing

- The **Array** and **Pointer** kinds are reserved and unimplemented.
- **Resolution** — reading at a writer's offsets — does not exist, and it constrains the shape of `load`.
  See [Evolution](../spec/schema/evolution.md).
- The descriptor table is never written to or read from a file.
