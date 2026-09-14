# Address Table: A Worked Walkthrough

A step-by-step trace of the bookkeeping structures of [[address-table]] through one allocation's life, followed by an adversarial search for sequences that break the accounting.

This version assumes the **adopted pin model**, in which a `Size` statement takes only `A` (size authority) and `F` (fragment) pins and never a `D` (denial) pin.
That change was derived in an earlier revision of this document and is summarised in [[#Preliminaries]]; it eliminates the unbounded over-count that the previous model suffered from, and it makes one earlier claim obsolete — a `Size` can no longer hold all three pin kinds at once, only two ([[#Findings]], item 6).

One correctness requirement survives the change (scenario (v)) and one bounded over-count remains (scenario (vii)).

## Preliminaries

**Pins.**
A statement is live exactly while `pins > 0`, and `pins` is a single counter over heterogeneous holders:

| Pin | Held by | Taken when | Released when |
| --- | --- | --- | --- |
| `F` | any statement | a fragment resolves *through* it | that fragment is destroyed or re-owned |
| `A` | the newest `Size` for an id | it becomes the id's size authority | a newer `Size` supersedes it, or the id is tombstoned |
| `D` | the newest `Tombstone` for an id | initialised to the count of physically-present statements naming the id | one of those statements is physically dropped |

The model is deliberately asymmetric, and the asymmetry is the point.
A `Tombstone` claims **every** probe and denies existence outright, so it genuinely denies every older statement naming the id; `D` is exact and `mentions` computes it in `O(1)`.
A `Size(id, n)` claims only `[n, ∞)`, so it denies an older statement only when that statement reaches past `n` — a count no allocation-level counter can produce — and it turns out never to need the pin at all: **a `Size` may be dropped iff it is not the authority and owns no fragment**, because a non-authority `Size` cannot affect the size (its epoch is below `size_epoch`, and the `max` term ranges only over content statements), and it cannot affect content beyond the probes its `F` pins already count.
Nor can a later grow expose anything: the newest `Size` always has `n_new <= size`, so it already denies every probe from `size` upward.

Only the **newest** suppressor of each kind carries pins.
For tombstones this is exact for the same reason: a newer tombstone subsumes an older one entirely, so older tombstones are droppable on sight, and `AllocationMeta` needs a single `tombstone` pointer rather than the list of suppressors the previous model required.

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
| `pins` | on every fragment gain or loss, on every change of size authority, and — tombstones only — whenever a denied statement is physically dropped | at creation: one per fragment it wins, plus 1 if it becomes the authority, plus (tombstone only) `mentions` read *before* the flush's own additions and *after* its own drops | the liveness test `pins > 0`; the `1 → 0` transition releases `framing_len` from `coverage[page]` and removes the statement from `statement_bytes` |

Note that `pins` can *rise* after creation: growing an allocation can bring a range into existence that an existing `Size` wins, as Step 3 shows.

**`AllocationMeta`.**

```rust
struct AllocationMeta {
    size: Size,
    fragment_count: u32,
    statement_bytes: u32,
    mentions: u32,
    size_statement: Option<StatementRef>,
    tombstone: Option<StatementRef>,
}
```

| Field | Updated when | Value | Read for |
| --- | --- | --- | --- |
| `size` | any flush that resizes the id, writes past its end, frees it, or re-allocates it | `0` if the id does not exist; otherwise `max(authority's n, extents of content statements with epoch > size_epoch)` — maintained incrementally rather than recomputed | answering `size()` in `O(1)`; bounding reads; deciding which probes are in range, hence which fragments exist at all |
| `fragment_count` | on every fragment created or destroyed for this id | count of the id's live fragments | defragmentation ranking — description overhead relative to `size` |
| `statement_bytes` | when a statement for this id is created, or when one's pins reach zero | sum of `framing_len` over the id's **live** statements | defragmentation ranking, paired with `fragment_count` |
| `mentions` | `+1` per statement naming the id written or read at open; `−1` when one is physically dropped | count of statements naming the id that are **physically present** in live address-table pages — *not* the resolution-live ones | initialising a new `Tombstone`'s `D` count, where it is exact; deciding id recycling (`mentions == 1` makes the tombstone droppable, `== 0` frees the id) |
| `size_statement` | when a `Size` is written, when the id is tombstoned (cleared), or when it is re-allocated | the id's current size authority, i.e. the newest `Size` above `tombstone_epoch` | moving the `A` pin from the old authority to the new one; telling a consolidator that a victim page holds an authority it must replace (scenario (v)) |
| `tombstone` | when a `Tombstone` is written (the new one takes the `D` pins, the old one is released outright) or when the tombstone dies | the id's newest `Tombstone`, if any | finding the single statement to decrement when a statement naming the id is physically dropped |

**Epochs belong to pages, not to statements.**
A statement inherits the epoch of the page it currently sits in, so moving a statement re-stamps it.
The header is rewritten every flush and states resolved truth, so everything in the header always carries the current epoch, and multi-epoch disagreement can exist only between the header and evicted leaves, or between two leaves.
This is why the example below needs evictions to arise at all.

**Framing sizes**, from the grammar in [[address-table#Physical level]], assuming a one-byte `id_delta` and two-byte addresses:

| Statement | Encoding | `framing_len` |
| --- | --- | --- |
| `Ref(7, 0, 1000, P1)` | 1 + 1 + 1 + 2 + 2 | 7 |
| `Ref(7, 50, 10, PB)` | 1 + 1 + 1 + 1 + 2 | 6 |
| `Size(7, n)` | 1 + 1 + 1 | 3 |
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
| `AllocationMeta[7]` | `size` 1000, `fragment_count` 1, `statement_bytes` 7, `mentions` 1, `size_statement` **None**, `tombstone` None |
| `coverage[P1]` | 4082, of which 1000 belong to id 7 |
| `coverage[L1]` | 4082 |

`size_statement` is `None`: with no `Size` statement the resolved size is simply the largest content extent, `0 + 1000`, so there is no authority and no `A` pin to hold.

### Step 2 (flush 5) — truncate to 10 bytes

`Size(7, 10)` is written into the header, then evicted to a fresh leaf `L2`@5 by further bulk traffic.
`size_epoch` becomes 5, so `size = max(10, extents with epoch > 5) = 10`, and probes `>= 10` are outside the allocation.

| Structure | Change |
| --- | --- |
| `Ref(7,0,1000,P1)`@3 | fragment narrows `[0,1000) → [0,10)`; pins stay **1** (`F`) |
| `Size(7,10)`@5 (new, `L2`) | pins **1** — `A` only. It owns no fragment (`[10, 10)` is empty), and under the adopted model it takes no `D` pin against `Ref@3` |
| Fragment map | `(7, 0)` now covers `[0, 10)` |
| `AllocationMeta[7]` | `size` 10, `fragment_count` 1, `statement_bytes` 10, `mentions` 2, `size_statement` `Size@5`, `tombstone` None |
| `coverage[P1]` | **−990** → 3092 |
| `coverage[L1]` | unchanged — a partially shadowed statement keeps its whole framing charge, which is right, since the entire encoding is still needed to describe the surviving 10 bytes |
| `coverage[L2]` | `+3` for `Size@5`'s framing |

Under the previous model this statement would have had pins 2.
The `D` pin it no longer holds was the one that made it immortal in the old scenario (vii).

### Step 3 (flush 9) — grow to 60 and write `[50, 60)`

The application grows allocation 7 to 60 bytes and writes 10 bytes at offset 50, into data page `PB`; `Ref(7, 50, 10, PB)` goes into the header and stays there, being hot.
`size_epoch` is still 5, so `size = max(10, 50 + 10) = 60`.

| Range | Matching statements | Winner | Source |
| --- | --- | --- | --- |
| `[0, 10)` | `Ref@3` | `Ref@3` | `P1` |
| `[10, 50)` | `Ref@3`, `Size@5` (since `10 <= probe`) | **`Size@5`** | `Undefined` |
| `[50, 60)` | `Ref@3`, `Size@5`, `Ref(50,10)@9` | `Ref@9` | `PB` |

The middle row is the one to watch: growing the allocation brought `[10, 50)` into existence, and it resolves *through* the `Size` statement, which therefore **gains a fragment pin**.

| Structure | Change |
| --- | --- |
| `Ref(7,0,1000,P1)`@3 | pins **1** (`F`) — unchanged |
| `Size(7,10)`@5 | pins **1 → 2** = `A` + `F` (owns `[10, 50)`) |
| `Ref(7,50,10,PB)`@9 (new, `H`) | pins **1** (`F`) |
| Fragment map | `(7,0) → P1+0`; `(7,10) → Undefined, stmt Size@5`; `(7,50) → PB+0` |
| `AllocationMeta[7]` | `size` 60, `fragment_count` 3, `statement_bytes` 16, `mentions` 3, `size_statement` `Size@5`, `tombstone` None |
| `coverage[PB]` | `+10` |

This is the state the scenarios branch from.
It is also the witness that `pins` must remain a single counter over heterogeneous holders: `Size@5` holds `A` and `F` simultaneously, and no consumer ever asks which is which.

## Scenarios

Each scenario branches from the end of Step 3 unless stated otherwise.

### (i) Consolidate `L1`, the leaf holding `Ref@3`

`L1` is rewritten as resolved truth at epoch 10.
`Ref@3` owns exactly one fragment, `[0, 10)`, so it is re-emitted narrowed as `Ref(7, 0, 10, P1)`@10, `framing_len` 6.

| Structure | Change |
| --- | --- |
| `Ref@3` | fragment re-owned by the new statement → pins **1 → 0** → framing released; then physically dropped with the page |
| `Size(7,10)`@5 | **unchanged at 2** — under the adopted model it never held a pin on `Ref@3`, so the drop does not reach it |
| `Ref(7,0,10,P1)`@10 (new) | pins **1** (`F`) |
| Fragment map | `(7,0)`'s `statement` re-pointed; `source` unchanged |
| `AllocationMeta[7]` | `statement_bytes` 16 → 15; `mentions` 3 → 3 (one dropped, one added) |
| `coverage[L1]` | → 0; reusable after the two-epoch quarantine |
| `coverage[P1]` | **unchanged** — consolidating the address table moves no data |

The middle row is the whole benefit of the model change in one line.
Previously this drop had to decrement a suppressor in an unrelated page; now dropping a content statement is a purely local event, because nothing but a tombstone ever depends on a content statement's continued physical presence.

### (ii) Resize to 55 bytes

Flush 10 writes `Size(7, 55)` into the header.
The header also carries `Ref(7,50,10,PB)` forward, but at epoch 10 its extent (60) would exceed the new size, which [[address-table#No conflicts within each epoch]] forbids within one epoch — so the resolved-truth discipline narrows it on the way out to `Ref(7, 50, 5, PB)`.

| Structure | Change |
| --- | --- |
| `Ref@3` | pins **1** (`F`, still owns `[0,10)`) |
| `Size(7,10)`@5 | loses `A` to the newer `Size`; keeps `F` (still owns `[10,50)`, since `Size(7,55)` matches only probes `>= 55`) → pins **2 → 1** |
| `Ref(7,50,10,PB)`@9 | superseded by its narrowed self; record dropped |
| `Size(7,55)`@10 (new) | pins **1** = `A`. It owns no fragment, since probes `>= 55` are outside the allocation, and it takes no denials |
| `Ref(7,50,5,PB)`@10 (new) | pins **1** (`F`) |
| `AllocationMeta[7]` | `size` 55, `fragment_count` 3, `statement_bytes` 19, `mentions` 4, `size_statement` `Size@10`, `tombstone` None |
| `coverage[PB]` | **−5** — bytes `[PB+5, PB+10)` are now garbage |

`Size@5` survives here on its `F` pin alone, and correctly so: it is the statement making `[10, 50)` read as `Undefined` rather than exposing `Ref@3`'s bytes.
Under the previous model it would have shown pins 2, with the extra pin contributing nothing, and `Size@10` would have shown 3 instead of 1.
The previous model also needed `suppressors` to become a two-element list at this point; the adopted model keeps `tombstone` at `None`.

### (iii) Overwrite bytes `[0, 20)`

Flush 10 writes data page `PC` and emits `Ref(7, 0, 20, PC)`@10; the header carries `Ref(7,50,10,PB)` forward at epoch 10, disjoint and so conflict-free.
`size_epoch` is still 5, so `size = max(10, 20, 60) = 60`, unchanged.

| Range | Winner | Source |
| --- | --- | --- |
| `[0, 20)` | `Ref(0,20)@10` | `PC` |
| `[20, 50)` | `Size@5` | `Undefined` |
| `[50, 60)` | `Ref(50,10)@10` | `PB` |

| Structure | Change |
| --- | --- |
| `Ref@3` | its only fragment is shadowed → pins **1 → 0** → **dead, but still physically present in `L1`** |
| `Size(7,10)`@5 | fragment narrows `[10,50) → [20,50)`; pins stay **2** (`A` + `F`) |
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
| `Size(7,10)`@5 | loses `F` (no probes remain) and loses `A` (`size_statement` is cleared when the id ceases to exist) → pins **2 → 0**, immediately dead |
| `Ref(7,50,10,PB)`@9 | dropped with the header rewrite |
| `Tombstone(7)`@10 (new) | pins **2** = `D`, from `mentions` after this flush's drops: `Ref@3` and `Size@5` |
| `AllocationMeta[7]` | `size` 0, `fragment_count` 0, `mentions` 3, `size_statement` **None**, `tombstone` `Tombstone@10` |
| `coverage[P1]`, `coverage[PB]` | each loses its remaining referenced bytes |
| `coverage[L2]` | **−3** — `Size@5` dies at once instead of lingering |

The cascade then clears itself with no special pass:
consolidating `L1` drops `Ref@3` → `Tombstone` pins 2 → 1;
consolidating `L2` drops `Size@5` → `Tombstone` pins 1 → 0;
the tombstone is then omitted by the next rewrite of its page → `mentions` 0 → id 7 is recyclable.

The `coverage[L2]` row is a second, quieter win from the model change.
Previously `Size@5` retained a denial pin on `Ref@3` and stayed "live" until `L1` happened to be cleaned, so `L2` looked fuller than it was and was correspondingly less likely to be chosen; now it drops to zero at the moment of the free, when the information that it is garbage first exists.

### (v) Consolidate `L2`, the leaf holding the size authority — break 1

This requirement survives the model change unaltered, and becomes easier to state.

`L2` is rewritten as resolved truth at epoch 10.
For id 7 it owns one statement, `Size(7,10)`@5, whose surviving contribution *to content* is the `Undefined` fragment `[10, 50)`.
A consolidator driven by the fragment map — the natural implementation, since the fragment map is where resolved truth lives — will reproduce exactly that, emitting `Undefined(7, 10, 40)`@10 and nothing else.

That silently corrupts the file.
Dropping `Size@5` removes the only `Size` for id 7, so `size_epoch` falls back to `-1` and the size becomes the maximum extent over **all** content statements, including `Ref(7,0,1000,P1)`@3 still sitting in `L1`:

```
size = max(1000, 50, 60) = 1000     // was 60
```

Allocation 7 silently grows to 1000 bytes and re-exposes stale bytes from `P1` across `[10, 1000)` — content the application truncated away at flush 5.

The cause is not a tempting shortcut but a structural blind spot: the fragment map is a **content** view, and a `Size` statement's other contribution — being the size authority — is not a fragment, so it is invisible there.
It is visible in exactly two places, `AllocationMeta.size_statement` and the statement's own `A` pin, and under the adopted model the `A` pin is now the *only* extra pin a `Size` can hold, which makes the rule sharp:

> A page holding a statement with an `A` pin cannot be consolidated without transferring the authority.

The fix emits a replacement alongside the content: `Size(7, 60)`@10 **and** `Undefined(7, 10, 40)`@10, which do not conflict within the epoch (`10 + 40 = 50 <= 60`).

| Structure | Change (with the fix) |
| --- | --- |
| `Size(7,10)`@5 | dropped; `mentions` −1 |
| `Size(7,60)`@10 (new) | pins **1** = `A`; owns no fragment |
| `Undefined(7,10,40)`@10 (new) | pins **1** (`F`) |
| `Ref@3` | unchanged, pins **1** |

The previous model obscured this by letting `D` pins share the blame; here the diagnosis is unambiguous.

### (vi) The hostage: a correct pin that cannot be released

Continue from scenario (iii), where `Ref@3` is dead but physically present in `L1`, a bulk-loaded page holding some 580 still-live statements.
Losing `Ref@3`'s 7 bytes takes `coverage[L1]` from 4082 to 4075 — a live fraction of 99.83 % — so victim selection will never choose `L1` while anything sparser exists.

**Under the adopted model this is no longer a hostage situation.**
`Ref@3` is dead and inert: it pins nothing, because content statements are never denied by anything except a tombstone, and there is no tombstone here.
Its continued presence costs 7 wasted bytes in `L1` and a `mentions` count that no live-allocation decision reads.
`Size@5` sits at pins 2 on its own merits (`A` + `F`) and is unaffected.

The effect survives only on the **free path**, and in narrowed form.
After scenario (iv), `Tombstone@10` holds `D` pins on `Ref@3` and `Size@5`, so it cannot retire until both are physically dropped — which for `Ref@3` means cleaning `L1`.
The observation that motivated this scenario therefore still applies, but only to tombstones and only until the id is recycled:

> Victim selection ranked purely by live fraction cannot see the benefit of cleaning a page whose dead statements are pinning a tombstone elsewhere.
> A benefit term counting "dead statements whose removal would decrement a tombstone" makes that visible.

The blast radius is much smaller than before: previously *any* dead content statement could strand a `Size` indefinitely, on any allocation, live or freed.

### (vii) The immortal redundant suppressor — break 2

**This break is eliminated by the adopted model.**
Reproducing it, then showing why it no longer bites.

Continue from scenario (vi) and let flush 11 rewrite the allocation entirely: the application overwrites `[0, 60)`, so the flush writes data page `PD` and the header emits `Ref(7, 0, 60, PD)`@11 together with `Size(7, 60)`@11.

State: `L1`@3 holds dead `Ref(7,0,1000,P1)`; `L2`@5 holds `Size(7,10)`; `H`@11 holds `Ref(7,0,60,PD)` and `Size(7,60)`.
`size_epoch` is 11, `size` is 60, and `[0, 60)` resolves entirely through `Ref@11`.

`Size(7,10)`@5 now owns no fragment — its `[20, 50)` gap was overwritten — and is not the authority.

- **Previous model:** it still held `D` on `Ref@3`, so pins 1.
  That pin could only be released by cleaning `L1`, which scenario (vi) showed never happens, and consolidating `L2` merely carried the pin forward into the replacement statement.
  `coverage[L2]` over-reported by 3 bytes permanently, which biased `L2` *away* from cleaning — a self-reinforcing error — and `N` resizes could strand `Θ(N)` such statements.
- **Adopted model:** pins **0**.
  The statement is dead the moment the overwrite lands, `coverage[L2]` falls by 3 immediately, and the next rewrite of `L2` drops it.

The residual, and it is worth being precise that one remains.
A `Size` that **is** the authority can still be redundant: with `Size(7, 100)`@5 and `Ref(7, 0, 100, X)`@9, dropping the `Size` would leave `size = max(100) = 100`, unchanged — yet `A` keeps it alive.
Three things distinguish this from the old break:

1. It is **bounded** — at most one such statement per allocation, since non-authority `Size` statements no longer linger — where the old one was unbounded in the number of resizes.
2. It is **cheaply correctable**: when consolidating a page holding an authority, compare the authority's bound against the largest extent among content statements newer than it, and emit no replacement when the bound does not exceed it.
3. It is **not self-reinforcing at scale**: three bytes per allocation does not meaningfully shift a page's live fraction, whereas the old pathology accumulated.

## Findings

1. **Consolidating a page that holds an id's size authority must transfer the authority** (scenario (v)).
   The `A` pin is the signal, and it is now the only extra pin a `Size` can hold, so the test is unambiguous.
   A fragment-map-driven consolidator cannot derive this on its own, because the authority is not a fragment; it must consult `AllocationMeta.size_statement` or the pin.
2. **A `Size` may be dropped iff it is not the authority and owns no fragment.**
   Its denials are subsumed by the newest `Size` (which always denies from `size` upward) together with whatever newer content statements cover the rest, so `D` pins on `Size` are redundant — the result adopted here.
3. **Only the newest suppressor of each kind carries pins.**
   For tombstones this is exact, since a newer tombstone subsumes an older one entirely; older tombstones are droppable on sight.
   `AllocationMeta` therefore needs a single `tombstone` pointer, not the list of suppressors the previous model required.
4. **`mentions` is exact for tombstones and would be an over-count for anything else.**
   A tombstone denies every probe, so every physically-present statement naming the id is genuinely denied.
   A `Size(id, n)` would deny only statements reaching past `n`, which no allocation-level counter can express — and under the adopted model nothing needs it to.
5. **Cleaning should be ranked by pins released as well as bytes reclaimed** (scenario vi), but the case is now confined to pages whose dead statements pin a *tombstone*, rather than any dead statement anywhere.
6. **A `Size` holds at most two pin kinds, `A` and `F`, never three.**
   [[address-table#What counts as a live byte]] argues for a single `pins` counter using a worked example in which a `Size` holds all three at once; that example is obsolete and the section needs updating.
   The argument itself survives unharmed — `Size@5` holds `A` and `F` together in Step 3, no consumer ever asks which kind a pin is, and two fields would still encode a distinction that is never read.
7. **One bounded over-count remains** (scenario vii): a redundant *authority*, at most one per allocation, correctable by a cheap extent comparison during consolidation.
8. **Confirmed sound:** the pin graph is acyclic — `F` and `A` point from the resolved view into statements, and `D` points strictly from a newer tombstone to older statements — so no set of statements can mutually pin itself alive.
9. **Confirmed sound:** content statements need no denial pins, because a content statement's denial is co-extensive with what it defines, and covering a range requires some newer statement to reach its far end, so the size cannot shrink when one is dropped.
10. **Ordering rule:** a flush applies its own drops before initialising a new tombstone's pins, so a statement replaced within a flush is never counted as something the replacement must deny.
