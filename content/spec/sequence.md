---
title: The sequence
---

What a kladde file's content is: one byte sequence holding the root value's encoding, beside the schema and the implementation's private state, each a chain of [spans](span-table.md).

## Three chains

**The header names three chains, and each holds a byte string:**

| chain | header field | holds | changed by |
| --- | --- | --- | --- |
| the **sequence** | `sequence_head` | the root value's [encoding](schema/type-descriptors.md#encodings), by the root type's descriptor | the journal, and flushes |
| the **schema** | `schema_head` | the [descriptor table](schema/canonical-encoding.md#table-encoding) of the root type, root first | flushes only |
| the **private chain** | `private_head` | whatever the writing implementation keeps from one session to the next ([rules](file-format.md#the-private-chain)) | flushes only |

The journal edits only the sequence, so no journal record needs to say which chain it edits, and every position in the journal is a position in the sequence, counted in bytes from the root value's first byte.
The schema changes only when a flush writes it: when a file is created, or upgraded to a new schema before the application gets it.
A file whose root fingerprint matches the reader's never needs to decode its schema chain ([the fast path](schema/fingerprints.md#as-a-fast-path)).

*Why chains of their own rather than a prefix.*
The schema could be a prefix of the sequence, which would work as well; but it would offset every position by the schema's length, and a reader would decode the schema to find where the root begins.
Chains cost nothing extra in the span format.

## One sequence, holding the root value's encoding

**The sequence is the encoding of the root value, with every value inline in its parent; there is no allocation and no pointer.**
A container states how many elements it holds, text ends in a byte that UTF-8 never contains, no value states its own encoded size, and every value decodes front to back from its type's descriptor.

For example, with these types,

```rust
struct Drawing { title: Text, shapes: Sequence<Shape> }
enum Shape { Circle { r: f32 }, Path { points: Sequence<(f32, f32)> } }
```

a drawing titled "Map" with a circle of radius 1 and a path from (0, 0) to (1, 1) is this sequence of 28 bytes:

```
4D 61 70 FF                  title: "Map", and the end of the text
02                           shapes: 2 elements
00 00 00 80 3F               Circle (variant 0), r = 1.0
01 02                        Path (variant 1), 2 points
00 00 00 00 00 00 00 00        (0.0, 0.0)
00 00 80 3F 00 00 80 3F        (1.0, 1.0)
```

Nothing in it is a pointer, a size of an allocation, or a byte of padding.
Renaming the drawing to "World map" is one journal record, `Splice(0, 3, "World map")`, and every byte behind it moves by 6 without being mentioned; on file it is [two span entries](span-table.md#a-worked-example), wherever in the sequence the splice lands.

**Why one sequence.**

- **The description stops depending on the data model.**
  When every value lives in an allocation of its own, every value costs a pointer in its parent, a description naming its allocation, and a size, so the description grows with the number of values, and short strings and short lists dominate that number.
  With one sequence, the description depends only on how a writer cuts the sequence into spans, which it chooses and the data model never sees.
- **The unit of description and the unit of the data model stop pulling against each other.**
  A cheap description wants few allocations, so values move inline into long ones; a cheap splice wants short allocations, since content keyed by its offset in an allocation makes a splice restate everything behind it.
  Separating the two removes the trade-off rather than choosing a point on it.
- **Everything that exists because of allocations goes with them**: ownership, freeing, publishing a pointer before freeing what it replaced, tombstones, id recycling, and leaks.
  Removing a value is one splice, whatever it held, and nothing can leak, since nothing can be unreachable.
- **The file is its own serialization.**
  A tool decodes the whole content from the schema, with no pointer graph to follow and no opaque container to stop at.

It gives up reaching a value on file without decoding what lies before it, which kladde never does, since it [reads in bulk](../impl/index.md#the-shape-of-an-implementation).
And positions change whenever something before them changes size, so an implementation tracks them in memory: each container knows where its children start, relative to itself ([the reference algorithms](../impl/values.md#positions-belong-to-containers)).
[Allocations and the address table](../superseded/allocations.md) records the design this replaced.

## What a valid sequence is

**At every transaction boundary, the sequence is the canonical encoding of exactly one value of the root type**: it decodes by the root descriptor to the end, with no byte left over, and every encoding in it is canonical — minimal varints, valid UTF-8, discriminants that name variants, integers in range of their type, and zeros wherever a `Fixed` value pads.
A reader refuses a file whose sequence, after the journal is replayed, is not.

Inside a transaction, anything goes: the states between its records are never observed.
This is [the journal's one ordering rule](journal.md#ordering), and it is all that is left of the ordering discipline allocations needed: nothing can dangle and nothing can leak, so nothing has to be published before something else is freed.

A canonical encoding makes the sequence a function of the root value, so two implementations that hold the same value hold the same bytes, and a test can compare a file's sequence with the canonical encoding of the value in memory, byte for byte.

## Free space

Free space is not represented on disk, and neither is liveness.
A page is live if it is reachable from a committed header; bytes within a data page are live if a span a chain reaches shows them.
Both are derived, and neither is recorded; see [page states](durability.md#page-states).
An implementation's in-memory accounting of how much of each page is still relied upon — what drives its consolidation — is its own business, and nothing about it is persisted.

## Reclamation is not part of this specification

Whether an implementation reclaims garbage, when it does so, and which pages it chooses are unconstrained.
A consolidation step writes live content into fresh pages and states the spans that show it again, producing a file indistinguishable from one that happened to be laid out that way from the start.
Two implementations with different policies therefore produce files that are equally valid and mutually readable.
An implementation that never consolidates is conforming; it will simply accumulate garbage until it runs out of space, which is a quality-of-implementation failure rather than a correctness one.

The one thing that *is* required is that reclamation be **externally invisible**: it must not change the content of any chain.
How the content is cut into spans, and which span ids it uses, are not content: a writer may split, merge, renumber and restate spans freely.

## Partial loading, later

**Loading part of a file means skipping a subtree without decoding it and without reading its spans, and one sequence offers neither.**
A [segment](../drafts/segments.md) would therefore be a chain of its own, named from the sequence by a reference: one coarse kind of pointer, reintroduced deliberately, for exactly the subtrees an application declares.
The format already has three chains, so a fourth kind changes the header's fields and a descriptor kind, not the span machinery; whether references to segments may move, and how their transactions relate to the sequence's, is open.
