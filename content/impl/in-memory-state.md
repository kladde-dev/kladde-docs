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

    /// Resolves to zero through a `Zero`, `Shrink`, or `Tombstone`.
    ZeroExplicitly { statement: StatementRef },

    /// Resolves to zero because no statement matches these probes.
    ZeroByDefault,

    /// Taken by the flush in progress, and not stated yet; see "During a flush".
    /// The cut turns it into `Bytes` or `ZeroExplicitly`, so no other code path
    /// outside a flush ever meets it.
    Pending(PendingRef),
}

struct PageOffset(u16);          // offset into a page
struct AllocationOffset(u32);    // offset into an allocation; the map's key
struct StatementRef(NonZeroU32); // See below why non-zero.
```

The three permanent variants answer two questions at once — what the bytes *are*, and who is *responsible* for them — and the third is where those answers come apart: a range that resolves by default has a definite answer to the first and none at all to the second.
The fourth answers the second with "the flush in progress", and exists only between a flush taking a range and [stating it](#during-a-flush).

**An ordered map, because the query is a predecessor search.**
Reading offset `p` of allocation `i` means finding the greatest key `≤ (i, p)` — `O(log F)` for `F` live fragments — and a sequential read iterates from there.
A hash map cannot answer that; a B-tree can, and its node-at-a-time layout suits a structure that is rebuilt in bulk at open and then edited incrementally.

**A fragment stores no length.**
Its extent runs to the next key for the same id, or to the allocation's size for the last one.
That keeps a fragment small and a split cheap, and it makes the next rule mandatory rather than merely tidy.

### The fragments of an existing id exactly partition `[0, size)`

A hole is not representable: omitting an entry does not describe a gap, it extends the preceding fragment, and the predecessor lookup then answers with a neighbour's bytes.
That is a wrong-bytes bug rather than a missing-data bug — the worst kind to leave representable — so a range that resolves to zero *by default* sits in the map like any other.

The only allocation with no fragments at all is a zero-sized one, where `[0, 0)` is empty and `Grow(id, 0)` is the allocation's only trace, in memory as on disk.

### Where `ZeroByDefault` comes from

A range resolves by default when no statement matches its probes.
Two sources, the first of which needs a name: call `[previous_size, new_size)` the **exposed range** of a growth — the offsets that were outside the allocation before it and are inside it after.

- a bare `Grow`, which matches no probe and therefore leaves its exposed range unowned;
- a `Ref` or `Inline` written at `offset > previous_size`, whose exposed range `[previous_size, offset)` it leaves uncovered;
- and, away from any resize, a range that was simply never written: write `[0, 10)` and then `[20, 35)`, and `[10, 20)` is matched by nothing.

In the first two cases the range is `ZeroByDefault` **exactly when the id has no anchor with `n <= probe`**, and the condition is decisive rather than merely necessary: an anchor `Shrink(id, n)` or `Tombstone` matches every probe from `n` upward (with `n = 0` for `Tombstone`), hence every probe in an exposed range, and it outranks everything with older `epoch`; nothing above it reaches there, since content above the anchor is bounded by the size the allocation had before this flush.
And an id with no anchor has never decreased in size, so every content statement it has is bounded by `previous_size` and none reaches into the gap either.

What an anchor does in general is put a floor under where an unowned hole can survive: `Shrink(id, n)` matches every probe from `n` up, so holes live only in `[0, n)`, and a `Tombstone`, anchoring at `n = 0`, leaves none at all.
That is why a tombstoned id's fragments are always owned, and why a re-allocated id's tombstone holds genuine pins.

### Consequences the implementation must honour

- **Coalescing compares the whole variant.**
  Adjacent fragments merge only when they resolve identically *and* name the same statement, so two `ZeroByDefault` ranges merge but a `Shrink`-owned zero range and a `ZeroByDefault` one must not — they differ in exactly the thing that carries a pin.
- **A size increase adds at most one fragment; a write past the end at most two.**
  Raising the size either extends the last fragment, when the exposed range resolves the way that fragment already does, or adds one entry for it.
  A `Ref` or `Inline` at `offset > size` adds that entry *and* its own, which is the only way a single statement adds two.
- **The blow-up is bounded.**
  `ZeroByDefault` fragments are separated by owned ones, so they at most double an id's entry count.
- **Every code path that reaches for a fragment's owner must handle the variant that has none.**
  `ZeroByDefault` carries no `statement` field at all, so this is a match arm rather than a null check — and it is the arm that is easy to forget, because the other two variants both have one.
  The concrete case is pin accounting: overwriting a range releases the previous owner's pin, and a `ZeroByDefault` fragment has no owner and no pin to release, so a blind release there would decrement some unrelated statement or panic.
  The same applies to a consolidator asking "does this fragment belong to the statement I am rewriting?", where the answer for this variant is always no.
  During a flush `Pending` joins it: it has no owner either, and a source that takes a range already pending simply discards the older entry, with no pin to release.
- **Growth into default territory is free.**
  If the last fragment is already `ZeroByDefault`, raising the size extends it with no new entry at all, since its end is implied by the size.

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
That costs one slot once and buys niche optimization for `AllocationMeta::anchor`, `AllocationMeta::grow_witness`, and `RecyclableId::tombstone`.
These optimizations are the motivation why the specification bounds the number of statements to at most `2^32 - 1` rather than `2^32`.
Reserving slot 0 also makes for a simple free-list terminator (`page_or_next == 0`), although this argument is not load bearing since the end of the free-list could also be indicated by a self-reference instead.

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
    last_written: u32,                  // the flush that last wrote the id; see below
    anchor: Option<StatementRef>,       // newest Shrink or Tombstone
    grow_witness: Option<StatementRef>, // Grow with n == size, if any
}
```

