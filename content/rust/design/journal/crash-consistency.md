---
title: Crash consistency
---

What survives a crash, and the discipline that makes it survive.

**Status:** designed, not built.
Nothing here is exercised, because there is no real file backend yet.

## Three separate failure windows

They need different mechanisms, and conflating them is the usual source of confusion.

### 1. A crash while appending to the log

Handled by **framing**.
Every record carries a length prefix and a checksum, so a torn record at the tail is detectable, and recovery truncates it.
Nothing before it is affected.

The mutation that record belonged to is lost — but it is exactly the mutation that had not returned yet, so nothing was promised.

### 2. A crash between complete mutations, before a fold

Handled by **replay**.
The snapshot is stale but valid; the log holds everything since.
Recovery replays and arrives at the last acknowledged state.

This is the easy case, and it is the one the whole design is arranged around.

### 3. A crash during a fold

Handled by **copy-on-write**.
The fold never overwrites live data in place; it writes new and moved allocations to space that is not currently live.
The old snapshot stays byte-for-byte valid throughout.

The fold commits with a single small, synced write of the header's commit marker, which becomes durable only after the data it names.
A crash before that leaves the old state intact and the half-written new data inert.
A crash after it leaves the new state, with the old space merely unreferenced.

Cost: old and new coexist until the commit, so a fold temporarily needs extra space, and the superseded space is reclaimed afterwards — non-atomically, which is fine, because a crash during reclamation leaks space that a later fold recovers.

This is the simplest member of the shadow-paging family, and it needs no undo information at all.

## The ordering discipline

The mechanism above covers the *fold*.
It does not cover the **within-a-mutation** window, and that one is not automatic.

> **Any prefix of one mutation's records, replayed alone, must leave a valid state.**

Valid permits stale.
Valid permits slightly leaky.
Valid does **not** permit dangling.

In practice every mutation must decompose as:

1. **prepare** new state that nothing yet points at;
2. **publish** it with a single write;
3. **clean up** whatever it replaced.

Growing a vector, removing from the middle of one, and replacing a map entry all fit this shape.
A vector push that must grow, for instance, resizes and writes the new element *before* bumping the stored length — because the length is what makes the element reachable.

This is a discipline every container implementation must follow, and **no type system enforces it**.
It is the single largest source of latent correctness risk in the design.

### The escape hatch, if one is needed

If a mutation shape is found that genuinely resists the decomposition, the fallback is an explicit atomic group: bracket the records, and have recovery truncate back through an unterminated opening bracket as if the group never happened.

Whether the format includes this is **TBD**.
It has not been needed for any mutation shape checked so far.

## Why an auto-flush cannot tear a mutation

A guard borrows the root value.
A flush needs access to the root.
So while any guard is alive, no flush can run — the code does not compile.

Since a multi-record mutation is only ever in flight while a guard is alive, an automatic flush can fire only *between* complete mutations.

This is a genuinely nice property, obtained for free from Rust's borrow checker, and it is one of the places where the language earns its keep in this design.
Note that it holds only as long as flushing is never reachable through a path that has just a shared reference to the backend, which is what a guard holds — see the [checkpoint-versus-commit](semantics.md#open-checkpoint-versus-commit) discussion, where the auto-checkpoint deliberately *is* such a path and the argument has to be remade on other grounds.

## Testing

The ordering discipline is the thing most worth testing mechanically, and the test is cheap:

> For each of a set of mutation sequences, truncate the resulting log at **every** byte offset, replay, and assert the result is a valid state and a prefix of the intended one.

This turns a hand-waved invariant into a machine-checked one.
It is buildable as soon as a real log exists, and it should be built at the same time.

A complementary test is a **leak detector**: walk every allocation reachable from the root via the type structure, compare against the heap's live set, and assert they match.
That checks the "clean up" half of the discipline, which the prefix test does not.

## What is not decided

The [checkpoint-versus-commit](semantics.md#open-checkpoint-versus-commit) question determines whether a fold is a durability boundary.
Until it is settled, everything above assumes fold-equals-commit, which is what makes atomicity free.

If they are separated, then a checkpoint writes uncommitted data into the file, and something must keep it out of the recovered state — either write-ahead logging with undo, or checkpointing only committed prefixes.
The first bounds memory inside long transactions and needs undo; the second is far simpler and does not.
