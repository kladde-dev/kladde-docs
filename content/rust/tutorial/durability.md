---
title: Durability and flushing
---

What is guaranteed when, and what a crash actually costs you.

**Status:** this describes the intended guarantees.
The current implementation runs against an in-memory backend, so none of it is yet exercised against a real file.

## The two-stage model

A mutation does two things.

1. It updates the in-memory value — immediately, synchronously, before the call returns.
2. It appends records describing the byte-level change to the **journal**, which is durable before the call returns.

Separately and later, the journal is **folded** into the snapshot: the records are applied to the compact on-disk form and the journal is discarded.

The important consequence: durability comes from step 2, not from the fold.
A mutation that has returned survives a crash whether or not a fold has happened.

## What a crash costs

**Nothing that has been acknowledged.**
If `push` returned, the element is in the file.

**A partial mutation is never visible.**
The records of a single mutation are ordered so that any prefix of them, replayed alone, leaves a valid state.
Nothing becomes reachable until everything it depends on is already recorded, and nothing is freed until its replacement is published.
So a crash halfway through a `push` leaves you with the vector as it was before, never a vector with a bumped length pointing at uninitialised space.

**A crash during a fold costs the fold, not the data.**
The fold never overwrites live data in place; it writes to unused space and commits with one small atomic write.
A crash before that commit leaves the old snapshot intact and the half-written new data inert, and the journal still holds everything.
The next open simply redoes the fold.

**A torn journal record is detected and discarded.**
Every record carries a length prefix and a checksum, so a crash mid-append leaves a detectable partial record at the tail.
Recovery truncates it.
That record's mutation is lost, but it is exactly the one that had not returned yet.

## Flushing

You should not normally have to think about this.

The intent is that flushing is **automatic**: when the journal grows past a threshold, the next mutation folds it before proceeding.
Application authors mutate state and never call anything.

An explicit `flush()` remains available for the cases where you want the fold to happen now — before a long idle period, or in a test.

```rust
journal.flush();
```

Note what flushing is *not* for.
It is not what makes your data durable — the journal already did that.
It is what bounds memory and keeps open times short.

## Why flushing cannot corrupt a mutation

A fold cannot happen in the middle of a mutation, and this is enforced by the borrow checker rather than by care.

A guard borrows the root value.
A flush needs access to the root.
So while any guard is alive, no flush can run — the code does not compile.
Since a multi-record mutation is only ever in flight while a guard is alive, an automatic flush can only fire *between* complete mutations.

This is a nice property to have for free, and it is one of the places where Rust's ownership model earns its keep in this design.

## Checkpoint versus commit

**Not yet decided**, and it will matter if you use long-running batch mutations.

The open question is whether an automatic fold triggered by journal size should also be a *commit point* — a boundary that recovery snaps to.
Today they are the same thing, which gives atomicity for free but means memory cannot be bounded inside a very long sequence of mutations.

If your application does something like "rebuild the whole index in one pass," this is the question that determines whether that pass can bound its memory use.
See [Journal semantics](../design/journal/semantics.md) for the design discussion.

## What is not implemented

Everything on this page describes intent.
Concretely missing:

- real file storage — there is no `Kladde::open(path)`;
- the copy-on-write fold and its commit marker;
- journal framing and checksums;
- the automatic flush trigger;
- reclamation of freed space.

The ordering discipline that makes prefix-replay work is designed and partially implemented; it is not yet mechanically tested, which is the single most valuable test to add once a real file exists.
