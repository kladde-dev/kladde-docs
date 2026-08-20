---
title: Fold and schedule
---

How a flush turns a log into the minimum work that reproduces it.

**Status: design note.** Not implemented.
Read [Semantics](semantics.md) first.

This is a small optimizing compiler, and it should be built like one: oracle first, optimizer second.

## Phase A — the fold

Per id, linear, no graph.
For each id the log touches, walk its op subsequence maintaining whether it exists, its current size, and a **piece table** over its bytes.

```rust
enum Source {
    Literal(ArenaPos),      // bytes buffered in the log's byte arena
    Storage(Id, Offset),    // must be read from the file
    Undefined,              // uninitialized; may be anything
}
```

A piece table is a sorted map from **segment start offset** to `Source` — one entry per segment boundary, never per byte.
A byte's origin is found by taking the greatest key ≤ the offset and advancing that source by the difference.
`Source` is offset-relative precisely so that advance is meaningful.

The distinction that drives everything:

- **Fresh** ids — an `Alloc` appears in this log — start `{0 → Undefined}`.
  Their content is *fully symbolic*; nothing about them ever needs to be read.
- **Persistent** ids — live from an earlier checkpoint — start `{0 → Storage(self, 0)}`.

Every table therefore begins with exactly **one** entry, which is what makes the representation below natural rather than bolted on.

### Per-op rules

| op | effect on the table |
| --- | --- |
| `Alloc(s)` | fresh table, `Undefined` over `[0, s)` |
| `Write(off, bytes)` | overwrite `[off, off+len)` with `Literal` |
| `Resize(s′)` | clip to `s′`, or extend with `Undefined` |
| `Splice(off, old_len, new)` | overwrite, then **shift the suffix segments** |
| `Copy(src, …)` | overwrite the destination range with pieces taken from *src's current table* |
| `Free` | mark released |

Overwrite is the only non-trivial primitive: split at both boundaries, drop the entries strictly inside, insert the new one.

**Coalesce on insert.**
A loop of sequential writes produces adjacent literals contiguous in the arena, and without merging you get one entry per write where one per run would do.
This is not a micro-optimization; it is what keeps the common case at one or two segments.

### What falls out unasked

- **A splice on a fresh allocation costs zero I/O.**
  It is a table edit; the shift never touches bytes.
  Since strings and vectors splice in loops, this is probably the largest single win available.
- **A transient allocation — fresh and freed in the same log — never touches storage.**
  Recycle its counter and emit nothing.
- **An identity piece emits nothing.**
  `Storage(self, k)` with `k` equal to the segment start means the bytes are already where they belong.
  Without this check, every untouched region of every persistent allocation would emit a self-copy.

## Phase B — the schedule

Vertices are the **emitted actions**, not the log's ops — the fold changed the op set:

```
Claim(id, size)          Release(id)
Reshape(id, size)        Transfer(src, src_off → dst, dst_off, len)
Write(id, off, bytes)
```

**`Reshape` stays one vertex.**
It is tempting to decompose it into claim/transfer/release — that decomposition is a good *explanation* of why conversion and multi-transaction resize are the same latent hole — but it is the wrong granularity here.
The heap decides the new placement and moves the bytes as one atomic operation; splitting it would put the old range's release into the schedule as a separate vertex, and a frees-first pass could then hand that range to another claim before the copy ran.

### Edges

1. `Claim(id)` → every action writing into `id`.
2. A transfer reading `id` → `Release(id)`.
3. A transfer reading a range → later actions writing that range.
   A write-after-read anti-dependency, easy to forget because it runs backwards from the usual intuition.
4. Earlier writes to a range → a transfer reading it.
   The fold has usually already turned these into literals, so this edge mostly evaporates.

**It is a DAG by construction**: every hard edge points from a lower log position to a higher one.

### Frees-first is a preference, not an edge

Running releases before claims gives the placement pass more space to work with.
It must be encoded as a **priority**, never as an edge — a conversion produces `Claim(new) → Transfer → Release(old)`, and adding `Release → Claim` as a hard edge makes that a three-cycle.

So: Kahn's algorithm with a priority queue over the ready set, in order

1. anything that unblocks a release,
2. releases,
3. claims, largest first,
4. everything else.

First-fit-decreasing therefore becomes *best-effort over the ready set* rather than global.
Only conversion-style chains block, but the guarantee is weaker than an unconditional sort and should be stated where it is relied on.

Compaction stays outside the graph as a final phase.

## Read hoisting, and why the graph may be unnecessary

### A worked example

After an earlier checkpoint the heap holds `A1` at address 1000 size 256, and `A2` at address 2000 size 128 — both persistent.
This transaction logs:

```
1.  Copy(src = A1, src_off = 0, len = 64, dst = A2, dst_off = 32)
2.  Free(A1)
3.  Alloc(A3, 200)          // 200 ≤ 256, so it would fit in A1's gap
4.  Write(A3, 0, [64 bytes])
```

Actions: `Transfer(A1@0 → A2@32, 64)`, `Release(A1)`, `Claim(A3, 200)`, `Write(A3, 0, …)`.
Edges: `Transfer → Release(A1)` and `Claim(A3) → Write(A3)`.

