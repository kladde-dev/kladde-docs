# Address Table: A Worked Walkthrough

A step-by-step trace of the bookkeeping structures of [[address-table]] through one allocation's life, followed by an adversarial search for sequences that break the accounting.

This version assumes two adopted changes, both summarised in [[#Preliminaries]].
The **pin model**: a size statement takes only `A` (the resolved size depends on it) and `F` (fragment) pins, never a `D` (denial) pin, which eliminates the unbounded over-count the earlier model suffered from.
And the **`Grow`/`Shrink` split**: the old `Size` statement is separated into a size-reducing half that claims content and a size-increasing half that does not, which stops a run of grows from planting latent content claims.

One correctness requirement survives the change (scenario (v)) and one bounded over-count remains (scenario (vii)).

## Preliminaries

**Pins.**
A statement is live exactly while `pins > 0`, and `pins` is a single counter over heterogeneous holders:

| Pin | Held by | Taken when | Released when |
| --- | --- | --- | --- |
| `F` | any statement | a fragment resolves *through* it | that fragment is destroyed or re-owned |
| `A` | the **anchor** (newest `Shrink`), and a `Grow` with `n == size` | the anchor: on becoming newest; a `Grow`: while it may be the sole witness of the size | the anchor: a newer `Shrink` supersedes it, or the id is tombstoned; a `Grow`: as soon as `size > n` or it falls below `anchor_epoch` |
| `D` | the newest `Tombstone` for an id | initialised to the count of physically-present statements naming the id | one of those statements is physically dropped |

**`Size` is split into `Grow` and `Shrink`.**
The old `Size(id, n)` did two jobs — anchoring the size, and claiming that everything at or past `n` is `Undefined` — and those two are needed in opposite directions.
A **shrink** needs both: it destroys content that must not resurface, and it must pull old extents out of the `max`.
A **grow** needs only the size: the territory it exposes is already denied by the last shrink, since anything that could cover a probe at or past the old size necessarily has an epoch below `anchor_epoch`, or was never occupied at all.
So a grow-emitted `Size` used to carry a *dormant* content claim that activated later, when the allocation grew past its bound — and that dormancy is what manufactured strays.
`Shrink(id, n)` keeps the old semantics; `Grow(id, n)` bounds the size from below and matches no probe.

The model is deliberately asymmetric, and the asymmetry is the point.
A `Tombstone` claims **every** probe and denies existence outright, so it genuinely denies every older statement naming the id; `D` is exact and `mentions` computes it in `O(1)`.
A `Shrink(id, n)` claims only `[n, ∞)`, so it denies an older statement only when that statement reaches past `n` — a count no allocation-level counter can produce — and it turns out never to need the pin at all: **a `Shrink` may be dropped iff it is not the anchor and owns no fragment**, because a non-anchor `Shrink` cannot affect the size (its epoch is below `anchor_epoch`, and the `max` ranges only over `Grow` bounds and content extents above the anchor), and it cannot affect content beyond the probes its `F` pins already count.
Nor can a later grow expose anything: the anchor always has `n <= size`, so it already denies every probe from `size` upward.
A `Grow` is simpler still — matching no probe, it can never own a fragment, so it holds `A` alone, dies as soon as `size > n`, and at most one per allocation is ever alive.

Only the **newest** suppressor of each kind carries pins.
For tombstones this is exact for the same reason: a newer tombstone subsumes an older one entirely, so older tombstones are released outright and droppable on sight, and `AllocationMeta` needs a single `tombstone` pointer rather than the list of suppressors the previous model required.

**A tombstone is an epoch floor, not a matcher.**
The on-disk rules describe a `Tombstone` as matching every probe and yielding `Undefined`; the in-memory resolver inverts that, discarding every statement at or below `tombstone_epoch` and taking the highest of what remains, defaulting to `Undefined` when nothing does.
The two are equivalent, since a statement can only win by outranking every matcher and the tombstone matches all of them.
The framing matters here for one reason: **a tombstone never owns a fragment**, because it is never anything's winner — ranges left uncovered above the floor are ordinary unowned defaults.
That is what lets `Tombstone` hold `D` alone even when its id has been allocated again beneath it, and hence what lets an id be recycled the moment its tombstone is committed, with no condition on `mentions` at all ([[address-table#Tombstones and id recycling]]).

**`StatementRecord`.**

```rust
struct StatementRecord {
    page: PageNumber,
    framing_len: u16,
    pins: u16,
}
```

| Field | Updated when | Value | Read for |
| --- | --- | --- | --- |
| `page` | at creation only; a statement never moves (relocation means a *new* statement) | the page the flush writes it into | knowing whose `coverage` to decrement when the framing dies, and whether a consolidation victim holds this statement |
| `framing_len` | at creation only | the statement's encoded size **excluding** any `Inline` payload, since payload bytes are charged per byte through `fragment.source` instead | the amount subtracted from `coverage[page]` at the `1 → 0` pin transition; summed into `AllocationMeta.statement_bytes` |
| `pins` | on every fragment gain or loss, whenever the anchor or grow-witness changes hands, and — tombstones only — whenever a denied statement is physically dropped | at creation: one per fragment it wins, plus 1 if it becomes the anchor or the grow witness, plus (tombstone only) `mentions` read *before* the flush's own additions and *after* its own drops | the liveness test `pins > 0`; the `1 → 0` transition releases `framing_len` from `coverage[page]` and removes the statement from `statement_bytes` |

Note that `pins` can *rise* after creation: growing an allocation can bring a range into existence that an existing `Shrink` wins, as Step 3 shows.

**`AllocationMeta`.**

```rust
struct AllocationMeta {
    size: Size,
    fragment_count: u32,
    statement_bytes: u32,
    mentions: u32,
    anchor: Option<StatementRef>,        // newest Shrink
    grow_witness: Option<StatementRef>,  // Grow with n == size, if any
    tombstone: Option<StatementRef>,
}
```

**Note:** we should measure how many allocations have a `Some` value for `anchor`, `grow_witness` or `tombstone`.
If this is rare in practice, we should outsource them to a side table.

| Field             | Updated when                                                                                                                | Value                                                                                                                                                                 | Read for                                                                                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `size`            | any flush that resizes the id, writes past its end, frees it, or re-allocates it                                            | `0` if the id does not exist; otherwise `max(authority's n, extents of content statements with epoch > size_epoch)` — maintained incrementally rather than recomputed | answering `size()` in `O(1)`; bounding reads; deciding which probes are in range, hence which fragments exist at all                                      |
| `fragment_count`  | on every fragment created or destroyed for this id                                                                          | count of the id's live fragments                                                                                                                                      | defragmentation ranking — description overhead relative to `size`                                                                                         |
| `statement_bytes` | when a statement for this id is created, or when one's pins reach zero                                                      | sum of `framing_len` over the id's **live** statements                                                                                                                | defragmentation ranking, paired with `fragment_count`                                                                                                     |
| `mentions`        | `+1` per statement naming the id written or read at open; `−1` when one is physically dropped                               | count of statements naming the id that are **physically present** in live address-table pages — *not* the resolution-live ones                                        | initialising a new `Tombstone`'s `D` count, where it is exact; deciding when the tombstone itself becomes droppable (`mentions == 1`, the tombstone alone). It does **not** gate id recycling, which needs only a committed tombstone |
| `anchor`, `grow_witness` | `anchor`: when a `Shrink` is written, when the id is tombstoned (cleared), or when it is re-allocated. `grow_witness`: when a `Grow` is written, and when `size` moves past its bound | `anchor`: the newest `Shrink` above `tombstone_epoch`. `grow_witness`: the `Grow` with `n == size`, if any — separate slots, since both can be pinned at once for different reasons                                                                                       | moving the `A` pin from the old authority to the new one; telling a consolidator that a victim page holds an authority it must replace (scenario (v))     |
| `tombstone`       | when a `Tombstone` is written (the new one takes the `D` pins, the old one is released outright) or when the tombstone dies | the id's newest `Tombstone`, if any                                                                                                                                   | finding the single statement to decrement when a statement naming the id is physically dropped                                                            |

**Epochs belong to pages, not to statements.**
A statement inherits the epoch of the page it currently sits in, so moving a statement re-stamps it.
The header is rewritten every flush and states resolved truth, so everything in the header always carries the current epoch, and multi-epoch disagreement can exist only between the header and evicted leaves, or between two leaves.
This is why the example below needs evictions to arise at all.

**Framing sizes**, from the grammar in [[address-table#Physical level]], assuming a one-byte `id_delta` and two-byte addresses:

| Statement | Encoding | `framing_len` |
| --- | --- | --- |
| `Ref(7, 0, 1000, P1)` | 1 + 1 + 1 + 2 + 2 | 7 |
| `Ref(7, 50, 10, PB)` | 1 + 1 + 1 + 1 + 2 | 6 |
| `Shrink(7, n)` or `Grow(7, n)` | 1 + 1 + 1 | 3 |
| `Undefined(7, 10, 40)` | 1 + 1 + 1 + 1 | 4 |
| `Tombstone(7)` | 1 + 1 | 2 |

**Pages.**
`H` is the header (the address table's root and write buffer), `L1`/`L2` are address-table leaves, `P1`/`PB`/`PC`/`PD` are data pages, and `MAX_PAGE_CONTENT` is 4082.
Header pages are exempt from coverage-driven victim selection, since they are rewritten unconditionally every flush.

**Ordering rule**, used throughout: a flush applies its own drops before initialising any new suppressor's pins, so a statement replaced within a flush is never counted as something the replacement must deny.

## Building the state

### Step 0 — empty file

Every structure is empty; the header exists and names no leaves.

### Step 1 (flush 3) — create allocation 7 and write 1000 bytes

The application allocates id 7 and writes 1000 bytes.
The flush is part of a bulk load, so the header overflows and evicts a batch to a fresh leaf `L1`@3; at creation every statement has the same age, so the eviction clock cannot distinguish them and allocation 7's statement is in the batch.

| Structure | Value after the step |
| --- | --- |
| Statements | `Ref(7, 0, 1000, P1)` in `L1`@3, `framing_len` 7, pins **1** (`F`) |
| Fragment map | `(7, 0) → { source: P1+0, statement: Ref@3 }`, covering `[0, 1000)` |
| `AllocationMeta[7]` | `size` 1000, `fragment_count` 1, `statement_bytes` 7, `mentions` 1, `anchor` **None**, `tombstone` None |
| `coverage[P1]` | 4082, of which 1000 belong to id 7 |
| `coverage[L1]` | 4082 |

`anchor` is `None`: with no `Shrink` statement the resolved size is simply the largest content extent, `0 + 1000`, so there is no anchor and no `A` pin to hold.

### Step 2 (flush 5) — truncate to 10 bytes

`Shrink(7, 10)` is written into the header, then evicted to a fresh leaf `L2`@5 by further bulk traffic.
`anchor_epoch` becomes 5, so `size = max(10, extents with epoch > 5) = 10`, and probes `>= 10` are outside the allocation.

| Structure | Change |
| --- | --- |
| `Ref(7,0,1000,P1)`@3 | fragment narrows `[0,1000) → [0,10)`; pins stay **1** (`F`) |
| `Shrink(7,10)`@5 (new, `L2`) | pins **1** — `A` only. It owns no fragment (`[10, 10)` is empty), and under the adopted model it takes no `D` pin against `Ref@3` |
| Fragment map | `(7, 0)` now covers `[0, 10)` |
| `AllocationMeta[7]` | `size` 10, `fragment_count` 1, `statement_bytes` 10, `mentions` 2, `anchor` `Shrink@5`, `tombstone` None |
| `coverage[P1]` | **−990** → 3092 |
| `coverage[L1]` | unchanged — a partially shadowed statement keeps its whole framing charge, which is right, since the entire encoding is still needed to describe the surviving 10 bytes |
| `coverage[L2]` | `+3` for `Shrink@5`'s framing |

Under the previous model this statement would have had pins 2.
The `D` pin it no longer holds was the one that made it immortal in the old scenario (vii).

### Step 3 (flush 9) — grow to 60 and write `[50, 60)`

The application grows allocation 7 to 60 bytes and writes 10 bytes at offset 50, into data page `PB`; `Ref(7, 50, 10, PB)` goes into the header and stays there, being hot.
`anchor_epoch` is still 5, so `size = max(10, 50 + 10) = 60`.

| Range | Matching statements | Winner | Source |
| --- | --- | --- | --- |
| `[0, 10)` | `Ref@3` | `Ref@3` | `P1` |
| `[10, 50)` | `Ref@3`, `Shrink@5` (since `10 <= probe`) | **`Shrink@5`** | `Undefined` |
| `[50, 60)` | `Ref@3`, `Shrink@5`, `Ref(50,10)@9` | `Ref@9` | `PB` |

The middle row is the one to watch: growing the allocation brought `[10, 50)` into existence, and it resolves *through* the `Shrink` statement, which therefore **gains a fragment pin**.
Note the grow itself emits nothing: `Ref(7,50,10,PB)`'s extent of 60 already reaches the new size, so no `Grow` is needed.

| Structure | Change |
| --- | --- |
| `Ref(7,0,1000,P1)`@3 | pins **1** (`F`) — unchanged |
| `Shrink(7,10)`@5 | pins **1 → 2** = `A` + `F` (owns `[10, 50)`) |
| `Ref(7,50,10,PB)`@9 (new, `H`) | pins **1** (`F`) |
| Fragment map | `(7,0) → P1+0`; `(7,10) → Undefined, stmt Size@5`; `(7,50) → PB+0` |
| `AllocationMeta[7]` | `size` 60, `fragment_count` 3, `statement_bytes` 16, `mentions` 3, `anchor` `Shrink@5`, `tombstone` None |
| `coverage[PB]` | `+10` |

This is the state the scenarios branch from.
It is also the witness that `pins` must remain a single counter over heterogeneous holders: `Shrink@5` holds `A` and `F` simultaneously, and no consumer ever asks which is which.

## Scenarios

Each scenario branches from the end of Step 3 unless stated otherwise.

### (i) Consolidate `L1`, the leaf holding `Ref@3`

`L1` is rewritten as resolved truth at epoch 10.
`Ref@3` owns exactly one fragment, `[0, 10)`, so it is re-emitted narrowed as `Ref(7, 0, 10, P1)`@10, `framing_len` 6.

The consolidator finds out that `Ref@3` owns only fragment `[0, 10)` as follows: it decodes the victim page and, for each statement it finds, range-scans the fragment map.
For `Ref`/`Inline`/`Undefined` the scan is `(id, offset) .. (id, offset + size)`; for a `Shrink(id, n)` it is `(id, n) .. (id, size)`, since those are the only probes it can win; a `Grow` needs no scan at all, since it wins nothing.
Within that window it keeps the fragments whose `statement` matches.

It can also stop early, because `pins` already says how many to expect: for a content statement `F == pins` exactly, and for a `Shrink`, `F == pins − 1` when it is the id's `anchor` and `== pins` otherwise.
In this step, `Ref@3.pins == 1` tells the consolidator to expect exactly one fragment before it looks.

The deeper point is that this is not an implementation tax at all — it is the consolidation algorithm.
A page is rewritten as resolved truth, and you cannot emit resolved truth without first determining which parts of it this page is responsible for.
The walk *is* that determination, and its cost is predictable in advance from `AllocationMeta.fragment_count`, which lets the ratio ranking in [[address-table#Goals, and the one currency behind them]] price a candidate before committing to it.

| Structure | Change |
| --- | --- |
| `Ref@3` | fragment re-owned by the new statement → pins **1 → 0** → framing released; then physically dropped with the page |
| `Shrink(7,10)`@5 | **unchanged at 2** — under the adopted model it never held a pin on `Ref@3`, so the drop does not reach it |
| `Ref(7,0,10,P1)`@10 (new) | pins **1** (`F`) |
| Fragment map | `(7,0)`'s `statement` re-pointed; `source` unchanged |
| `AllocationMeta[7]` | `statement_bytes` 16 → 15; `mentions` 3 → 3 (one dropped, one added) |
| `coverage[L1]` | → 0; reusable after the two-epoch quarantine |
| `coverage[P1]` | **unchanged** — consolidating the address table moves no data |

The middle row is the whole benefit of the model change in one line.
Previously this drop had to decrement a suppressor in an unrelated page; now dropping a content statement is a purely local event, because nothing but a tombstone ever depends on a content statement's continued physical presence.

### (ii) Resize to 55 bytes

Flush 10 resizes allocation 7 to 55 bytes.
The header carries `Ref(7,50,10,PB)` forward, but at epoch 10 its extent (60) would exceed the new size, which [[address-table#No conflicts within each epoch]] forbids within one epoch — so the resolved-truth discipline narrows it on the way out to `Ref(7, 50, 5, PB)`.
This is a **shrink**, so the flush does emit a `Shrink(7, 55)`@10 — always, for the reason worked out below the table.
It happens to be unnecessary here, which the table reflects by leaving it out; the flush could not have known that.

| Structure                 | Change                                                                                                                                                                   |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Ref@3`                   | pins **1** (`F`, still owns `[0,10)`)                                                                                                                                    |
| `Shrink(7,10)`@5            | keeps `A` (still the only `Shrink` for the id, hence still the anchor) and keeps `F` (still owns the `Undefined` fragment `[10,50)`, which nothing newer covers) → pins stays at **2** |
| `Ref(7,50,10,PB)`@9       | superseded by its narrowed self; record dropped                                                                                                                          |
| `Ref(7,50,5,PB)`@10 (new) | pins **1** (`F`)                                                                                                                                                         |
| `AllocationMeta[7]`       | `size` 55, `fragment_count` 3, `statement_bytes` 16, `mentions` 3, `anchor` `Shrink@5`, `tombstone` None                                                            |
| `coverage[PB]`            | **−5** — bytes `[PB+5, PB+10)` are now garbage                                                                                                                           |

**Why the `Shrink(7,55)` is unnecessary here, and why the flush emits it anyway.**
Work out the size *without* it, from `Ref(7,0,1000,P1)`@3, `Shrink(7,10)`@5 and `Ref(7,50,5,PB)`@10.
`anchor_epoch` stays 5, so `size = max(10, extents of content statements with epoch > 5) = max(10, 50 + 5) = 55` — the target, reached with no new statement at all.
Content resolves identically too: `[0,10)` through `Ref@3`, `[10,50)` through `Shrink@5`, `[50,55)` through the narrowed `Ref@10`.
The narrowing that [[address-table#No conflicts within each epoch]] forced on the tail is what makes it redundant: it created a statement ending exactly at the new size.

**The emission rule splits by direction, and only the shrink half is undecidable.**

- **Grow** — emit `Grow(id, S_new)` **iff nothing this flush writes reaches offset `S_new`**.
  Exact and `O(1)`: everything *not* written by this flush is bounded by the old size, so the flush need only inspect its own output.
  The added extent needs no protection either — every statement that could cover a probe at or past the old size has an epoch below `anchor_epoch`, so the anchor already denies it — which is why Step 3 grew from 10 to 60 and emitted nothing.
  A grow that writes at the new end therefore costs nothing; a grow without writing costs one `Grow`, which is three bytes and, unlike a `Size`, plants no latent content claim.
- **Shrink** — always emit `Shrink(id, S_new)`.
  In principle it is needed exactly when the target is not already implied, i.e. when it differs from the maximum of the anchor's `n` and the extents of every **physically present** content statement above `anchor_epoch`.
  That check is not implementable: no structure reaches an id's physically present statements.
  The fragment map indexes only *live* fragments; `StatementRecord` exists only for statements that still have a reason to live, so a dead one has no record at all and survives purely as bytes in its page; and `mentions` is a count, not a list.
  Enumerating them means decoding every live address-table page — acceptable for consolidation, which decodes its victim anyway, absurd for a per-resize decision.
  Always emitting is sound, since an explicit `Shrink(id, T)` fixes the size at `T` whatever extents lurk in leaves, and costs three sometimes-superfluous bytes.

Contrast a resize of 60 → **30**: `Ref(7,50,10,PB)`@9 falls entirely outside the new size, so it is dropped rather than narrowed, leaving nothing above epoch 5, and the size would come out as `max(10) = 10`.
There the `Shrink(7,30)` is genuinely required — which is the point of always emitting, since the two cases are indistinguishable from inside the flush.

A superfluous `Shrink` is the same residual as [[#(vii) The immortal redundant suppressor — break 2]], caught at write time rather than at consolidation time.
Beware the near-miss version of the test: "the anchor is redundant whenever `size > n`" is **false**, because dropping an anchor also moves `anchor_epoch` and lets *older* extents back into the `max` — dropping `Shrink@5` here would expose `Ref@3`'s extent of 1000 and yield 1000, not 55.
That is break 1, and it is why the test has to be run against the anchor that would remain, not against the one being considered.
A `Grow` carries no such hazard, since it anchors nothing.

**How long an old `Shrink` survives:**
@Claude: walk through the following subsequent scenarios, focusing on what happens to `Shrink(7,10)`@5:

- (a) `Ref(7,50,5,PB)`@10 gets evicted to a non-header page at flush 11, then the allocation gets resized to 53 bytes at flush 12 (by only writing a `Shrink(7,53)` to the header page).
  How does kladde know that `Shrink(7,10)`@5 is still necessary although it is no longer the id's `AllocationMeta::anchor`?
- (b) continuing from (a), now the the content of the entire allocation (i.e., range `[0, 53)`) is overwritten.
  How does kladde know that `Shrink(7,10)@5` is now no longer necessary (but `Shrink(7,53)` is, to deny the bytes >= 53)?

> **Short answer: in (a) nothing special happens — the `F` pin alone keeps it alive; in (b) nothing special happens either — losing its last fragment kills it.**
> Neither case needs a rule beyond [[#Findings]] item 2, and walking them through shows why that rule is a *conjunction*.
>
> Starting state, per the correction above (no `Shrink(7,55)`, so `Shrink@5` is still the anchor):
> `Ref(7,0,1000,P1)`@3 in `L1`, pins 1 (`F`, owns `[0,10)`);
> `Shrink(7,10)`@5 in `L2`, pins 2 (`A` + `F`, owns `[10,50)`);
> `Ref(7,50,5,PB)`@10 in `H`, pins 1 (`F`, owns `[50,55)`); `size` 55.
>
> **(a) Eviction at flush 11, then resize to 53 at flush 12.**
> Eviction re-stamps: `Ref(7,50,5,PB)` lands in a leaf `L3`@11, still pins 1.
> The resize writes `Shrink(7, 53)` into the header at epoch 12, and that statement is genuinely required — without it `anchor_epoch` stays 5 and `size = max(10, 55) = 55`, not 53.
>
> | Statement | After flush 12 |
> | --- | --- |
> | `Ref(7,0,1000,P1)`@3 | pins **1** (`F`, `[0,10)`) |
> | `Shrink(7,10)`@5 | loses `A` to `Shrink@12`; keeps `F` (`[10,50)`) → pins **2 → 1** |
> | `Ref(7,50,5,PB)`@11 | fragment narrows `[50,55) → [50,53)`; pins **1**; `coverage[PB]` **−2** |
> | `Shrink(7,53)`@12 (new) | pins **1** = `A`; owns no fragment, since probes `>= 53` are outside the allocation |
>
> **How kladde knows `Shrink@5` is still needed: it still owns a fragment, so `pins > 0`.**
> That is the whole mechanism, and it needs no reference to authority at all.
> `Shrink(7,53)` matches only probes `>= 53`, so writing it re-resolved nothing in `[10, 50)`; that fragment's `statement` back-pointer still names `Shrink@5`, and its `F` pin was never touched.
> Only the `A` pin moved, via `anchor`, taking the count from 2 to 1.
>
> Substantively it *must* survive: drop it and `[10, 50)` falls through to `Ref@3`, re-exposing P1's bytes from the original 1000-byte write — the content the truncation at flush 5 destroyed.
> This is exactly why item 2 reads "not the authority **and** owns no fragment".
> Losing authority is not evidence of anything; the two conjuncts are independent, and here they disagree.
>
> **(b) Overwrite all of `[0, 53)` at flush 13** — into a fresh data page `PE`, emitting `Ref(7, 0, 53, PE)`@13 in the header.
>
> | Statement | After flush 13 |
> | --- | --- |
> | `Ref(7,0,1000,P1)`@3 | `[0,10)` shadowed → pins **1 → 0**, dead; `coverage[L1]` −7, `coverage[P1]` −10 |
> | `Shrink(7,10)`@5 | `[10,50)` shadowed → pins **1 → 0**, dead; `coverage[L2]` **−3** |
> | `Ref(7,50,5,PB)`@11 | `[50,53)` shadowed → pins **1 → 0**, dead; `coverage[L3]` −6, `coverage[PB]` −3 |
> | `Ref(7,0,53,PE)`@13 (new) | pins **1** (`F`, owns `[0,53)`) |
> | `Shrink(7,53)`@13 | carried in the header and re-stamped from 12; pins **1** = `A` |
>
> **How kladde knows `Shrink@5` is no longer needed: its last fragment was destroyed.**
> Writing `Ref(7,0,53,PE)`@13 re-resolved `[0, 53)`, which re-owned the `(7,10)` fragment; that destruction decremented `Shrink@5` from 1 to 0, and the `1 → 0` transition released its three framing bytes from `coverage[L2]` and removed it from `statement_bytes`.
> No test was run and no authority consulted — the count simply reached zero.
>
> **Why `Shrink(7,53)` survives is the more interesting half, and it is not because of the live bytes.**
> After this flush *every* older statement for id 7 is dead, so one might expect the size to be implied by `Ref(7,0,53,PE)`'s own extent of 53.
> It is not, because `Ref(7,50,5,PB)`@11 is **dead but still physically present** in `L3`, with extent 55 — and the size formula's `max` ranges over physically present statements, not resolution-live ones.
> Drop `Shrink(7,53)` and `anchor_epoch` falls back to 5, admitting that extent and yielding `size = max(10, 55, 53) = 55`.
> Its `A` pin is therefore doing real work, and stays until `L3` is cleaned.
>
> That carries a correction to the test stated earlier in this scenario: "the largest extent among content statements above `anchor_epoch`" must be read over **physically present** statements.
> Reading it over live ones is the same blind spot as [[#(v) Consolidate `L2`, the leaf holding the size authority — break 1]] — a statement that wins no probe is invisible in the fragment map yet still sets the size — and it makes the check cost a walk of the id's statements (bounded by `mentions`) rather than a glance at `size`.
>
> One bookkeeping detail visible in the last row: because the header is rewritten every flush, a `Shrink` or `Grow` carried in it becomes a *new* statement record at the new epoch each time, so `anchor` re-points and the `A` pin moves on every flush.
> That is routine rather than churn — header pages are rewritten unconditionally and exempt from victim selection — but it is why the `A` pin has to be a movable pin rather than a flag baked into the statement.

### (iii) Overwrite bytes `[0, 20)`

Flush 10 writes data page `PC` and emits `Ref(7, 0, 20, PC)`@10; the header carries `Ref(7,50,10,PB)` forward at epoch 10, disjoint and so conflict-free.
`anchor_epoch` is still 5, so `size = max(10, 20, 60) = 60`, unchanged.

| Range | Winner | Source |
| --- | --- | --- |
| `[0, 20)` | `Ref(0,20)@10` | `PC` |
| `[20, 50)` | `Shrink@5` | `Undefined` |
| `[50, 60)` | `Ref(50,10)@10` | `PB` |

| Structure | Change |
| --- | --- |
| `Ref@3` | its only fragment is shadowed → pins **1 → 0** → **dead, but still physically present in `L1`** |
| `Shrink(7,10)`@5 | fragment narrows `[10,50) → [20,50)`; pins stay **2** (`A` + `F`) |
| `AllocationMeta[7]` | `fragment_count` 3, `statement_bytes` 15 (dead `Ref@3` excluded), `mentions` 4 |
| `coverage[L1]` | **−7** (dead framing) |
| `coverage[P1]` | **−10** → id 7 relies on none of `P1` |

Keep the distinction in view: `pins == 0` means *contributes no live bytes*, while physical removal happens only when the page is rewritten, which is what `mentions` counts.
Under the adopted model a dead content statement is inert — it holds nothing else hostage — which is what defuses scenario (vi).

### (iv) Free the allocation

Flush 10 emits `Tombstone(7)`, and the header stops carrying the id's content statements.
Existence is decided by the highest-epoch statement mentioning the id, so the allocation ceases to exist and `size` becomes 0.

| Structure | Change |
| --- | --- |
| `Ref@3` | no fragments → pins **0** |
| `Shrink(7,10)`@5 | loses `F` (no probes remain) and loses `A` (`anchor` is cleared when the id ceases to exist) → pins **2 → 0**, immediately dead |
| `Ref(7,50,10,PB)`@9 | dropped with the header rewrite |
| `Tombstone(7)`@10 (new) | pins **2** = `D`, from `mentions` after this flush's drops: `Ref@3` and `Shrink@5` |
| `AllocationMeta[7]` | `size` 0, `fragment_count` 0, `mentions` 3, `anchor` **None**, `tombstone` `Tombstone@10` |
| `coverage[P1]`, `coverage[PB]` | each loses its remaining referenced bytes |
| `coverage[L2]` | **−3** — `Shrink@5` dies at once instead of lingering |

The cascade then clears itself with no special pass:
consolidating `L1` drops `Ref@3` → `Tombstone` pins 2 → 1;
consolidating `L2` drops `Shrink@5` → `Tombstone` pins 1 → 0;
the tombstone is then omitted by the next rewrite of its page, and `AllocationMeta[7]` can go.

Note that **id 7 is reusable from flush 11**, before any of that happens.
Recycling waits only on the tombstone being committed, not on `mentions` falling, because the tombstone is an epoch floor: whatever of the old incarnation still sits physically in `L1` and `L2` is below the floor and unreachable.
The cascade above is therefore about reclaiming *bytes*, on its own schedule, and never about holding an id out of circulation.

The `coverage[L2]` row is a second, quieter win from the model change.
Previously `Shrink@5` retained a denial pin on `Ref@3` and stayed "live" until `L1` happened to be cleaned, so `L2` looked fuller than it was and was correspondingly less likely to be chosen; now it drops to zero at the moment of the free, when the information that it is garbage first exists.

### (v) Consolidate `L2`, the leaf holding the size authority — break 1

This requirement survives the model change unaltered, and becomes easier to state.

`L2` is rewritten as resolved truth at epoch 10.
For id 7 it owns one statement, `Shrink(7,10)`@5, whose surviving contribution *to content* is the `Undefined` fragment `[10, 50)`.
A consolidator driven by the fragment map — the natural implementation, since the fragment map is where resolved truth lives — will reproduce exactly that, emitting `Undefined(7, 10, 40)`@10 and nothing else.

That silently corrupts the file.
Dropping `Shrink@5` removes the only `Shrink` for id 7, so `anchor_epoch` falls back to `-1` and the size becomes the maximum extent over **all** content statements, including `Ref(7,0,1000,P1)`@3 still sitting in `L1`:

```
size = max(1000, 50, 60) = 1000     // was 60
```

Allocation 7 silently grows to 1000 bytes and re-exposes stale bytes from `P1` across `[10, 1000)` — content the application truncated away at flush 5.

The cause is not a tempting shortcut but a structural blind spot: the fragment map is a **content** view, and a `Shrink` statement's other contribution — being the anchor — is not a fragment, so it is invisible there.
It is visible in exactly two places, `AllocationMeta.anchor` and the statement's own `A` pin, and under the adopted model the `A` pin is now the *only* extra pin a `Size` can hold, which makes the rule sharp:

> A page holding a statement with an `A` pin cannot be consolidated without transferring the authority.

The fix emits a replacement alongside the content: `Shrink(7, 60)`@10 **and** `Undefined(7, 10, 40)`@10, which do not conflict within the epoch (`10 + 40 = 50 <= 60`).

| Structure | Change (with the fix) |
| --- | --- |
| `Shrink(7,10)`@5 | dropped; `mentions` −1 |
| `Shrink(7,60)`@10 (new) | pins **1** = `A`; owns no fragment |
| `Undefined(7,10,40)`@10 (new) | pins **1** (`F`) |
| `Ref@3` | unchanged, pins **1** |

The previous model obscured this by letting `D` pins share the blame; here the diagnosis is unambiguous.

### (vi) The hostage: a correct pin that cannot be released

Continue from scenario (iii), where `Ref@3` is dead but physically present in `L1`, a bulk-loaded page holding some 580 still-live statements.
Losing `Ref@3`'s 7 bytes takes `coverage[L1]` from 4082 to 4075 — a live fraction of 99.83 % — so victim selection will never choose `L1` while anything sparser exists.

**Under the adopted model this is no longer a hostage situation.**
`Ref@3` is dead and inert: it pins nothing, because content statements are never denied by anything except a tombstone, and there is no tombstone here.
Its continued presence costs 7 wasted bytes in `L1` and a `mentions` count that no live-allocation decision reads.
`Shrink@5` sits at pins 2 on its own merits (`A` + `F`) and is unaffected.

The effect survives only on the **free path**, and in narrowed form.
After scenario (iv), `Tombstone@10` holds `D` pins on `Ref@3` and `Shrink@5`, so it cannot retire until both are physically dropped — which for `Ref@3` means cleaning `L1`.
The observation that motivated this scenario therefore still applies, but only to tombstones:

> Victim selection ranked purely by live fraction cannot see the benefit of cleaning a page whose dead statements are pinning a tombstone elsewhere.
> A benefit term counting "dead statements whose removal would decrement a tombstone" makes that visible.

What is *not* at stake any more is the id.
An earlier draft gated recycling on `mentions == 0`, which put the id space itself behind this hostage: one dead seven-byte `Ref` in a page that is never chosen could hold an id out of circulation for the life of the file, and a workload that churns allocations would leak ids at the rate its cold pages resist cleaning.
Recycling now waits only on a committed tombstone, so the stake is two bytes of tombstone framing rather than an id.

The blast radius is much smaller than before in two independent ways: previously *any* dead content statement could strand a `Shrink` indefinitely on any allocation, live or freed, and the ids of freed allocations were hostage as well.

### (vii) The immortal redundant suppressor — break 2

**This break is eliminated by the adopted model.**
Reproducing it, then showing why it no longer bites.

Continue from scenario (vi) and let flush 11 rewrite the allocation entirely: the application overwrites `[0, 60)`, so the flush writes data page `PD` and the header emits `Ref(7, 0, 60, PD)`@11.
No size statement is needed — the size is unchanged at 60, and the new `Ref`'s extent reaches it.

State: `L1`@3 holds dead `Ref(7,0,1000,P1)`; `L2`@5 holds `Shrink(7,10)`; `H`@11 holds `Ref(7,0,60,PD)`.
`anchor_epoch` is 5, `size` is `max(10, 60) = 60`, and `[0, 60)` resolves entirely through `Ref@11`.

`Shrink(7,10)`@5 now owns no fragment — its `[20, 50)` gap was overwritten — but it is still the anchor, so it keeps its `A` pin.
Take the anchor out of the picture for a moment (say a later flush re-anchors elsewhere) and it is the non-anchor case that the old model got wrong.

- **Previous model:** it still held `D` on `Ref@3`, so pins 1.
  That pin could only be released by cleaning `L1`, which scenario (vi) showed never happens, and consolidating `L2` merely carried the pin forward into the replacement statement.
  `coverage[L2]` over-reported by 3 bytes permanently, which biased `L2` *away* from cleaning — a self-reinforcing error — and `N` resizes could strand `Θ(N)` such statements.
- **Adopted model:** pins **0**.
  The statement is dead the moment the overwrite lands, `coverage[L2]` falls by 3 immediately, and the next rewrite of `L2` drops it.

The residual, and it is worth being precise that one remains.
A `Shrink` that **is** the anchor can still be redundant: with `Shrink(7, 100)`@5 and `Ref(7, 0, 100, X)`@9, dropping it would leave `size = max(100) = 100`, unchanged — yet `A` keeps it alive.
Three things distinguish this from the old break:

1. It is **not** bounded at one per allocation, as an earlier version of this section claimed.
   A non-anchor `Shrink` lingers whenever it owns a fragment, and it can own one *while being redundant* — when the range it wins would resolve to `Undefined` anyway, because nothing older matches it.
   Building such a chain now requires *alternating* shrinks and grows, though, which is what the `Grow`/`Shrink` split of [[#Preliminaries]] buys: a run of plain grows plants no content claims at all.
   Under the old single `Size`, `Shrink(7,10)`@5, `Size(7,20)`@7 and `Size(7,30)`@8 followed by `Ref(7,50,10,PX)`@9 left three fragment-owning strays over `[10,20)`, `[20,30)` and `[30,50)`; with `Grow(7,10)`@5, `Grow(7,20)`@7 and `Grow(7,30)`@8 the same history leaves **none**, since `[0,50)` is then unowned default and all three are dead the moment `size` exceeds their bounds.
   What still bounds the alternating case is derived in [[#How stray `Size` statements accumulate and are pruned]] below.
2. It is **cheaply correctable for `Grow`, not for `Shrink`**.
   A `Grow` is dead as soon as `size > n` — an `O(1)` test — so at most one per allocation is ever alive.
   For a `Shrink`, deciding whether anything older is left to deny needs the id's physically present statements, which nothing reaches.
3. It is **not self-reinforcing at scale**: three bytes per stray does not meaningfully shift a page's live fraction, whereas the old pathology compounded through the coverage counter.

### How stray `Size` statements accumulate and are pruned

Take the alternating history that can still build a chain — `Shrink(7,10)`@5, grows to 20 and 30, `Ref(7,50,10,PX)`@9 taking the id to 60 — and continue it with `Shrink(7,15)`@11 and then `Ref(7,70,10,PY)`@13.
Assume each statement is evicted to a leaf in the flush that writes it, so epochs stay put.

**Grows never join the chain.**
`Grow(7,20)`@7 and `Grow(7,30)`@8 match no probe, so they own nothing throughout, and both are dead from the moment `Ref@9` takes `size` past 30.
Only `Shrink(7,10)`@5 owns anything — `[10,50)` — and it is the anchor, doing the job the truncation created it for.

**A later shrink prunes, permanently.**
`Shrink(7,15)`@11 takes the anchor and retracts the size to 15, so `[15,50)` leaves the allocation and `Shrink@5`'s fragment narrows to `[10,15)`; `Ref(7,50,10,PX)`@9 loses its range entirely and dies.
`Shrink@5` survives on that window, which `Shrink@11` does not match.

**The later grow does not revive anything.**
`Ref(7,70,10,PY)`@13 takes the id back to 80, yet `Grow@7`, `Grow@8` and `Ref@9` all stay at zero pins: `Shrink@11` has a *lower* bound and a *higher* epoch, so it matches and outranks them at every probe `>= 15`.
What it does instead is hand `Shrink@11` a fragment over `[15,70)`, taking it from 1 pin to 2 — another instance of pins rising after creation.

**The rule, and hence the bound.**
A dead `Shrink(n)` can only revive if some probe `>= n` becomes winnable, which requires every newer `Shrink` to have a bound strictly greater than that probe.
Order an id's physically present `Shrink` statements newest-first: one can hold an `F` pin only if its bound is strictly below the minimum bound among all newer ones, so **the live set is exactly the running-minimum chain**.
Here that chain is `Shrink@11` (15) → `Shrink@5` (10).
Each extra link costs an *alternating* shrink and grow — a run of grows contributes nothing, since `Grow` statements are absent from the chain by construction — and **any shrink to bound `b` permanently kills every `Shrink` with bound `>= b`**.
The stray count is the length of the decreasing-bound chain, pruned by every shrink: a precise characterisation rather than a constant, and one the split makes much harder to grow.

**Not every surviving `Shrink` is a stray**, and the same example shows the difference.
`Shrink@11` is load-bearing: drop it and `anchor_epoch` falls to 5, so `[50,60)` resolves through `Ref(7,50,10,PX)`@9 — dead, but still physically present in its leaf — re-exposing stale bytes from `PX`.
Distinguishing the two cases is exactly the "is there anything older to deny" predicate that [[#(ii) Resize to 55 bytes]] shows we cannot evaluate cheaply, which is why `Shrink` strays are tolerated rather than detected — and exactly the predicate a `Grow` never needs, since it denies nothing.

## Findings

1. **Consolidating a page that holds an id's anchor must transfer the anchor** (scenario (v)).
   The `A` pin is the signal, and it is the only extra pin a `Shrink` can hold, so the test is unambiguous.
   A fragment-map-driven consolidator cannot derive this on its own, because the anchor is not a fragment; it must consult `AllocationMeta.anchor` or the pin.
   A `Grow` carries no such obligation, since it anchors nothing.
2. **A `Shrink` may be dropped iff it is not the anchor and owns no fragment.**
   Its denials are subsumed by the anchor (which always denies from `size` upward) together with whatever newer content statements cover the rest, so `D` pins on size statements are redundant — the result adopted here.
3. **Only the newest suppressor of each kind carries pins.**
   For tombstones this is exact, since a newer tombstone subsumes an older one entirely; older tombstones have their pins released **outright** when the new one is written, rather than being left to drain, and are then droppable on sight.
   `AllocationMeta` therefore needs a single `tombstone` pointer, not the list of suppressors the previous model required.
   Tombstone chains are reachable, because recycling does not wait for the old tombstone to die, so the latest tombstone must be pinned by every earlier mention including earlier tombstones — initialising from `mentions` gets that right.
4. **A tombstone is an epoch floor, not a matcher**, which is what keeps it holding `D` alone.
   Represented as a matcher it would acquire `F` pins the moment its id was allocated again beneath it, and `mentions == 1` would stop being an exact droppability test.
   Represented as a floor, the ranges a new incarnation leaves uncovered are ordinary unowned defaults.
   This is also what makes an id reusable as soon as its tombstone is committed, with **no condition on `mentions`**: everything below the floor is unreachable however much of it survives physically, so leftover statements can never consume the id space.
   Freeing and re-allocating within a *single* flush is the one case needing care — a tombstone and the new incarnation would make contradicting existence claims in one epoch — and is handled by emitting statements that fully cover the new extent instead of a tombstone.
5. **`mentions` is exact for tombstones and would be an over-count for anything else.**
   A tombstone denies every probe, so every physically-present statement naming the id is genuinely denied.
   A `Shrink(id, n)` would deny only statements reaching past `n`, which no allocation-level counter can express — and under the adopted model nothing needs it to.
6. **Cleaning should be ranked by pins released as well as bytes reclaimed** (scenario vi), but the case is now confined to pages whose dead statements pin a *tombstone*, rather than any dead statement anywhere.
7. **A size statement holds at most two pin kinds, `A` and `F`, never three** — and a `Grow` holds only `A`.
   The single-`pins` argument survives: `Shrink@5` holds `A` and `F` together in Step 3, no consumer ever asks which kind a pin is, and two fields would still encode a distinction that is never read.
8. **A residual over-count remains, and it is not bounded at one per allocation** (scenario vii) — but the `Grow`/`Shrink` split narrows it sharply.
   Redundant `Shrink` statements survive on `F` pins over ranges that would read `Undefined` anyway; the live set is the running-minimum-bound chain read newest-first, and any shrink to bound `b` permanently kills every `Shrink` with bound `>= b`, while growth never revives one.
   Each extra link now costs an *alternating* shrink and grow, since `Grow` statements match no probe and so never join the chain — where a run of plain resizes used to strand one apiece.
9. **The emission rule splits by direction, and only the shrink half is undecidable.**
   A **grow** emits `Grow(id, S_new)` iff nothing the flush itself writes reaches `S_new` — exact and `O(1)`, since everything else is bounded by the old size — and needs no content protection, the anchor already denying the exposed territory.
   A **shrink** always emits `Shrink(id, S_new)`, because the principled test needs the id's *physically present* statements and nothing reaches them: the fragment map indexes only live fragments, `StatementRecord` exists only for statements with a reason to live, and `mentions` is a count rather than a list.
   Always emitting can only be superfluous, never wrong, at three bytes a time.
10. **A `Grow` is locally decidable, which a `Shrink` is not.**
    It matches no probe, so it never owns a fragment; it is dead as soon as `size > n` or it falls below `anchor_epoch`; and at most one per allocation is ever alive, since bounds are distinct and only the one equal to `size` can be pinned.
    Dropping one can never readmit older extents, so it has no break-1 hazard either.
11. **Confirmed sound:** the pin graph is acyclic — `F` and `A` point from the resolved view into statements, and `D` points strictly from a newer tombstone to older statements — so no set of statements can mutually pin itself alive.
12. **Confirmed sound:** content statements need no denial pins, because a content statement's denial is co-extensive with what it defines, and covering a range requires some newer statement to reach its far end, so the size cannot shrink when one is dropped.
13. **Ordering rule:** a flush applies its own drops before initialising a new tombstone's pins, so a statement replaced within a flush is never counted as something the replacement must deny.
