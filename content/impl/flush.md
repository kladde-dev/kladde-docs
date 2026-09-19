---
title: The flush
---

How a journal segment becomes pages: what to write, where to put it, and in what order.

The [protocol that commits a flush](../spec/durability.md#the-flush-protocol) is normative; everything about *what* a flush chooses to write is not, and is described here.

This is a small optimizing compiler, and it should be built like one: **oracle first, optimizer second**.

## Phase A — the fold

Per id, linear, no graph.
For each id the segment touches, walk its record subsequence maintaining whether it exists, its current size, and a **piece table** over its bytes.

```rust
enum Source {
    Literal(ArenaPos),      // bytes buffered in the journal's byte arena
    Storage(Id, Offset),    // must be read from the file
    Zero,                   // resolves to zero; occupies no bytes anywhere
}
```

A piece table is a sorted map from **segment start offset** to `Source` — one entry per segment boundary, never per byte.
A byte's origin is found by taking the greatest key `≤` the offset and advancing that source by the difference.
`Source` is offset-relative precisely so that advance is meaningful.

The distinction that drives everything:

- **Fresh** ids — an `Alloc` appears in this segment — start `{0 → Zero}`.
  Their content is *fully symbolic*; nothing about them ever needs to be read.
- **Persistent** ids — live from an earlier flush — start `{0 → Storage(self, 0)}`.

Every table therefore begins with exactly **one** entry, which is what makes the compact representation below natural rather than bolted on.

### Per-record rules

| record | effect on the table |
| --- | --- |
| `Alloc(s)` | fresh table, `Zero` over `[0, s)` |
| `Write(off, bytes)` | overwrite `[off, off+len)` with `Literal` |
| `Resize(s′)` | clip to `s′`, or extend with `Zero` |
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
- **A transient allocation — fresh and freed in the same segment — never touches storage.**
  Recycle its id and emit nothing.
- **An identity piece emits nothing.**
  `Storage(self, k)` with `k` equal to the segment start means the bytes are already where they belong.
  Without this check, every untouched region of every persistent allocation would emit a self-copy.

### Representing a piece table cheaply

Most touched allocations have exactly one segment, so a tree each is wasteful:

```rust
enum Content {
    Uniform(Source),   // one segment over [0, size) — no allocation at all
    Spilled,           // entries live in the flush-wide map
}
```

`Uniform` covers freshly-allocated-and-fully-written, untouched-persistent, and grown-but-unwritten.
On the second segment, spill into **one** flush-wide map keyed by `(Id, Offset)`.
Zero tree allocations in the common case, exactly one in total otherwise.

## Phase B — placement

The fold says *what* each allocation's content is; placement says *where* the new bytes go.

### Cutting content across pages

A flush writes whole pages, so a dirty range must be cut into pieces that each fit in one page.
Two rules govern the cutting:

- **The writer prefers maximal pieces.**
  A long dirty range becomes `MAX_PAGE_CONTENT`-sized pieces in whole pages plus a remainder in a shared page; a small allocation is written as a single piece in a shared page.
  This is what keeps address-table statements compact.
- **Whoever rewrites bytes may re-cut them.**
  Piece boundaries carry no meaning beyond "these bytes are stored contiguously here", so a flush re-cuts the ranges it rewrites at will, and consolidation may split a piece it relocates — or merge adjacent pieces it relocates together — so that its target pages come out exactly full.

*Why free re-cutting rather than a fixed head/middle/tail scheme.*
The problem it solves is **worst-case consolidation**.
With rigid piece sizes, an application that only ever allocates `MAX_PAGE_CONTENT / 2 + 1` bytes pins every data page at ~50 % fill, and no amount of relocation can fix it, because the pieces cannot be made to fit.
One flexible cut per allocation only softens this — sizes just over half a page still strand ~25 % — and fixed-size middle pieces freeze a large allocation's page alignment at creation, so consolidation could never re-pack it without rewriting all of it.

Free re-cutting dissolves both problems at once: consolidation packs pages the way stock is cut rather than the way bins are packed — fill the page, cut whatever piece crosses the boundary, continue with its remainder in the next page — so fill approaches 100 % for *any* population of live bytes, and there is no alignment left to preserve because there is no grid.
The price is bounded and small: a packed page gains at most two boundary-crossing cut pieces, i.e. at most two extra statements, roughly 10–20 address-table bytes per ~4 KiB of data.

A flush writes every piece it produces **whole**, even when only one byte of the range changed.
This is not new cost in disguise: the operating system would have rewritten the surrounding page in place anyway.
What is new is that the old bytes remain behind as garbage until consolidation reclaims them.

### Contiguity is a preference, not an assumption

When a single flush writes many pieces of one allocation, it should place them in one run of reusable pages — a run is always available by growing the file — so that the statements collapse to few.
That covers bulk writes, whole-allocation copies, and compact-on-close.

It does *not* cover an allocation filled incrementally across many flushes, whose pieces land wherever each flush put them.
There the honest statement is that the description fragments in proportion to the number of flushes that touched the allocation, and that cross-flush contiguity is **consolidation's** job, not the flush's.

Uninitialized ranges help the sparse case for free: an allocation created large but written sparsely stores locations only for the ranges actually written, and occupies no data pages for the rest.

### The `Inline` threshold

The [format](../spec/address-table.md#statement-types) fixes the ceiling at 251 bytes; the policy threshold belongs far below it.

Below roughly the encoded size of a `Ref` (~10 bytes), inlining is strictly better — the pointer would be as large as the data.
A starting policy: inline everything up to ~64 bytes, plus anything whose eviction would leave a data page holding only scraps; then measure.

Between there and the ceiling it is a trade: inline payloads ride the header for free while hot but inflate the address table when cold, whereas a `Ref` plus its data bytes is written once but occupies a data-page slot and keeps that page partially live.
Note the accounting asymmetry that argues for keeping the threshold low: an inline's payload is charged to the address-table page that holds it and is only reclaimed by rewriting that page, whereas a `Ref`'s content is charged to a data page that consolidation can clean independently of the statement describing it.

**This is what makes small allocations first-class.**
Below the threshold, an allocation lives its entire life — creation, every update, deletion — as a few bytes of statements inside pages the flush was writing anyway, with no page-granularity amplification anywhere.
That is the cost profile a dynamically typed guest language, allocating by the thousands, needs.

## Phase C — ordering

Vertices are the **emitted actions**, not the journal's records — the fold changed the set:

```
Claim(id, size)          Release(id)
Reshape(id, size)        Transfer(src, src_off → dst, dst_off, len)
Write(id, off, bytes)
```

**`Reshape` stays one vertex.**
It is tempting to decompose it into claim/transfer/release — that decomposition is a good *explanation* of why conversion and multi-flush resize are the same latent hole — but it is the wrong granularity here: splitting it would put the old range's release into the schedule as a separate vertex, and a frees-first pass could then hand that range to another claim before the copy ran.

### Edges

1. `Claim(id)` → every action writing into `id`.
2. A transfer reading `id` → `Release(id)`.
3. A transfer reading a range → later actions writing that range.
   A write-after-read anti-dependency, easy to forget because it runs backwards from the usual intuition.
4. Earlier writes to a range → a transfer reading it.
   The fold has usually already turned these into literals, so this edge mostly evaporates.

**It is a DAG by construction**: every hard edge points from a lower journal position to a higher one.

### Frees-first is a preference, not an edge

Running releases before claims gives placement more space to work with.
It must be encoded as a **priority**, never as an edge — a conversion produces `Claim(new) → Transfer → Release(old)`, and adding `Release → Claim` as a hard edge makes that a three-cycle.

So: Kahn's algorithm with a priority queue over the ready set, in order

1. anything that unblocks a release,
2. releases,
3. claims, largest first,
4. everything else.

First-fit-decreasing therefore becomes *best-effort over the ready set* rather than global.
Only conversion-style chains block, but the guarantee is weaker than an unconditional sort and should be stated where it is relied on.

### Why the graph may be unnecessary

**A worked example.**
After an earlier flush the file holds `A1` (size 256) and `A2` (size 128), both persistent.
This segment records:

```
1.  Copy(src = A1, src_off = 0, len = 64, dst = A2, dst_off = 32)
2.  Free(A1)
3.  Alloc(A3, 200)          // 200 ≤ 256, so it would fit in A1's space
4.  Write(A3, 0, [64 bytes])
```

With a naive priority the ready set is `{Transfer, Claim(A3)}` — the release is blocked — and claims outrank transfers, so `Claim(A3)` runs first while `A1` is still live, and `A3` lands elsewhere.
Note the scheduler did not *postpone* the free; it ran it as early as the dependencies allowed, and that was not early enough.
With priority rule 1, the transfer runs first, the release becomes ready and top-priority, and `A3` reuses `A1`'s space with no address-level reasoning at all.

**Hoisting.**
At fold time, if a `Storage(src, …)` piece references a range this segment will **disturb**, read those bytes immediately and turn the piece into a `Literal`.
Disturbed means any of: `src` is released this segment, the range is written this segment, or `src` is reshaped this segment (since it may relocate).
All three are known during the fold, which already runs at flush with full read access.

Applied to the example: `A1` is released, so `A2`'s middle piece becomes a literal during the fold.
The transfer disappears, the release loses its only predecessor, and frees-first applies unconditionally.
Same result, and the only cost is 64 buffered bytes — bytes that were going to be read anyway.

**Hoisting every disturbed read collapses the DAG.**
Every surviving transfer then reads a range that is not released, not written, and not reshaped, and its source stays live so no claim can be placed over it.
The only remaining edge is `Claim(dst) → Transfer`, which the fixed phase order *releases → claims → reshapes → transfers and writes* already satisfies.

So: **build hoisting first and skip the graph entirely.**
It is dramatically simpler and it is correct; its only cost is memory proportional to the volume of disturbed reads, which for kladde's containers is small.
Build the scheduler when a workload shows the buffering hurting.

The weakest point is that the hoist condition is conservative on *reshaped*: an allocation whose size changed might not actually relocate, but the fold cannot know before placement runs.

### The content-blind fast path

Set a flag when any record produces a piece `Storage(other_id, …)` with `other_id` not equal to the id itself.
That is `Copy` and conversion-on-a-persistent-id, and nothing else — in particular **not** plain resize, and **not** splice, whose source and destination are the same id.

When the flag is clear at flush — the overwhelming majority of segments — run *releases → first-fit-decreasing claims → reshapes → writes* with no graph, no hoisting, and no per-piece analysis.

## The header as write buffer

The root of the address table is the header page, and the header page is rewritten by every flush no matter what.
That page is free real estate, so it doubles as the address table's **write buffer**: every statement a flush produces lands in the header first, at zero additional page writes.

Consequences:

- The common small flush writes **no** address-table page beyond the header.
- The header always holds **resolved truth** at the current epoch, which is why a statement evicted from it needs no re-resolution, and why multi-epoch disagreement can exist only between the header and leaves, or between two leaves.
- A statement carried in the header becomes a *new* record at the new epoch each flush, so an anchor re-points and its pin moves every flush.
  Header pages are exempt from coverage-driven victim selection, since they are rewritten unconditionally.

When the header overflows, the coldest statements are evicted to a fresh leaf, ranked by an **eviction clock** recording how many flushes each has gone untouched.
Eviction and consolidation both emit statements sorted by id, so leaves *tend* to cover coherent id ranges — a soft property worth cultivating and not depending on, since it keeps consolidation windows aligned with page boundaries and keeps delta encoding dense.

## The shape of the tree

Address-table pages form a tree rooted at the header, and it is a **directory, not a search structure**: every lookup is served by the in-memory fragment map, nothing ever descends the tree by key, so there is no ordering invariant to maintain across pages — and a B-tree's ~25 % expected slack is precisely the price of maintaining that invariant under inserts.

Interior pages are packed full, in arrival order; at each level only the newest interior page is partially filled.

- While everything fits in the header: **zero extra pages** — the steady state of every small file.
- Overflow spills to leaves referenced directly from the header, with statements continuing to share the header alongside the child references.
  At ~3 bytes per child reference, a header can reference on the order of a thousand leaves, i.e. a few MiB of statements, before this stops sufficing.
- Beyond that, one layer of interior pages reaches roughly a million leaves; deeper is not going to be needed.

Structural updates are copy-on-write path copies, but the paths are short and the root is free: adding or removing a leaf rewrites at most `depth − 1` pages besides the leaf itself.
Because interior pages are plain arrays of page numbers, removing a leaf's entry compacts its interior page for free during that rewrite.
A depth change is a single-page event: when the header's child list outgrows its budget, move the whole list into one fresh interior page and reference that instead.

## Correctness: the differential oracle

Build the oracle before the optimizer.

Keep a naive in-order replayer as a reference implementation, and differential-test the optimized flush against it: same segment, then compare the entire observable state — every live allocation's geometry and bytes, compared in full.
Nothing is masked out: every byte of a live allocation has exactly one right answer, so a schedule that differs from the replayer anywhere is wrong.
Randomized segments with shrinking.

Two properties, asserted separately because they fail differently:

- **fold agreement** — the fold's geometry equals the write-phase geometry the mutation path already tracked.
  Cheap enough to leave enabled outside tests.
- **schedule equivalence** — the differential comparison above.

## Implementation order

1. **Log every mutation.** Fixes the recovery hole; the only step that is not optional.
2. **Write-phase geometry as a delta** over the committed state.
3. **The differential oracle**, before any optimization.
4. **The fold**, with the `Uniform`/spill representation and the fold-agreement assertion.
5. **Read hoisting** and the content-blind fast path.
   At this point splice and conversion become implementable, and copy becomes addable.
6. **The scheduler** — only if buffering measured in step 5 proves too expensive.
   It may never be needed.

## Walk-through of an epoch

A finer-grained version of the [recovery case analysis](../spec/durability.md#case-analysis-for-a-power-cut-during-flush-e), tracking page states step by step.
This exists because two points in the argument are easy to get backwards: most pages of the older world are also in the newer one and therefore simply live, and a header slot whose write never arrived holds a *valid* stale header rather than garbage.

**During the journaling phase of epoch `E`:**

- Header `E − 2` exists and has been fsynced.
- Header `E − 1` was written but not fsynced yet.
- Journal segment `E` is partially written, not fsynced.
- All `Data` and `AddressTable` pages of world `E − 1` have been fsynced and are **live** — this flush must not overwrite them.
- Pages of world `E − 2` that are *not also* part of world `E − 1` are **fallback**, and must not be overwritten either.
  This includes journal segment `E − 1`, which header `E − 2` names.
  Pages belonging to both worlds are simply live, which is the common case rather than the exception: typically most pages of world `E − 2` are in both.

**Steps 1–7** of [[spec/durability#The flush protocol|the flush protocol]] write data and address-table pages for epoch `E`, then fsync.
No header page is touched.
On a power cut during or after this:

- *If the slot written by flush `E − 1` holds header `E − 1`* — guaranteed once fsync `E` completed, possible earlier — state `E − 1` is recovered, the start page of journal `E` is found from that header, and a possibly-empty prefix of journal `E` is replayed.
- *If that slot does not hold header `E − 1`* — either its write tore, leaving an invalid CRC, or it never reached the disk, leaving the stale header `E − 3` with a **valid** CRC, which is the more common sub-case — then header `E − 2` has the highest epoch among valid headers and governs.
  Recovery reaches state `E − 2` and replays journal `E − 1` in full, arriving at the same state as the other case with an empty journal-`E` prefix.
  The lost journal-`E` transactions were never covered by a completed fsync, so this is within the guarantee.

Recovery is indifferent between the torn and never-arrived sub-cases, because it selects the valid header with the *highest epoch* rather than merely a valid one.

**Step 8** overwrites header `E − 2` with header `E`.
This turns journal `E − 1` and everything reachable only from header `E − 2` from fallback to **reusable**, and it turns everything reachable from `E − 1` but not from `E` from live to **fallback** (including journal `E`).
On a power cut during or after:

- *If the slot holds header `E`*: the flush completed; recover to state `E` plus whatever prefix of journal `E + 1` accumulated.
- *If it does not*: header `E − 1` exists with a valid CRC, because it was fsynced in step 7, and carries the highest epoch among valid headers.
  Same situation as above, except that header `E − 2` may no longer exist — which is fine, since it is only consulted on the path where header `E − 1` is absent.
