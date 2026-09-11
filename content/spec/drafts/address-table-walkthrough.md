# Address Table: A Worked Walkthrough

A step-by-step trace of the bookkeeping structures of [[address-table]] through one allocation's life, followed by an adversarial search for sequences that break the accounting.
The state built in [[#Building the state]] is the one used in [[address-table#What counts as a live byte]] to argue that a `Size` statement can hold all three kinds of pin at once.

Two breaks are found, and both are real: scenario (v) is a **correctness** bug in an obvious-looking consolidation shortcut, and scenario (vii) is a **permanent over-count** that the stated liveness rule cannot repair.
[[#Findings]] collects the rules that follow.

## Preliminaries

**What is being tracked.**
Per statement, a `StatementRecord { page, framing_len, pins }`.
Per allocation, `AllocationMeta { size, fragment_count, statement_bytes, mentions, size_statement, suppressors }`.
Per page, a coverage counter.
Plus the fragment map, a B-tree `(id, offset) → Fragment { source, statement }`.

**Pins.**
A statement is live exactly while `pins > 0`.
Three things take a pin: each **fragment** resolving to it, each physically-present **older statement it denies** (suppressors only), and being the id's **size authority**.
Pins are abbreviated below as `F` / `D` / `A`.

@Claude:
- Why do we need to keep track of the `A` type for pins?
  Is there any scenario where a statement is the size authority for an allocation but does not also either deny some older statement or resolve to a fragment (and thus have `pins > 0` anyway)?
- If it turns out that we don't need to keep track of type `A`, would not keeping track of it also remove the need for `AllocationMeta::size_statement` or is that still required for something else?

> **Yes, `A` is needed, and the cleanest witness is an allocation that is never written.**
> Create allocation 7 with size 100 and never touch it.
> The only statement is `Size(7, 100)`: no fragment resolves *through* it (it matches probes `>= 100`, all of which are outside the allocation), it denies nothing (nothing older exists), and yet it is the *only evidence the allocation exists at all*.
> Drop it and id 7 vanishes.
> Note the careful wording: a fragment over `[0, 100)` does exist, and reads need it, but it has **no owning statement** — the `Undefined` there comes from the default clause of [[address-table#Conflict resolution across epochs]] ("no matching statement"), not from the `Size`.
>
> A second witness, with content present: `Size(7, 100)`@5 and `Ref(7, 0, 10, X)`@9, nothing older.
> Range `[10, 100)` resolves to `Undefined` *by default* — no statement matches it, since `Size` only matches probes `>= 100` — so `F = 0`; nothing older exists, so `D = 0`; and dropping it sends `size_epoch` to `-1`, shrinking the allocation from 100 to 10.
>
> The general reason the two cannot substitute for each other: `F` counts the probes a `Size` *wins*, which lie in `[n, size)`, whereas `A` is about `size` itself.
> Whenever nothing has grown past the bound — i.e. `size == n`, the common case for a `Size` statement — that interval is empty, so `F` is *necessarily* zero exactly when `A` matters most.
>
> So `size_statement` stays as well, since it is the mechanism for moving the `A` pin when a new `Size` takes over.
> One implementation note: `A` is a one-bit pin, either held or not, so it could equally be a flag consulted at drop time rather than a `+1`; folding it into the counter just avoids a special case.
> (See the answer on `Size` semantics below for the converse result: `Size` statements do *not* need `D` pins, so `A` and `F` are the only two a `Size` ever holds.)
>
> **The alternative worth recording: attribute the unowned tail to the authority.**
> Since the unowned region above every content extent exists *because* the authority says so, one could give the authority ownership of it — turning `A` into an ordinary `F` pin.
> That handles both witnesses above, and it fails for two reasons.
>
> First, an existent zero-sized allocation has an empty tail, hence no fragment, hence nothing to pin, yet `Size(7, 0)` alone is the only evidence it exists.
>
> Second, and decisively, the scheme only holds while `Size` statements keep their `D` pins.
> Take `Ref(7, 0, 100, X)`@3 with `Size(7, 50)`@5 and nothing newer: `[0, 50)` is *owned* by `Ref@3`, so there is no unowned tail at all, and dropping the `Size` sends `size_epoch` to `-1`, jumping the size to 100 and re-exposing stale bytes across `[50, 100)`.
> Only the denial of `Ref@3` stops that — which means **the two simplifications are mutually exclusive**: dropping `D` from `Size` requires `A`, and replacing `A` with tail attribution requires `D`.
>
> Break 2 settles the choice, because it *is* the `D`-pin-on-`Size` pathology: a non-authority `Size` with `F = 0`, held alive by a denial that the newer `Size` already subsumes.
> Any scheme retaining `D` on `Size` retains break 2 by construction, so tail attribution would buy nothing there while still needing a zero-size special case and churning the fragment map on every resize (the tail must be re-sized and re-pointed at each new authority, where `A` transfers with a single pin move).
> Hence: `A` + `F`, and no tail attribution.

**Epochs belong to pages, not to statements.**
A statement inherits the epoch of the page it currently sits in, so moving a statement re-stamps it.
The header is rewritten every flush and states resolved truth, so *everything in the header always carries the current epoch*, and multi-epoch disagreement can only exist between the header and evicted leaves — or between two leaves.
This is why the example needs evictions to arise at all: had all three statements stayed in the header, the resolved-truth discipline would have collapsed them into one.

**Framing sizes** are computed from the grammar in [[address-table#Physical level]], assuming a one-byte `id_delta` and two-byte addresses:

| Statement | Encoding | `framing_len` |
| --- | --- | --- |
| `Ref(7, 0, 1000, P1)` | 1 + 1 + 1 + 2 + 2 | 7 |
| `Ref(7, 50, 10, PB)` | 1 + 1 + 1 + 1 + 2 | 6 |
| `Size(7, n)` | 1 + 1 + 1 | 3 |
| `Undefined(7, 10, 40)` | 1 + 1 + 1 + 1 | 4 |
| `Tombstone(7)` | 1 + 1 | 2 |

**Pages.** `H` is the header (the address table's root and write buffer), `L1`/`L2` are address-table leaves, `P1`/`PB`/`PC`/`PD` are data pages.
`MAX_PAGE_CONTENT` is 4082.
Header pages are exempt from coverage-driven victim selection, since they are rewritten unconditionally every flush; only leaves and data pages compete for cleaning.

## Building the state

### Step 0 — empty file

No statements, no fragments, no allocations.
Every structure is empty; the header exists and names no leaves.

### Step 1 (flush 3) — create allocation 7 and write 1000 bytes

The application allocates id 7 and writes 1000 bytes.
This flush is part of a bulk load that creates many allocations at once, so the header overflows and evicts a batch to a fresh leaf `L1`, stamped epoch 3.
At creation every statement has the same age, so the eviction clock cannot distinguish them and allocation 7's statement is in the batch.

Writes: data page `P1` (1000 bytes for id 7, the rest for other allocations, filled to 4082); leaf `L1` (full of statements, including ours).

| Structure | Value after the step |
| --- | --- |
| Statements | `Ref(7, 0, 1000, P1)` in `L1`@3, `framing_len` 7, pins **1** (`F`) |
| Fragment map | `(7, 0) → { source: P1+0, statement: Ref@3 }`, covering `[0, 1000)` |
| `AllocationMeta[7]` | `size` 1000, `fragment_count` 1, `statement_bytes` 7, `mentions` 1, `size_statement` **None**, `suppressors` `[]` |
| `coverage[P1]` | 4082 (of which 1000 belong to id 7) |
| `coverage[L1]` | 4082 |
@Claude: I don't understand what `AllocationMeta::mentions` is good for.
Point me to an example in this document where it is read to make some decision.

> **Two readers, both exercised in this document.**
>
> *1. Initializing a new suppressor's `D` count, in `O(1)`.*
> Every denial count in this walkthrough is `mentions` read at the moment the suppressor is written, before the flush's own additions: Step 2's `Size@5` gets `D = 1`, scenario (ii)'s `Size(7,55)`@10 gets `D = 2`, scenario (iv)'s `Tombstone@10` gets `D = 2`.
> Without the counter, writing a tombstone would mean scanning every live address-table page for statements naming the id, because the statements it must deny are precisely the ones that own no fragment and are therefore invisible in the fragment map.

@Claude2: Regardless of whether we adopt the `Size` result below, answer to help me understand: how could an allocation-level `mentions` counter have been used to initialize the denial counter of a `Size` statement? If there's a `Ref(id,0,10,...)` statement from an older epoch followed by a `Size(id, 20)` statement in a newer epoch, wouldn't the `Ref` count toward the `id`'s `mentions` counter? But shouldn't it be ignored by the `Size` statement's denial counter? Or am I missing something here?

> **You are not missing anything — `mentions` cannot initialize a `Size`'s denial count, and your example is exactly the hole.**
> `Size(id, 20)` claims probes `>= 20`; `Ref(id, 0, 10, …)` claims `[0, 10)`.
> The two ranges are disjoint, so the `Size` denies the `Ref` not at all — yet `mentions` counts it, and the shortcut would hand the `Size` a denial pin it does not hold.
>
> The general rule is that `mentions` is **exact for one suppressor kind and an upper bound for the other**.
> A `Tombstone` claims *every* probe and denies existence outright, so every older statement naming the id is genuinely denied, and `mentions` is exact.
> A `Size(id, n)` claims only `[n, ∞)`, so it denies an older statement iff that statement claims some probe `>= n` — for a content statement, iff `offset + len > n`; for an older `Size` or `Tombstone`, always.
> Since the denied set is a subset of the mentioned set, the error is always an over-count, never an under-count: safe, but wrong as a claim, and wrong in the direction that strands statements.
>
> So the earlier answer about `mentions` was overstated.
> The three numbers in this document are correct, but not because `mentions` produced them:
> Step 2's `Size(7,10)` against `Ref(7,0,1000)` — `1000 > 10`, genuinely denied;
> scenario (ii)'s `Size(7,55)` against `Ref(7,0,1000)` (`1000 > 55`) and against `Size(7,10)` (always) — both genuinely denied;
> scenario (iv)'s `Tombstone` — exact by the rule above.
> Every instance in the walkthrough happens to be one where the shortcut and the truth coincide, which is how the error survived being written down.
>
> Computing the count exactly is materially more expensive than a counter.
> The denied statements are the physically-present ones with `offset + len > n`, and the **dead** ones among them — `pins == 0`, hence absent from the fragment map — are precisely the ones that force the extra structure, since a live one could be found by walking the id's fragments.
> So `mentions` would have to be promoted from an integer to a per-id list of `(epoch, extent)` covering every physically-present statement, maintained on every write and every drop.
>
> Two over-counts are worth separating here, because they have different causes:
> **spurious** denials, where the pin records a denial that never existed (your example — an artefact of the initialization shortcut), and **redundant** denials, where the denial is real but already subsumed by a newer suppressor (break 2 — an artefact of the liveness rule).
> Adopting the `Size` result removes both at once by removing the category, and `mentions` then reverts to serving only tombstones, where it was exact all along.

> *2. Deciding id recycling.*
> The last line of scenario (iv): "`mentions` 0 → id 7 is recyclable", with the tombstone becoming droppable one step earlier at `mentions == 1`.
>
> There is also a heuristic use in Finding 2: ids whose `mentions` far exceeds their `fragment_count` are exactly the ones carrying dead-but-present statements, which makes them the candidates worth a per-id pass.
>
> Caveat, if the `Size` result below is adopted: with `Size` statements no longer taking `D` pins, reader (1) shrinks to tombstones only — and since a live tombstone means a *freed* allocation, `mentions` would then be read only on the free-and-recycle path.

Note `size_statement` is `None`: with no `Size` statement, the resolved size is just the largest extent among content statements, `0 + 1000`.
No statement holds an authority pin, because there is no authority to hold.

### Step 2 (flush 5) — truncate to 10 bytes

The application truncates allocation 7 to 10 bytes.
The flush writes `Size(7, 10)` into the header; further bulk traffic overflows the header again and the statement is evicted to a fresh leaf `L2`@5.

Resolution afterwards: `size_epoch` = 5, so `size = max(10, extents with epoch > 5) = 10`.
Probes `>= 10` are outside the allocation, so `Size@5` owns **no** fragment yet.

| Structure | Change |
| --- | --- |
| `Ref(7,0,1000,P1)`@3 | fragment narrows `[0,1000) → [0,10)`; pins unchanged at **1** (`F`) |
| `Size(7,10)`@5 (new, `L2`) | pins **2** = `A` (authority) + `D` (denies `Ref@3`, physically present) |
| Fragment map | `(7, 0)` now covers `[0, 10)`; no other entries |
| `AllocationMeta[7]` | `size` 10, `fragment_count` 1, `statement_bytes` 10, `mentions` 2, `size_statement` `Size@5`, `suppressors` `[Size@5]` |
| `coverage[P1]` | **−990** → 3092; id 7 now relies on only 10 of its bytes |
| `coverage[L1]` | unchanged (the `Ref`'s framing is still live — it still owns a fragment) |
| `coverage[L2]` | includes `Size@5`'s 3 bytes |

The 990 bytes released in `P1` are the truncation's garbage.
Nothing in `L1` changed: a partially shadowed statement keeps its full framing charge, which is correct — the whole encoding is still needed to describe the surviving 10 bytes.

### Step 3 (flush 9) — grow to 60 and write `[50, 60)`

The application grows allocation 7 to 60 bytes and writes 10 bytes at offset 50.
The flush writes data page `PB` and puts `Ref(7, 50, 10, PB)` in the header, where it stays (it is hot).

Resolution afterwards: `size_epoch` is still 5, so `size = max(10, 50 + 10) = 60`.
Content:

| Range | Matching statements | Winner | Source |
| --- | --- | --- | --- |
| `[0, 10)` | `Ref@3` | `Ref@3` | `P1` |
| `[10, 50)` | `Ref@3`, `Size@5` (since `10 <= probe`) | **`Size@5`** | `Undefined` |
| `[50, 60)` | `Ref@3`, `Size@5`, `Ref(50,10)@9` | `Ref@9` | `PB` |

@Claude: could any issues that you encountered be avoided if we changed the semantics such that `Size` no longer makes a statement about the content, and instead required that the content of every byte in an allocation must be stated explicitly in some `Ref`, `Inline`, or `Undefined` statement? In this particular situation, such a change in semantics would mean that, together with the `Ref@9`, the flush would also have to explicitly emit `Undefined(7,10,50)`. Would this avoid any issues later on? A less intrusive version of this could be that we don't change the spec but only simplify our in-memory management in such a way that it simply introduces explicit `Undefined` statements whenever a new `Size` statement shadows an older `Size` statement (see discussion in Scenario (ii) below).

> **The spec change does not work as stated, but the instinct behind it is right and yields a sharper result: `Size` statements never need `D` pins at all — which makes break 2 disappear.**
>
> *Why the full change fails.*
> If `Size` no longer denied content, the truncation in Step 2 would have to deny `Ref@3`'s tail some other way, and the only candidate is an explicit `Undefined(7, 10, 990)`.
> But content statements *lower-bound* the size, so emitting that would inflate the allocation straight back to 1000.
> Denying bytes **beyond the current size** is precisely what a content statement cannot express, and that is the job `Size` is carrying.
>
> *What survives, and is stronger.*
> Ask when a *non-authority* `Size` may be dropped.
> It cannot affect the size: it is not the authority, and since its epoch is below `size_epoch` it is not in the `max` either (which ranges only over `Ref`/`Undefined`/`Inline`).
> So the only risk is content, and it wins exactly the probes in `[n, size)` that nothing newer covers — which is precisely what its `F` pins count.
> **Therefore `F = 0` plus "not the authority" is already sufficient to drop a `Size`, and its `D` pins are redundant.**
>
> The one worry is a later *grow* re-exposing a range the dropped statement used to deny, and that cannot happen: the newest `Size` has `n_new <= size` (since `size = max(n_new, …)`), so it already denies everything from `size` upward, and growing can only expose `[size, new_size)`.
>
> *Against the two breaks.*
> In break 1, `Size@5` holds `A`, so it is not droppable — the `A` pin alone prevents it, and Finding 1 should say so rather than pointing at denial pins.
> In break 2, `Size@5` has `F = 0` and is not the authority, so under this rule it **is** droppable, and the immortal-statement pathology is gone.
>
> *Which makes the injection idea unnecessary rather than wrong.*
> Emitting `Undefined(7,10,40)` when a new `Size` shadows an older one is a way of driving the old statement's `F` to zero so it can retire — but dropping `D` retires it directly, without spending four bytes to reclaim three.

The middle row is the interesting one: growing the allocation brought range `[10, 50)` into existence, and it resolves *through the `Size` statement*.
`Size@5` therefore **gains a fragment pin** — pins can go up, not only down.

| Structure | Change |
| --- | --- |
| `Ref(7,0,1000,P1)`@3 | pins **1** (`F`) — unchanged |
| `Size(7,10)`@5 | pins **2 → 3** = `F` (owns `[10,50)`) + `D` (`Ref@3`) + `A` (authority) |
| `Ref(7,50,10,PB)`@9 (new, `H`) | pins **1** (`F`) |
| Fragment map | `(7,0) → P1+0`; `(7,10) → Undefined, stmt Size@5`; `(7,50) → PB+0` |
| `AllocationMeta[7]` | `size` 60, `fragment_count` 3, `statement_bytes` 16, `mentions` 3, `size_statement` `Size@5`, `suppressors` `[Size@5]` |
| `coverage[PB]` | +10 |

**This is the state referenced in [[address-table#What counts as a live byte]]**, and `Size(7,10)`@5 holds all three pin kinds at once — the reason `pins` is one counter rather than two fields.

## Scenarios

Each scenario branches from the end of Step 3 unless stated otherwise.

### (i) Consolidate `L1`, the leaf holding `Ref@3`

`L1` is rewritten as resolved truth into a fresh page at epoch 10.
`Ref@3` owns exactly one fragment, `[0, 10)`, so it is re-emitted narrowed: `Ref(7, 0, 10, P1)`@10, `framing_len` 6.

Ordering matters for the denial-pin arithmetic, so state it once: **a flush's own drops are applied before any new suppressor's pins are initialized.**

| Structure | Change |
| --- | --- |
| `Ref@3` | fragment re-owned by the new statement → pins **1 → 0** → dead → framing released; then physically dropped with the page |
| `Size(7,10)`@5 | `D` released because `Ref@3` is *physically gone* → pins **3 → 2** (`F` + `A`) |
| `Ref(7,0,10,P1)`@10 (new) | pins **1** (`F`); content statements never take denial pins |
| Fragment map | `(7,0)`'s `statement` field re-pointed; `source` unchanged |
| `AllocationMeta[7]` | `statement_bytes` 16 → 15; `mentions` 3 → 3 (one dropped, one added) |
| `coverage[L1]` | → 0; the page becomes reusable after the two-epoch quarantine |
| `coverage[P1]` | **unchanged** — consolidating the address table moves no data |

The last row is worth keeping in mind: address-table consolidation and data-page consolidation are independent, and rewriting a `Ref` does not relocate what it points at.

### (ii) Resize to 55 bytes

Flush 10 writes `Size(7, 55)` into the header.
The header also carries `Ref(7,50,10,PB)` forward, but at epoch 10 that statement's extent (60) would exceed the new size (55) — which [[address-table#No conflicts within each epoch]] forbids in a single epoch.
The resolved-truth discipline resolves this by narrowing it on the way out: the header emits `Ref(7, 50, 5, PB)`.

| Structure | Change |
| --- | --- |
| `Ref@3` | pins **1** (`F`, still owns `[0,10)`) |
| `Size(7,10)`@5 | loses `A` to the newer `Size` → pins **3 → 2** (`F` + `D`) |
| `Ref(7,50,10,PB)`@9 | superseded by its narrowed self; record dropped |
| `Size(7,55)`@10 (new) | pins **3** = `A` + `D`(`Ref@3`) + `D`(`Size@5`); owns no fragment, since probes `>= 55` are outside the allocation |
| `Ref(7,50,5,PB)`@10 (new) | pins **1** (`F`) |
| `AllocationMeta[7]` | `size` 55, `fragment_count` 3, `mentions` 4, `size_statement` `Size@10`, `suppressors` `[Size@5, Size@10]` |
| `coverage[PB]` | **−5** — bytes `[PB+5, PB+10)` are now garbage |

@Claude: Instead of keeping track of a list of `suppressors` in `AllocationMeta`, could we do the following:
- Only keep track of a single `suppressor` and always set it to the latest suppressor (`Size`@10 in this case).
- reduce `pins` for `Size`@5 in this step to 0 by assigning all of its pins to the new `Size`@10;
- Keep track of a single `suppressed_by` field for each statement; in this step, we would set `suppressed_by` for `Size`@5 to the new `Size`@10.

> **Sound for `D` pins, unsound for `F` pins, and largely moot if the `Size` result above is adopted.**
>
> The transfer is valid for denials.
> `Size@10` denies everything older, so it can take over `Size@5`'s denial of `Ref@3`, and it additionally denies `Size@5` itself — arriving at 2, the same count the list-based scheme computes.
> Each denied statement's `suppressed_by` then names the one suppressor to decrement, making the drop `O(1)` with no epoch comparison.
>
> But bullet 2 is wrong as written.
> In this very step `Size@5` also holds an `F` pin — it owns the `Undefined` fragment `[10, 50)` — and a fragment pin cannot be transferred: the fragment genuinely resolves through `Size(7,10)`, and re-pointing it at `Size(7,55)` would be a lie, since `Size(7,55)` does not match probes in `[10, 50)` at all.
> So the rule has to read *"transfer the `D` pins"*, leaving `Size@5` at pins 1 rather than 0.
>
> *Cost.*
> `suppressed_by` is a per-statement pointer, paid by every statement in the file; `suppressors` is a per-allocation list that is empty for every allocation that has no tombstone.
> The list is cheaper in the common case, and the epoch comparison it needs is against a list that is almost always length 0 or 1.
>
> *And it mostly dissolves.*
> If `Size` statements take no `D` pins, `suppressors` only ever holds tombstones — empty for every live allocation, one entry for a freed one — at which point neither the list nor the back-pointer is doing appreciable work.

Note `suppressors` now has two entries, both pinned by the same `Ref@3`.
Dropping `Ref@3` later must decrement **both**, which is why that field is a list and not a single newest pointer.

### (iii) Overwrite bytes `[0, 20)`

Flush 10 writes data page `PC` and emits `Ref(7, 0, 20, PC)`@10; the header carries `Ref(7,50,10,PB)` forward at epoch 10 (disjoint, so no conflict).

`size_epoch` is still 5, so `size = max(10, 20, 60) = 60`, unchanged.

| Range | Winner | Source |
| --- | --- | --- |
| `[0, 20)` | `Ref(0,20)@10` | `PC` |
| `[20, 50)` | `Size@5` | `Undefined` |
| `[50, 60)` | `Ref(50,10)@10` | `PB` |

| Structure | Change |
| --- | --- |
| `Ref@3` | its only fragment is shadowed → pins **1 → 0** → **dead, but still physically present in `L1`** |
| `Size(7,10)`@5 | its fragment narrows `[10,50) → [20,50)`; pins stay **3** |
| `AllocationMeta[7]` | `fragment_count` 3, `statement_bytes` 15 (dead `Ref@3` excluded), `mentions` 4 |
| `coverage[L1]` | **−7** (dead framing) |
| `coverage[P1]` | **−10** → id 7 now relies on none of `P1` |

This is the state that sets up both breaks: a statement that is dead but physically immortal (see Scenario (vi) below).
Note the distinction the design turns on — `pins == 0` means *contributes no live bytes*; physical removal happens only when the page is rewritten, and `mentions` counts physical presence, not liveness.

### (iv) Free the allocation

Flush 10 emits `Tombstone(7)`, and the header stops carrying the id's content statements.

| Structure | Change |
| --- | --- |
| `Ref@3` | no fragments → pins **0** |
| `Size(7,10)`@5 | `tombstone_epoch` = 10 and no `Size` is newer, so `size_epoch` = 10: the authority pin is **released**. Fragment gone too → pins **3 → 1** (`D` on `Ref@3` only) |
| `Ref(7,50,10,PB)`@9 | dropped with the header rewrite |
| `Tombstone(7)`@10 (new) | pins **2** = `D`(`Ref@3`) + `D`(`Size@5`) |
| `coverage[P1]`, `coverage[PB]` | each loses its remaining referenced bytes |
| `AllocationMeta[7]` | `size` 0, `fragment_count` 0, `mentions` 3 |

@Claude: Couldn't we just count only the size authority (`Size`@5) in `pins`? In turn, `Size`@5 is pinned by `Ref`@3, so the guarantee that the tombstone outlives `Ref`@3 is enforced transitively anyway.

> **No — the chain it relies on is not guaranteed to exist.**
>
> Witness: allocation 7 written twice and never resized.
> `Ref(7, 0, 100, X)`@3 in `L1`, `Ref(7, 0, 100, Y)`@5 in `L2`, then `Tombstone(7)`@10.
> There is no `Size` statement anywhere, so there is no size authority to pin, and content statements never take `D` pins — so under the proposal the tombstone's pins are 0 and it is immediately droppable.
> Drop it, and `Ref@5` becomes the highest-epoch statement mentioning id 7: the allocation resurrects with its pre-free contents.
>
> The transitive argument holds in scenario (iv) only by accident, because a truncation happened to leave a `Size` between `Ref@3` and the tombstone that denies it.
> Nothing guarantees such an intermediary — and after the `Size` result above, `Size` statements stop taking `D` pins at all, so even when one is present it is no longer a link in any chain.
>
> A correct transitive scheme does exist: have every statement pin its immediate predecessor, so the chain is complete by construction.
> But that puts a `D` pin on every statement, content statements included, which is strictly more bookkeeping than the tombstone's single count — and that count is already initialized in `O(1)` from `mentions`, so there is nothing to win.

The cascade then clears itself, in order, with no special pass:
consolidating `L1` drops `Ref@3` → `Size@5` pins 1 → 0 and `Tombstone` pins 2 → 1;
consolidating `L2` drops `Size@5` → `Tombstone` pins 1 → 0;
the tombstone is then omitted by the next rewrite of its page → `mentions` 0 → id 7 is recyclable.

### (v) Consolidate `L2`, the leaf holding the size authority — break 1

@Claude: I don't understand this example. It seems there are some hidden assumptions you are making here. Why would one think that one could drop the `Size` statement when consolidating `L2`? It seems obviously incorrect to me at this point. What reasoning would the "obvious shortcut" you mention be based on? You say below that the `Undefined` fragment is the only surviving contribution of the `Size` statement. That's not true. It's the only surviving contribution *to allocation content* of the `Size` statement, but it's not the only surviving contribution of the `Size` statement overall.

> **Both objections are right, and the section is mis-framed.**
>
> The sentence is simply wrong: the `Undefined` fragment is the statement's only surviving contribution *to content*.
> It also contributes the size cap and the denial of `Ref@3` beyond offset 10, and collapsing those three into the word "contribution" is what makes the next step read as reasonable when it is not.
>
> On what the shortcut would be based — not on judgement, but on a natural implementation.
> [[address-table#Goals, and the one currency behind them]] specifies address-table consolidation as rewriting a sparse page's live statements, and [[address-table#The header as write buffer]] fixes the discipline for such rewrites as emitting *resolved truth* — but the structure that holds resolved truth is the fragment map, which is a **content** view.
> A consolidator that walks the fragment map over the victim page's id range and re-emits statements reproducing what it finds will reproduce the `Undefined` fragment and be structurally blind to the other two contributions, because neither is a fragment.
> It never decides to drop the `Size`; it never sees that there is a `Size` to keep.
>
> So the example is worth keeping, but framed as *"here is the information a fragment-map-driven consolidator lacks"* rather than *"here is a tempting shortcut"* — the missing information being that a victim page may hold an id's size authority, which is visible only in `AllocationMeta.size_statement` and the `A` pin.
>
> Two corrections to Finding 1 follow.
> It should name the **`A` pin specifically**, not "the pin count": per the `Size`-semantics answer above, a `Size`'s `D` pins are redundant, so `A` alone is what prevents this break.
> And its escape clause, "unless that authority's denial pins are zero", is wrong — the never-written allocation in the first answer above has an authority with `D = 0` whose removal destroys the allocation outright.
> The correct clause is *unless a newer `Size` exists*, which is just another way of saying the statement is not the authority.

This is where an obvious shortcut is wrong.

`L2` is rewritten as resolved truth at epoch 10.
For id 7, `L2` owns exactly one statement, `Size(7,10)`@5, whose surviving contribution is the `Undefined` fragment `[10, 50)`.
The natural output is therefore `Undefined(7, 10, 40)`@10 — and a consolidator that stops there has silently corrupted the file.

Dropping `Size@5` removes the *only* `Size` statement for id 7, so `size_epoch` falls back to `-1`, and the size becomes the maximum extent over **all** content statements — including `Ref(7,0,1000,P1)`@3, still sitting in `L1`:

```
size = max(1000, 50, 60) = 1000     // was 60
```

Allocation 7 silently grows to 1000 bytes and re-exposes stale bytes from `P1` across `[10, 1000)` — content the application truncated away at flush 5.

The fix is to emit a replacement authority alongside the content: `Size(7, 60)`@10 **and** `Undefined(7, 10, 40)`@10.
They do not conflict within the epoch (`10 + 40 = 50 <= 60`), and resolution is then unchanged.

| Structure | Change (with the fix) |
| --- | --- |
| `Size(7,10)`@5 | dropped; `mentions` −1 |
| `Size(7,60)`@10 (new) | pins **2** = `A` + `D`(`Ref@3`); owns no fragment |
| `Undefined(7,10,40)`@10 (new) | pins **1** (`F`) |
| `Ref@3` | unchanged, pins **1** |

**The rule this yields:** a suppressor may be dropped only when its pins are zero, and the denial pins are exactly what makes that safe.
The tempting shortcut — "resolved truth contains at most one `Size` per id, so a redundant one can go" — is only valid when nothing older survives that the `Size` was capping, which is precisely the statement `pins == 0` makes.
A naive consolidator that reasons from the *resolved view* alone cannot see `Ref@3` at all, because it owns no fragment; it is invisible in every structure except `mentions` and the denial pins.

### (vi) The hostage: a correct pin that cannot be released

Continue from scenario (iii), where `Ref@3` is dead but physically present in `L1`.

Suppose `L1` was written by a bulk load and holds ~580 statements, all still live.
Losing `Ref@3`'s 7 bytes takes `coverage[L1]` from 4082 to 4075 — a live fraction of 99.83 %.
Victim selection ranks by live fraction, so `L1` will never be chosen while anything sparser exists.

Consequences, all of them correct and none of them repairable by the cleaning policy as stated:

- `Ref@3` stays physically present indefinitely.
- `Size(7,10)`@5 keeps its denial pin indefinitely, so it can never be dropped — and if `L2` is consolidated, the pin migrates to the replacement statement rather than disappearing.
- Allocation 7's resolved state therefore requires three pages forever, and `mentions` never falls below 4.

Nothing here is mis-counted: the pin is genuine, because dropping `Size@5` while `Ref@3` survives is exactly Break 1.
What is wrong is the *ranking*: the benefit of cleaning `L1` is not the seven bytes it reclaims but the pin it releases, and a policy that scores only bytes-reclaimed-per-byte-written cannot see that benefit at all.

### (vii) The immortal redundant suppressor — break 2

Now make the pin genuinely unnecessary and watch the accounting fail to notice.

Continue from (vi), and let flush 11 rewrite the whole allocation: the application overwrites `[0, 60)`, so the flush writes data page `PD` and the header emits `Ref(7, 0, 60, PD)`@11 together with `Size(7, 60)`@11.

State: `L1`@3 holds dead `Ref(7,0,1000,P1)`; `L2`@5 holds `Size(7,10)`; `H`@11 holds `Ref(7,0,60,PD)` and `Size(7,60)`.

Resolution: `size_epoch` = 11, `size = 60`, and `[0, 60)` resolves entirely through `Ref@11`.
`Size(7,10)`@5 now owns **no** fragment (its `[20,50)` gap was overwritten) and is **not** the authority (`Size@11` is).
Its only remaining pin is the denial on `Ref@3`.

**But `Size@5` is now redundant.**
Dropping it would leave `size_epoch` = 11, hence `size = 60` unchanged, and every probe in `[0,60)` still resolves through `Ref@11`; probes beyond 60 are outside the allocation, and `Size(7,60)`@11 caps `Ref@3` anyway.
So the statement can be removed with no observable effect — and the accounting says it is live.

The over-count is small but permanent and self-reinforcing:

- `coverage[L2]` over-reports by `Size@5`'s 3 bytes, which biases `L2` *away* from being cleaned — the over-counted bytes are exactly what keeps the page out of the victim pool.
- The pin cannot reach zero while id 7 is alive unless `L1` is cleaned, and (vi) established that `L1` is effectively never chosen.
- Consolidating `L2` does not fix it either, if the consolidator's rule is "keep any statement with `pins > 0`": it will faithfully re-emit `Size(7,10)` — or a resolved-truth equivalent — carrying the pin forward into the new page.

And it is unbounded in the number of resizes.
Every resize while the allocation is cold enough to be evicted leaves another `Size` statement behind, each pinned by the same immortal `Ref@3` and by every older `Size`, so `N` resizes strand `Θ(N)` immortal statements.

The root cause is that `pins > 0` is **sufficient but not necessary** for a suppressor to be needed.
Deciding necessity requires comparing the suppressor against the *physically present* statements for that id — information no per-page view contains, since the relevant statements live in other pages and own no fragments.

## Findings

1. **Consolidating a page that holds an id's size authority must emit a replacement authority** (Break 1), unless that authority's denial pins are zero.
   The safe test is the pin count, not the resolved view, because the statements at risk of resurrection are invisible in the resolved view by construction.
2. **`pins > 0` is sufficient but not necessary for a suppressor to be needed** (Break 2), so a "keep while pinned" consolidator makes redundant suppressors immortal.
   The repair is a per-**id** pass rather than a per-**page** one: when consolidating a page that holds a suppressor, gather all physically present statements for that id and recompute whether the suppressor still changes anything.
   `AllocationMeta.mentions` already bounds the cost, and ids with `mentions` far above their `fragment_count` are exactly the candidates worth the pass.
3. **Cleaning should be ranked by pins released, not only by bytes reclaimed** (scenario vi).
   A page can be 99.8 % live and still be the only thing standing between a suppressor and its retirement; a benefit term counting "dead statements whose removal would decrement a pin elsewhere" makes that visible.
4. **The over-count is self-reinforcing**, since over-reported bytes keep a page out of the victim pool — which argues for computing the correction in finding 2 eagerly rather than waiting for the page to be chosen on byte-coverage grounds.
5. **Confirmed sound:** the pin graph is acyclic (denial pins point strictly from newer to older epochs, fragment and authority pins point from the allocation's resolved view into statements), so no set of statements can mutually pin itself alive.
6. **Confirmed sound:** content statements never need denial pins, because a content statement's denial is co-extensive with what it defines; the size formula is safe too, since covering a range requires some newer statement to reach its far end.
7. **Ordering rule, used throughout:** a flush's own drops are applied before any new suppressor's denial pins are initialized, so a statement replaced within a flush is never counted as a thing the replacement must deny.