**With a naive priority**, the ready set is `{Transfer, Claim(A3)}` — the release is blocked — and claims outrank transfers, so `Claim(A3)` runs first.
At that instant **A1 is still live in the heap**, so the gap at 1000 does not exist and A3 cannot be placed there.
A3 lands elsewhere and compaction claws the space back later.
Note the scheduler did not *postpone* the free; it ran it as early as the dependencies allowed, and that was not early enough.

**With priority rule 1** — prefer actions that unblock a release — the transfer runs first, the release becomes ready and top-priority, the gap at 1000 opens, and `Claim(A3, 200)` lands there.
A3 reuses A1's space with no address-level reasoning at all.

This is why the priority queue beats a fixed phase order.
A fixed order cannot express it: if A2 were fresh, the correct sequence interleaves as `Claim(A2) → Transfer → Release(A1) → Claim(A3)`.

### The aggressive alternative, and why not to build it

One could instead free A1 first, let A3 be placed over its address range — the free moves no bytes, so A1's content is still physically there — and only then ensure the read happens before those bytes are overwritten.

It is sound, and it needs: lowering the graph from ids to **addresses**; inserting edges **during execution**, when a claim picks an address that overlaps a pending transfer's source; and overlapping-move care.
It does not deadlock, since a dynamic edge always points at an action that was only just enabled.

But in this scenario it produces exactly the same reads, writes, and placement as the priority fix.
It wins only when the free must precede the read for placement reasons the priority cannot reach.

### Hoisting

At fold time, if a `Storage(src, …)` piece references a range this log will **disturb**, read those bytes immediately and turn the piece into a `Literal`.
Disturbed means any of:

- `src` is released this log,
- the range is written this log,
- `src` is reshaped this log, since it may relocate.

All three are known during the fold, which already runs at flush with full read access to storage.

Applied to the example: A1 is released, so A2's middle piece becomes a literal during the fold.
The transfer disappears, the release loses its only predecessor, and frees-first applies unconditionally.
Same result, and the only cost is 64 buffered bytes — bytes that were going to be read anyway.

**Hoisting every disturbed read collapses the DAG.**
Every surviving transfer then reads a range that is not released, not written, and not reshaped, and its source stays live in the heap so no claim can be placed over it.
The only remaining edge is `Claim(dst) → Transfer`, which the fixed phase order *releases → claims → reshapes → transfers and writes* already satisfies.

So the recommendation is to **build hoisting first and skip the graph entirely**.
It is dramatically simpler and it is correct; its only cost is memory proportional to the volume of disturbed reads, which for kladde's containers is small.
Build the scheduler when a workload shows the buffering hurting.

The weakest point is that the hoist condition is conservative on *reshaped*: an allocation whose size changed might not actually relocate, but the fold cannot know before placement runs.

## The content-blind fast path

Set a flag when any op produces a piece `Storage(other_id, …)` with `other_id` not equal to the id itself.
That is `Copy` and conversion-on-a-persistent-id, and nothing else — in particular **not** plain resize, and **not** splice, whose source and destination are the same id.

When the flag is clear at flush — the overwhelming majority of transactions — run *releases → first-fit-decreasing claims → reshapes → writes* with no graph, no hoisting, and no per-piece analysis.

### Representing a piece table cheaply

Most touched allocations have exactly one segment, so a tree each is wasteful:

```rust
enum Content {
    Uniform(Source),   // one segment over [0, size) — no allocation at all
    Spilled,           // entries live in the flush-wide map
}
```

`Uniform` sits inline in the `Folded` entry and covers freshly-allocated-and-fully-written, untouched-persistent, and grown-but-unwritten.
On the second segment, spill into **one** flush-wide map keyed by `(Id, Offset)`.
Zero tree allocations in the common case, exactly one in total otherwise.

This lives in `Folded`, not in `pending` — `pending` stays geometry-only and write-phase-only.

## Correctness: the differential oracle

Build the oracle before the optimizer.

Keep the naive in-order replayer as a reference implementation, and differential-test the optimized flush against it: same log, then compare the entire observable state — every live allocation's geometry and bytes, with `Undefined` ranges **masked out**, since comparing them would reject legal schedules.
Randomized logs with shrinking.

Two properties, asserted separately because they fail differently:

- **fold agreement** — `Folded`'s geometry equals `pending`.
  Cheap enough to leave enabled outside tests.
- **schedule equivalence** — the differential comparison above.

## Implementation order

1. **Log every mutation.**
   Fixes the recovery hole; the only step that is not optional.
2. **`pending` as a delta** over the heap's committed table.
   Fixes the post-checkpoint query and mutation holes, and the counter leak.
3. **The differential oracle**, before any optimization.
4. **The fold**, with the `Uniform`/spill representation and the fold-agreement assertion.
   All three known bugs become consequences of the per-piece bounds rather than three separate fixups.
5. **Read hoisting** and the content-blind fast path.
   At this point splice and conversion become implementable, and copy becomes addable.
6. **The scheduler** — only if buffering measured in step 5 proves too expensive.
   It may never be needed.
