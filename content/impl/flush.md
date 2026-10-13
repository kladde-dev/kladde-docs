---
title: The flush
---

How the journal becomes pages: what to write, where to put it, and in what order.

The [protocol that commits a flush](../spec/durability.md#the-flush-protocol) is normative; everything about *what* a flush chooses to write is not, and is described here.
It is a small optimizing compiler, and it should be built like one: **oracle first, optimizer second**.

**A flush folds the journal into the new sequence, maps it onto spans, lets the span policy merge what it wants merged, places new bytes in data pages with consolidation's victims riding along, cuts the span table into the header and leaves, and commits.**

## The fold

**The fold keeps a piece table over the sequence, starting as one piece, the committed sequence, and the journal's records edit it.**
A piece is a run of the new sequence and where its bytes come from: the committed sequence, from some position on, as it stood after the last flush, or the journal's byte arena, from some position on.

```rust
enum Source {
    Committed(u64),   // the committed sequence, from this position on
    Arena(u64),       // the journal buffer's arena, from this position on
}

struct Piece { src: Source, len: u64 }
```

The pieces sit in a counted tree, as the [rope](in-memory-state.md#2-the-rope) does over spans, so each record costs `O(log p)` for the `p` pieces the journal made, however long the sequence:

| record | effect on the piece table |
| --- | --- |
| `Write(pos, bytes)` | take `[pos, pos + len)` out, as a cut; insert one `Arena` piece |
| `Splice(pos, removed, bytes)` | take `[pos, pos + removed)` out, as a cut if it is not empty; insert one `Arena` piece |
| `Move(from, len, to)` | take `[from, from + len)` out and insert it at `to` |
| `Paste(pos, removed, cut)` | take `[pos, pos + removed)` out and drop it; insert the cut's tree |

Taking a range out is two splits and a join, and returns the range as a tree of its own; inserting is a split and two joins.
**A transaction's cuts are kept as those trees until the transaction ends**, so a paste inserts the pieces its cut removed and the flush states three to six spans for the two, however large the value, and writes none of its bytes; replay at open does the same.
The cuts of a transaction are freed when the next transaction starts, or the fold ends.

At the end, the fold lists the new sequence as pieces, merging adjacent pieces that continue each other.
A record whose range does not lie inside the sequence, or a paste of a cut its transaction did not make or already pasted, makes the journal invalid.

**What falls out unasked:**

- **A splice that lands in bytes the same flush wrote costs nothing beyond them**: it edits `Arena` pieces, and the bytes it shifts are never mentioned.
- **A move costs no bytes**: its pieces are committed ranges, which keep the spans that show them.
- **Repeated writes to one place cost one write**: only the last one's bytes survive in the pieces.

## Mapping pieces to spans

**A committed range is shown by the spans that showed it, found through the rope; where a range begins or ends inside a span, the span is split**: its left part is stated again with a shorter length, and its right part becomes a new span over the same bytes.
New bytes become new spans, inline below the [threshold](#the-inline-threshold) and placed in data pages above it.
Every span is linked to the one that now follows it, and every span whose content, length or successor changed is stated, with every new one.

So a splice cuts the chain at its two ends and adds the span of its new bytes: one to three entries.
A move cuts it at three places — the start and the end of the moved run, and its destination — and splits at most one span at each; the three left parts are also exactly the spans whose successors change: the one before the run now links to what followed it, the run's last span to what follows the destination, and the span before the destination to the run's first span.
Where a place is the sequence's start, the header's first span changes instead, which costs no entry, and where a place already falls between two spans, its split goes; so a move states three to six spans, however many it moves.
Every span inside a moved run keeps its entry, since neither its bytes nor its successor change.

Spans that no chain reaches any more are dropped: their ids are free, and their entries dead.
The [reach rule](../spec/span-table.md#rules-for-writers) holds by construction, since a span becomes reached only as a new span, or as a span whose entry the flush writes anyway.

### Overwrites: split or rewrite

**An edit that cuts into a span can split it, or write it again whole with the edit's bytes, and the flush rewrites a `Ref` shorter than a threshold and splits a longer one.**

- **Splitting** costs three entries the first time a place is written, and one for every later overwrite of the same bytes, which restates the middle span; it leaves the description fragmented until [the span policy](#the-span-policy) merges the pieces.
- **Rewriting** costs one entry and the span's length in data bytes, and fragments nothing; it suits short spans, and is costly for long ones.

So a drag that moves a point many times splits its span once and restates an inline span of a few bytes at every later flush.
**An `Inline` span is always rewritten**, since splitting one restates its whole payload as well, in two or three entries instead of one.
The threshold applies to the span an edit cuts into, not to the parts a cut leaves: a `Ref` long enough to be split leaves a left part that may be shorter than the threshold, which is stated as it is, and the span policy merges it later.
A third way, shadowing part of a span with an entry of its own, is [left out of the format](../spec/span-table.md#design-directions).

### Zeros

**A run of zeros among the new bytes, long enough that a `Zero` entry is cheaper than the bytes, becomes a `Zero` span**: 32 bytes is a reasonable cut.
Encodings without padding hold few zeros, but `Fixed` values and large preallocated arrays have them.

## The span policy

**Two policies bound the description, and they combine: one bounds it in the leaves at a constant cost per edit, the other drives it toward nothing where the sequence has gone cold; spans whose entries sit in the header are exempt from both.**

- **Bounded spans**, the discipline of a B-tree's leaves without the interior nodes: no two neighbouring spans whose entries lie in leaves are shorter than `L` together.
  A flush keeps the rule after its fold has been mapped to spans and before it places anything: it writes the spans that break it again as one, among its own new bytes, which costs at most `L` bytes per boundary removed and never cascades, since a merged span is longer than either neighbour was.
  The mean span in the leaves is then at least `L / 2` long, everywhere and at all times.
- **Merging by temperature**: a run of spans whose content has not been written for a while is written again as spans of up to a page, by [the rotating walk](consolidation.md#merging-spans-rides-the-rotating-walk).
  Each boundary is removed once, after the edits that made it, and a cold span of a page costs about six bytes, 0.15 % of what it shows.
  It needs a temperature per span, which [heat](in-memory-state.md#heat) gives.
- **The header is exempt**: spans of any length may keep their entries there.
  An edit site that every flush touches — typing, dragging a shape, moving a slider until a setting sounds right — splits its spans while it is hot, and merging them would write the same bytes again at every flush, only for the next edit to split them anew.
  The header can afford them, since it holds the hottest entries, is written by every flush anyway, and holds at most a page of entries, so the exemption adds at most a page to the description, however large the file.

**The bound takes effect where entries leave the header**: when the header would overflow, the flush chooses its coldest runs of spans to evict to leaves, by heat, and merges each run's short spans as it evicts them, so their bytes are written again once, after they have gone cold.
Since merging writes bytes, the flush makes that choice before it places anything, from entry sizes it estimates without encoding them.

The first policy gives the worst-case bound of [the guiding principle](index.md#overhead-and-work-independent-of-size), the second the low overhead of a large file that is mostly cold, and the exemption keeps the first from charging hot edits for merges that the next edit undoes.
A larger `L` makes the description smaller and each edit more expensive.
Neither touches the format, which allows spans of any length from one byte to a page.

**The constants**, as [measurements](../evaluation/one-sequence.md#the-span-policy) chose them for kladde-rs: the bound `L`, 64 bytes; the length below which a `Ref` cut by an edit is rewritten rather than split, 64 bytes; the inline threshold, 64 bytes; the flushes after which a span counts as cold for the walk, 16; and the walk's price `μ`, 0.02 entry bytes per byte written.
They were measured on a handful of drawings and texts, and remain open questions, as do what should count as cold, and how hot spans and the header should share the span table's room.

## The `Inline` threshold

The [format](../spec/span-table.md#entries) fixes the ceiling at 126 bytes; the policy threshold belongs below it.

Below roughly the encoded size of a `Ref`, inlining is strictly better — the address would be as large as the data.
Between there and the ceiling it is a trade: inline payloads ride the header for free while hot but inflate the table pages when cold, whereas a `Ref` plus its data bytes is written once but occupies room in a data page and keeps that page partially live.
The accounting argues for keeping the threshold low: an inline payload is charged to the table page that holds it and is only reclaimed by rewriting that page, whereas a `Ref`'s bytes are charged to a data page that consolidation can clean apart from the entry.
64 bytes is a good default.

## Placement

### Cutting content across pages

A flush writes whole pages, and a `Ref` shows bytes of one page only, so new bytes are cut into spans that each fit in one page.

- **A run longer than a page becomes whole pages first**, each one span, plus a remainder; that is the one cut that is forced rather than chosen.
- **The remainders are packed in sequence order with a bounded look-ahead**, pulling consolidation's victims into any page with room its own content cannot use, and cutting only to keep a page from closing more than `θ` empty; [packing](consolidation.md#packing-in-sequence-order-with-look-ahead) is described with consolidation, since what it has to know is consolidation's side of the interface.
- **Whoever writes bytes may cut them anew**: span boundaries carry no meaning beyond "these bytes lie contiguously here", so a flush re-cuts what it writes at will, and consolidation may split or merge spans it relocates.

Free re-cutting removes the worst case of fixed-size pieces: chunks all just over half a page could otherwise never share a page.
The price of each cut is one entry, and in ordinary flushes the packer [prefers slack to a split](consolidation.md#slack-is-cheaper-than-a-split--up-to-a-point).

A flush writes every span it produces **whole**; the old bytes remain behind as garbage until consolidation reclaims them.

**A large payload is written twice**: once into the journal when its record is appended, and again into data pages when the flush places it — a pasted image, say, or a document the application built outside any guard and brought in whole.
A flush could instead state spans over the payload's bytes where they already lie, in the journal's pages, turning those pages into data pages, which would halve what a large paste writes; it would need journal pages that hold whole pages of a payload aligned to the data pages' content, and the [reuse rule](../spec/durability.md#the-reuse-rule) to hold for pages that change kind, and is an open idea.

### Page contiguity buys nothing

**Adjacent pages need exactly as many entries as scattered ones**, because a `Ref` [shows bytes of one page only](../spec/span-table.md#bounds), so a bulk write of many pages is one span per page wherever those pages are.
Placement therefore does not seek runs of pages at all; it takes the lowest reusable page, which is what keeps [outer fragmentation](consolidation.md#outer-fragmentation) from forming.

### Where the pages come from

The reusable pool is an ordered set, a list, and a watermark, because the [quarantine](../spec/durability.md#the-reuse-rule) is a rotation rather than a computation:

```rust
struct FreePages {
    ready:    BTreeSet<PageNumber>,   // reusable: neither live nor fallback
    retiring: Vec<PageNumber>,        // dropped by the last commit; not yet writable
    next_new: PageNumber,             // one past the last page of the file
}

// At each commit, in this order:
free.ready.extend(free.retiring.drain(..));
free.retiring = pages_this_commit_stopped_referencing();
```

Taking a page takes the **lowest** page in `ready`, and extends the file only when `ready` is empty; journal pages come from the same pool.
Lowest-first lets pages at the end of the file stay dead once they die, so that [truncation](consolidation.md#truncation) can return them.

## The header as write buffer

The root of the span table is the header page, and the header page is rewritten by every flush no matter what.
That page is free real estate, so it doubles as the span table's **write buffer**: every flush states the header's entries again, and fills the header with the hottest entries before any leaf, at zero additional page writes.

- The common small flush writes **no** table page beyond the header, and no data page when its new bytes are all inline.
- An entry carried in the header is restated every flush at the new epoch, for free.

**When the header overflows, the coldest entries go to leaves**, ranked by [heat](in-memory-state.md#heat).
What the header can hold is bounded by `MAX_PAGE_CONTENT`, so the ranking covers a few hundred entries however large the file is, and a sort of them on the rare flush that overflows is beneath measurement.
Evicting the coldest runs of spans contiguous in id, rather than the coldest entries wherever they sit, keeps the leaves' ids dense, which keeps their delta encoding short, and lets the evicted run be [merged to the bound](#the-span-policy) as it leaves.

## The shape of the tree

Table pages form a tree rooted at the header, and it is a **directory, not a search structure**: every lookup is served by the in-memory span table, nothing descends the tree by key, so there is no ordering invariant to maintain across pages.

- While everything fits in the header: **zero extra pages** — the steady state of every small file.
- Overflow goes to leaves referenced directly from the header, with entries continuing to share the header alongside the child references.
  At one to two bytes per child reference, a header can reference two to four thousand leaves, one to two million spans.
- Beyond that, one layer of interior pages reaches some million leaves; deeper is not going to be needed.

Structural updates are copy-on-write path copies, but the paths are short and the root is free: adding or removing a leaf rewrites at most `depth − 1` pages besides the leaf itself.
A depth change is a single-page event: when the header's child list outgrows its room, move the whole list into one fresh interior page and reference that instead.

## The cut

**The cut writes every stated span's entry: the header's into the header, the rest into new leaves, in id order**, each leaf closed when the next entry does not fit, so that every leaf but the last is full to within one entry.
The last leaf, or the header when there is no leaf, takes **fillers**: whole sparse leaves whose live entries fit its room, restated there, which drains them for free ([the page rewrite](consolidation.md#the-page-rewrite)).
The header's capacity is what remains after the file-header fields and its child list, and the child list depends on how many leaves the flush adds and unlinks, so the header's share is chosen against an estimate and corrected: if the list turns out longer, the header gives up its coldest entries to the leaves.

Entries are derived from the span table as it finally stands, after everything else in the flush, so each span is stated exactly once in the flush's epoch, as the [format requires](../spec/span-table.md#rules-for-writers), however many sources touched it.

## Correctness: the differential oracle

Build the oracle before the optimizer.

**Test the store on its own against a plain byte vector**: any series of `Write`, `Splice`, `Move` and `Paste`, flushes, reopens and simulated power cuts must leave the sequence holding the vector's bytes.
Above the store, **after every operation, the sequence must equal the canonical encoding of the in-memory root value**, byte for byte.
Randomized operation series with shrinking.

## Walk-through of an epoch

A finer-grained version of the [recovery case analysis](../spec/durability.md#case-analysis-for-a-power-cut-during-flush-e), tracking page states step by step.
This exists because two points in the argument are easy to get backwards: most pages of the older world are also in the newer one and therefore simply live, and a header slot whose write never arrived holds a *valid* stale header rather than garbage.

**During the journaling phase of epoch `E`:**

- Header `E − 2` exists and has been fsynced.
- Header `E − 1` was written but not fsynced yet.
- Journal `E` is partially written, not fsynced.
- All `Data` and `Table` pages of world `E − 1` have been fsynced and are **live** — this flush must not overwrite them.
- Pages of world `E − 2` that are *not also* part of world `E − 1` are **fallback**, and must not be overwritten either.
  This includes journal `E − 1`, which header `E − 2` names.
  Pages belonging to both worlds are simply live, which is the common case rather than the exception: typically most pages of world `E − 2` are in both.

**Steps 1–7** of [the flush protocol](../spec/durability.md#the-flush-protocol) write data and table pages for epoch `E`, then fsync.
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
