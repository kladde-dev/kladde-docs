---
title: Specification
---

The normative, language-independent description of kladde: what a file is, what invariants it must uphold, and what guarantees an implementation must provide.

Two audiences: **implementers** porting kladde to a new language, and **tool authors** writing something that reads kladde files without knowing the application that wrote them.
The [reference algorithms](../impl/) are a worked example of one way to satisfy this contract, not part of it.

Documents here state *why* as well as *what*.
A specification that records only the rule invites reimplementation of the mistake it was written to avoid, so each decision below carries a short motivation — briefly, and clearly marked as motivation rather than requirement.

## The layers

A kladde file is described by six layers, from the bytes upward.

| layer | defines | document |
| --- | --- | --- |
| **Pages** | the page grid, page framing, the two header slots, and epochs | [File format](file-format.md) |
| **Durability** | which pages a write may touch, the flush protocol, and what survives a crash | [Durability](durability.md) |
| **Span table** | the entries that describe the content as chains of spans, and how they are resolved | [Span table](span-table.md) |
| **The sequence** | what the chains hold: the root value's encoding, the schema, and the private state | [The sequence](sequence.md) |
| **Journal** | the record set for durable mutation of the sequence, transactions, framing, and recovery | [Journal](journal.md) |
| **Schema** | how a value type is encoded, how its descriptor is serialized and fingerprinted, and how it evolves | [Schema](schema/) |

Above these sits the application's own data, whose meaning kladde does not define.

In one paragraph: a kladde file is a grid of pages, committed copy-on-write through two alternating header pages.
Its content is one byte sequence, the encoding of the application's root value, with every value inline in its parent, and no pointers.
On file, the sequence is a chain of spans, each described by an entry in a table page that shows a run of its bytes and names the span that follows.
Every mutation appends a record on positions in the sequence to a journal before it returns, and a flush periodically folds the journal into new table and data pages and commits them with one `fsync` and one header write.

## The compatibility contract

> A file written by any conforming implementation can be opened by any other conforming implementation, with no conversion step, as long as the opening application implements equivalent data structures.

"Equivalent data structures" is doing real work in that sentence.
Kladde guarantees that the *representation* round-trips: an implementation that reads a file finds the same sequence and the same declared types it would have found had it written the file itself.
It does not guarantee that an application which has never heard of a type can do anything useful with values of that type.
See [Schema](schema/) for exactly where that line falls, and [Tooling](tooling.md) for what a type-ignorant reader can still do.

## What is fixed and what is free

Fixed here, and therefore identical in every implementation:

- the page grid, page framing, and the two-slot header commit;
- the reuse rule and the flush protocol that make durability a guarantee rather than a hope;
- the span-table entry forms and the rules that resolve them;
- the journal record encoding and the rules for recovering a torn tail;
- the encoding of every value, the type-descriptor model, its canonical byte encoding, and the fingerprint computation.

Explicitly **not** fixed, and expected to vary:

- **placement** — which reusable page a flush writes to, how it cuts content across pages, and how it cuts it into spans;
- **consolidation** — whether an implementation reclaims garbage at all, and by what policy;
- **when** a flush happens, and how aggressively it optimizes what it writes;
- **batching** — how an implementation joins subsequent application-level operations or transactions into larger library-level transactions when the application temporarily allows the library to do so (useful during large batch operations).
- everything above the storage layer: the API shape, the mutation mechanism, the container implementations.
  These decisions should follow conventions of the host language to make a kladde implementation as idiomatic as possible, and this specification is language agnostic.

The test for whether something belongs in the fixed column is simple: *could two implementations disagree about it and still read each other's files?*
If yes, it stays free.

## Guarantees an implementation must provide

Beyond byte-level agreement, a conforming implementation owes the application five things.

**Durability.**
When a mutating call returns, the mutation survives an application crash.
When `flush()` returns, every transaction it folded survives a power cut.
Recovery always reaches a transaction boundary — never a partial transaction, and never a partially applied flush.
The details, and the one thing that is *not* promised (transactions issued after the last completed flush survive only as far as the operating system happened to write them out), are in [Durability](durability.md).

**Bounded sizes.**
An implementation must support the [bounds](span-table.md#bounds) the format states — span ids, the sequence's length, page numbers, file size — and must fail cleanly rather than silently wrap when an application exceeds them.

**Asymptotic complexity.**
The format is designed so that these are achievable, and an implementation that misses them is conforming but not useful:

| operation | required |
| --- | --- |
| a splice, overwrite or move, on file | a constant number of span entries, wherever it lands and however long the sequence |
| a mutation, in the store | `O(log k)` for `k` spans, besides the bytes it writes |
| a flush | `O(r · log k)` for `r` records folded and the spans stated, plus the pages it writes |
| opening a file | `O(entries + n)` for the entries of the live table pages and a sequence of `n` bytes |
| a non-mutating operation once the file is loaded | no overhead over a comparable non-persisted data type |

The live pages can be held at a constant multiple of the sequence's length, at a constant cost in bytes written per modification: [the reference algorithms](../impl/index.md#overhead-and-work-independent-of-size) show how.
Nothing may require a scan of the file at run time, and nothing may require a stop-the-world pause: reclamation is incremental by construction, and a flush's work is bounded by a budget the implementation chooses.

**Replacing a whole value records only what changed.**
When an application replaces a whole Text, or a whole Opaque value without a fixed size, with a new one, the records appended rewrite at most the smallest range of the old content that holds every changed byte, and, for an Opaque whose length changes, its length prefix.
The implementation may rewrite the prefix and that range as one range, together with what lies between them, where it judges that cheaper — when the change lies near the start, say.
Whether it goes further and finds several changed ranges far apart is its own choice.

*Why.*
An application often sets a whole string where only part of it changed, because that is the easy thing to write: a drawing program that writes a path's whole `d` attribute for every step of a dragged control point, or a form that stores its whole text at every keystroke.
Comparing the old content with the new costs a pass over memory the application has just written, and turns a rewrite of the whole value into a splice of a few bytes; guaranteeing it lets applications write the easy code without paying for it in the journal.

**Crash atomicity of transactions.**
A transaction is all-or-nothing under both an application crash and a power cut, and ordering is preserved: if a transaction survives a crash or power cut, then all previous transactions survive too, in their recorded order.
These are the guarantees application authors build on directly, and they are why transactions — unlike batches — are part of this specification.

## Versioning

The specification carries a version.
A file records both the version it was written with and the minimum version required to read it, so that an implementation can distinguish "written by something newer, but still readable" from "written by something newer that used a feature I do not have."

The details are in [File format](file-format.md#versioning).

## Status

**Draft.** No part of this specification is frozen.

The schema layer is the most settled: it is specified precisely enough to implement, and has two reference implementations, in Rust and C++.
The page, durability, span-table and sequence layers are settled in design and worked out in detail, and kladde-rs and kladde-cpp implement them byte for byte as stated, but nothing is final.
The journal is at the same stage, byte encoding and page layout included.
