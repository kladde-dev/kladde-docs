---
title: The journal
---

Durability, and what a flush is allowed to do with what it recorded.

**Status: the current implementation is known broken.**
[Semantics](semantics.md) explains what is wrong and why it is one bug rather than three; [Fold and schedule](fold-and-schedule.md) describes the replacement.

## The documents

| document | contents |
| --- | --- |
| [Semantics](semantics.md) | what the journal records, why the current design fails, and the log-as-authority model |
| [Fold and schedule](fold-and-schedule.md) | how a flush turns a log into the minimum work that reproduces it |
| [Crash consistency](crash-consistency.md) | the ordering discipline, and what survives a crash |

## The idea

A mutation does two things: it updates the in-memory value, and it appends a description of the byte-level change to a log.
The log is durable before the mutating call returns.
Periodically the log is **folded** into the heap and discarded.

The log is deliberately **type-agnostic**.
It records effects on allocations, not application operations — allocate, free, resize, write, and two content-moving operations.
There is no "push element."

This is a real trade.
What it buys: replay never dispatches on an application type, never needs a type registry, and never calls back into a container.
That is what makes a file readable by an implementation that has never heard of the container that wrote it, and it is why the [format specifies the record set](../../../spec/journal.md) but not the containers.

What it gives up: the ability to collapse operations that cancel only at the semantic level.
A rope that applies a hundred edits which cancel out still records a hundred edits' worth of byte changes, because nothing in the log knows they were edits.

## Two jobs, often confused

The journal does two things that are worth keeping separate in one's head, because they have different requirements and are currently fused.

**Durability.** Make a mutation survive a crash.
Requires that records reach stable storage before the call returns, and that a torn tail is detectable.

**Deferral.** Avoid touching the heap on every mutation.
Requires only that the log be replayable later; nothing about it needs to be durable for this purpose.

Today both are served by the same structure at the same moment, which is why the [checkpoint-versus-commit question](semantics.md#open-checkpoint-versus-commit) is unresolved.
Separating them is what would let an implementation bound its memory inside a long transaction.
