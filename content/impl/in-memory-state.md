---
title: In-memory state
---

The four structures a loaded kladde file is held in, what each is for, and why each is the shape it is.

Integer widths below follow directly from the [bounds the specification states](../spec/address-table.md#bounds), and are given because they are the same in any language.

## 1. The fragment map

The resolved-content view, and the structure everything else hangs off: an ordered map from `(id, offset)` to `Fragment`, where each entry describes the resolved content from `offset` up to the next key with the same `id`.

```rust
enum Fragment {
    /// Resolves through a `Ref` or `Inline`. `(page, offset)` points
    /// at the *data*, never at the *framing* of the statement.
    Bytes { page: u32, offset: PageOffset, statement: StatementRef },

    /// Resolves to zero through a `Zero`.
    Zero { statement: StatementRef },

    /// Taken by the flush in progress, and not stated yet; see "During a flush".
    /// The cut turns it into `Bytes` or `Zero`, so no other code path
    /// outside a flush ever meets it.
    Pending(PendingRef),
}

struct PageOffset(u16);          // offset into a page
struct AllocationOffset(u32);    // offset into an allocation; the map's key
struct StatementRef(NonZeroU32); // See below why non-zero.
```

The two permanent variants answer two questions at once — what the bytes *are*, and which statement is *responsible* for them.
The third answers the second with "the flush in progress", and exists only between a flush taking a range and [stating it](#during-a-flush).

**An ordered map, because the query is a predecessor search.**
Reading offset `p` of allocation `i` means finding the greatest key `≤ (i, p)` — `O(log F)` for `F` live fragments — and a sequential read iterates from there.
A hash map cannot answer that; a B-tree can, and its node-at-a-time layout suits a structure that is rebuilt in bulk at open and then edited incrementally.

**A fragment stores no length.**
Its extent runs to the next key for the same id, or to the allocation's size for the last one.
That keeps a fragment small and a split cheap, and it makes the next rule mandatory rather than merely tidy.

### The fragments of an existing id exactly partition `[0, size)`

A hole is not representable: omitting an entry does not describe a gap, it extends the preceding fragment, and the predecessor lookup then answers with a neighbour's bytes.
That is a wrong-bytes bug rather than a missing-data bug — the worst kind to leave representable — and the file format rules it out at the source: [every byte below an allocation's size is stated](../spec/address-table.md#the-coverage-rule), so every fragment outside a flush has an owner, a range of zeros included.

The only allocation with no fragments at all is a zero-sized one, where `[0, 0)` is empty and its size statement, such as `Size(id, 0)`, is the allocation's only trace, in memory as on disk.

### Consequences the implementation must honour

- **Coalescing compares the whole variant.**
  Adjacent fragments merge only when they resolve identically *and* name the same statement, so two zero ranges owned by different `Zero` statements must not merge — they differ in exactly the thing that carries a pin.
- **A growth adds one fragment, and a write past the end two.**
  A flush that grows an allocation takes the range the growth exposes, `[previous_size, new_size)`, as pending zeros, since the cut must state it; a write past the end takes the written range on top of that, leaving the gap below it pending as zeros.
- **During a flush, every code path that reaches for a fragment's owner must handle `Pending`**, which has none.
  The concrete case is pin accounting: overwriting a range releases the previous owner's pin, and a source that takes a range already pending simply discards the older entry, with no pin to release.

## 2. The statement slab

One `StatementRecord` per live statement.

```rust
struct StatementSlab {
    records: Vec<StatementRecord>,  // 8 bytes per slot, indexed by StatementRef
    framing_len: Vec<u8>,           // each entry is <= 21, per the spec's bounds
    free_head: u32,                 // first free slot, or 0 for "none"
}

struct StatementRecord {
    /// Address-table page where the statement's encoding lives — not the data
    /// page a `Ref` points into. If `pins == 0`, the index of the next free
    /// slot instead, or 0 to end the list.
    page_or_next: u32,

    /// Number of reasons this statement must stay; see Section "Liveness".
    /// Nonzero for live statements, zero for empty slots.
    pins: u32,
}
```

**A slab indexed by a dense integer, not a hash map.**
`StatementRef` is a 32-bit index, possible because of the specification's bound that at most `2^32 - 1` statements exist.
This makes every lookup an array index rather than a hash.
Dead slots thread into a free list through `page_or_next`, which is unambiguous because a slot is free exactly when `pins == 0`.

**Slot 0 is never handed out.**
That costs one slot once and buys niche optimization for `AllocationMeta::size_statement` and `RecyclableId::tombstone`.
These optimizations are the motivation why the specification bounds the number of statements to at most `2^32 - 1` rather than `2^32`.
Reserving slot 0 also makes for a simple free-list terminator (`page_or_next == 0`), although this argument is not load bearing since the end of the free-list could also be indicated by a self-reference instead.

**A record exists exactly while `pins > 0`.**
At the `1 → 0` transition its framing is released from its page's coverage, and the slot is freed.
A dead statement then survives purely as bytes in its page until a rewrite of that page decodes it and drops it.

This has a consequence worth naming early, because several arguments rest on it: **an id's physically present statements are not reachable from memory**.
The fragment map indexes only live fragments, the slab holds only live statements, and the allocation map's `mentions` is a count rather than a list.

**Ordering obligation.** On the `1 → 0` transition, read the page number and release its coverage *before* overwriting `page_or_next` with the free-list link.

**Stale references cannot arise**, which is what lets the slab recycle slots freely: every holder of a `StatementRef` — a fragment, an allocation's size statement, or a recyclable id's tombstone — gives it up as part of the same event that removes the statement's last pin.
That argument rests on every release site being correct, so a debug-only parallel array of generation counters is worth keeping in mind: it turns a violation into an assertion rather than silent corruption, at no cost in release builds.

## 3. The allocation map

A hash map from `id` to per-allocation metadata.

```rust
struct AllocationMeta {
    size: u32,
    fragment_count: u32,
    statement_bytes: u32,
    mentions: u32,
    last_written: u32,                    // the flush that last wrote the id; see below
    size_statement: Option<StatementRef>, // newest statement that states the size
}
```

**A hash map, because the query is a point lookup.**
Nothing ever iterates the allocation map in id order at run time; consolidation's id-range sweep uses the fragment map, which is already ordered.

| field | updated when | read for |
| --- | --- | --- |
| `size` | any flush that resizes the id, writes past its end, frees it, or re-allocates it | answering `size()` in `O(1)`; bounding reads; supplying the extent of an id's last fragment |
| `fragment_count` | on every fragment created or destroyed for this id | [pricing description defragmentation](consolidation.md#description-defragmentation-rides-the-rotating-window) — description overhead relative to `size` |
| `statement_bytes` | when a statement for this id is created, or when one's pins reach zero | pricing defragmentation, paired with `fragment_count` |
| `mentions` | `+1` per statement naming the id written or read at open; `−1` when one is physically dropped | one thing only: releasing a tombstone's pin when the count falls to 1 |
| `last_written` | whenever the application's writes reach the id — a flush that writes, resizes, or allocates it | the age of [description defragmentation's candidates](consolidation.md#where-it-is-called-and-how-it-is-executed) |
| `size_statement` | when a statement that states the size is written: a `Size`, a sizing `Ref*`, `Zero*`, or `Inline*`, or, for an id re-allocated, the tombstone it carries over until the new incarnation states its size | holding the [size pin](liveness.md#pins); telling a consolidator that a victim page holds a size statement it must state again |

`size` is maintained **incrementally**, never recomputed: every operation that resizes the id sets it, and once the flush that resized it has cut, it is what `size_statement` states.

**`last_written` measures content age, where an epoch measures location age.**
A statement's epoch [is its page's](#cost), so every restatement re-stamps it — the header re-stamps everything it holds on every flush — whereas `last_written` moves only when the application writes the id.
It counts flushes in 32 bits from a base the session sets at open, and a session that would outrun them rebases every entry in one pass.
At load it is restored from the [consolidator state](consolidator-state.md#content-ages); an id the state has no current record for falls back to the youngest page that holds one of its fragments, which can only understate the age, since content is never younger than the page that holds it; so it errs toward treating an id as recently written.

### A tombstoned id keeps no allocation-map entry

The flush that frees an id removes it and moves the only two fields a non-existent id still reads into the id allocator's recyclable set:

```rust
struct RecyclableId {
    mentions: u32,
    tombstone: Option<StatementRef>,  // None once the tombstone dies, at mentions == 1
}
```

Everything else is either constant for a non-existent id — `size` 0, `fragment_count` 0 — or already recorded in the tombstone's own slab entry.

So a page rewrite that decodes a statement looks its id up in the allocation map first, and in the recyclable set if it is not there.
The tombstone reference is what the pin release must reach, what a consolidator consults before re-emitting a tombstone it has decoded, and what becomes the `size_statement` if the id is allocated again — until the flush that allocates it binds the new incarnation's own size statement, which releases the tombstone.
Within the recyclable set `mentions` only falls, since nothing writes a statement naming a non-existent id; at 0 both fields are spent and the entry is a bare recyclable id.

## 4. The page table

For every page: its page number, kind, epoch, and live-byte counter (**coverage**).

The counter is 32 bits rather than page-sized, because a page's live bytes exceed its capacity whenever several `Ref` statements claim the same bytes — which is legal, and [bounded](../spec/address-table.md#bounds) precisely so that 32 bits suffice.

Pages are bucketed by live fraction, a handful of buckets suffices, together with an age mark.
That makes victim selection `O(1)` rather than a priority queue's `O(log P)`, with `O(1)` bucket moves as counters change.
`Data` and non-header `Table` pages are bucketed [separately](consolidation.md#the-structures-behind-the-ranking), and header pages in neither.
The header is excluded here since it is rewritten every flush, so it is never a victim, and its content is accounted for by being [taken and stated again by every flush](consolidation.md#one-dirty-set-and-why-statements-are-derived-last) rather than by its coverage.

The in-memory state of an `Table` page carries one more field: its parent's page number, four bytes, updated whenever the parent is replaced.
[Unlinking an emptied page](consolidation.md#unlinking-an-emptied-page) needs it, since a page may be named by an interior page rather than by the header.

An ordered set tracks reusable pages under the [two-generation quarantine](../spec/durability.md#the-reuse-rule), handing out the lowest first: a page dropped by commit `E` becomes writable in flush `E + 2`.

## Derived structures

Two more, rebuilt at open and never persisted:

- **The id allocator** — the next fresh id, plus the recyclable set described above. See [Id recycling](id-recycling.md).
- **The eviction clock** — over the fragments the header states, recording how many flushes each has gone untouched, which open seeds from their allocations' `last_written`. See [The flush](flush.md#the-header-as-write-buffer).

## During a flush

A flush changes the structures above in two steps: it **takes** the fragments it changes as it goes, and it **states** them only when it cuts the address table.
[Why the two are separate](consolidation.md#each-fragment-is-stated-once-per-epoch) belongs to consolidation; between them, three pieces of transient state exist, all dropped when the flush commits.

**Pending fragments.**
A fragment the flush has taken names an entry in a per-flush table rather than a statement:

```rust
struct Pending {
    origin: Origin,    // where its bytes are now
    place: Place,      // where they will be stated from
    heat: u32,         // flushes untouched, for the header's split
}

enum Origin {
    Arena(ArenaPos),   // written this flush
    File(Address),     // already in the file
    None,              // zeros: nothing to read
}

enum Place {
    Unplaced,          // a chunk: `pack` gives it a data page
    Data(Address),     // in a data page, placed or left where it was
    Inline,            // in the address table, as an `Inline` payload
    Zero,              // a `Zero` statement's range
}
```

A pending fragment has no owner and so holds no pin; pins are handed out when the cut binds its statement.
Its coverage follows its place: a fragment in `Data` is charged to that page exactly as a `Bytes` fragment is, an `Unplaced` one is charged when `pack` places it, and an `Inline` payload when the cut decides which table page carries it.

**The dirty set.**
Every key range the flush has taken, kept merged and sorted, and per touched id a record of what the fragment map cannot say: the size and existence the id had when the flush began, whether it was freed, and whether a page the flush retires held its size statement or tombstone.
The cut [derives every statement](consolidation.md#one-dirty-set-and-why-statements-are-derived-last) from these two.

**Heat** rides on the pending entries rather than on statements, since statements are derived afresh every flush: a statement derived from several fragments takes the hottest of them.

## Cost

Maintenance of all of the above costs `O(log F)` per fragment created or destroyed, and a flush creates or destroys at most a small multiple of the statements it writes.

**Epochs belong to pages, not to statements.**
A statement inherits the epoch of the page it currently sits in, so moving a statement re-stamps it.
The header is rewritten every flush and states resolved truth, so everything in the header always carries the current epoch, and multi-epoch disagreement can exist only between the header and evicted leaves, or between two leaves.
It also means an epoch says when a statement was last *moved*, not when its content was last written, which is why the allocation map keeps [`last_written`](#3-the-allocation-map) as well.
