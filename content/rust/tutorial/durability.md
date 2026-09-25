---
title: Durability and flushing
---

What is guaranteed when, and what a crash actually costs you.

## The two-stage model

A mutation does two things before it returns.

1. It appends records describing the byte-level change to the **journal**.
2. It updates the in-memory value.

Separately and later, the journal is **folded**: the records are applied to the file's compact form, written to fresh pages, and a new journal segment is started.

Durability comes from the journal, not from the fold.
A mutation that has returned survives an application crash whether or not a fold has happened, because its record is in the file — the operating system writes it out even if your process dies.
A *power* cut is the case where the fold matters; see [below](#what-a-crash-costs).

## What a crash costs

**After an application crash: nothing that returned `Ok`.**
If `push` returned `Ok`, the element is in the file, and `Kladde::open` finds it.

**After a power cut: at most what came after the last flush.**
Journal appends are not forced to disk one by one, so a power cut can take the ones the operating system had not written out yet.
Everything folded by a completed flush survives, and what survives of the rest is always a prefix: never a later mutation without the earlier ones.

**A partial mutation is never visible.**
Each mutation, and each transaction, is recorded so that recovery replays all of it or none of it, and the built-in containers order their records so that every state in between is valid anyway.

**A crash during a fold costs the fold, not the data.**
The fold never overwrites live data in place; it writes to unused space and commits with one small atomic write.
A crash before that commit leaves the old state intact and the half-written new data inert, and the journal still holds everything, so the next open simply redoes the fold.

## Flushing

You do not normally have to think about this.

Flushing is **automatic**: once the journal outgrows its budget, the mutation that pushed it over folds it before returning.
Application authors mutate state and never call anything.

An explicit `flush()` remains available for the cases where you want the fold to happen now — before a long idle period, say, since a flush is also what makes everything before it survive a power cut.
`close()` flushes and then shrinks the file to its live pages.

```rust
journal.flush()?;
journal.close()?;
```

What flushing is *not* for: making your data survive an application crash.
The journal already did that.

## When the disk fails

A failed `fsync` means the operating system may have dropped writes it had already accepted, so kladde **poisons** the `Kladde`: every later call returns `Error::Poisoned`.
Drop it and open the file again; it then holds every mutation that returned `Ok`.

## Long runs of mutations

A single transaction is held in memory until it ends, because the fold only ever folds complete transactions.
So keep transactions short, and use a [batch](../transactions.md) for a long run of mutations that are each valid on their own: a batch groups them into reasonably sized transactions, and the automatic flush can then fold between them.
