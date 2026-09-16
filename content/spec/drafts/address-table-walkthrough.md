# Address Table: A Worked Walkthrough

A step-by-step trace of the bookkeeping structures of [[address-table]] through one allocation's life, followed by an adversarial search for sequences that break the accounting.

This version assumes three adopted changes, all summarised in [[#Preliminaries]].
The **pin model**: a statement takes only `A` (the resolved size depends on it) and `F` (fragment) pins, never a `D` (denial) pin, which eliminates the unbounded over-count the earlier model suffered from.
The **`Grow`/`Shrink` split**: the old `Size` statement is separated into a size-reducing half that claims content and a size-increasing half that does not, which stops a run of grows from planting latent content claims.
And the **tombstone merge**: a `Tombstone` is treated as a `Shrink(id, 0)` that also denies existence, so it is an ordinary anchor rather than a separate kind of thing, which is what leaves only the two pin kinds above.

One correctness requirement survives the changes (scenario (v)) and one bounded over-count remains (scenario (vii)).

## Preliminaries

**Pins.**
A statement is live exactly while `pins > 0`, and `pins` is a single counter over heterogeneous holders:

| Pin | Held by                                                         | Taken when                                                                             | Released when                                                                                                                                                                                                    |
| --- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `F` | any statement                                                   | a fragment resolves *through* it                                                       | that fragment is destroyed or re-owned                                                                                                                                                                           |
| `A` | the **anchor** — the newest `Shrink` *or* `Tombstone` — and a `Grow` with `n == size` | the anchor: on becoming newest; a `Grow`: while it may be the sole witness of the size | the anchor: a newer `Shrink` or `Tombstone` supersedes it, or — a tombstone anchor only — `mentions` falls to 1, leaving it the last statement for its id; a `Grow`: as soon as `size > n`, or `n <= anchor.n`, or it falls below `anchor_epoch` — which tombstoning also causes, since the tombstone anchors above it |

**`Size` is split into `Grow` and `Shrink`.**
The old `Size(id, n)` did two jobs — anchoring the size, and claiming that everything at or past `n` is `Undefined` — and those two are needed in opposite directions.
A **shrink** needs both: it destroys content that must not resurface, and it must pull old extents out of the `max`.
A **grow** needs only the size: the territory it exposes is already denied by the last shrink, since anything that could cover a probe at or past the old size necessarily has an epoch below `anchor_epoch`, or was never occupied at all.
So a grow-emitted `Size` used to carry a *dormant* content claim that activated later, when the allocation grew past its bound — and that dormancy is what manufactured strays.
`Shrink(id, n)` keeps the old semantics; `Grow(id, n)` bounds the size from below and matches no probe.

**A `Tombstone` is a `Shrink(id, 0)` that also denies existence.**
It matches every probe, exactly as [[address-table#Conflict resolution across epochs]] says, and it takes the same pins as a `Shrink`: `A` while it is the anchor, `F` for every fragment it wins.
So a tombstone *can* own fragments — the ranges a re-allocated incarnation leaves uncovered, which it wins because it outranks everything older and nothing newer covers them.
**Re-allocated** means recycled in a *later* flush, with the tombstone still physically present beneath the new incarnation's statements, which recycling makes possible from the very next flush; freeing and re-allocating within one flush emits no tombstone at all and is a different case entirely ([[#Findings]] item 4).
While the id does not exist its size is 0, so no probes exist and it owns nothing; it is then the anchor, and that alone keeps it alive.

**One droppability rule covers both: a `Shrink` or `Tombstone` may be dropped iff it is not the anchor and owns no fragment.**
For a `Shrink(id, n)` this holds because a non-anchor cannot affect the size — its epoch is below `anchor_epoch`, and the `max` ranges only over `Grow` bounds and content extents above the anchor — and cannot affect content beyond the probes its `F` pins already count.
Nor can a later grow expose anything: the anchor always has `n <= size`, so it already denies every probe from `size` upward.
For a tombstone the same argument runs one step further: every probe in `[0, size)` is won by something newer, or the tombstone would own that probe; every probe `>= size` is denied by the anchor; and existence is decided by a statement newer than the tombstone, since one exists by assumption.
Nothing it was denying can resurface — *even though the statements it denies may still be physically present*, which is the over-conservatism the old `D` pin carried.

**The one thing pins do not decide on their own is the last tombstone.**
A tombstone that is the newest statement for its id is the anchor, so `A` keeps it alive, and that is correct: dropping it while any older statement mentioning the id is still physically present would resurrect the allocation.
It becomes droppable only once it is the *last* statement mentioning the id, when the id resolves as non-existent by default anyway.
That condition is `mentions == 1`, and it is the single remaining reader of `mentions`.
The rule is therefore that **a tombstone anchor releases `A` when `mentions` falls to 1**, which keeps `pins == 0` an exact droppability test everywhere.

Two things follow without further rules.
**Older tombstones need no special handling**: one with a newer tombstone above it is not the anchor and owns no fragment, since the newer one matches every probe and outranks it, so it is droppable on sight — no list of suppressors, and no `tombstone` pointer beside `anchor`.
And **an id is recyclable as soon as its tombstone is committed**, with no condition on `mentions` ([[address-table#Tombstones and id recycling]]): the tombstone matches every probe and outranks every older statement, so the old incarnation can never win one, however much of it survives physically.

A `Grow` is simpler than either — matching no probe, it can never own a fragment, so it holds `A` alone, dies as soon as `size > n` or `n <= anchor.n` or it falls below `anchor_epoch`, and at most one per allocation is ever alive.

**The fragment map.**
The fragment map is the resolved-content view: one ordered map keyed by `(id, offset)`, holding one entry per maximal range of an allocation that resolves the same way.

```rust
struct Fragment {
    source: Source,                   // where the bytes are
    statement: Option<StatementRef>,  // who is responsible for them; None = nobody is
}

enum Source {
    Bytes { page: PageNumber, offset: u16 },  // a data page, or the address-table page holding an `Inline` payload
    Undefined,
}
```

**A fragment stores no length**; its extent runs from its key to the next key for the same id, or to `AllocationMeta::size` for the last one.
That keeps a fragment small and a split cheap, and it is what makes the next rule mandatory rather than merely tidy.

**The fragments of an existing id exactly partition `[0, size)`.**
A hole is not representable: omitting an entry does not describe a gap, it extends the preceding fragment, and the lookup — "greatest key `<= probe`" — then answers with a neighbour's source.
That is a wrong-bytes bug rather than a missing-data bug, so a range that resolves to `Undefined` *by default* sits in the map like any other.
The only allocation with no fragments at all is a zero-sized one, where `[0, 0)` is empty and `Grow(id, 0)` is the allocation's only trace, in memory as on disk.

**`statement: None` marks a range that resolves by default**, because no statement matches those probes — the state a bare `Grow` leaves behind, or a `Ref` or `Inline` with `offset > previous_size`, which leaves `[previous_size, offset)` uncovered; in both cases only if the id has no anchor that would own the range (and it is the reason a `Grow` never owns a fragment).
Your rephrasing is correct, and the condition on the anchor is not just necessary but decisive: if an anchor exists it *always* owns such a range, and if none exists the range is *always* `None`.
An anchor `Shrink(id, n)` or `Tombstone` matches every probe `>= n`, hence every probe in the exposed range, and it outranks everything below it; nothing above it reaches there, since content above the anchor is bounded by the size the allocation had before this flush.
And if the id has no anchor, its size has never decreased, so every content statement it has is bounded by `previous_size` and none of them reaches into the gap either.

`None` fragments also arise away from any grow, wherever a range was simply never written: write `[0, 10)` and then `[20, 35)`, and `[10, 20)` is matched by nothing.
What an anchor does is put a floor under where such a hole can survive, since `Shrink(id, n)` matches every probe from `n` upward — so `None` fragments live only in `[0, n)`, and a `Tombstone`, anchoring at `n = 0`, leaves none at all.
That is why a tombstoned id's fragments are always owned, and why a re-allocated id's tombstone holds real `F` pins.

The two fields answer different questions: `source` says what the bytes *are*, `statement` says who is *responsible* for them, and a default range has a definite answer to the first and none at all to the second.
`None` is also what makes the pin arithmetic come out right, since such a fragment must pin nothing: a range that resolves by default depends on no statement staying alive.
Given a niche in `StatementRef`, the `Option` costs no memory.

| Field | Updated when | Value | Read for |
| --- | --- | --- | --- |
| `source` | when the range is resolved anew, and on either side of a split | the page and offset the bytes live at, or `Undefined` | serving reads; charging content bytes to the page that physically holds them, which is what accounts `Inline` payloads per byte without a per-statement counter |
| `statement` | on the same events, plus whenever a newer statement re-owns the range | the winning statement for this range, or `None` when nothing matches it | the `F` pin — creating the fragment takes one, destroying or re-owning it releases one; and telling a consolidator which fragments its victim's statements own |

Four consequences of the `Option`, all of which the implementation has to honour:

- **Coalescing compares both fields.** Adjacent fragments merge only when `source` *and* `statement` agree, so two `None` ranges merge, but a `Shrink`-owned `Undefined` and a `None` must not — they differ in exactly the thing that carries a pin.
- **A size increase adds at most one fragment, a write past the end at most two.** Raising `size` either extends the last fragment, when the exposed range resolves the way that fragment already does, or adds one entry for it. A `Ref` or `Inline` at `offset > size` adds that entry *and* its own, which is the only way a single statement adds two.
- **The blow-up is bounded.** `None` fragments are separated by owned ones, so they at most double an id's entry count. `fragment_count` counts them, since they are real entries with real memory cost; the defragmentation ranking pairs it with `statement_bytes`, so an id heavy in `None` fragments reads as many fragments for few statement bytes — correctly, since there is nothing on disk to reclaim there.
- **Every dereference of `statement` must handle `None`.** Re-owning a range decrements the previous owner's pins, and a `None` fragment has nothing to decrement; the consolidator's "keep the fragments whose `statement` matches" skips them for free, which is right, because no page holds a statement responsible for them and consolidation has nothing to re-emit.
- **Growth into default territory is free.** If the last fragment is already `None`, raising `size` extends it with no new entry at all, since its end is implied by `size` — the in-memory counterpart of a grow that emits nothing on disk.

**`StatementRecord`.**

```rust
struct StatementRecord {
    page: PageNumber,
    framing_len: u16,
    pins: u16, // Could be NonZeroU16 since the transition to `pins == 0` makes the statement dead
}
```

A `StatementRecord` is held *only for live statements*.
A record exists exactly while `pins > 0`; at the `1 → 0` transition its framing is released from the page's coverage, it is removed from `statement_bytes`, and the record is freed.
A dead statement survives purely as bytes in its page, counted in `mentions` and nowhere else, until a rewrite of that page decodes it and drops it — at which point `mentions` is decremented from the decoded bytes rather than from any record.
This is the concrete reason an id's physically present statements are unreachable ([[#(ii) Resize to 55 bytes]]): the only per-statement structure covers the live ones, and the dead ones are exactly the ones a shrink-emission test would need to see.

| Field | Updated when | Value | Read for |
| --- | --- | --- | --- |
| `page` | at creation only; a statement never moves (relocation means a *new* statement) | the page the flush writes it into | knowing whose `coverage` to decrement when the framing dies, and whether a consolidation victim holds this statement |
| `framing_len` | at creation only | the statement's encoded size **excluding** any `Inline` payload, since payload bytes are charged per byte through `fragment.source` instead | the amount subtracted from `coverage[page]` at the `1 → 0` pin transition; summed into `AllocationMeta.statement_bytes` |
| `pins` | on every fragment gain or loss, whenever the anchor or grow-witness changes hands, and — tombstone anchors only — when `mentions` falls to 1 | at creation: one per fragment it wins, plus 1 if it becomes the anchor or the grow witness | the liveness test `pins > 0`; the `1 → 0` transition releases `framing_len` from `coverage[page]` and removes the statement from `statement_bytes` |

Note that `pins` can *rise* after creation: growing an allocation can bring a range into existence that an existing `Shrink` wins, as Step 3 shows.

**`AllocationMeta`.**

```rust
struct AllocationMeta {
    size: Size,
    fragment_count: u32,
    statement_bytes: u32,
    mentions: u32,
    anchor: Option<StatementRef>,        // newest Shrink or Tombstone
    grow_witness: Option<StatementRef>,  // Grow with n == size, if any
}
```

**Note:** we should measure how many allocations have a `Some` value for `anchor` or `grow_witness`.
If this is rare in practice, we should outsource them to a side table.

**A tombstoned id has no `AllocationMeta` entry.**
The entry is removed by the flush that frees the id, and two fields move into the recyclable-id pool in its place:

```rust
struct RecyclableId {
    mentions: u32,                // physically present statements naming this id
    tombstone: Option<StatementRef>,  // None once it has been swept
}
```

Everything else is meaningless for a non-existent id and is reconstructed if the id is allocated again: `size` is 0, `fragment_count` is 0, `grow_witness` is `None`, and `statement_bytes` is the tombstone's own two bytes, already recorded in its `StatementRecord`.
The two that remain are the two that are read.
`mentions` still has to be decremented as statements naming the id are physically dropped, and still releases the tombstone's `A` pin at 1 — so a page rewrite that decodes a statement looks the id up in `AllocationMeta` first and in the pool if it is not there.
`tombstone` is what that release has to reach, what a consolidator consults to decide whether to re-emit a tombstone it has decoded, and what becomes `anchor` if the id is allocated again.

Within the pool `mentions` only ever falls, since nothing writes a statement naming a non-existent id; when it reaches 0 the tombstone has been swept, both fields are spent, and the entry is a bare recyclable id.
Re-allocation moves it the other way: the pool entry is removed and a fresh `AllocationMeta` is inserted with `mentions` carried over and `anchor` set to the tombstone, while `size`, `fragment_count` and `statement_bytes` start from the new incarnation's statements.

| Field             | Updated when                                                                                                                | Value                                                                                                                                                                 | Read for                                                                                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `size`            | any flush that resizes the id, writes past its end, frees it, or re-allocates it                                            | `0` if the id does not exist; otherwise `max(anchor's n, Grow bounds and extents of content statements with epoch > anchor_epoch)` — maintained incrementally rather than recomputed | answering `size()` in `O(1)`; bounding reads; deciding which probes are in range, hence which fragments exist at all; supplying the extent of an id's last fragment |
| `fragment_count`  | on every fragment created or destroyed for this id                                                                          | count of the id's fragments, **including** those with `statement: None`, which are entries like any other                                                              | defragmentation ranking — description overhead relative to `size`                                                                                         |
| `statement_bytes` | when a statement for this id is created, or when one's pins reach zero                                                      | sum of `framing_len` over the id's **live** statements                                                                                                                | defragmentation ranking, paired with `fragment_count`                                                                                                     |
| `mentions`        | `+1` per statement naming the id written or read at open; `−1` when one is physically dropped. Moves to the recyclable-id pool when the id is tombstoned, and back on re-allocation | count of statements naming the id that are **physically present** in live address-table pages — *not* the resolution-live ones                                        | one thing only: releasing a tombstone anchor's `A` pin when the count falls to 1, since the tombstone is then the last statement for its id and the id reads as non-existent without it. It does **not** gate id recycling, which needs only a committed tombstone |
| `anchor`, `grow_witness` | `anchor`: when a `Shrink` or a `Tombstone` is written. `grow_witness`: when a `Grow` is written, and when `size` moves past its bound | `anchor`: the id's newest `Shrink` or `Tombstone`, whose `n` is 0 for a tombstone. `grow_witness`: the `Grow` with `n == size`, if any — separate slots, since both can be pinned at once for different reasons                                                            | moving the `A` pin from the old anchor to the new one; telling a consolidator that a victim page holds an anchor it must replace (scenario (v))           |

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

**`mentions` is a net count**, so a flush that replaces a statement leaves it unchanged, and the order in which the flush applies its own writes and drops does not matter.
The previous model needed an explicit ordering rule here, because a new tombstone initialised its `D` count from `mentions` and would otherwise have counted the statements the same flush was dropping; with `D` gone, so is the rule.

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
| `AllocationMeta[7]` | `size` 1000, `fragment_count` 1, `statement_bytes` 7, `mentions` 1, `anchor` **None** |
| `coverage[P1]` | 4082, of which 1000 belong to id 7 |
| `coverage[L1]` | 4082 |

`anchor` is `None`: with no `Shrink` statement the resolved size is simply the largest content extent, `0 + 1000`, so there is no anchor and no `A` pin to hold.

### Step 2 (flush 5) — truncate to 10 bytes

`Shrink(7, 10)` is written into the header, then evicted to a fresh leaf `L2`@5 by further bulk traffic.
`anchor_epoch` becomes 5, so `size = max(10, extents with epoch > 5) = 10`, and probes `>= 10` are outside the allocation.

| Structure | Change |
| --- | --- |
| `Ref(7,0,1000,P1)`@3 | fragment narrows `[0,1000) → [0,10)`; pins stay **1** (`F`) |
| `Shrink(7,10)`@5 (new, `L2`) | pins **1** — `A` only. It owns no fragment (`[10, 10)` is empty), and under the adopted model it takes no denial pin against `Ref@3` |
| Fragment map | `(7, 0)` now covers `[0, 10)` |
| `AllocationMeta[7]` | `size` 10, `fragment_count` 1, `statement_bytes` 10, `mentions` 2, `anchor` `Shrink@5` |
| `coverage[P1]` | **−990** → 3092 |
| `coverage[L1]` | unchanged — a partially shadowed statement keeps its whole framing charge, which is right, since the entire encoding is still needed to describe the surviving 10 bytes |
| `coverage[L2]` | `+3` for `Shrink@5`'s framing |

Under the model this document replaces, this statement would have had pins 2.
The denial pin it no longer holds was the one that made it immortal in the old scenario (vii).

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
| `AllocationMeta[7]` | `size` 60, `fragment_count` 3, `statement_bytes` 16, `mentions` 3, `anchor` `Shrink@5` |
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
Previously this drop had to decrement a suppressor in an unrelated page; now dropping a content statement is a purely local event, except that it decrements `mentions`, which matters only to a tombstone that is the last statement standing for its id.

### (ii) Resize to 55 bytes

Flush 10 resizes allocation 7 to 55 bytes.
The header carries `Ref(7,50,10,PB)` forward, but at epoch 10 its extent (60) would exceed the new size, which [[address-table#No conflicts within each epoch]] forbids within one epoch — so the resolved-truth discipline narrows it on the way out to `Ref(7, 50, 5, PB)`.
This is a **shrink**, so the flush emits `Shrink(7, 55)`@10 — always, for the reason worked out below the table.
It happens to be unnecessary here, but the flush could not have known that, and it is **physically present** from this flush on; the table shows it.

| Structure                 | Change                                                                                                                                                                                                                                                                                                   |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Ref@3`                   | pins **1** (`F`, still owns `[0,10)`)                                                                                                                                                                                                                                                                    |
| `Shrink(7,10)`@5          | **loses `A`** to the newer `Shrink(7,55)`@10, which is now the anchor; keeps `F` (still owns the `Undefined` fragment `[10,50)`, which neither `Shrink(7,55)` — matching only probes `>= 55` — nor anything else newer covers) → pins **2 → 1** |
| `Ref(7,50,10,PB)`@9       | superseded by its narrowed self; record dropped                                                                                                                                                                                                                                                          |
| `Ref(7,50,5,PB)`@10 (new) | pins **1** (`F`)                                                                                                                                                                                                                                                                                         |
| `Shrink(7,55)`@10 (new)   | pins **1** = `A`; owns no fragment, since probes `>= 55` are outside the allocation; emitted although unnecessary, because the flush cannot tell                                                                                                                                                         |
| `AllocationMeta[7]`       | `size` 55, `fragment_count` 3, `statement_bytes` 19, `mentions` 4, `anchor` `Shrink@10`, `grow_witness` None                                                                                                                                                                                            |
| `coverage[PB]`            | **−5** — bytes `[PB+5, PB+10)` are now garbage                                                                                                                                                                                                                                                           |

The fragment map for id 7 after flush 10:

| key       | covers     | source      | statement           |
| --------- | ---------- | ----------- | ------------------- |
| `(7, 0)`  | `[0, 10)`  | `P1+0`      | `Ref@3`             |
| `(7, 10)` | `[10, 50)` | `Undefined` | `Shrink(7,10)`@5    |
| `(7, 50)` | `[50, 55)` | `PB+0`      | `Ref(7,50,5,PB)`@10 |

`Shrink(7,55)`@10 appears nowhere in it, which is the whole of its situation: it holds `A` and nothing else.
One subtlety: `Ref(7,50,5,PB)`@10 shares the anchor's epoch, so it is *not* in the `max`, which ranges over epochs strictly above `anchor_epoch`.
The size of 55 comes from the anchor's own `n`; the narrowed `Ref` merely fails to conflict with it (`50 + 5 = 55`, not `> 55`).

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
  Run directly, that check is not implementable: no structure reaches an id's physically present statements.
  The fragment map indexes only *live* fragments; `StatementRecord` exists only for statements that still have a reason to live, so a dead one has no record at all and survives purely as bytes in its page; and `mentions` is a count, not a list.
  Enumerating them would mean decoding every live address-table page, which nothing in the design does or needs to do (see below); it would certainly be absurd for a per-resize decision.
  It does not follow that the decision is *unknowable*, and the rule is not that kladde cannot tell.
  Everything that can lurk is leaf-resident, since the header is rewritten as resolved truth every flush and so holds exactly this flush's own output and never a dead statement.
  One bit per allocation could therefore make the test exact — set it whenever a statement naming the id is written into a non-header page above the id's `anchor_epoch`, clear it whenever a `Shrink` installs a new anchor; with the bit clear, the maximum that survives the flush is `max(anchor.n, the size claims of this flush's own output)`, and the statement may be omitted exactly when that equals `S_new`.
  This proposal declines to pay for it: the bit would cost a lookup and a store on the eviction path for every evicted statement, and a fourth piece of `AllocationMeta` state that would silently corrupt the file if it was ever maintained wrongly, to save three bytes on the subset of shrinks whose id has not been evicted since its last shrink.
  This proposal instead always emits `Shrink(id, S_new)` — this is sound, since an explicit `Shrink(id, T)` fixes the size at `T` whatever extents lurk in leaves, and a superfluous one retires at the very next shrink ([[#Findings]] item 9).
  So the honest motivation of the rule is **not** that kladde cannot know, but that the one bit which would decide it costs more to maintain than the statements it saves.

**Why enumeration of all physically present statements is never necessary.**
Consolidation decodes exactly one page: its victim.
It never needs an id's physically present statements across the table, because it only rewrites what the victim holds — resolved truth for the fragments the victim's statements own, plus a replacement anchor if the victim holds one ([[#(v) Consolidate `L2`, the leaf holding the anchor — break 1]]).
Both are answered from the fragment map and `AllocationMeta`, neither of which requires looking at any other page.
The one operation that *would* need a full sweep is the shrink-emission test in the direct form stated above, and the design's answer is to not run it.

Contrast a resize of 60 → **30**: `Ref(7,50,10,PB)`@9 falls entirely outside the new size, so it is dropped rather than narrowed, leaving nothing above epoch 5, and the size would come out as `max(10) = 10`.
There the `Shrink(7,30)` is genuinely required — which is the point of always emitting, since the flush separates the two cases only by computing the maximum that would survive it, and that is the computation it declines to pay for.

A superfluous `Shrink` is the same residual as [[#(vii) The immortal redundant suppressor — break 2]], caught at write time rather than at consolidation time.
Beware the near-miss version of the test: "the anchor is redundant whenever `size > n`" is **false**, because dropping an anchor also moves `anchor_epoch` and lets *older* extents back into the `max` — in the Step 3 state, where `Shrink@5` is the anchor with `size = 60 > 10`, dropping it would expose `Ref@3`'s extent of 1000 and yield 1000, not 60.
That is break 1, and it is why the test has to be run against the anchor that would remain, not against the one being considered.
A `Grow` carries no such hazard, since it anchors nothing.

**How long an old `Shrink` survives:**
walk through the following subsequent scenarios, focusing on what happens to `Shrink(7,10)`@5:

- (a) `Ref(7,50,5,PB)`@10 gets evicted to a non-header page at flush 11, then the allocation gets resized to 53 bytes at flush 12 (by only writing a `Shrink(7,53)` to the header page).
  How does kladde know that `Shrink(7,10)`@5 is still necessary although it is no longer the id's `AllocationMeta::anchor`?
- (b) continuing from (a), now the the content of the entire allocation (i.e., range `[0, 53)`) is overwritten.
  How does kladde know that `Shrink(7,10)@5` is now no longer necessary (but `Shrink(7,53)` is, to deny the bytes >= 53)?

> **Short answer: in (a) nothing special happens — the `F` pin alone keeps `Shrink@5` alive; in (b) nothing special happens either — losing its last fragment kills it.**
> Neither case needs a rule beyond [[#Findings]] item 2, and walking them through shows why that rule is a *conjunction*.
> The corrected state adds a third thing to watch: the superfluous `Shrink(7,55)`, and exactly when it retires.
>
> Starting state, the end of scenario (ii):
> `Ref(7,0,1000,P1)`@3 in `L1`, pins 1 (`F`, owns `[0,10)`);
> `Shrink(7,10)`@5 in `L2`, pins 1 (`F`, owns `[10,50)`);
> `Ref(7,50,5,PB)`@10 in `H`, pins 1 (`F`, owns `[50,55)`);
> `Shrink(7,55)`@10 in `H`, pins 1 (`A`); `size` 55, `anchor` `Shrink@10`.
>
> **(a) Eviction at flush 11, then resize to 53 at flush 12.**
> Eviction re-stamps: both header statements land in a leaf `L3`@11, as `Ref(7,50,5,PB)`@11 and `Shrink(7,55)`@11, pins unchanged; the anchor is now the re-stamped `Shrink@11`.
> The resize writes `Shrink(7, 53)` into the header at epoch 12, and that statement is genuinely required — without it the anchor stays `Shrink(7,55)` and the size stays 55.
>
> | Statement | After flush 12 |
> | --- | --- |
> | `Ref(7,0,1000,P1)`@3 | pins **1** (`F`, `[0,10)`) |
> | `Shrink(7,10)`@5 | pins **1** (`F`, `[10,50)`) — unchanged; it lost `A` back at flush 10 |
> | `Ref(7,50,5,PB)`@11 | fragment narrows `[50,55) → [50,53)`; pins **1**; `coverage[PB]` **−2** |
> | `Shrink(7,55)`@11 | **loses `A`** to `Shrink@12`; never owned a fragment → pins **1 → 0, dead**; `coverage[L3]` **−3** |
> | `Shrink(7,53)`@12 (new) | pins **1** = `A`; owns no fragment, since probes `>= 53` are outside the allocation |
>
> **How kladde knows `Shrink@5` is still needed: it still owns a fragment, so `pins > 0`.**
> That is the whole mechanism, and it needs no reference to the anchor at all.
> `Shrink(7,53)` matches only probes `>= 53`, so writing it re-resolved nothing in `[10, 50)`; that fragment's `statement` back-pointer still names `Shrink@5`, and its `F` pin was never touched.
> Substantively it *must* survive: drop it and `[10, 50)` falls through to `Ref@3`, re-exposing P1's bytes from the original 1000-byte write — the content the truncation at flush 5 destroyed.
> This is exactly why item 2 reads "not the anchor **and** owns no fragment": the conjuncts are independent, and here they disagree.
>
> The row to notice is `Shrink(7,55)`'s.
> It was emitted at flush 10 because the flush could not tell it was unnecessary; it lived as the anchor for two flushes; and the very next shrink retires it, because that shrink takes `A` and the statement never had anything else.
> This is the life cycle of every superfluous `Shrink`: present until the next shrink, then dead.
>
> **(b) Overwrite all of `[0, 53)` at flush 13** — into a fresh data page `PE`, emitting `Ref(7, 0, 53, PE)`@13 in the header.
>
> | Statement | After flush 13 |
> | --- | --- |
> | `Ref(7,0,1000,P1)`@3 | `[0,10)` shadowed → pins **1 → 0**, dead; `coverage[L1]` −7, `coverage[P1]` −10 |
> | `Shrink(7,10)`@5 | `[10,50)` shadowed → pins **1 → 0**, dead; `coverage[L2]` **−3** |
> | `Ref(7,50,5,PB)`@11 | `[50,53)` shadowed → pins **1 → 0**, dead; `coverage[L3]` −6, `coverage[PB]` −3 |
> | `Shrink(7,55)`@11 | already dead; still physically present in `L3` |
> | `Ref(7,0,53,PE)`@13 (new) | pins **1** (`F`, owns `[0,53)`) |
> | `Shrink(7,53)`@13 | carried in the header and re-stamped from 12; pins **1** = `A` |
>
> **How kladde knows `Shrink@5` is no longer needed: its last fragment was destroyed.**
> Writing `Ref(7,0,53,PE)`@13 re-resolved `[0, 53)`, which re-owned the `(7,10)` fragment; that destruction decremented `Shrink@5` from 1 to 0, and the `1 → 0` transition released its three framing bytes from `coverage[L2]` and removed it from `statement_bytes`.
> No test was run and no anchor consulted — the count simply reached zero.
>
> **Why `Shrink(7,53)` survives is the more interesting half, and the corrected state gives the cleaner reason.**
> After this flush *every* older statement for id 7 is dead, so one might expect the size to be implied by `Ref(7,0,53,PE)`'s own extent of 53.
> It is not.
> Drop `Shrink(7,53)` and the anchor falls back to the newest remaining `Shrink` — the dead-but-present `Shrink(7,55)`@11 in `L3` — so `anchor_epoch` becomes 11 and `size = max(55, extents above epoch 11) = max(55, 53) = 55`.
> The superfluous statement that was harmless while alive becomes harmful the moment its successor is removed: this is break 1 seen from the other side, an anchor's *predecessor* rather than its older extents doing the damage.
> `Shrink(7,53)`'s `A` pin is therefore doing real work, and stays until `L3` is cleaned.
>
> That carries a correction to the test stated earlier in this scenario: "the largest extent among content statements above `anchor_epoch`" must be read over **physically present** statements.
> Reading it over live ones is the same blind spot as [[#(v) Consolidate `L2`, the leaf holding the anchor — break 1]] — a statement that wins no probe is invisible in the fragment map yet still sets the size — and it makes the check cost a walk of the id's statements (bounded by `mentions`) rather than a glance at `size`.
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
Under the adopted model a dead content statement pins nothing; its only remaining hold on anything is the `mentions` count, and that matters to a tombstone alone — which is what narrows scenario (vi) to the free path.

### (iv) Free the allocation

Flush 10 emits `Tombstone(7)`, and the header stops carrying the id's content statements.
Existence is decided by the highest-epoch statement mentioning the id, so the allocation ceases to exist and `size` becomes 0.

| Structure | Change |
| --- | --- |
| `Ref@3` | no fragments → pins **0** |
| `Shrink(7,10)`@5 | loses `F` (no probes remain) and loses `A` to the tombstone, which is the newer anchor → pins **2 → 0**, immediately dead |
| `Ref(7,50,10,PB)`@9 | dropped with the header rewrite |
| `Tombstone(7)`@10 (new) | pins **1** = `A`. It is the id's newest `Shrink`-or-`Tombstone`, so it is the anchor, with `n = 0`; it owns no fragment, since `size` is 0 and no probes exist |
| `AllocationMeta[7]` | **removed** — for a non-existent id every field but `mentions` and the anchor is meaningless |
| recyclable-id pool | gains `7 → { mentions: 3, tombstone: Tombstone@10 }`; id 7 is available for re-allocation from the next flush |
| `coverage[P1]`, `coverage[PB]` | each loses its remaining referenced bytes |
| `coverage[L2]` | **−3** — `Shrink@5` dies at once instead of lingering |

**Re-allocation, worked through**, since it is where the tombstone stops being a formality.
This needs a separate sequence, on its own id 12, because id 7 is never re-allocated in this walkthrough.
Take `Ref(12, 0, 100, P1)`@3, then `Tombstone(12)`@5, then id 12 re-allocated at flush 7 to 100 bytes with only `[0, 10)` written, as `Ref(12, 0, 10, PX)`@7 **and** `Grow(12, 100)`@7 — the grow is required, since the flush's own output reaches offset 10, not 100, and without it the size would resolve to `max(10) = 10`.
Then a truncation to 50 at flush 9.

| Statement | After flush 7 | After flush 9 |
| --- | --- | --- |
| `Ref(12,0,100,P1)`@3 | pins **0** — every probe it matches is won by the tombstone or by `Ref@7` | pins **0** |
| `Tombstone(12)`@5 | pins **2** = `A` + `F`: still the newest `Shrink`-or-`Tombstone`, and it wins `[10, 100)`, which nothing newer covers | loses `A` to `Shrink@9`; keeps `F` over `[10, 50)` → pins **2 → 1** |
| `Ref(12,0,10,PX)`@7 (new) | pins **1** (`F`) | pins **1** |
| `Grow(12,100)`@7 (new) | pins **1** = `A` as the grow witness, `n == size == 100`; owns nothing | **dead** |
| `Shrink(12,50)`@9 (new) | — | pins **1** = `A` |
| bookkeeping | flush 7 removes `12` from the recyclable-id pool and inserts `AllocationMeta[12]` with `mentions` carried over and `anchor` = `Tombstone@5` | `anchor` = `Shrink@9` |

Three things in that table are worth naming.

**The tombstone earns an ordinary `F` pin**, over the range the new incarnation leaves uncovered, and that pin is doing real work: drop the tombstone and `[10, 50)` would resolve through `Ref(12, 0, 100, P1)`@3, serving the *first* incarnation's bytes as the second incarnation's content.
Under the floor framing this range was an unowned default and the tombstone was kept alive by `D` instead — which worked, but tied its retirement to every older statement being physically dropped rather than to anything about this allocation.
Covering `[10, 100)` with content now retires it on the spot.

**`Grow(12,100)`@7 dies by the third of its three death tests**, which nothing else in this document exercises.
`size > n` is false (50 < 100) and `n <= anchor.n` is false (100 > 50); what kills it is `Shrink(12,50)`@9 anchoring above its epoch, so it drops out of the `max` altogether.
That test exists exactly for an allocation shrunk below a standing grow bound.

**Nothing here needs a rule that a plain `Shrink` does not already need.**
The `Grow` cannot conflict with `Ref(12,0,10,PX)`@7 in the same epoch, since it claims no content and only bounds the size from below; and it does not disturb the anchor, since only a `Shrink` or `Tombstone` anchors.

Back to the freed id, which is the case where the tombstone owns nothing and `A` is all it has.
The cascade then clears itself with no special pass, driven by `mentions` rather than by pins on other statements:
consolidating `L1` drops `Ref@3` → `mentions` 3 → 2;
consolidating `L2` drops `Shrink@5` → `mentions` 2 → 1, which releases the tombstone's `A` pin → pins **1 → 0**;
the tombstone is then omitted by the next rewrite of its page, `mentions` reaches 0, and the pool entry is left holding nothing but the id itself.

That last release is the one place where a pin is not decided by the resolved view alone.
While any older statement for id 7 is still physically present, dropping the tombstone would let that statement decide existence again and resurrect the allocation, so the tombstone must outlive it — and the only structure that sees those statements is `mentions`.
Once `mentions` reaches 1 the tombstone is the id's last statement, the id reads as non-existent without it, and it can go.

Note that **id 7 is reusable from flush 11**, before any of that happens.
Recycling waits only on the tombstone being committed, not on `mentions` falling: the tombstone matches every probe and outranks every statement below it, so whatever of the old incarnation still sits in `L1` and `L2` can never win one.
The cascade above is therefore about reclaiming *bytes*, on its own schedule, and never about holding an id out of circulation.

The `coverage[L2]` row is a second, quieter win from the model change.
Previously `Shrink@5` retained a denial pin on `Ref@3` and stayed "live" until `L1` happened to be cleaned, so `L2` looked fuller than it was and was correspondingly less likely to be chosen; now it drops to zero at the moment of the free, when the information that it is garbage first exists.

### (v) Consolidate `L2`, the leaf holding the anchor — break 1

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
It is visible in exactly two places, `AllocationMeta.anchor` and the statement's own `A` pin, and under the adopted model the `A` pin is now the *only* extra pin a `Shrink` can hold, which makes the rule sharp:

> A page holding a statement with an `A` pin cannot be consolidated without transferring the anchor.

The fix emits a replacement alongside the content: `Shrink(7, 60)`@10 **and** `Undefined(7, 10, 40)`@10, which do not conflict within the epoch (`10 + 40 = 50 <= 60`).

| Structure | Change (with the fix) |
| --- | --- |
| `Shrink(7,10)`@5 | dropped; `mentions` −1 |
| `Shrink(7,60)`@10 (new) | pins **1** = `A`; owns no fragment |
| `Undefined(7,10,40)`@10 (new) | pins **1** (`F`) |
| `Ref@3` | unchanged, pins **1** |

The previous model obscured this by letting `D` pins share the blame; here the diagnosis is unambiguous.

**The rule now covers tombstones too**, since a tombstone can be the anchor, and the replacement it needs depends on which job it is doing.
If the id has been re-allocated, existence is decided by newer statements, so the replacement is an ordinary `Shrink(id, size)` plus `Undefined` coverage of whatever fragments the tombstone owned.
If the id does not exist, the replacement must be a `Tombstone(id)` again — a `Shrink` would resurrect it — unless `mentions == 1`, in which case the consolidator may emit nothing at all and let the id vanish.

### (vi) The hostage: a correct pin that cannot be released

Continue from scenario (iii), where `Ref@3` is dead but physically present in `L1`, a bulk-loaded page holding some 580 still-live statements.
Losing `Ref@3`'s 7 bytes takes `coverage[L1]` from 4082 to 4075 — a live fraction of 99.83 % — so victim selection will never choose `L1` while anything sparser exists.

**Under the adopted model this is no longer a hostage situation.**
`Ref@3` is dead and inert: it pins nothing, because nothing but a tombstone ever depends on another statement's physical presence, and there is no tombstone here.
Its continued presence costs 7 wasted bytes in `L1` and a `mentions` count that no live-allocation decision reads.
`Shrink@5` sits at pins 2 on its own merits (`A` + `F`) and is unaffected.

The effect survives only on the **free path**, and only for an id that is not re-allocated.
After scenario (iv), `Tombstone@10` is the anchor of a non-existent id, so its `A` pin is released only when `mentions` falls to 1 — which needs both `Ref@3` and `Shrink@5` physically dropped, and for `Ref@3` that means cleaning `L1`.
The observation that motivated this scenario therefore still applies, narrowed to that case:

> Victim selection ranked purely by live fraction cannot see the benefit of cleaning a page whose dead statements are holding a tombstone's `mentions` above 1.
> A benefit term counting "dead statements whose removal would let a tombstone retire" makes that visible.

A **re-allocated** id escapes it entirely, which is the merge's practical gain.
There the tombstone is kept alive by `A` and `F` on its own terms, so covering its gap with content or superseding it with a newer `Shrink` retires it immediately, no matter what still sits in cold pages.
Under the previous model its `D` pins waited on `L1` either way.

What is *not* at stake any more is the id.
An earlier draft gated recycling on `mentions == 0`, which put the id space itself behind this hostage: one dead seven-byte `Ref` in a page that is never chosen could hold an id out of circulation for the life of the file, and a workload that churns allocations would leak ids at the rate its cold pages resist cleaning.
Recycling now waits only on a committed tombstone, so the stake is two bytes of tombstone framing rather than an id.

The blast radius is much smaller than before in two independent ways: previously *any* dead content statement could strand a `Shrink` indefinitely on any allocation, live or freed, and the ids of freed allocations were hostage as well.

### (vii) The immortal redundant suppressor — break 2

**This break is eliminated by the adopted model**, and branching from the corrected scenario (ii) shows it without recourse to a hypothetical.

Continue from the end of [[#(ii) Resize to 55 bytes]], where `Shrink(7,55)`@10 is the anchor and `Shrink(7,10)`@5 is therefore *already* a non-anchor, alive on a single `F` pin over `[10, 50)`.
Let flush 11 rewrite the allocation entirely: the application overwrites `[0, 55)`, so the flush writes data page `PD`, and the header emits `Ref(7, 0, 55, PD)`@11, carries `Shrink(7,55)` forward re-stamped to epoch 11, and drops the shadowed `Ref(7,50,5,PB)`.
The overwrite is not a resize, so it emits no size statement of its own.

State afterwards: `L1`@3 holds `Ref(7,0,1000,P1)`, `L2`@5 holds `Shrink(7,10)`, and `H`@11 holds `Ref(7,0,55,PD)` and `Shrink(7,55)`.
`anchor_epoch` is 11, `size` is 55, and `[0, 55)` resolves entirely through `Ref@11`.

| Statement | After flush 11 |
| --- | --- |
| `Ref(7,0,1000,P1)`@3 | `[0,10)` shadowed → pins **1 → 0**, dead; `coverage[L1]` −7, `coverage[P1]` −10 |
| `Shrink(7,10)`@5 | `[10,50)` shadowed → pins **1 → 0**, dead; `coverage[L2]` **−3** |
| `Ref(7,50,5,PB)`@10 | dropped with the header rewrite; `coverage[PB]` −5 |
| `Ref(7,0,55,PD)`@11 (new) | pins **1** (`F`, owns `[0,55)`) |
| `Shrink(7,55)`@11 | re-stamped from epoch 10; pins **1** = `A` |
| `AllocationMeta[7]` | `size` 55, `fragment_count` 1, `statement_bytes` 9, `mentions` 4, `anchor` `Shrink@11` |

`Shrink(7,10)`@5 is the statement the break was about, and it now sits in exactly the position the old model mishandled: a non-anchor that has just lost its last fragment.

- **Previous model:** it still held `D` on `Ref@3` — dead, but physically present in `L1` — so pins 1.
  That pin could only be released by cleaning `L1`, which scenario (vi) showed never happens, and consolidating `L2` merely carried the pin forward into the replacement statement.
  `coverage[L2]` over-reported by 3 bytes permanently, which biased `L2` *away* from cleaning — a self-reinforcing error — and `N` resizes could strand `Θ(N)` such statements.
- **Adopted model:** pins **0**.
  The statement is dead the moment the overwrite lands, `coverage[L2]` falls by 3 immediately, and the next rewrite of `L2` drops it.

Branching from (ii) is what makes that comparison mean anything.
An earlier version of this scenario built its state from (iii) via (vi), where `Shrink(7,10)`@5 is still the *anchor*, and then asked the reader to take the anchor out of the picture for a moment — a re-anchoring it never performed, since that needs a later `Shrink` the branch never writes.
With the `A` pin in place the old model's `D` pin decides nothing, so the demonstration was vacuous exactly where it mattered; `Shrink(7,55)`@10 is the later `Shrink` it was missing.

**How we know that no physically present statement reaches past the size.**
Not by checking — the size *is* that maximum, by construction.
Content is only ever emitted within `[0, size)` at its own epoch, and the size can decrease only through a shrink, which installs a new anchor as it does so; so every physically present statement above `anchor_epoch` was written when the size was at most what it is now, and everything below `anchor_epoch` — `Ref@3`, with its extent of 1000 — is excluded from the `max` by the anchor.
The semantic size and the physical maximum coincide, and keeping them in step is precisely what the `A` pin protects.

The residual, and it is worth being precise that one remains.
A `Shrink` that **is** the anchor can still be redundant: with `Shrink(7, 100)`@5 and `Ref(7, 0, 100, X)`@9, dropping it would leave `size = max(100) = 100`, unchanged — yet `A` keeps it alive.
Three things distinguish this from the old break:

1. It is **not** bounded at one per allocation, as an earlier version of this section claimed.
   A non-anchor `Shrink` lingers whenever it owns a fragment, and it can own one *while being redundant* — when the range it wins would resolve to `Undefined` anyway, because nothing older matches it.
   In fragment-map terms the redundancy is sharp: dropping such a statement would turn its `Some(Shrink)` fragment into a `None` fragment covering the same range with the same resolved content, and the only thing standing in the way of doing so is that nothing can prove no older statement matches.
   Building such a chain now requires *alternating* shrinks and grows, though, which is what the `Grow`/`Shrink` split of [[#Preliminaries]] buys: a run of plain grows plants no content claims at all.
   Take an allocation created empty and grown three times without writing: under the old single `Size`, `Size(7,10)`@5, `Size(7,20)`@7 and `Size(7,30)`@8 followed by `Ref(7,50,10,PX)`@9 left three fragment-owning strays over `[10,20)`, `[20,30)` and `[30,50)`; with `Grow(7,10)`@5, `Grow(7,20)`@7 and `Grow(7,30)`@8 the same history leaves **none**, since `[0,50)` is then unowned default and all three are dead the moment `size` exceeds their bounds.
   What still bounds the alternating case is derived in [[#How stray `Size` statements accumulate and are pruned]] below.
2. It is **cheaply correctable for `Grow`, not for `Shrink`**.
   A `Grow` is dead as soon as `size > n`, or `n <= anchor.n`, or it falls below `anchor_epoch` — all `O(1)` tests — so at most one per allocation is ever alive.
   For a `Shrink`, deciding whether anything older is left to deny needs the id's physically present statements, which nothing reaches.
   This is a strictly harder question than the shrink-emission test of [[#(ii) Resize to 55 bytes]], and no single bit summarises it: there, all that matters is whether anything lurks above the anchor at all; here, what matters is which *ranges* the lurkers cover.
3. It is **not self-reinforcing at scale**: three bytes per stray does not meaningfully shift a page's live fraction, whereas the old pathology compounded through the coverage counter.

### How stray `Size` statements accumulate and are pruned

Take the alternating history that can still build a chain — `Ref(7,0,1000,P1)`@3 from Step 1, `Shrink(7,10)`@5, grows to 20 and 30, `Ref(7,50,10,PX)`@9 taking the id to 60 — and continue it with `Shrink(7,15)`@11 and then `Ref(7,70,10,PY)`@13.
Assume each statement is evicted to a leaf in the flush that writes it, so epochs stay put.

**Grows never join the chain.**
`Grow(7,20)`@7 and `Grow(7,30)`@8 match no probe, so they own nothing throughout; each is the grow witness for one flush, and `Grow@7` is dead as soon as `Grow@8` takes `size` past 20, `Grow@8` as soon as `Ref@9` takes it past 30.
Only `Shrink(7,10)`@5 owns anything — `[10,50)` — and it is the anchor, doing the job the truncation created it for: `Ref@3` still sits beneath it, covering everything up to 1000.

**A later shrink prunes, permanently.**
`Shrink(7,15)`@11 takes the anchor and retracts the size to 15, so `[15,50)` leaves the allocation and `Shrink@5`'s fragment narrows to `[10,15)`; `Ref(7,50,10,PX)`@9 loses its range entirely and dies.
`Shrink@5` survives on that window, which `Shrink@11` does not match.

**The later grow does not revive anything.**
`Ref(7,70,10,PY)`@13 takes the id back to 80, yet `Grow@7`, `Grow@8` and `Ref@9` all stay at zero pins: `Shrink@11` has a *lower* bound and a *higher* epoch, so it matches and outranks them at every probe `>= 15`.
What it does instead is hand `Shrink@11` a fragment over `[15,70)`, taking it from 1 pin to 2 — another instance of pins rising after creation.

**The rule, and hence the bound.**
A dead `Shrink(n)` can only revive if some probe `>= n` becomes winnable, which requires every newer `Shrink` to have a bound strictly greater than that probe.
Order an id's physically present `Shrink` statements newest-first: one can hold an `F` pin only if its bound is strictly below the minimum bound among all newer ones, so **the live set is contained in the running-minimum chain** — a link of the chain can still own nothing, if newer content covers its whole window.
Here that chain is `Shrink@11` (15) → `Shrink@5` (10), and both links do own something.
Each extra link costs an *alternating* shrink and grow — a run of grows contributes nothing, since `Grow` statements are absent from the chain by construction — and **any shrink to bound `b` permanently kills every `Shrink` with bound `>= b`**.
The stray count is the length of the decreasing-bound chain, pruned by every shrink: a precise characterisation rather than a constant, and one the split makes much harder to grow.

**A surviving `Shrink` is not necessarily a stray**, and in this history neither one is.
`Shrink@11` is load-bearing: drop it and `anchor_epoch` falls to 5, so `[50,60)` resolves through `Ref(7,50,10,PX)`@9 — dead, but still physically present in its leaf — re-exposing stale bytes from `PX`.
`Shrink@5` is load-bearing too: drop it and `[10,15)` falls through to `Ref@3`, re-exposing bytes from `P1`.
It turns into a stray only once `L1` is consolidated and `Ref@3` narrowed to `[0,10)`, after which its window would read `Undefined` by default — and nothing about `Shrink@5` itself changes when that happens.
Distinguishing the two cases is exactly the "is there anything older to deny" predicate that [[#(ii) Resize to 55 bytes]] shows we cannot evaluate cheaply, which is why `Shrink` strays are tolerated rather than detected — and exactly the predicate a `Grow` never needs, since it denies nothing.

## Findings

1. **Consolidating a page that holds an id's anchor must transfer the anchor** (scenario (v)).
   The `A` pin is the signal, and it is the only extra pin a `Shrink` or `Tombstone` can hold, so the test is unambiguous.
   A fragment-map-driven consolidator cannot derive this on its own, because the anchor is not a fragment; it must consult `AllocationMeta.anchor` or the pin.
   A `Grow` carries no such obligation, since it anchors nothing.
   For a tombstone anchor the replacement is a `Shrink(id, size)` if the id has been re-allocated, and a `Tombstone(id)` again if it has not — a `Shrink` there would resurrect the id.
2. **A `Shrink` or `Tombstone` may be dropped iff it is not the anchor and owns no fragment.**
   Its denials are subsumed by the anchor (which always denies from `size` upward) together with whatever newer content statements cover the rest, so denial pins are redundant — the result adopted here.
   For a tombstone the argument runs one step further, since existence is then decided by a statement newer than it.
3. **Nothing older than the newest physically present tombstone is live.**
   Such a statement cannot hold `F`, since the tombstone matches every probe and outranks it at all of them; it cannot be the anchor, which sits at or above the tombstone's epoch; and it cannot be the grow witness, since a `Grow` below `anchor_epoch` is dead by its third death test.
   Several arguments here rest on this lemma, and two consequences follow from it with no bookkeeping at all.
   **At most one tombstone per id is ever live**, so `AllocationMeta` needs no `tombstone` pointer beside `anchor` and tombstone chains need no special handling, even though recycling does not wait for the old tombstone to die — the older one is neither the anchor nor an owner of fragments, so item 2 retires it on sight.
   And a tombstone is the one statement kind for which item 8's running-minimum chain is capped at length one: it is that chain's bound-`0` element, and a strictly decreasing chain of non-negative bounds holds at most one `0`.
   That is the whole of the difference between the two — the chain is unbounded for `Shrink` because `Shrink(id, m)` still wins the probes in `[m, n)` that a newer `Shrink(id, n)` does not match, and there is no bound below `0` for a tombstone to leave uncovered.
4. **A tombstone is a matcher, hence an ordinary anchor** — a `Shrink(id, 0)` that also denies existence.
   It wins the ranges a re-allocated incarnation leaves uncovered, taking `F` pins for them, and it is the newest `Shrink`-or-`Tombstone`, taking `A`.
   This reverses an earlier decision in favour of an epoch-floor framing, which had been adopted to keep `mentions == 1` an exact droppability test; the matcher framing is what makes one droppability rule cover both kinds, and it costs no fragment-map entry, only a pin, since the uncovered range has an entry either way.
   An id is still reusable as soon as its tombstone is committed, with **no condition on `mentions`**: the tombstone outranks everything below it, so leftover statements can never win a probe and can never consume the id space.
   Freeing and re-allocating within a *single* flush is the one case needing care — a tombstone and the new incarnation would make contradicting existence claims in one epoch — and is handled by emitting statements that fully cover the new extent instead of a tombstone.
5. **`mentions` survives the merge, with exactly one reader.**
   A tombstone that is the newest statement for its id is the anchor, so `A` keeps it alive; it may be dropped only once it is the id's *last* physically present statement, since until then dropping it would let an older statement decide existence and resurrect the allocation.
   That is `mentions == 1`, and the rule is that a tombstone anchor releases `A` at that moment, which keeps `pins == 0` an exact droppability test.
   The merge was expected to eliminate `mentions` along with the `D` pin; it does not, because no other structure sees an id's physically present statements.
   What the merge does buy here is that `mentions` is the *only* thing a tombstoned id still needs, alongside a reference to the tombstone itself, so its `AllocationMeta` entry is dropped at the free and those two fields move to the recyclable-id pool.
6. **Cleaning should be ranked by retirements enabled as well as bytes reclaimed** (scenario vi), and the case is now confined to the tombstone of an id that was freed and never re-allocated, whose `mentions` cold pages hold above 1.
   A re-allocated id's tombstone escapes it entirely, since `A` and `F` decide its fate on this allocation's own terms.
7. **A statement holds at most two pin kinds, `A` and `F`** — and a `Grow` holds only `A`.
   There is no third kind left in the model.
   The single-`pins` argument survives: `Shrink@5` holds `A` and `F` together in Step 3, a re-allocated id's tombstone holds both in scenario (iv), no consumer ever asks which kind a pin is, and two fields would still encode a distinction that is never read.
8. **A residual over-count remains, and it is not bounded at one per allocation** (scenario vii) — but the `Grow`/`Shrink` split narrows it sharply.
   Redundant `Shrink` statements survive on `F` pins over ranges that would read `Undefined` anyway; the live set is contained in the running-minimum-bound chain read newest-first, and any shrink to bound `b` permanently kills every `Shrink` with bound `>= b`, while growth never revives one.
   Each extra link now costs an *alternating* shrink and grow, since `Grow` statements match no probe and so never join the chain — where a run of plain resizes used to strand one apiece.
   Whether a link is a stray or load-bearing depends on what is physically present beneath it, which nothing can see: the same `Shrink` is load-bearing while the extent it truncated is still in a leaf and a stray once that leaf has been consolidated.
9. **The emission rule splits by direction, and only the shrink half is undecidable.**
   A **grow** emits `Grow(id, S_new)` iff nothing the flush itself writes reaches `S_new` — exact and `O(1)`, since everything else is bounded by the old size — and needs no content protection, the anchor already denying the exposed territory.
   A **shrink** always emits `Shrink(id, S_new)`, because the principled test needs the id's *physically present* statements and nothing reaches them: the fragment map indexes only live fragments, `StatementRecord` exists only for statements with a reason to live, and `mentions` is a count rather than a list.
   Always emitting can only be superfluous, never wrong, at three bytes a time — and a superfluous `Shrink` is physically present from its flush on, takes the anchor from its predecessor, holds `A` alone, and dies at the next shrink unless an intervening grow handed it fragments (scenario (ii)).
   State tables must show it; semantic redundancy and physical presence are different questions.
10. **A `Grow` is locally decidable, which a `Shrink` is not.**
    It matches no probe, so it never owns a fragment; it is dead as soon as `size > n`, or `n <= anchor.n`, or it falls below `anchor_epoch`; and at most one per allocation is ever alive, since the bounds of the `Grow` statements above the anchor are distinct and only the one equal to `size` can be pinned.
    Dropping one can never readmit older extents, so it has no break-1 hazard either.
11. **Confirmed sound:** the pin graph is acyclic, and trivially so now that both remaining kinds point the same way — `F` and `A` both point from the resolved view into statements, and no statement pins another — so no set of statements can mutually pin itself alive.
    The `D` pin was the only statement-to-statement edge the model ever had.
12. **Confirmed sound:** content statements need no denial pins, because a content statement's denial is co-extensive with what it defines, and covering a range requires some newer statement to reach its far end, so the size cannot shrink when one is dropped.
13. **A `Shrink` can hold the size up as well as down** (scenario (ii), after the resize to 30).
    As anchor it contributes its own `n` to the `max`, so once consolidation has removed the older extents it excluded, an otherwise identical file *without* it holds a smaller allocation; the statement is then functionally a `Grow`, but cannot be rewritten as one, since no consolidator can verify that nothing older still reaches past `n`.
14. **A dead predecessor makes an anchor load-bearing** (scenario (ii), case (b)).
    With every statement above the anchor dead, dropping the anchor still re-anchors at the newest physically present `Shrink` beneath it — possibly one that is itself dead and superfluous — and readmits *its* bound; break 1 from the other side, and a second reason the `A` pin is not substitutable by any test on live state.
15. **`None` fragments are never-written holes that no anchor reaches** ([[#Preliminaries]]).
    An anchor matches every probe from its own bound upward, so it owns whatever a grow or an out-of-range write exposes and `None` fragments can survive only in `[0, n)`; an id with no anchor has never decreased in size, so nothing reaches into an exposed range there and the whole of it is `None`.
    A `Tombstone` anchors at `n = 0` and therefore leaves no such hole at all, which is why a re-allocated id's tombstone holds genuine `F` pins.
    This is also why a `Grow` can never own a fragment, and why the `Option` in `Fragment::statement` is not a loose end but the exact shape of the model.
