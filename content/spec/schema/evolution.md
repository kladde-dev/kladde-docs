---
title: Evolution
---

How a reader reconciles its own types against the types a writer used.

**Status: the least settled part of the specification.**
The direction is decided; most of the policy is TBD.
This document records what has been settled and marks the rest explicitly.

## The levels

It helps to separate four things that "compatibility" usually runs together.

| level | meaning |
| --- | --- |
| **(a) Detection** | a reader can tell that the file's schema differs from its own, and refuses rather than misreading |
| **(b) Backward-compatible migration** | a newer reader can read an older writer's file |
| **(c) Tolerant structural read** | a reader can read a file whose schema differs structurally — fields added, removed, reordered — without bespoke per-version code |
| **(d) Forward compatibility with preservation** | an *older* reader can read a newer writer's file and write it back **without losing** the fields it does not know about |

Kladde targets **(a) through (c)** for the single-authoritative-application case, which is its bread and butter.

Level (d) is partially available for free — an older reader keeps working after a newer one writes, as long as the newer one added no persisted fields — but the harder flavour, where a *new* field survives a round trip through an old reader, is a **non-goal** for the first frozen format.
It requires a preservation or overflow area and fights the fixed-offset model directly.

## What is settled

**Detection is mandatory.**
The [header](../file-format.md#header-pages) carries a magic number, a format version, and the root [fingerprint](fingerprints.md), and a reader that finds a mismatch it cannot resolve **must fail closed**.
Silent misinterpretation is the one outcome the format does not permit.
This is a correctness property, not a feature.

**Identity is by name, not by position.**
Fields and variants are identified by their names, and enum variants by their explicit discriminant values.
This is what makes reordering non-breaking and removes the declaration-order discriminant hazard, where inserting a variant silently renumbers everything after it.

**Resolution happens at load, once per type.**
This is the observation that makes the whole thing affordable.
A kladde reader reconstructs a fresh in-memory value when it opens a file and writes native layout on the next fold, so it interprets a foreign schema **exactly once** and never pays for evolution on the hot path.

Concretely: reconcile the writer's type graph against the reader's once at open, producing a small per-type **read plan** — for each of the reader's fields, either "read from writer offset *X*" or "use a default."
Every value then follows the cached plan, with no string comparison and no graph walk.

**A reader must be able to read at writer offsets.**
A loader cannot assume its own compile-time slot size.
This is the mechanism that lets slot sizes evolve at all: the writer's graph encodes the old sizes, the reader's encodes the new ones, and resolution bridges them.
It is a real constraint on the shape of a language binding's load path, and it needs designing in from the start even if resolution itself lands later.

**For packed values, the read plan is a small program that decodes the writer's encoding.**
The writer's offsets inside a [packed](type-descriptors.md#places) value depend on each value, but the writer's descriptors, and the choices of encoding they record, still determine them, so resolution produces a per-type parse rather than a list of offsets, run during the sequential load that decodes every value anyway.
A changed choice of encoding is a changed layout like any other: a reader whose type declares a field slotted that the writer packed reads the writer's varint, and stores a fixed-width integer on its next write.

**In-place mutation must run against the allocation's active layout.**
This is the corollary, and it is the subtle one.
If a reader loads a foreign layout but keeps pointing at the original allocation, then mutating it with native offsets corrupts it.
An implementation must make "which layout is this allocation in" explicit:

- an **upgrade**-mode allocation is re-laid-out natively, at load or at first touch, after which static offsets are valid;
- a **retain**-mode allocation carries a runtime layout that the mutation path must use.

Static offsets are valid **only** for native allocations.
Retaining a packed value is harder still, since its offsets would have to be computed from the writer's descriptors at every mutation, so upgrade is the natural mode for packed values.

**Cycle-aware canonicalization is shared machinery.**
Recursive types need it for fingerprinting and for descriptor equality alike; see [Fingerprints](fingerprints.md#cycles).

## What is TBD

**Layout policy: default and granularity.**
Is *upgrade* or *retain* the default?
Is it a per-type attribute, a per-open flag, or decided per allocation by inspecting the diff?
And is the schema tagged per allocation, per schema-boundary region, or globally for the file?
Global is simplest and all-or-nothing; finer granularity allows lazy migration but needs a per-region schema reference.

**Default behaviour on the first fold after a resolving open.**
Upgrade-and-rewrite is simple but breaks readback by the older application.
Pinning the writer's format preserves that but complicates the write path.
One should be the default and the other should be available.

**How much semantic-migration machinery to expose.**
Structural resolution handles add, remove, reorder, and rename-with-annotation.
It cannot handle a change that is semantic rather than structural — splitting one field into two, changing units.
Whether kladde offers registered from-fingerprint/to-fingerprint hooks, or leaves such changes entirely to the application, is undecided.

**Rename robustness.**
Name-based identity is ergonomic but not rename-robust.
An optional explicit ordinal annotation, used when present and falling back to the name otherwise, would cover both; whether to specify one is undecided.

**Defaults for added fields.**
A reader whose type gained a field needs a value for it when reading an older file.
Whether the default is declared in the schema, supplied by the language binding, or required to be the type's zero value is undecided.

**Relationship to application-level versioning.**
A file-level semantic version for the *application* — as opposed to the format or the schema — has a natural home in the same header slot.
These should be one mechanism, not two parallel ones.

## What was considered and declined

**A fingerprint alone, with no descriptor table.**
Compact and a fine integrity check, but a hash can only report that something differs.
It cannot say *what* differs, so it cannot implement levels (b) or (c).
It also conflates "I changed" with "something I contain changed," and is unusable for generic types, where `Container<A_v1>` and `Container<A_v2>` hash differently with nothing shared.
The fingerprint is kept, but as a fast path on top of the descriptors rather than as a replacement.

**Per-value vtables**, in the FlatBuffers style, giving true in-place forward compatibility.
Declined: it taxes the steady state of every value to solve a problem kladde only has at load.
Revisit only if level (d) with preservation becomes a hard requirement for files concurrently shared between application versions.

## Prior art

The model borrowed here is **Avro's**: the writer's schema travels with the data, the reader has its own, and resolution reconciles them at read time.
The fingerprint plays the role of Avro's schema-registry key.

What kladde does *not* borrow is Avro's wire format, which is designed for streaming records rather than for a randomly addressable, mutable heap.