**A hash map, because the query is a point lookup.**
Nothing ever iterates the allocation map in id order at run time; consolidation's id-range sweep uses the fragment map, which is already ordered.

| field | updated when | read for |
| --- | --- | --- |
| `size` | any flush that resizes the id, writes past its end, frees it, or re-allocates it | answering `size()` in `O(1)`; bounding reads; supplying the extent of an id's last fragment |
| `fragment_count` | on every fragment created or destroyed for this id | [pricing description defragmentation](consolidation.md#description-defragmentation-rides-the-rotating-window) — description overhead relative to `size`. Counts `ZeroByDefault` fragments, which are real entries with real memory cost |
| `statement_bytes` | when a statement for this id is created, or when one's pins reach zero | pricing defragmentation, paired with `fragment_count` |
| `mentions` | `+1` per statement naming the id written or read at open; `−1` when one is physically dropped | one thing only: releasing a tombstone anchor's pin when the count falls to 1 |
| `last_written` | whenever the application's writes reach the id — a flush that writes, resizes, or allocates it | the age of [description defragmentation's candidates](consolidation.md#where-it-is-called-and-how-it-is-executed) |
| `anchor`, `grow_witness` | `anchor`: when a `Shrink` or `Tombstone` is written. `grow_witness`: when a `Grow` is written, and when `size` moves past its bound | moving the anchor pin; telling a consolidator that a victim page holds an anchor it must replace |

`size` is maintained **incrementally**, never recomputed: it is `max(anchor's n, Grow bounds and content extents above anchor_epoch)`, and every operation that could change it knows which term it changed.

**`last_written` measures content age, where an epoch measures location age.**
A statement's epoch [is its page's](#cost), so every restatement re-stamps it — the header re-stamps everything it holds on every flush — whereas `last_written` moves only when the application writes the id.
It counts flushes in 32 bits from a base the session sets at open, and a session that would outrun them rebases every entry in one pass.
At load it is seeded from the newest epoch among the id's statements, which can only understate the age, since restatements make epochs newer and never older; so it errs toward treating an id as recently written.

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

For every page: its page number, kind, epoch, and live-byte counter (**coverage**).

The counter is 32 bits rather than page-sized, because a page's live bytes exceed its capacity whenever several `Ref` statements claim the same bytes — which is legal, and [bounded](../spec/address-table.md#bounds) precisely so that 32 bits suffice.

Pages are bucketed by live fraction, a handful of buckets suffices, together with an age mark.
That makes victim selection `O(1)` rather than a priority queue's `O(log P)`, with `O(1)` bucket moves as counters change.
`Data` and non-header `AddressTable` pages are bucketed [separately](consolidation.md#the-structures-behind-the-ranking), and header pages in neither.
The header is excluded here since it is rewritten every flush, so it is never a victim, and its content is accounted for by being [taken and stated again by every flush](consolidation.md#one-dirty-set-and-why-statements-are-derived-last) rather than by its coverage.

The in-memory state of an `AddressTable` page carries one more field: its parent's page number, four bytes, updated whenever the parent is replaced.
[Unlinking an emptied page](consolidation.md#unlinking-an-emptied-page) needs it, since a page may be named by an interior page rather than by the header.

An ordered set tracks reusable pages under the [two-generation quarantine](../spec/durability.md#the-reuse-rule), handing out the lowest first: a page dropped by commit `E` becomes writable in flush `E + 2`.

## Derived structures

Two more, rebuilt at open and never persisted:

- **The id allocator** — the next fresh id, plus the recyclable set described above. See [Id recycling](id-recycling.md).
- **The eviction clock** — over the fragments the header states, recording how many flushes each has gone untouched. See [The flush](flush.md#the-header-as-write-buffer).

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
Every key range the flush has taken, kept merged and sorted, and per touched id a record of what the fragment map cannot say: the size and existence the id had when the flush began, whether it was freed, and whether a page the flush retires held its anchor or grow witness.
The cut [derives every statement](consolidation.md#one-dirty-set-and-why-statements-are-derived-last) from these two.

**Heat** rides on the pending entries rather than on statements, since statements are derived afresh every flush: a statement derived from several fragments takes the hottest of them.

## Cost

Maintenance of all of the above costs `O(log F)` per fragment created or destroyed, and a flush creates or destroys at most a small multiple of the statements it writes.

**Epochs belong to pages, not to statements.**
A statement inherits the epoch of the page it currently sits in, so moving a statement re-stamps it.
The header is rewritten every flush and states resolved truth, so everything in the header always carries the current epoch, and multi-epoch disagreement can exist only between the header and evicted leaves, or between two leaves.
It also means an epoch says when a statement was last *moved*, not when its content was last written, which is why the allocation map keeps [`last_written`](#3-the-allocation-map) as well.
