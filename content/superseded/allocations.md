---
title: Allocations and the address table
---

**Superseded.** Replaced by [the sequence](../spec/sequence.md) and [the span table](../spec/span-table.md).

The design between the relocatable heap and the present one: the file's content as allocations that pointed at each other, described on copy-on-write pages by an address table of statements keyed by an allocation's id and an offset in it.
Implemented in full by kladde-rs, whose branch `address-table` holds it as it stood, measured in most of the [evaluation](../evaluation/) pages, and specified in detail; kladde-docs' history holds every document of it, up to commit `bf13c73`.
What follows is its outline, and why it was replaced.

## What it was

**Allocations.**
An allocation was an untyped byte range with a stable 32-bit id, a size, and content; ids were never zero, which left zero for a null pointer.
A value lived in an *inline slot* of a fixed `INLINE_SIZE` inside some allocation, and a value with content of variable length — a string, a vector, a map — owned allocations of its own, reached through a pointer serialized as the target's id.
Every allocation had exactly one owning pointer, so the value graph was a tree of ownership; dropping or overwriting an owning value freed what it owned, recursively, in the same transaction and after the write that superseded the pointer.

**The address table** was a list of *statements* in table pages, each inheriting its page's epoch: `Ref(id, offset, size, address)`, `Zero(id, offset, size)`, and `Inline(id, offset, size, payload)` stated content, each with a *sizing* variant that also said the allocation ended where the content ended; `Size(id, n)` stated a size alone, and `Tombstone(id)` ended an allocation's existence.
Statements of different epochs were resolved per byte, the newest winning wherever it applied, and an allocation's size was its newest size statement's.
A writer had to keep the *coverage rule* — every byte that became part of an allocation was stated in that epoch — or truncated content could reappear.

**The journal** recorded effects on allocations: `Free`, `Resize`, `Write`, `Splice`, `Copy` and `Move`, each but `Free` bringing its allocation into existence.
Every mutation decomposed as *prepare new, unreachable state → one publishing write → clean up what it replaced*, an ordering discipline no type system enforced.

**In memory**, a loaded file was a *fragment map* from `(id, offset)` to the resolved content, a *statement slab* counting each live statement's *pins* — the fragments, sizes and tombstones that kept it alive — an *allocation map* of sizes, fragment counts and `mentions`, and the page table.
A flush folded the journal into a piece table per allocation, took the fragments it changed, and derived every statement from one dirty set at the cut; consolidation added a fourth mechanism, *description defragmentation*, which rewrote stippled ranges of an allocation as one statement, materializing the zeros between them.

**Values** came in *slotted* and *packed* places, small values kept short content inline, and the Rust binding had `Location`, `UniquePointer`, `store`, `load` and `free`, with one hole through which `load` could mint a second owner.

## Why it was replaced

**The description was paid per allocation and a splice per allocation's tail, so keeping either small made the other grow.**
A small description wanted values inline in few allocations, and then each allocation was long, and a splice into one restated or rewrote everything behind it, since statements were keyed by their offset in the allocation: on kladde-svg's small layout of the world map, flushes [were measured](../drafts/splices.md#what-a-splice-costs-today) to restate 24 bytes of statements per edit that had lain in leaves and to write 16 bytes of data again, growing with the length of the lists.
Cheap splices wanted short allocations, and then each value paid its pointer and statements, a cost that grew with the number of values rather than with their bytes.
No policy escaped that, because the format tied the unit of description to the unit of the data model.
The one-sequence design untied them: the description depends only on how a writer cuts one sequence into spans, and spans are keyed by id, so a splice restates the spans at its cuts and nothing behind them.
[The file as one sequence, measured](../evaluation/one-sequence.md) compares the two on the same drawings and texts.

## Compared with the present design

| | allocations and the address table | one sequence |
| --- | --- | --- |
| description written per splice | every statement behind it in its allocation, or its bytes written again | one to three entries |
| moving a value within a container | the moved bytes, or [`Move`](../drafts/move-op.md)'s statement edits | three to six spans, no bytes |
| moving a value between containers | its pointers written again, its allocations left where they are | a cut and a paste in one transaction: three to six spans, no bytes |
| description written per overwrite | one statement | three entries the first time, one after |
| description per value | a pointer, its statements, and its size | none; a few bytes per span of up to a page |
| what a page rewrite must also state | the sizes and tombstones of the ids it touches | nothing |
| liveness | pins over fragments, size pins, tombstones, `mentions` | the newest entry of a reached span |
| the store's memory | fragment map, statement slab, allocation map, page table | span table, rope, page table, reverse index |
| removing a value | a splice, then a free walk over what it owns | a splice |
| field access when mutating | constant offsets | constant up to the first variable-size field |
| opening | a sweep resolving bytes across epochs | one pass over entries, a chain walk, and the decode |
| upgrading to a new schema | the allocations holding values whose layout changed, written again | the values whose encoding changed, spliced in again |
| writing in the writer's schema | offsets from the writer's inline sizes | offsets and encodings interpreted from the writer's descriptors |
| parts of a file in different schema versions | not supported; a capsule would take an allocation | not supported; a capsule would be inline |
| tooling | opaque containers, a pointer graph | a decode from the schema |

What the present design pays for it: containers keep positions in memory, four bytes per variable-size element; offsets after a value whose size depends on its value are computed at run time; making a guard costs a lookup in its parent's index and a change of size an update per level; an overwrite inside a long span splits it; every insertion into a sequence takes two records, the element and the count; moving a value between containers holds a transaction open; and partial loading needs segments, which need a pointer of their own.

## The vocabulary that went with it

| term | why it went | instead |
| --- | --- | --- |
| allocation, allocation id, allocation size | there are none | the sequence, and a value's region |
| pointer, owning pointer, ownership, `UniquePointer`, `Location` | nothing points at anything | a region; a segment's reference, later |
| free, freeing, free walk, leak | nothing is allocated | removing, deleting: a splice |
| tombstone, size statement, sizing variant, `Size` | no size is stated | — |
| coverage rule | no byte becomes part of an allocation | the reach rule |
| fragment, fragment map, stipple, shadowing of part of a statement | no entry shows part of a span | span, run of spans; an entry replaces another whole |
| pin, `mentions`, statement slab, allocation map, recyclable set | liveness is one bit | live entry, reached span |
| `Resize`, `Free`, `Copy` | nothing to act on | — |
| slotted and packed places; inline size, `INLINE_SIZE` | a type has one encoding | fixed-size, variable-size, `Fixed(T)`, encoded size |
| small value, spilling | everything is inline | — |
| root allocation, schema table, consolidator state, as allocations | they are chains | the sequence, the schema chain, the private chain |
| address table, statement | entries describe spans whole | span table, entry |
| description defragmentation | there is no stipple to fill | merging spans |
