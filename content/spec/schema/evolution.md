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
It requires a preservation or overflow area for values the older reader cannot decode into its own types.

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
A kladde reader decodes a fresh in-memory value from the sequence when it opens a file, so it interprets a foreign schema **exactly once** and never pays for evolution on the hot path.

Concretely: reconcile the writer's type graph against the reader's once at open, producing a small per-type **read plan** — for each of the reader's fields, either "decode it from the writer's field *X*" or "use a default", and for each of the writer's fields the reader lacks, "skip it".
Encodings are variable-size, so the plan is a parse of the writer's encoding rather than a list of offsets; it runs during the decode the open does anyway.
Every value then follows the cached plan, with no string comparison and no graph walk.

**A reader must be able to decode by the writer's descriptors.**
Every encoding is decodable from the descriptors alone, so a reader can skip a value it does not know, and read one whose type changed, by the writer's graph.
It is a real constraint on the shape of a language binding's load path, and it needs designing in from the start even if resolution itself lands later.

**The whole file decodes by one schema at every commit.**
The header names one schema chain and one root fingerprint, so a session that opens a file with a different schema either **upgrades** everything whose encoding changed before its first commit, or **retains** the writer's schema, writing in it, all session.

**Upgrading writes the bytes of what changed, and nothing else.**
Say a file was written while `Shape` had two variants, `Circle` and `Path`, and the application opening it declares a third, `Rect`, and has given `Path` a new field.
At open, the reader decodes every shape by the plan into its own `Shape`, and compares each shape's bytes in the file with that shape's encoding by its own schema.
A circle's are equal, since adding a variant changes none of the old variants' bytes, so it keeps them; a path's differ, since its encoding now holds the new field, so the upgrade splices the new encoding in over the old one.
Values whose type is the same in both schemas, such as a title, are not encoded at all; nor are containers whose types differ only through `Shape`, so the comparison passes through them to their shapes.
Each splice states a constant number of spans besides its new bytes, and nothing behind a changed value is restated, although all of it moves, since no position is stored on file.
The upgrade is a flush at open that writes these splices along with the new schema chain, before the application gets the file, and needs no journal: a crash before it commits leaves the old file, and the next open upgrades again.

**Retaining interprets the writer's descriptors at every write.**
To write in the writer's schema, a writer needs the writer's encoding of each value it writes, and the positions of fields after a variable-size one, which come only from interpreting the writer's descriptors on the in-memory value at each write; only inside `Fixed` values do the descriptors give offsets directly.
Upgrading is therefore the natural default, and retaining a mode for files that an older application must still read.

**Cycle-aware canonicalization is shared machinery.**
Recursive types need it for fingerprinting and for descriptor equality alike; see [Fingerprints](fingerprints.md#cycles).

## What is TBD

**Upgrade or retain: the default, and how it is chosen.**
Is it a per-type attribute, a per-open flag, or decided by inspecting the diff?
Upgrading is simple but breaks readback by the older application; retaining preserves that but complicates the write path.
One should be the default and the other should be available.

**Parts of a file in different schema versions: capsules.**
Keeping parts of a file in different versions of their schema, and upgrading each part on its own, is what *capsules* were sketched for, and one sequence makes them cheap.
Three ideas made them up:

- a `Capsule<T>` is a schema boundary: a subtree that records which version of `T`'s schema it is written in, as an index into a table of the versions the file holds, each stored once and resolved once per session, however many capsules use it;
- fingerprints computed outside a capsule erase its inner type, so the root fingerprint does not change when `T` does, and a stale capsule is found by comparing the table's fingerprints with the reader's, once per version;
- each capsule upgrades on its own, at first touch, or keeps its version for as long as nothing writes it.

In one sequence a value's size concerns nothing but positions in memory, so a capsule is inline: its version's index as a varint, then `T`'s encoding in that version.
Upgrading one at first touch is a splice of its region, with the new index, in the same transaction as the write that touched it; the capsule's handle, or whatever a binding has in its place, is where that happens.
The schema chain would hold the table of versions in use: a session whose types are missing from it adds them in the flush at open, before any capsule names them, and a count of capsules per version, built at open, says when a flush may drop a version.
Nothing in the span format or the journal would change: capsules are a matter of the schema and of one wrapper type, and can come later.
They would let a large file whose element type changed upgrade a part at a time rather than every element at the first open.

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

Kladde's encoding is close to Avro's binary encoding too — no tags, no padding, zigzag varints, a union's branch as a varint, counts before sequences — and differs where mutation asks it to: Avro writes data once and reads it in sequence, while kladde edits values in place, so it keeps a sequence's count in one place rather than in blocks, ends text in a terminator so that an edit of text is one splice, and offers `Fixed` for values that change often.
