---
title: Journal semantics
---

What a transaction records, and what a flush may do with the recording.

**Status: design note.** Not implemented.
The current `JournaledWriteBackend` contradicts this document in three places and is known-broken.

## What is broken, and why it is one bug

The current implementation keeps **two** records of a transaction:

- `pending: HashMap<Pointer, Size>` — a *state snapshot*.
  Unordered, last-writer-wins, self-annihilating.
  Allocate, free, resize, and sizedness conversion all fold into it.
- `journal: Vec<(Pointer, Size, Vec<u8>)>` — an *operation log*.
  Ordered, append-only, replayed verbatim.

No order relates the two.
So any operation that invalidates an earlier log entry must reach into the log and repair it by hand — and exactly one does.
The sizedness conversion scans the log re-anchoring writes; nothing else repairs anything.

The consequences:

1. **`alloc → write → free` panics at flush.**
   The allocate and free annihilate inside `pending`, so the id is never claimed.
   The buffered write survives in the other structure and its replay looks up an id the heap has never heard of.
2. **A write followed by a shrinking resize silently corrupts a neighbour.**
   The seek does no bounds check, so a write buffered while the allocation was large replays at an offset that now lies outside it.
   Reproduced: allocate A at 128 bytes, write `0xAA` at offset 64, shrink A to 64, allocate B at 8 bytes.
   After the flush, B reads back as `[170; 8]`.
3. **A sizedness conversion of an allocation claimed by an earlier flush loses its content.**
   The immediate path is *mint → allocate new → copy `min(old, new)` → free old*.
   The deferred path performs only the mint.
   The copy is not deferred or approximated; it is absent.

None of these is the fundamental problem.
The fundamental problem is that **the log does not record every mutation**, and it shows up without any annihilation at all:

> A value that allocates a child and writes the child's id into its own header emits a `write` the log keeps, and an `alloc` the log does not.
> Recovery replays the log, finds a live pointer in the parent, and that pointer names an allocation nothing ever created.

Two operations, no free in sight, and the result is a dangling pointer in recovered application data.

## The model

> **The log is the sole authority.**
> It is a totally ordered sequence of every mutation.
> Everything else is derived from it and may be discarded and rebuilt.

The log carries six record kinds, which is exactly the closure under *"an entry whose absence would make some other entry unreplayable or misinterpretable"*:

| record | why it must be logged |
| --- | --- |
| `Alloc(id, size)` | otherwise a write has no target and a serialized id dangles. Sizedness rides on the id, so it needs no field. |
| `Free(id)` | annihilation, and the id's release |
| `Resize(id, size)` | bounds for replayed writes; content destruction |
| `Convert(old → new, size)` | id identity across a sizedness conversion |
| `Write(id, offset, bytes)` | content |
| `Splice(id, offset, old_len, bytes)` | content, with a tail shift |

