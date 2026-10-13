---
title: The store
---

The Rust-specific half of the [storage layer](../impl/): interior mutability, file I/O, and the concrete types of the [in-memory structures](../impl/in-memory-state.md).

None of this changes what the structures *mean*; it is about realising them in Rust without making them awkward.

## The interface

`kladde-store` is a persistent store of one byte sequence, usable on its own: `Store` implements `WriteBackend`, whose methods are the journal's records — `write`, `splice`, `move_range` and `paste` — and the transaction calls, plus `len`.
`write` and `splice` return the cut of what they removed, an `Option<CutId>`, inside a transaction, and `splice` records a `Write` wherever the lengths agree.
`Store::flush`, `read_all`, `schema` and `set_schema`, `private` and `set_private`, and `stats` complete it; the schema and private chains are byte strings the next flush writes.

## Interior mutability

**`Store` keeps all of its state behind one `RefCell`, and borrows it for the length of one call.**
`WriteBackend`'s methods take `&self` so that sibling guards can share a backend, which forces interior mutability somewhere, and one cell is enough: no store method calls out into user code, so no borrow is ever held across a callback, and a single dynamic borrow per call costs nothing measurable next to the journal append it guards.

The one reentrant path is the [automatic flush](../impl/transactions-and-batches.md#flush-triggers), which runs inside the mutating call whose transaction would not fit in what is left of the journal's budget.
It runs after that call's own borrow has ended, and takes the cell again for itself.

`Store` is therefore `Send` but not `Sync`, which matches the [single-writer scope](../spec/file-format.md#concurrency) of the format.

## Storage

The store reads and writes pages through a `Storage` trait with two implementations.

**`FileStorage`** is a `std::fs::File` driven by positional reads and writes, so no call depends on a shared file cursor, and synced with `sync_data`, which covers the file-size change a growth makes.
It extends the file with `set_len`, since the standard library offers no `fallocate`, and that is the [ungraceful form](../spec/durability.md#detecting-that-the-disk-is-full) of detecting a full disk: a disk that fills up surfaces at the flush's `fsync`, which ends the session, rather than as an error the flush could return.

**`MemoryStorage`** is a byte vector, used by `Kladde::new` and by tests.
It also keeps the image as of the last sync apart from the writes since, so a test can simulate a power cut by discarding any subset of those writes and opening what is left — the [flush-interruption test](../spec/conformance.md#the-suite) the conformance suite calls for.

## The mirror

Table pages stay resident as the [reference algorithms recommend](../impl/index.md#the-mirror), each a boxed page-sized array keyed by page number.
Data pages are read into a mirror while a file is opened, in page order, and dropped once the sequence is decoded; a flush that needs a span's bytes again, to move, merge or rewrite it, reads them from the file through a small cache.

## The in-memory structures

### The span table

A `Vec<Span>` indexed by span id, of **48 bytes per span** rather than the 16 the reference algorithms count, since each record also holds what the store keeps beside the span table in the language-agnostic design: the span's chain, its rope node, its heat, its entry's size and its place in its table page, and its place in its data page's reverse list.
A bitmap of used ids beside it serves the [id allocator](../impl/in-memory-state.md#5-the-span-id-allocator), which hands out the id after the predecessor's, if it is free, and the lowest free id otherwise — which keeps the spans a flush makes one after another consecutive without looking for a run.

### The treap

**The rope is an implicit treap, not a counted B-tree**: a randomized binary tree in an arena of nodes, each knowing its parent and the total length below it, `O(log k)` expected steps per operation and 40 bytes per node.
It is there for ease of implementation: a split at a position and a join of two trees are all that the rope, the fold's piece table and a transaction's cuts need, so one arena type of under 400 lines serves all three, while splitting and joining whole B-trees at a position is the intricate part of a counted B-tree.
Its priorities come from a seeded generator, so runs reproduce.
With the treap's node, a span costs about 90 bytes in memory, against the 32 of the reference algorithms; for the files measured that is a fraction of a megabyte, and a B-tree is the better choice once memory per span matters.

### Maps keyed by id or page

Sets and maps keyed by span id or page number use a multiplicative hasher rather than `std`'s SipHash, whose protection against HashDoS a map built from the file does not need.
The page table is a `Vec` of an enum over the page states, indexed by page number, since [lowest-first placement](../impl/consolidation.md#lowest-page-first) keeps page numbers dense; only a live page has an epoch and a victim bucket.

### The journal buffer

Every record since the last flush lives in one `VecDeque<Record>` beside a byte arena, also a `VecDeque`, that holds their payloads, with positions counted from the start of the session ([one buffer and three cursors](../impl/transactions-and-batches.md#one-buffer-and-three-cursors)).
A record holds absolute positions in memory and is delta-encoded only when it is appended.

## Where the integer widths come from

Every width below is forced by [the specification's bounds](../spec/span-table.md#bounds), not chosen:

| quantity | width | because |
| --- | --- | --- |
| span id | `u32` | span ids are 32-bit, and 0 means none |
| span length | `u32` | a `Zero` span may be up to `2^32 − 1` bytes |
| page offset | `u16` | page sizes are at most 64 KiB |
| page number | `u32` | page numbers are 32-bit |
| position in the sequence | `u64` | the sequence is shorter than `2^48` bytes |
| a size in a container's index | `u32` | four bytes per element; a sum of 4 GiB or more panics today, where it should fail cleanly ([implementation notes](../impl/implementation-notes.md)) |
