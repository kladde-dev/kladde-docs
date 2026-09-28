---
title: Tutorial
---

For people who want to *use* kladde in a Rust application.
Assumes ordinary Rust knowledge and no prior exposure to kladde.

1. **[Getting started](getting-started.md)** — your first durable value, and the mental model.
2. **[The built-in containers](containers.md)** — vector, hash map, string, blob.
3. **[Deriving your own types](deriving.md)** — structs and enums.
4. **[Compared to serde](comparison-to-serde.md)** — when you want kladde and when you want serde, and why they are not competitors.
5. **[Durability and flushing](durability.md)** — what is guaranteed when, what a crash costs you, and transactions.
6. **[Writing a custom `Persistable`](custom-persistable.md)** *(advanced)* — opaque types, and implementing the trait by hand.

## Is kladde the right tool?

Kladde is a good fit when **all** of these hold:

- you have a long-lived, mutable data structure that must survive a crash;
- you read it far more often than you write it, and you want reads to cost what in-memory reads cost;
- your mutations are small and frequent relative to the size of the whole structure;
- one process owns the file.

It is a poor fit when:

- you want to *exchange* data with other systems — that is serde's job, and kladde's format is a heap, not a document;
- you need concurrent writers, or readers in other processes;
- your data is append-only and never mutated in place — a log file is simpler and faster;
- your working set does not fit in memory.
  Kladde keeps the whole structure resident by design.

The comparison that most often matters in practice is with "just serialize the whole thing on every change," which is what most applications do.
That is `O(total size)` per mutation.
Kladde is `O(size of the change)`, at the cost of a more constrained type vocabulary.

## Status warning

Kladde is early: the file format is a draft, and a file written today may not open with a later version.