A future `Copy(src, …, dst, …)` joins the content group, and so does the proposed [`Move`](#proposed-the-move-record) below.

Two derived structures, and it matters that they are derived:

- **`pending`** — write-phase geometry, so id queries are `O(1)`.
  Discarded at checkpoint.
- **`Folded`** — geometry *and* content, built at flush from the log alone, consumed by the scheduler, then dropped.

`Folded` recomputes the geometry `pending` already had, and **that redundancy is the point**: comparing them on every flush is a consistency check that all three bugs above would have tripped.
It is asymptotically free, since the fold is already linear in log length and the comparison is linear in touched ids.

## Content semantics

A caller **must not assume anything** about the contents of any uninitialized part of an allocation.
Except for the first `min(old, new)` bytes after a resize, the content is arbitrary.

Truncation is therefore *allowed to* destroy content, not required to.
A shrink immediately followed by a growth may be optimized away entirely, and the re-grown region may legally hold whatever was there before.

This licenses most of the fold's rules.
Without it they would be unsound.

## Proposed: the `Move` record

**Status: sketch, not specified.**
Recorded here because it changes what the storage layer must support, and because it is the reason the address table can decline copy-on-write clones.

`Move(src_id, src_offset, len, dst_id, dst_offset)` relocates a range from one allocation to another, or within one, **without copying the bytes on disk**.

**It needs no new address-table statement.**
The flush emits a `Ref(dst_id, dst_offset, len, address)` pointing at the bytes where they already lie, and denies the source's claim in the same epoch; the old `Ref(src_id, src_offset, len, address)` is shadowed by that denial, loses its fragments, and dies.
So the on-disk format is untouched, and `Move` is a log record and API operation rather than a format addition.

**It keeps the byte-referenced-once invariant**, which is the point.
That invariant forbids two *live* `Ref`s over one byte, and a move transfers rather than shares: the new claim and the old claim's denial land in the same epoch, so the two are never live together.
Data-page coverage therefore stays a plain counter — the source fragment's destruction subtracts the length, the destination fragment's creation adds it back.
One ordering caution: add before subtracting, or net the two, so coverage never transiently reads 0 for a page whose last live extent is being moved, which a flush that frees pages part-way through could otherwise hand to the free list.

**What it buys.** Rope- and B-tree-like structures shuffle ranges between neighbouring nodes constantly, and without `Move` every one of those is a byte copy.
With it, a split or merge that donates a suffix costs one `Ref` and one `Shrink`.
This is what lets [the address table](../../../spec/drafts/address-table.md#design-directions-and-trade-offs) decline copy-on-write clones: cloning would then be needed only for genuine simultaneous sharing — snapshots and deduplication — and not for moving data between neighbours.

**Open questions**, none of them settled:

- **Which moves to allow.** A tail move is the cheap case, since a suffix donation leaves the source needing only a `Shrink(src_id, src_offset)` and shifts no offsets. A head or interior move is where the cost sits, because closing the gap shifts every later offset in the source. Restricting the record to head and tail moves may be worth it; it has not been decided.
- **What becomes of the source range.** Three candidates, not interchangeable:
  - `Undefined(src_id, src_offset, len)` — one statement, no shifting, but the source keeps its size and carries a hole until defragmentation pays it down.
  - `Shrink(src_id, src_offset)` — available for a tail move only, and then the cheapest of the three.
  - a splice closing the gap — `O(fragments behind the splice point)` restated statements, which [the address table](../../../spec/drafts/address-table.md#design-directions-and-trade-offs) identifies as this design's weak spot.

  Allowing all three and letting the caller's shape pick is the likely answer, but the choice interacts with the previous question and has not been worked through.
- **Overlapping self-moves.** With `src_id == dst_id` and overlapping ranges, the new `Ref` and the denial cannot both name the overlap in one epoch, per [no conflicts within each epoch](../../../spec/drafts/address-table.md#no-conflicts-within-each-epoch), so the denial must cover `source \ destination` only.
- **Folding.** A move is a content operation with a geometry side effect on *two* ids, so it touches two entries of the state machine at once, as `convert` already does. The rules for annihilating a move against a later free, resize, or overwrite of either id are not written.
- **Cleaner heuristics.** [The CoW design](../../../spec/drafts/cow.md)'s cost-benefit victim selection assumes a data page's residents are the chunks that one flush wrote together. After moves, residents belong to ids that never wrote them, so the age signal gets noisier. That assumption is documented as degrading gracefully, so this is a note rather than an objection.

## One id, one lifetime

> **A counter returns to the id pool only at checkpoint**, after the frees are applied.

This is a precondition, not an optimization.
The log and both derived structures are keyed by id, so if a counter could be recycled mid-transaction then

```
Alloc(id), Write(id, …), Free(id), Alloc(id), Write(id, …)
```

would place two allocation lifetimes under one key.
The fold cannot emit two claims for one key, and the first write would be attributed to the second allocation.

It also makes `Freed → New` unreachable rather than merely discouraged, which is what keeps the state machine below finite.

## The `pending` state machine

Five states, of which two are "absent" and must be distinguished:

| state | in `pending`? | live in heap? | checkpoint action |
| --- | --- | --- | --- |
| `Absent/live` | no | **yes** — claimed earlier, untouched this transaction | nothing |
| `Absent/dead` | no | no — never minted, or freed and checkpointed | nothing |
| `New(S)` | yes | no | allocate |
| `Resized(S)` | yes | yes | resize |
| `Freed` | yes | maybe | free **if live**, then recycle the counter |

`New` and `Resized` are distinct because they call different heap methods, not because they carry different data.

### Transitions

| op | from | to | note |
| --- | --- | --- | --- |
| `alloc` | `Absent/dead` | `New(S)` | the only source state |
| `write` | any live state | **unchanged** | content only; `pending` is geometry |
| `resize` | `New(S)` | `New(S′)` | |
| | `Absent/live` | `Resized(S′)` | |
| | `Resized(S)` | `Resized(S′)` | |
| | `Freed`, `Absent/dead` | — | error |
| `convert` | old: any live | old: `Freed` | **two entries at once** |
| | new: `Absent/dead` | new: `New(S′)` | fresh id, fresh counter |
| `splice` | as `resize` | as `resize` | size change plus content ops |
| `free` | `Absent/live`, `Resized(S)` | `Freed` | |
| | `New(S)` | `Freed` | **not `Absent`** |
| checkpoint | `New`, `Resized` | `Absent/live` | map cleared |
| | `Freed` | `Absent/dead` | counters recycled here, and only here |

`write` earns a row precisely because it is a no-op on the map.
If `write` ever needs to touch `pending`, something has gone wrong.

Freeing a `New` entry goes to `Freed`, not `Absent`.
Going to `Absent` loses the information that the counter must be recycled — that is the current counter leak.
Making the checkpoint's free lookup-guarded lets one state serve both cases without a fourth variant.

Most error cells are statically unreachable, since freeing consumes the owned handle.
The escape hatch is `resolve`, which can mint a second owner — so those cells *are* reachable through a duplicate handle and do need runtime checks.
Query order is therefore `pending` first, heap second; never trust the typestate alone.

## Open: checkpoint versus commit

Undecided, and consequential.

- A **checkpoint** applies buffered work so the in-memory log can be released.
  Triggered by resource pressure; lands wherever that pressure falls.
- A **commit** is a boundary the recovered state may snap to.
  Triggered by the application's notion of a completed change.

Today they are the same event, which gives atomicity for free.
The intended model separates them: application authors never call flush, and when the log grows too long the guard method that would overflow it checkpoints automatically.
A flush triggered by buffer size cannot also be a durability boundary, or durability would be set by how much memory the log happens to use.

Three consequences follow immediately:

**Flush takes `&self`.**
The auto-checkpoint fires from inside a guard method, which holds a shared reference; there is no exclusive one in reach.
This is safe — callers hold ids and locations, never addresses, and writes resolve addresses at write time, so relocation under a live guard is transparent.
Note that it is a *reentrant* call, so no write method may hold the interior borrow across a callback.

**The trigger belongs at guard-method boundaries**, not at append time.
A check on append can fire between two writes of one logical mutation.
A guard method is the natural atomic unit, being the smallest thing an application author perceives as one change.

**Flush splits into two knobs**: fold-and-apply, which bounds memory, and truncate-the-log, which is only safe up to the last commit.

What is *not* decided is how uncommitted data is kept out of the recovered state — write-ahead logging with undo, or checkpointing only committed prefixes.
See [Crash consistency](crash-consistency.md).
