---
title: The store
---

The Rust-specific half of the [storage layer](../impl/): interior mutability, file I/O, and the concrete types of the [in-memory structures](../impl/in-memory-state.md).

None of this changes what the structures *mean*; it is about realising them in Rust without making them awkward.

## Interior mutability

**`Store` keeps all of its state behind one `RefCell`, and borrows it for the length of one call.**
`WriteBackend`'s methods take `&self` so that sibling guards can share a backend, which forces interior mutability somewhere, and one cell is enough: no store method calls out into user code, so no borrow is ever held across a callback, and a single dynamic borrow per call costs nothing measurable next to the journal append it guards.

The one reentrant path is the [automatic flush](../impl/transactions-and-batches.md#flush-triggers), which runs inside the mutating call that pushed the journal over its budget.
It runs after that call's own borrow has ended, and takes the cell again for itself.

`Store` is therefore `Send` but not `Sync`, which matches the [single-writer scope](../spec/file-format.md#concurrency) of the format.

## Storage

The store reads and writes pages through a `Storage` trait with two implementations.

**`FileStorage`** is a `std::fs::File` driven by positional reads and writes, so no call depends on a shared file cursor, and synced with `sync_data`, which covers the file-size change a growth makes.
It extends the file with `set_len`, since the standard library offers no `fallocate`, and that is the [ungraceful form](../spec/durability.md#detecting-that-the-disk-is-full) of detecting a full disk: a disk that fills up surfaces at the flush's `fsync`, which ends the session, rather than as an error the flush could return.

**`MemoryStorage`** is a byte vector, used by `Kladde::new` and by tests.
It also keeps the image as of the last sync apart from the writes since, so a test can simulate a power cut by discarding any subset of those writes and opening what is left — the [flush-interruption test](../spec/conformance.md#the-suite) the conformance suite calls for.

## The mirror

Address-table pages stay resident as the [implementation notes recommend](../impl/index.md#the-mirror), each a boxed page-sized array keyed by page number.
Data pages are read through a small cache while a file is loaded, and dropped afterwards; a flush that needs their bytes again, to relocate survivors or to fold a `Copy`, reads them from the file.

## The in-memory structures

### Keys

An `(id, offset)` key is packed into one `u64`, id in the high half, so the fragment map is a `BTreeMap<u64, Fragment>` whose order is exactly the `(id, offset)` order the format needs, and a range over one id is a range of integers.

### Fragments

`Fragment` is an ordinary Rust enum of twelve bytes, with a pending fragment carrying an index into the flush's table of pending entries.

A packed ten-byte representation is available if memory ever matters: the discriminant can be stolen from the page-offset field, using two values that a real offset can never take because [the page framing](../spec/file-format.md#page-framing) puts a CRC there.
It is not used, because it costs `unsafe` reads of `MaybeUninit` fields on every access for two bytes per fragment.

### The statement slab

The language-agnostic shape is a slab of `{ page_or_next, pins }` plus framing lengths.
Rust realises it as **two parallel arrays**, `Vec<StatementRecord>` of eight-byte records and `Vec<u8>` of framing lengths, rather than one packed record.
A nine-byte packed record would need `#[repr(packed)]`, where taking a reference to `pins` — the field touched on every fragment gained or lost — is undefined behaviour; splitting gets the same nine bytes per slot with natural alignment, and reads `framing_len` only when a statement dies.

`StatementRef` is a `NonZeroU32`, and slot 0 is never handed out, so `Option<StatementRef>` is four bytes rather than eight: that saves twelve bytes per allocation across the allocation map's `anchor` and `grow_witness` and the recyclable set's `tombstone`.

`pins` is `u32` rather than `u16`, because a `Shrink` or `Tombstone` wins the gaps *between* newer statements anywhere in `[n, size)`, so its fragment pins are bounded by the allocation's fragment count and nothing smaller.

### Maps keyed by id or page

The allocation map is a `HashMap<u32, AllocationMeta>` with a multiplicative hasher rather than `std`'s SipHash, whose protection against HashDoS an id-keyed map built from the file does not need.
The page table is a `Vec<PageInfo>` indexed by page number, since [lowest-first placement](../impl/consolidation.md#lowest-page-first) keeps page numbers dense.

## Where the integer widths come from

Every width below is forced by [the specification's bounds](../spec/address-table.md#bounds), not chosen:

| type | width | because |
| --- | --- | --- |
| `Pointer` | `NonZeroU32` | allocation ids are 32 bit; zero is reserved so `Option<Pointer>` is free |
| page offset | `u16` | page sizes are at most 64 KiB |
| allocation offset | `u32` | allocation sizes are below `2^32` |
| page number | `u32` | page numbers are 32 bit |
| `StatementRef` | `NonZeroU32` | at most `2^32 - 1` statements in a file, and zero is the free niche |
| framing length | `u8` | a statement's framing is at most 21 bytes |
