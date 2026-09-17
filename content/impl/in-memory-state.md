---
title: In-memory state
---

The four structures a loaded kladde file is held in, what each is for, and why each is the shape it is.

Integer widths below follow directly from the [bounds the specification states](../spec/address-table.md#bounds), and are given because they are the same in any language.

## 1. The fragment map

The resolved-content view, and the structure everything else hangs off: an ordered map from `(id, offset)` to `Fragment`, where each entry describes the resolved content from `offset` up to the next key with the same `id`.

```rust
enum Fragment {
    /// Resolves through a `Ref` or `Inline`. `offset` points at the data,
    /// never at the framing of an `Inline` statement.
    Bytes { page: u32, offset: PageOffset, statement: StatementRef },

    /// Resolves through an `Undefined`, `Shrink`, or `Tombstone`.
    UndefinedExplicitly { statement: StatementRef },

    /// Resolves to `Undefined` because no statement matches these probes.
    UndefinedByDefault,
}

struct PageOffset(u16);        // offset into a page
struct AllocationOffset(u32);  // offset into an allocation; the map's key
struct StatementRef(NonZeroU32);
```

The three variants answer two questions at once — what the bytes *are*, and who is *responsible* for them — and the third variant is where those answers come apart: a range that resolves by default has a definite answer to the first and none at all to the second.

**An ordered map, because the query is a predecessor search.**
Reading offset `p` of allocation `i` means finding the greatest key `≤ (i, p)` — `O(log F)` for `F` live fragments — and a sequential read iterates from there.
A hash map cannot answer that; a B-tree can, and its node-at-a-time layout suits a structure that is rebuilt in bulk at open and then edited incrementally.

**A fragment stores no length.**
Its extent runs to the next key for the same id, or to the allocation's size for the last one.
That keeps a fragment small and a split cheap, and it makes the next rule mandatory rather than merely tidy.

### The fragments of an existing id exactly partition `[0, size)`

A hole is not representable: omitting an entry does not describe a gap, it extends the preceding fragment, and the predecessor lookup then answers with a neighbour's bytes.
That is a wrong-bytes bug rather than a missing-data bug — the worst kind to leave representable — so a range that resolves to `Undefined` *by default* sits in the map like any other.

The only allocation with no fragments at all is a zero-sized one, where `[0, 0)` is empty and `Grow(id, 0)` is the allocation's only trace, in memory as on disk.

### Where `UndefinedByDefault` comes from

A range resolves by default when no statement matches its probes.
Two sources:

- a bare `Grow`, which matches no probe and therefore leaves its exposed range unowned;
- a `Ref` or `Inline` written at `offset > previous_size`, which leaves `[previous_size, offset)` uncovered;
- and, away from any resize, a range that was simply never written: write `[0, 10)` and then `[20, 35)`, and `[10, 20)` is matched by nothing.

In the first two cases the range is `UndefinedByDefault` **exactly when the id has no anchor**, and the condition is decisive rather than merely necessary: an anchor `Shrink(id, n)` or `Tombstone` matches every probe from `n` upward, hence every probe in an exposed range, and it outranks everything below it; nothing above it reaches there, since content above the anchor is bounded by the size the allocation had before this flush.
And an id with no anchor has never decreased in size, so every content statement it has is bounded by `previous_size` and none reaches into the gap either.

What an anchor does in general is put a floor under where an unowned hole can survive: `Shrink(id, n)` matches every probe from `n` up, so holes live only in `[0, n)`, and a `Tombstone`, anchoring at `n = 0`, leaves none at all.
That is why a tombstoned id's fragments are always owned, and why a re-allocated id's tombstone holds genuine pins.

### Consequences the implementation must honour

- **Coalescing compares the whole variant.**
  Adjacent fragments merge only when they resolve identically *and* name the same statement, so two `UndefinedByDefault` ranges merge but a `Shrink`-owned `Undefined` and an `UndefinedByDefault` must not — they differ in exactly the thing that carries a pin.
- **A size increase adds at most one fragment; a write past the end at most two.**
  Raising the size either extends the last fragment, when the exposed range resolves the way that fragment already does, or adds one entry for it.
  A `Ref` or `Inline` at `offset > size` adds that entry *and* its own, which is the only way a single statement adds two.
- **The blow-up is bounded.**
  `UndefinedByDefault` fragments are separated by owned ones, so they at most double an id's entry count.
- **Every dereference of `statement` must handle its absence.**
  Re-owning a range releases the previous owner's pin, and an `UndefinedByDefault` fragment has no owner to release.
- **Growth into default territory is free.**
  If the last fragment is already `UndefinedByDefault`, raising the size extends it with no new entry at all, since its end is implied by the size.

## 2. The statement slab

One record per statement that still has a reason to live.

```rust
struct StatementSlab {
    records: Vec<StatementRecord>,  // 8 bytes per slot, indexed by StatementRef
    framing_len: Vec<u8>,           // <= 21, per the spec's bounds
    free_head: u32,                 // first free slot, or 0 for "none"
}

struct StatementRecord {
    /// Address-table page where the statement's encoding lives — not the data
    /// page a `Ref` points into. If `pins == 0`, the index of the next free
    /// slot instead, or 0 to end the list.
    page_or_next: u32,

    /// Number of reasons this statement must stay; see [Liveness](liveness.md).
    /// Nonzero for live statements, zero for empty slots.
    pins: u32,
}
```

**A slab indexed by a dense integer, not a hash map.**
`StatementRef` is an index, which is exactly what the specification's "at most `2^32` statements" bound buys, and it makes every lookup a array index rather than a hash.
Dead slots thread into a free list through `page_or_next`, which is unambiguous because a slot is free exactly when `pins == 0`.

**Slot 0 is never handed out.**
That costs one slot once and buys two things: a free-list terminator, which nothing else could be, since page numbers and slot indices both use the whole 32-bit range; and a nonzero `StatementRef`, so an optional reference to a statement is the width of a plain one — which matters three times over in the allocation map below.

**A record exists exactly while `pins > 0`.**
At the `1 → 0` transition its framing is released from its page's coverage, and the slot is freed.
A dead statement then survives purely as bytes in its page until a rewrite of that page decodes it and drops it.

This has a consequence worth naming early, because several arguments rest on it: **an id's physically present statements are not reachable from memory**.
The fragment map indexes only live fragments, the slab holds only live statements, and the allocation map's `mentions` is a count rather than a list.

**Ordering obligation.** On the `1 → 0` transition, read the page number and release its coverage *before* overwriting `page_or_next` with the free-list link.

**Stale references cannot arise**, which is what lets the slab recycle slots freely: every holder of a `StatementRef` — a fragment, an anchor, a grow witness, or a recyclable id's tombstone — gives it up as part of the same event that removes the statement's last pin.
That argument rests on every release site being correct, so a debug-only parallel array of generation counters is worth keeping in mind: it turns a violation into an assertion rather than silent corruption, at no cost in release builds.

## 3. The allocation map

A hash map from `id` to per-allocation metadata.

```rust
struct AllocationMeta {
    size: u32,
    fragment_count: u32,
    statement_bytes: u32,
    mentions: u32,
    anchor: Option<StatementRef>,       // newest Shrink or Tombstone
    grow_witness: Option<StatementRef>, // Grow with n == size, if any
}
```

**A hash map, because the query is a point lookup.**
Nothing ever iterates the allocation map in id order at run time; consolidation's id-range sweep uses the fragment map, which is already ordered.

| field | updated when | read for |
| --- | --- | --- |
| `size` | any flush that resizes the id, writes past its end, frees it, or re-allocates it | answering `size()` in `O(1)`; bounding reads; supplying the extent of an id's last fragment |
| `fragment_count` | on every fragment created or destroyed for this id | defragmentation ranking — description overhead relative to `size`. Counts `UndefinedByDefault` fragments, which are real entries with real memory cost |
| `statement_bytes` | when a statement for this id is created, or when one's pins reach zero | defragmentation ranking, paired with `fragment_count` |
| `mentions` | `+1` per statement naming the id written or read at open; `−1` when one is physically dropped | one thing only: releasing a tombstone anchor's pin when the count falls to 1 |
| `anchor`, `grow_witness` | `anchor`: when a `Shrink` or `Tombstone` is written. `grow_witness`: when a `Grow` is written, and when `size` moves past its bound | moving the anchor pin; telling a consolidator that a victim page holds an anchor it must replace |

`size` is maintained **incrementally**, never recomputed: it is `max(anchor's n, Grow bounds and content extents above anchor_epoch)`, and every operation that could change it knows which term it changed.

### A tombstoned id keeps no allocation-map entry

The flush that frees an id removes it and moves the only two fields a non-existent id still reads into the id allocator's recyclable set:

```rust
struct RecyclableId {
    mentions: u32,
    tombstone: Option<StatementRef>,  // None once the tombstone dies, at mentions == 1
}
```

Everything else is either constant for a non-existent id — `size` 0, `fragment_count` 0, `grow_witness` absent — or already recorded in the tombstone's own slab entry.

So a page rewrite that decodes a statement looks its id up in the allocation map first, and in the recyclable set if it is not there.
The tombstone reference is what the pin release must reach, what a consolidator consults before re-emitting a tombstone it has decoded, and what becomes the `anchor` if the id is allocated again.
Within the recyclable set `mentions` only falls, since nothing writes a statement naming a non-existent id; at 0 both fields are spent and the entry is a bare recyclable id.

## 4. The page table

For every page: its kind, its epoch, and its live-byte counter — its **coverage**.

Pages are bucketed by live fraction, a handful of buckets suffices, together with an age mark.
That makes victim selection `O(1)` rather than a priority queue's `O(log P)`, with `O(1)` bucket moves as counters change.

A free list tracks reusable pages under the [two-generation quarantine](../spec/durability.md#the-reuse-rule): a page dropped by commit `E` becomes writable in flush `E + 2`.

## Derived structures

Two more, rebuilt at open and never persisted:

- **The id allocator** — the next fresh id, plus the recyclable set described above. See [Id recycling](id-recycling.md).
- **The eviction clock** — over the statements currently buffered in the header page, recording how many flushes each has gone untouched. See [The flush](flush.md#the-header-as-write-buffer).

## Cost

Maintenance of all of the above costs `O(log F)` per fragment created or destroyed, and a flush creates or destroys at most a small multiple of the statements it writes.

**Epochs belong to pages, not to statements.**
A statement inherits the epoch of the page it currently sits in, so moving a statement re-stamps it.
The header is rewritten every flush and states resolved truth, so everything in the header always carries the current epoch, and multi-epoch disagreement can exist only between the header and evicted leaves, or between two leaves.
