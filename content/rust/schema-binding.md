---
title: Schema binding
---

The Rust binding of the [language-independent schema model](../spec/schema/), and the check it performs at open.

## Two layers

The split is deliberate and worth preserving in a port.

**`kladde-schema` — the model.**
The descriptor types, the canonical encoding, the fingerprint computation, and a SHA-256 implementation.
It has **no** connection to the store, to `Durable`, or to any Rust type system machinery, which is what makes it portable and testable on its own: the conformance vectors for [encoding](../spec/schema/canonical-encoding.md) and [fingerprints](../spec/schema/fingerprints.md) exercise this layer and nothing else.

**The binding — where Rust types meet the model.**
A `Durable` type declares its own descriptor in `describe_local`, and the derive macro generates that declaration.

## The binding

A type contributes its descriptor by describing itself into a `SchemaBuilder`, which interns descriptors into a table and hands back references:

- **Recursion terminates.**
  A type that refers to itself reserves its table slot, keyed by its `TypeId`, before describing its fields.
- **Descriptors are deduplicated** by type, so a type used in twenty places occupies one table entry.
- **The table is built once**, not per value.

## What a type declares

A struct declares a `Struct` descriptor with its fields in declaration order; an enum declares an `Enum` with its variants canonically ordered by discriminant.

A hand-written implementation declares **whatever descriptor matches the bytes it actually reads and writes** — which is the rule that most often trips people up.
A hand-written implementation of a plain field-sum declares `Struct`, not `Opaque`; "hand-written" and "opaque" are different axes, see [the guiding principle](../spec/schema/index.md#the-guiding-principle).

The containers declare structural descriptors, so that a tool can decode them: a `Vec<T>` and a `DurableVec<T>` alike a `Sequence` of the element type, a `String` `Text`, and a map a `Map` of its key type and its value type.
An enum's variant holds one payload: `()`, the empty struct, for a unit variant, the field's type for a variant of one unnamed field, and otherwise an anonymous struct of its fields named after the variant ([the derive](derive-macro.md#enums)).
`Option<T>` declares an enum `Option` with a one-byte discriminant width, `None` = 0 holding `()` and `Some` = 1 holding a `T`; a tuple a struct `TupleN` with positional fields; `Fixed<T>` a `Fixed` of `T`'s descriptor; and `Box<T>` is *schema-transparent*, reusing `T`'s descriptor, as `#[kladde(transparent)]` types do.
Only `DurableBlob` declares `Opaque`, without a fixed size, since its serde payload is not decomposable.

## At create and at open

`Kladde::create` encodes the root type's descriptor table into the schema chain, which the header's `schema_head` names, and writes the root's fingerprint into the header.
`Kladde::open` compares the header's fingerprint with the application's and **fails closed** on a mismatch, with an error carrying both fingerprints.
It never parses the descriptor table on the matching path, which is the [fast path](../spec/schema/fingerprints.md#as-a-fast-path) the fingerprint exists for.

Resolution — decoding by the writer's descriptors — does not exist yet, so a mismatch cannot be bridged; see [Evolution](../spec/schema/evolution.md).

## Design invariants

Worth keeping in mind when changing anything here:

- **The fingerprint must not depend on table layout.**
  References are encoded structurally, so any renumbering or reordering produces the same hash; a test permutes the table and asserts equality.
- **A type's own name is not fingerprinted**; its fields' and variants' names are.
- **Determinism over everything.**
  No hash-map iteration order, no addresses, nothing incidental may reach the hash.
- **Encoding and fingerprinting are separate functions** over the same model.
  They share the canonical order but not the code, because the fingerprint deliberately omits things the encoding includes.
