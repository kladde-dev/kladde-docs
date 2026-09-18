---
title: Liveness accounting
---

What makes a statement worth keeping, what makes a page worth reclaiming, and how both are decided in `O(1)`.

Nothing here is persisted.
All of it is rebuilt during the bulk read at open and maintained incrementally afterwards, and all of it is **policy**: an over-count only delays cleaning, an under-count only cleans a page earlier than ideal, and neither can make a resolved read wrong.

## Coverage

A page's **coverage** is the number of bytes in it that current state still relies on.
It is what makes a page a consolidation victim, so it has to fall as the page's contents become useless.

One rule covers both page kinds, and it is the rule data pages already follow.

**Content bytes are charged to the page that physically holds them.**
Every `Bytes` fragment names a page and an offset, so when a fragment is destroyed, decrement that page's coverage by the fragment's length.
A `Ref`'s fragments point into a `Data` page; an `Inline`'s fragments point into the `AddressTable` page carrying the payload; a zero fragment points nowhere and costs nothing.
Splitting a fragment changes nothing, since both halves still cover the same bytes.

An inline payload is therefore just content that happens to live in a table page, and it gets per-byte accounting for free — no per-statement payload counters, no special case, the same line of code.

*Why per-byte accounting for inline payloads is not optional.*
For a `Ref`, partial shadowing strands only ~10 bytes of framing in the address-table page, while the content bytes it no longer reaches are tracked exactly in the data page's counter, which duly falls.
For an `Inline` there is no data page: the payload *is* the content, so charging it all-or-nothing would let shadowing half of a 200-byte inline strand 100 bytes that no counter in the system ever notices, and a page holding sixty mostly-shadowed inlines would report itself nearly full and never be cleaned.

**Framing bytes are charged to the statement's own page**, and released in one step when the statement loses its last reason to live.
The two charges cover disjoint byte ranges of the encoding, so they cannot double-count.

## Pins

A statement is live exactly while `pins > 0`, and `pins` is a **single counter over heterogeneous holders**.

| pin | held by | taken when | released when |
| --- | --- | --- | --- |
| `F` | any statement | a fragment resolves *through* it | that fragment is destroyed or re-owned |
| `A` | the **anchor** — the newest `Shrink` or `Tombstone` — and a `Grow` with `n == size` | the anchor: on becoming newest; a `Grow`: while it may be the sole witness of the size | the anchor: a newer `Shrink` or `Tombstone` supersedes it, or — a tombstone anchor only — `mentions` falls to 1; a `Grow`: as soon as `size > n`, or `n <= anchor.n`, or it falls below `anchor_epoch` |

So a content statement (`Ref`, `Inline`, `Zero`) holds only `F`, a `Grow` holds only `A`, and a `Shrink` or `Tombstone` can hold both.

*Why one counter and not two.*
A `Shrink` holds `A` and `F` simultaneously — with `Ref(id, 0, 1000)`@3, `Shrink(id, 10)`@5 and `Ref(id, 50, 10)`@9 the size is 60 and `[10, 50)` resolves *through* the `Shrink` — and no consumer ever asks which kind a pin is.
Two fields would encode a distinction that is never read, while inviting the bug where a predicate checks one and forgets the other.

*Why "pin".*
This is a refcount whose holders are of different kinds, so `liveness` would read as a boolean and `refcount` would say nothing about what is doing the referring.
"Pin", in the buffer-manager sense of *something prevents this from being reclaimed*, is exactly the relationship.

**Pins can rise after creation.** Growing an allocation can bring a range into existence that an existing `Shrink` wins.

## The droppability rule

> **A `Shrink` or `Tombstone` may be dropped iff it is not the anchor and owns no fragment.**
> A content statement may be dropped iff it owns no fragment.
> A `Grow` may be dropped iff it is not the size's sole witness.

Equivalently, in all cases: `pins == 0`.

**Why a `Shrink` needs no denial pin.**
A non-anchor `Shrink(id, n)` cannot affect the size, since its epoch is below `anchor_epoch` and the size's maximum ranges only over `Grow` bounds and content extents above the anchor.
It cannot affect content beyond the probes its `F` pins already count, since it wins exactly the probes in `[n, size)` that nothing newer covers.
And a later grow cannot expose anything either, because the anchor always has `n <= size` and so already denies every probe from `size` upward.

