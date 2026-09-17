---
title: Specification
---

The normative, language-independent description of kladde: what a file is, what invariants it must uphold, and what guarantees an implementation must provide.

Two audiences: **implementers** porting kladde to a new language, and **tool authors** writing something that reads kladde files without knowing the application that wrote them.
The [reference algorithms](../impl/) are a worked example of one way to satisfy this contract, not part of it.

Documents here state *why* as well as *what*.
A specification that records only the rule invites reimplementation of the mistake it was written to avoid, so each decision below carries a short motivation — briefly, and clearly marked as motivation rather than requirement.

## The layers

A kladde file is described by five layers, from the bytes upward.

| layer | defines | document |
| --- | --- | --- |
| **Pages** | the page grid, page framing, the two header slots, and epochs | [File format](file-format.md) |
| **Durability** | which pages a write may touch, the flush protocol, and what survives a crash | [Durability](durability.md) |
| **Address table** | the statements that map allocation ids to sizes and content, and how they are resolved | [Address table](address-table.md) |
| **Allocations** | what an allocation is, how it is named, how one refers to another, and what its bytes mean | [Allocations](allocations.md) |
| **Journal** | the record set for durable mutation, transactions, framing, and recovery | [Journal](journal.md) |
| **Schema** | how a value type's byte layout is described, encoded, fingerprinted, and evolved | [Schema](schema/) |

Above these sits the application's own data, whose meaning kladde does not define.

## The compatibility contract

> A file written by any conforming implementation can be opened by any other conforming implementation, with no conversion step, as long as the opening application implements equivalent data structures.

"Equivalent data structures" is doing real work in that sentence.
Kladde guarantees that the *representation* round-trips: an implementation that reads a file finds the same allocations, the same pointer graph, and the same declared types it would have found had it written the file itself.
It does not guarantee that an application which has never heard of a type can do anything useful with values of that type.
See [Schema](schema/) for exactly where that line falls, and [Tooling](tooling.md) for what a type-ignorant reader can still do.

## What is fixed and what is free

Fixed here, and therefore identical in every implementation:

- the page grid, page framing, and the two-slot header commit;
- the reuse rule and the flush protocol that make durability a guarantee rather than a hope;
- the address-table statement types and the rules that resolve them;
- the journal record encoding and the rules for recovering a torn tail;
- the type-descriptor model, its canonical byte encoding, and the fingerprint computation.

Explicitly **not** fixed, and expected to vary:

- **placement** — which reusable page a flush writes to, and how it cuts content across pages;
- **consolidation** — whether an implementation reclaims garbage at all, and by what policy;
- **when** a flush happens, and how aggressively it optimizes what it writes;
- **batching** — how an implementation groups application operations into transactions;
- everything above the storage layer: the API shape, the mutation mechanism, the container implementations.

The test for whether something belongs in the fixed column is simple: *could two implementations disagree about it and still read each other's files?*
If yes, it stays free.

## Guarantees an implementation must provide

Beyond byte-level agreement, a conforming implementation owes the application four things.

**Durability.**
When a mutating call returns, the mutation survives an application crash.
When `flush()` returns, every transaction it folded survives a power cut.
Recovery always reaches a transaction boundary — never a partial transaction, and never a partially applied flush.
The details, and the one thing that is *not* promised (transactions issued after the last completed flush survive only as far as the operating system happened to write them out), are in [Durability](durability.md).

**Bounded sizes.**
An implementation must support the [bounds](address-table.md#bounds) the format states — allocation ids, allocation sizes, page numbers, file size — and must fail cleanly rather than silently wrap when an application exceeds them.

**Asymptotic complexity.**
The format is designed so that these are achievable, and an implementation that misses them is conforming but not useful:

| operation | required |
| --- | --- |
| read any byte of any allocation | `O(log F)` in the number of live fragments |
| allocation size query | `O(1)` |
| a mutation, in memory | `O(log F)` per contiguous range touched |
| a flush | `O((k + s) · log F)` for `k` dirty ranges and `s` statements rewritten, plus the pages it writes |
| opening a file | `O(n)` in the file's live bytes |

Nothing may require a scan of the file at run time, and nothing may require a stop-the-world pause: reclamation is incremental by construction, and a flush's work is bounded by a budget the implementation chooses.

**Crash atomicity of transactions.**
A transaction is all-or-nothing under both an application crash and a power cut.
This is the one guarantee application authors build on directly, and it is why transactions — unlike batches — are part of this specification.

## Versioning

The specification carries a version.
A file records both the version it was written with and the minimum version required to read it, so that an implementation can distinguish "written by something newer, but still readable" from "written by something newer that used a feature I do not have."

The details are in [File format](file-format.md#versioning).

## Status

**Draft.** No part of this specification is frozen.

The schema layer is the most settled: it is specified precisely enough to implement, and has a reference implementation.
The page, durability, and address-table layers are settled in design and worked out in detail, but their byte-level field lists are not final.
The journal's record set is settled; its framing is not.
