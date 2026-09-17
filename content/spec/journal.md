---
title: Journal
---

What a mutation records, how records are grouped into transactions, how they are framed, and how a torn tail is recovered.

**Status: draft.** The record set is settled; the byte encoding and the commit-boundary semantics are TBD.

## What the journal is

An append-only sequence of **transactions**, each a sequence of **records** describing byte-level effects on allocations.
Every change to the stored value graph appends to it before the mutating call returns, so an application crash loses nothing that has been reported as done.

The journal is deliberately **type-agnostic**.
It records effects on allocations, not application-level operations.
There is no "push element", no "insert key" — only allocate, free, resize, write, and two content-moving records.

*Why.* Replay never dispatches on an application type, never needs a type registry, and never calls back into a container implementation.
That is exactly what makes a file readable by an implementation that has never heard of the container that wrote it.
What it gives up is the ability to collapse operations that cancel only at the semantic level: a rope that applies a hundred edits which cancel out still records a hundred edits' worth of byte changes, because nothing in the log knows they were edits.

## Record kinds

| record | effect |
| --- | --- |
| `Alloc(id, size)` | brings `id` into existence with the given size |
| `Free(id)` | releases `id` |
| `Resize(id, new_size)` | changes `id`'s size, preserving `min(old, new)` bytes |
| `Write(id, offset, bytes)` | overwrites `bytes.len()` bytes at `offset` within `id` |
| `Splice(id, offset, old_len, bytes)` | replaces `old_len` bytes at `offset` with `bytes`, shifting the tail and resizing |
| `Copy(src, src_offset, len, dst, dst_offset)` | copies a byte range between, or within, allocations |

This set is **closed under replayability**: every record whose absence would make another record unreplayable or ambiguous is present.

That criterion is what put `Alloc` in the list, and it is worth stating the failure it prevents, because it is not an annihilation problem and is easy to miss:

> A value that allocates a child and writes the child's id into its own header emits a `Write` the log keeps, and an `Alloc` the log does not.
> Recovery replays the log, finds a live pointer in the parent, and that pointer names an allocation nothing ever created.

Two records, no free in sight, and the result is a dangling pointer in recovered application data.

`Splice` and `Copy` are the only records that read existing content.
That distinction matters to an implementation's flush, and is not visible in the file.

A future `Move` record would join the content group; see [the draft](../drafts/move-op.md).

## Transactions

The journal records mutations as a sequence of **transactions**, each a sequence of records.

> **Transactions are the unit of atomicity.**
> If the application crashes while recording a transaction, or while the journal is being appended to, the next open replays that transaction either completely or not at all.

This is the one durability guarantee application authors build on directly, which is why transactions are specified here while the mechanisms an implementation uses to *group* operations into them — batching, nesting, buffering — are not.
Those are [implementation concerns](../impl/transactions-and-batches.md).

A record that occurs outside any transaction is recorded as a transaction of its own, so a lone operation can never be torn.

## Framing

Every transaction is framed with a length prefix and a **chained, epoch-salted CRC**.

- *Chained*, so that recovery keeps the longest valid **prefix** rather than the longest valid set: a hole cannot be skipped, even if the bytes past it are intact.
  This matters because operating-system write-back is not ordered, so a later journal page can reach disk while an earlier one does not.
- *Epoch-salted*, so that stale bytes left in a reused page can never validate as journal content.
  This is what lets a flush hand journal segment `E + 1` a reusable page without writing anything to it first.

Exact framing — prefix width, checksum algorithm, whether records are batched into blocks — is **TBD**.

## Ordering

The journal's most important invariant is not about any single record.

> **Any prefix of the journal, replayed alone, must leave a valid state.**

"Valid" permits stale, and permits slightly leaky.
It does not permit dangling: nothing may become reachable before everything it depends on is already earlier in the sequence, and nothing may be freed before whatever superseded it is already published.

In practice this means every mutation decomposes as **prepare** new, unreachable state → one **publishing** write → **clean up** what it replaced.
Growing a container, removing from the middle of one, and replacing a map entry all fit this shape.
A vector push that must grow, for instance, resizes and writes the new element *before* bumping the stored length, because the length is what makes the element reachable.

This is a discipline every container implementation must follow, and **no type system enforces it**.
It is the single largest source of latent correctness risk in the design, which is why [the conformance suite](conformance.md#the-suite) makes it a mechanical check: truncate at every byte offset, replay, assert validity.

An implementation that cannot express some mutation this way needs an explicit atomic-group mechanism — bracketing records that a recovering reader truncates as a unit if the group is unterminated.
Whether the format includes such a mechanism is **TBD**; it has not been needed for any mutation shape checked so far.

## Recovery

1. Read and validate the [header](file-format.md#header-pages), taking the CRC-valid slot with the highest epoch.
2. Bulk-load its world, resolving the address table into the in-memory index.
3. Walk the journal segment the header names, validating each transaction's chained CRC.
4. Stop at the first transaction that fails validation or is short — that is the torn tail.
5. Replay the surviving transactions.

Replay is purely mechanical and involves no application code.

## Folding

Periodically the journal is **folded**: its records are applied, the on-file state is brought up to date, and a fresh segment is started.
The protocol that commits this is in [Durability](durability.md#the-flush-protocol).

An implementation has wide latitude in *what* it writes.
It may apply records naively in order, or it may optimize — cancelling an allocate/free pair that never escaped, dropping a write that a later write fully supersedes, eliding a shrink-then-grow.
It may reorder freely, so long as the result is indistinguishable from an in-order replay.
Because [unwritten content is unspecified](allocations.md#content-semantics), "indistinguishable" is a weaker and more permissive condition than it first appears.

### Checkpoint versus commit

**TBD, and consequential.**

A fold triggered by resource pressure — the journal has grown too large — is a *checkpoint*: it moves work out of memory and into the file.
A fold that marks a point the recovered state may snap to is a *commit*.

Today these are the same event, which is what gives atomicity for free.
Separating them is necessary for an implementation that wants to bound journal memory inside a long transaction, and it requires deciding how uncommitted data is kept out of the recovered state.

The format-level consequence is whether the journal needs an explicit commit record.
Until that is decided, this specification assumes fold-equals-commit.
The trade-offs are worked through in [the implementation notes](../impl/transactions-and-batches.md#checkpoint-versus-commit).
