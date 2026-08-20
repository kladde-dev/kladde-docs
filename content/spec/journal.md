---
title: Journal
---

The durability layer: what a mutation records, how records are framed, and how a torn tail is recovered.

**Status: draft.** The record set is settled; the byte encoding and the commit-boundary semantics are TBD.

## What the journal is

An append-only sequence of **mutation records**.
Every change to the stored value graph appends one or more records before the mutating call returns, so a crash loses nothing that has been reported as done.

The journal is deliberately **type-agnostic**.
It records byte-level effects on allocations, not application-level operations.
There is no "push element", no "insert key" — only allocate, free, resize, write.
This is a real trade: it means replay never dispatches on an application type, never needs a type registry, and never has to call back into a container implementation, which in turn is what makes a file readable by an implementation that has never heard of the container that wrote it.
What it gives up is the ability to collapse operations that cancel only at the semantic level.

## Record kinds

Every record is one of:

| record | effect |
| --- | --- |
| `Alloc(id, size, sizedness)` | brings `id` into existence with the given size |
| `Free(id)` | releases `id` |
| `Resize(id, new_size)` | changes `id`'s size, preserving `min(old, new)` bytes |
| `Convert(old_id, new_id, size)` | changes sizedness, minting `new_id` and moving the content |
| `Write(id, offset, bytes)` | overwrites `bytes.len()` bytes at `offset` within `id` |
| `Splice(id, offset, old_len, bytes)` | replaces `old_len` bytes at `offset` with `bytes`, shifting the tail and resizing |
| `Copy(src, src_offset, len, dst, dst_offset)` | copies a byte range between (or within) allocations |

This set is **closed under replayability**: every record whose absence would make another record unreplayable or ambiguous is present.
That is the criterion that put `Alloc` in the list — without it, a `Write` that stores a child's id into its parent would replay into a file where the child was never created, producing a pointer to nothing.

`Splice` and `Copy` are the only records that read existing content.
That distinction matters to an implementation's fold, and is discussed at length in [kladde-rust's journal design](../rust/design/journal/).
It is not visible in the file.

## Framing

Every record is framed with a length prefix and a checksum.
A crash mid-append therefore leaves a **detectable, truncatable** torn record at the tail, and nothing before it is affected.

Exact framing — prefix width, checksum algorithm, whether records are batched into blocks — is **TBD**.

## Ordering

The journal's most important invariant is not about any single record.

> **Any prefix of the journal, replayed alone, must leave a valid state.**

"Valid" permits stale and permits slightly leaky.
It does not permit dangling: nothing may become reachable before everything it depends on is already earlier in the sequence, and nothing may be freed before whatever superseded it is already published.

In practice this means every mutation decomposes as *prepare new, unreachable state → one publishing write → clean up what it replaced*.
Growing a container, removing from the middle of one, and replacing a map entry all fit this shape.

This is a discipline that each container implementation must follow, and it is not enforceable by any type system.
An implementation that cannot express some mutation this way needs an explicit atomic-group mechanism — bracketing records that a recovering reader truncates as a unit if the group is unterminated.
Whether the format includes such a mechanism is **TBD**; it is only needed if a mutation shape is found that genuinely resists the prepare-publish-clean decomposition.

## Recovery

Opening a file:

1. Read and validate the [header](file-format.md#header).
2. Reconstruct the `id → address` table by scanning the snapshot's live allocations.
3. Walk the journal from `journal start`, validating each record's checksum.
4. Stop at the first record that fails validation or is short — that is the torn tail.
   Truncate there.
5. Replay the surviving records against the snapshot state.

Replay is purely mechanical and involves no application code.

## Folding

Periodically the journal is **folded** into the snapshot: its records are applied, the snapshot is brought up to date, and the journal is discarded.

An implementation has wide latitude here.
It may apply records naively in order, or it may optimize — cancelling an allocate/free pair that never escaped, dropping a write that a later write fully supersedes, eliding a shrink-then-grow.
It may reorder freely, so long as the result is indistinguishable from an in-order replay.
Because [unwritten content is unspecified](allocations.md#content-semantics), "indistinguishable" is a weaker and more permissive condition than it first appears.

The fold must be crash-consistent; see [File format](file-format.md#crash-consistency-at-the-container-level).

### Checkpoint versus commit

**TBD, and consequential.**

A fold that is triggered by resource pressure — the journal has grown too large — is a *checkpoint*: it moves work out of memory and into the file.
A fold that marks a point the recovered state may snap to is a *commit*.

Today these are the same event in kladde-rust, which is what gives it atomicity for free.
Separating them is necessary for an implementation that wants to bound journal memory inside a long transaction, and it requires deciding how uncommitted data is kept out of the recovered state — either by write-ahead logging with undo, or by only ever checkpointing committed prefixes.

The format-level consequence is whether the journal needs an explicit commit record.
Until that is decided, the specification assumes fold-equals-commit.
The trade-offs are worked through in [kladde-rust's journal semantics](../rust/design/journal/semantics.md).