**Why a content statement needs none.**
A content statement's denial is exactly co-extensive with what it defines.
If a `Ref` covering `[a, b)` has lost every fragment, then every byte of `[a, b)` is covered by something strictly newer, and that newer thing also outranks everything the `Ref` was denying.
The size is safe too: covering `[a, b)` requires some newer statement to reach offset `b`, so the size cannot shrink when the `Ref` goes.

**Why a tombstone needs none either.**
The same argument runs one step further: every probe in `[0, size)` is won by something newer, or the tombstone would own that probe; every probe `>= size` is denied by the anchor; and existence is decided by a statement newer than the tombstone, since one exists by assumption.
Nothing it was denying can resurface — *even though the statements it denies may still be physically present*.

## Tombstones

A `Tombstone` is a `Shrink(id, 0)` that also denies existence.
It matches every probe, exactly as the [resolution rules](../spec/address-table.md#conflict-resolution-across-epochs) say, and it takes the same pins a `Shrink` does.

So a tombstone *can* own fragments — the ranges a re-allocated incarnation leaves uncovered, which it wins because it outranks everything older and nothing newer covers them.
While the id does not exist its size is 0, so no probes exist and it owns nothing; it is then the anchor, and that alone keeps it alive.

*Why a matcher and not an epoch floor.*
An earlier design inverted this: discard every statement at or below `tombstone_epoch`, so that a tombstone could never own a fragment and needed a denial pin instead.
The two framings resolve identically, but the matcher framing is what lets one droppability rule cover `Shrink` and `Tombstone` alike, and it costs no fragment-map entry — only a pin — since an uncovered range has an entry either way.
A resolver may still walk each id's statements newest-first and stop at the first tombstone; that is an implementation shortcut, not a separate model.

### The last tombstone

The one thing the two pins do not decide on their own.

A tombstone that is its id's newest statement is the anchor, so `A` keeps it alive — correctly, because dropping it while any older statement mentioning the id is still *physically present* would let that statement decide existence again and resurrect the allocation.
The count must be over physically present statements, not resolution-live ones: a statement with zero pins that has not yet been swept out of its page resurrects just as well.

So a tombstone anchor **releases `A` when `mentions` falls to 1**: it is then the id's last statement, the id reads as non-existent without it, and `pins == 0` is once again an exact droppability test.

This is the only reader of `mentions`.
A re-allocated id's tombstone escapes it entirely, being kept alive by `A` and `F` on its own terms.

### The lemma

> **Nothing older than the newest physically present tombstone is live.**

Such a statement cannot hold `F`, since the tombstone matches every probe and outranks it at all of them; it cannot be the anchor, which sits at or above the tombstone's epoch; and it cannot be the grow witness, since a `Grow` below `anchor_epoch` is dead by its third death test.

Two consequences follow with no bookkeeping at all.

**At most one tombstone per id is ever live**, so the allocation map needs no tombstone pointer beside `anchor` and tombstone chains need no special handling, even though recycling does not wait for the old tombstone to die — an older tombstone is neither the anchor nor an owner of fragments, so the rule above retires it on sight.

**A tombstone is the bound-`0` element of the stray chain below**, and a strictly decreasing chain of non-negative bounds holds at most one `0`.
That is the whole of the difference between `Shrink` and `Tombstone` here: the chain is unbounded for `Shrink` because `Shrink(id, m)` still wins the probes in `[m, n)` that a newer `Shrink(id, n)` does not match, and there is no bound below `0` for a tombstone to leave uncovered.

### The cascade

Statements shadowed by a tombstone contribute no content bytes, so their pages sink toward zero live fraction and become victims; consolidating those pages drops the statements, which decrements `mentions`; when it reaches 1 the tombstone goes dead, sinking *its* page's live fraction, which eventually recycles it too.
Nothing has to reason about epochs to make this happen — only about counters.

## `Grow` is locally decidable, and `Shrink` is not

A `Grow` matches no probe, so it can never own a fragment and never becomes a fragment-pinned stray.
It is dead as soon as `size > n`, since some other term then achieves the maximum; as soon as `n <= anchor.n`, since the anchor's own term does; and as soon as it falls below `anchor_epoch`, which tombstoning also causes.
All three tests are `O(1)`.

Because a `Grow`'s bound strictly exceeded the size at emission, and any later decrease moves the anchor above it, the bounds of the `Grow` statements above the anchor are distinct and **at most one `Grow` per allocation can be alive** — the one with `n == size`.

Dropping a `Grow` also carries no anchor hazard, because it does not anchor the epoch and so can only lower the maximum, never readmit older extents.

### The anchor pin is not substitutable

Three separate arguments say the `A` pin cannot be replaced by a test on live state.

**It may be the sole witness of the size.**
With `Grow(id, 100)`@5 and `Ref(id, 0, 10)`@9 and nothing older, `[10, 100)` resolves to zero *by default*, so the statement owns no fragment and denies nothing — yet dropping it would shrink the id from 100 to 10.
More starkly, an allocation created and never written has `Grow(id, n)` as the *only evidence it exists*.

**Dropping an anchor readmits older extents.**
The near-miss test "the anchor is redundant whenever `size > n`" is **false**, because dropping an anchor also moves `anchor_epoch`.
With `Ref(id, 0, 1000)`@3, `Shrink(id, 10)`@5 and `Ref(id, 50, 10)`@9, the size is 60 and `size > n`; dropping the anchor exposes `Ref@3`'s extent of 1000 and yields 1000.
This is the hazard a consolidator must respect: **a page holding a statement with an `A` pin cannot be rewritten without transferring the anchor.**

**A dead predecessor makes an anchor load-bearing.**
Even with every statement above the anchor dead, dropping the anchor re-anchors at the newest physically present `Shrink` beneath it — possibly one that is itself dead and superfluous — and readmits *its* bound.

### A `Shrink` can raise the size, not only lower it

`Shrink(id, n)` asserts `size >= n` — as anchor it contributes `n` to the maximum — as well as denying content at or past `n`.
Relative to its absence it lowers the size by excluding older extents and raises it through its own bound, and which effect dominates depends on what else is physically present.

Consolidation can remove the older extents it was excluding while the statement itself stays, after which only the raising effect remains: with `Shrink(id, 30)`@10 as anchor and `Ref(id, 0, 10, P)`@11 as the only other content statement, the size is 30 with the `Shrink` and 10 without it.
This is not a defect — the statement then behaves exactly as a `Grow(id, 30)` would — but a consolidator cannot rewrite it as one, since it cannot verify that nothing older still reaches past 30.
**The name records the operation that emitted the statement, not its steady-state role.**

## Emission: when a resize must write a statement

The rule splits by direction, and only the shrink half is undecidable.

**A grow emits `Grow(id, S_new)` iff nothing this flush writes reaches offset `S_new`.**
Exact and `O(1)`: everything *not* written by this flush is bounded by the old size, so the flush need only inspect its own output.
The added extent needs no protection either, since the anchor already denies the exposed territory.
So the common append pattern — growing *and* writing at the new end — emits nothing at all, and a grow without writing costs one three-byte statement that plants no latent content claim.

**A shrink always emits `Shrink(id, S_new)`.**
In principle it is needed exactly when the target is not already implied, i.e. when it differs from the maximum of the anchor's `n` and the extents of every **physically present** content statement above `anchor_epoch`.
Run directly, that check is not implementable: no structure reaches an id's physically present statements, for the reasons given in [In-memory state](in-memory-state.md#2-the-statement-slab).

It does not follow that the decision is unknowable.
Everything that can lurk is leaf-resident, since the header is rewritten as resolved truth every flush and so holds exactly this flush's own output and never a dead statement.
One bit per allocation therefore makes the test exact — set it whenever a statement naming the id is written into a non-header page above the id's `anchor_epoch`, clear it whenever a `Shrink` installs a new anchor; with the bit clear, the maximum that survives the flush is `max(anchor.n, this flush's own size claims)`, and the statement may be omitted exactly when that equals `S_new`.

The design declines to pay for it: the bit costs a lookup and a store on the eviction path for every evicted statement, and a fifth piece of per-allocation state that silently corrupts the file if maintained wrongly, to save three bytes on the subset of shrinks whose id has not been evicted since its last shrink.
Always emitting is sound, since an explicit `Shrink(id, T)` fixes the size at `T` whatever extents lurk in leaves, and a superfluous one retires at the very next shrink.

So the honest statement of the rule is **not** that an implementation cannot know, but that the one bit which would decide it costs more to maintain than the statements it saves.

## The residual

Redundant `Shrink` statements can survive, and the over-count is not bounded at one per allocation.

A superfluously emitted `Shrink` becomes the anchor, holds `A` alone, and dies at the next shrink — unless an intervening grow hands it fragments over `[n, size)`, in which case it was not superfluous for long.
A non-anchor `Shrink` lingers whenever it owns a fragment, and it can own one *while being redundant*: when the range it wins would resolve to zero anyway, because nothing older matches it.
In fragment-map terms the redundancy is sharp — dropping such a statement would turn its `ZeroExplicitly` fragment into a `ZeroByDefault` one covering the same range with the same resolved content.

**What bounds it.**
Order an id's physically present `Shrink` statements newest-first: one can hold an `F` pin only if its bound is strictly below the minimum bound among all newer ones, so the live set is **contained in the running-minimum chain**.
Any shrink to bound `b` therefore permanently retires every `Shrink` with bound `>= b`, and a later grow never revives one.

Each extra link costs an *alternating* shrink and grow, which is what the `Grow`/`Shrink` split buys: a run of plain grows plants no content claims at all.
Under a single `Size` statement, `Size(id,10)`, `Size(id,20)` and `Size(id,30)` followed by `Ref(id,50,10)` left three fragment-owning strays over `[10,20)`, `[20,30)` and `[30,50)`; with `Grow`s the same history leaves **none**.

**Whether a link is a stray or load-bearing depends on what is physically present beneath it, which nothing can see.**
The same `Shrink` is load-bearing while the extent it truncated is still in a leaf, and a stray once that leaf has been consolidated.
This is the same undecidable predicate as the shrink-emission test, seen from the consolidation side — and exactly the predicate a `Grow` never needs, since it denies nothing.

It is not self-reinforcing at scale: three bytes per stray does not meaningfully shift a page's live fraction.

## A possible extension, not adopted

An id's live statements are exactly the owners of its fragments together with its anchor and grow witness, since `F` and `A` are the only pins there are.
Walking the id's fragments and collecting the distinct owners therefore yields a live count, and comparing it with `mentions` says whether any *dead* statement naming the id is still physically present.

For a tombstone that test is **exact**, by the lemma above: if no dead mention remains then nothing older than the tombstone remains at all, so dropping it could change neither existence nor the size, and it could retire while still the anchor — which the `mentions == 1` rule never permits for a re-allocated id, since the new incarnation's own statements hold `mentions` above 1.

For a `Shrink` the same test would be **unsound**, and instructively so: statements older than a `Shrink(id, n)` stay *alive* below `n` and carry their extents with them, so an anchor could pass the test while an older `Ref` is live with a far greater extent, and dropping the anchor would readmit it.

The cost is `O(fragment_count)` per check, which is why it is left out for now.
Two cheaper triggers are worth revisiting if tombstones turn out to linger: running the count **at open**, which decodes every physically present statement anyway and so can retire every tombstone that is its id's only statement directly; or when the recyclable-id pool grows past a threshold.
The two do not cover the same population, and the load-time one covers strictly more: a tombstone frozen by re-allocation belongs to an id that is *not* in the pool, so no pool-size threshold notices it and `mentions == 1` can never fire for it again.

## Confirmed sound

Three properties that the adversarial walk-through of this model checked explicitly, and that any reimplementation should preserve:

- **The pin graph is acyclic**, trivially so now that both kinds point the same way: `F` and `A` both point from the resolved view into statements, and no statement pins another. The denial pin of the earlier model was the only statement-to-statement edge it ever had.
- **A statement holds at most two pin kinds**, and there is no third kind left in the model.
- **`mentions` is a net count**, so a flush that replaces a statement leaves it unchanged and the order in which a flush applies its writes and drops does not matter.

## Ranking

Cleaning should be ranked by **retirements enabled** as well as bytes reclaimed.

Victim selection ranked purely by live fraction cannot see the benefit of cleaning a page whose dead statements are holding some tombstone's `mentions` above 1, so a page at 99.8 % live fraction is never chosen even though cleaning it would let a tombstone elsewhere retire.
A benefit term counting "dead statements whose removal would let a tombstone retire" makes that visible.

The case is confined to the tombstone of an id that was freed and never re-allocated.
