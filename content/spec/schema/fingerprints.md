---
title: Fingerprints
---

A fixed-size, reproducible hash identifying a type's representation.

**Status:** settled and implemented.
This document is normative: two conforming implementations must produce **bit-identical** fingerprints for the same type graph.

## The value

A fingerprint is a **128-bit (16-byte)** hash.
Two fingerprints are equal iff all 128 bits are equal.
It carries no flags and no reserved bits — every bit is hash output.

## Hash function

**SHA-256, truncated to its leading 128 bits** — the first 16 bytes of the digest, byte 0 first.

SHA-256 is chosen for exact cross-language reproducibility.
It is present in every mainstream language's standard library, has universal test vectors, and "compute SHA-256, keep the first 16 bytes" is trivial to reimplement identically.
128 bits gives a negligible accidental-collision probability for any realistic number of distinct schemas.

Cryptographic strength is deliberately **not** relied upon.
A fingerprint is an integrity and identity check against accident, not against an adversary.
An attacker who can write to a file can do far worse than collide a schema hash, so the format does not attempt to defend against one here.

A future format revision may substitute another hash or width; the choice is a format-version property.

## What is and is not fingerprinted

The fingerprint of a type is computed from **the type graph reachable from that type**.

**Included** — everything that affects the representation or the identities used to reconcile it:

- the kind of each descriptor;
- primitive codes;
- discriminant widths and discriminant values;
- field names and variant names;
- field order and canonical variant order.

**Excluded:**

- the `name` of Struct and Enum descriptors.
  A type's own name does not affect its byte layout, so renaming a type must not change its fingerprint.
- table indices, entirely.
  References are encoded structurally, so the fingerprint is invariant under any renumbering or reordering of the descriptor table.

**Reduced:** an Opaque's `version` contributes only its [compatibility component](type-descriptors.md#opaque) — the stability flag plus the leading nonzero component.
Patch releases, and minor releases above `0.x`, therefore do not churn the fingerprints of every type that transitively contains them.

## Cycles

The type graph may contain cycles, so a naive post-order fold does not terminate.

The traversal is a **white/grey/black depth-first search with memoization**.
A node currently on the stack (grey) that is encountered again is a back edge, and is encoded as a **relative reference** — "the *k*-th enclosing type currently being hashed" — rather than by recursing.
This makes the hash a pure function of graph structure, and it is the standard technique for hashing mutually recursive definitions.

Memoization on black nodes keeps the traversal linear in the number of distinct reachable descriptors.

The same cycle-awareness is needed for descriptor *equality* on recursive types, so this is shared machinery rather than fingerprint-specific work.

## Determinism

The fingerprint must be a pure function of the type graph.
Two implementations that agree on the graph must agree on the hash, with no dependence on:

- table layout, index assignment, or descriptor ordering;
- traversal order beyond what the canonical order fixes;
- hash-map iteration order, allocation addresses, or any other incidental runtime state.

An implementation that cannot demonstrate this on the conformance vectors is not conforming.

## As a fast path

The [file header](../file-format.md#header) stores the root type's fingerprint.

This makes the overwhelmingly common case — an application opening a file it wrote itself, with an unchanged schema — a single 16-byte comparison.
If it matches, the reader knows its own compile-time layout is exactly the writer's, and can read at static offsets without parsing the descriptor table at all.

Only when it *differs* does the reader parse the writer's descriptors and attempt [resolution](evolution.md).
So the fast path costs one comparison and the slow path costs one graph reconciliation per open — never per value.

This is the standard arrangement: the fingerprint is a registry key, the schema is the source of truth.
A fingerprint alone can detect a difference; it cannot bridge one, which is why it complements the descriptor table rather than replacing it.
