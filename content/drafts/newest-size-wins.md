---
title: The newest size wins
---

**Status: a proposal for [the address table](../spec/address-table.md), analyzed; recommended, with three refinements.**
Under this proposal, an allocation's size is the size its newest *sizing* statement states, instead of a maximum over an anchor and everything stated above it.
That removes `Grow`, the anchor, the grow witness, the content claims of `Shrink` and `Tombstone`, and the `Shrink` that every shrink emits whether it is needed or not, together with the undecidable test it stands in for.
In memory, an allocation tracks one size statement instead of an anchor and a grow witness, and every fragment has an owner.

The price is that a writer must state the zeros a growth exposes, and on kladde-svg's drawings it already writes every byte it grows: in nine runs, not one of 1,305 to 85,532 growths per run left a byte unwritten, and not one `Grow` was stated.
Meanwhile, today's design spends 4 to 20 % of the statements it writes on `Shrink`, of which 61 to 99 % would cost nothing under the proposal, and 9 to 39 % of the statements live at the end of a run are ones the proposal would let go; [the measurements](#measured-on-kladde-svgs-drawings) are below.

The refinements:

1. **State the rule that makes zeros explicit as coverage of the range a growth exposes**, not as a bound on each statement's offset, which is too strict.
2. **At most one sizing statement per id and epoch**, even when two would agree.
3. **The size statement is a field of the allocation's metadata**, not an entry of the fragment map, since a `Size` owns no fragment.

## Why the size rule is the root

**The current rule makes every statement's extent a claim on the size, and every mechanism this proposal removes exists to keep stale claims from counting.**
[Today](../spec/address-table.md#conflict-resolution-across-epochs), the size is the maximum of the anchor's bound and of every `Grow` bound and content extent above the anchor, where the anchor is the newest `Shrink` or `Tombstone`.
So a statement deep in a leaf can raise the size if the anchor above it goes away, and a writer that wants to fix a size must anchor it, and must keep anchoring it as the anchor's page is retired:

- the anchor, an `A` pin that is invisible in the fragment map, and [the replacement-anchor obligation](../impl/address-table-operations.md#replacement_anchorid--the-one-correctness-obligation) of every page rewrite;
- `Grow`, a statement that bounds the size without anchoring it, its grow witness, and [its three death tests](../impl/liveness.md#grow-is-locally-decidable-and-shrink-is-not);
- the content claim of `Shrink`, which denies probes past its bound so that a later growth cannot expose truncated bytes, and which lets a `Shrink` own fragments, [linger as a stray](../impl/liveness.md#the-residual), and [raise the size](../impl/liveness.md#a-shrink-can-raise-the-size-not-only-lower-it);
- the [always-emitted `Shrink`](../impl/liveness.md#emission-when-a-resize-must-write-a-statement), since whether a shrink needs one depends on what lies in leaves, which no structure reaches;
- the conflict between a `Grow` and a replacement `Shrink` in one epoch, which [keeps nearly empty leaves from being consolidated](consolidation-from-statements.md#the-conflict-the-check-guards-against).

**None of this flexibility is ever used.**
A writer always knows the size it wants to state, so it never needs the reader to work out a size from what is physically present.

## The proposal, with the refinements

### Statement types

`len` below is the field the specification calls `size`, renamed here to keep it apart from the allocation's size.

| statement | existence of `id` | size it states | content of `id` |
| --- | --- | --- | --- |
| `Ref(id, offset, len, address)` | exists | none | `[offset, offset + len)` equals `file[address, address + len)` |
| `Zero(id, offset, len)` | exists | none | `[offset, offset + len)` is zero |
| `Inline(id, offset, len, payload)` | exists | none | `[offset, offset + len)` equals `payload`, with `1 <= len <= 125` |
| `Ref*`, `Zero*`, `Inline*`, with the same fields | exists | `offset + len` | as the plain variant |
| `Size(id, n)` | exists | `n` | none |
| `Tombstone(id)` | does not exist | `0` | none |

The starred variants are the **sizing** variants, and `Size`, the starred variants, and `Tombstone` together are the statements that **state a size**.
A sizing variant states the size whether that grows the allocation, shrinks it, or leaves it as it was.
`Grow` and `Shrink` are gone.

### Resolution

- **Existence**, as today: the id exists if its newest statement is not a `Tombstone`.
- **Size**: the size stated by the id's newest statement that states one, or `0` if none does.
- **Content at a probe below the size**: the newest `Ref`, `Zero`, or `Inline`, of either variant, with `offset <= probe < offset + len`.
  `Size` and `Tombstone` match no probe.
- **A probe below the size that no statement matches makes the file invalid**, which the load detects at no cost; see [Zeros are stated](#zeros-are-stated-and-implicit-ones-never-occurred).

A `Tombstone` states a size of `0` only so that a file in which an id exists again without stating a size resolves to an empty allocation rather than to its old incarnation's size; a conforming writer always states one.

### No conflicts within each epoch

- No two statements contradict each other on the existence of an id, as today.
- No two `Ref`, `Zero`, or `Inline` statements name the same byte, as today; `Size` and `Tombstone` name none.
- **At most one statement per id states its size.**
- **If an epoch states an id's size `n`, every content statement it holds for the id ends at or below `n`**, which is today's rule for `Shrink`.

### The coverage rule

> **A probe that enters an allocation in epoch `e` is matched by a content statement of epoch `e`.**
> A probe enters in epoch `e` if it is below the size `e` leaves the allocation with and not below the size the allocation had before `e`, which is `0` for an id that did not exist before.

It is a writer's rule: a reader cannot check it, since it would need the size each epoch started from.
It is also the only cross-epoch rule left, and [the next section](#why-the-newest-size-can-win) shows that it is all that keeps truncated bytes out.

### Physical format

```
statement        := id_delta (tagged_ref | tagged_zero | tagged_tombstone | tagged_size | inline)
tagged_ref       := (0 | 1):byte offset_delta:varint len:varint address:varint   ; 1: Ref*
tagged_zero      := (2 | 3):byte offset_delta:varint len:varint                   ; 3: Zero*
tagged_tombstone := 4:byte
tagged_size      := 5:byte offset_delta:varint                                   ; n
inline           := tag:byte offset_delta:varint payload:byte{(tag - 4) >> 1}     ; tag >= 6
```

**For every content statement, bit 0 of the tag says whether it is sizing.**
An `Inline`'s tag is `4 + 2·len + sizing`, which runs from 6 to 255 for `len` from 1 to 125.
The delta encoding is unchanged: a `Size` sorts at `n`, and the cursor moves forward by `len` after every content statement of either variant.
`offset_cursor` never has to move backwards, which now follows from the rules of one epoch alone; [the writer invariant](../spec/address-table.md#delta-encoding) that a `Grow` must not sit in a page whose content for its id reaches past its bound goes with `Grow`.

The payload ceiling halves from 251 to 125 bytes, which costs nothing at [the threshold of about 64 bytes](../impl/flush.md#the-inline-threshold) that kladde-rs inlines at, since the cut caps merged inline runs at the threshold rather than at the ceiling.

## Why the newest size can win

**A probe's winner is always a statement written since the probe last entered the allocation, so nothing a shrink cut off can come back, and a reader needs no record of past sizes.**
Today a `Shrink` keeps this at read time: it matches every probe at or past its bound, and outranks everything older there.
Under the proposal, the writer keeps it at write time, by the coverage rule, and by induction over the epochs a file is written in:

- A probe entering the allocation in epoch `e` is matched by a statement of epoch `e`, which outranks everything older.
- A probe that was already below the size keeps its winner, unless the flush writes it, or retires the page holding its winner and states it again, in either case as the flush's resolved content.
- Dropping a statement that owns no fragment changes no winner, and the size statement is pinned, so it is never dropped without the flush stating a size again.

**The coverage rule is needed: without it, the newest-size rule would serve truncated bytes.**
At epoch 3, allocation 7 holds 1000 bytes, `Ref*(7, 0, 1000, a)`; at epoch 5 it shrinks to 10, `Size(7, 10)`; at epoch 9 it grows to 50, and the application writes bytes 10 to 30, `Ref(7, 10, 20, b)`.
If epoch 9 stated the size as `Size(7, 50)`, the probes from 30 to 50 would resolve through `Ref*@3`, its stale bytes.
The coverage rule makes epoch 9 state `Zero*(7, 30, 20)` instead, which states the size and wins those probes.

**A bound on each statement's offset is the wrong form of the rule: too strict where a growth writes inside the range it exposes.**
"No `Ref`, `Zero`, or `Inline` starts past the allocation's old size", with a `Size` that may only shrink, does keep stale bytes out: a growth must then end in a sizing statement, which starts at or below the old size and so covers the exposed range by itself.
But a growth from 10 to 50 that writes bytes 20 to 30 then needs `Zero(7, 10, 10)`, `Ref(7, 20, 10, b)`, and `Zero*(7, 30, 20)`, where the `Ref` starts past the old size; under the offset bound it would have to write the zeros as data bytes, or take two flushes.
The coverage rule allows those three statements and forbids exactly what lets stale bytes through.

## Answers to the proposal's open points

### Sizing content statements may shrink

**Letting a sizing `Ref`, `Zero`, or `Inline` shrink the allocation raises no issue, and it is where the sizing variants pay off.**
Truncated content needs no denial under the coverage rule, and the rule of one epoch that content ends at or below the size the epoch states keeps the offset cursor monotone, exactly as it does for `Shrink` today.
A shrink often states a content statement ending at the new size anyway:

- a splice that deletes bytes restates the tail behind it, which ends at the new size;
- the header narrows a tail that the shrink cut into, as in [the worked trace](#before-and-after-the-worked-traces);
- a shrink that overwrites the new last bytes writes them.

In each case the sizing variant states the size at no cost, where today a `Shrink` is emitted beside it.

### `Size` replaces both `Shrink` and `Grow`

**Under the coverage rule, a growth always ends in a content statement at the new size, so a statement that states only a size is needed for three things alone: a shrink that states no content ending at the new size, an empty allocation, and stating a size again when the page holding the size statement is retired.**
It claims no content, and nothing about it is specific to shrinking any more, so the proposal calls it `Size`.
A `Size` may state a larger size than before, provided the epoch covers what it exposes; no writer needs that, since the content statement ending at the new size can be sizing.

### One sizing statement per id and epoch

**Allow at most one, even when two agree.**
No writer needs two: the cut states one per id, the content statement ending at the size if there is one, else a `Size`.
Then "the newest statement that states a size" is unique, the reader's tracker is one comparison per statement, and a tie, from a buggy writer, is a violation that the same comparison detects.
Allowing agreeing duplicates would cost a tie-break every reader must agree on, cheap but needed by no one.

### The size statement is a field, not a fragment-map entry

**Track it as `size_statement: StatementRef` in the allocation's metadata, holding one pin, `S`.**
In kladde-rs it replaces four fields, the anchor, its bound, the grow witness, and its bound, 16 bytes per allocation, with 4.

The fragment map cannot carry it alone, because a `Size` owns no fragment: after a shrink that wrote nothing at the new end, for an empty allocation, and after a size is stated again.
Carrying those would need an end entry per allocation, a B-tree key and a fragment, which costs several times the field, plus the B-tree's own overhead.

Even a sizing content statement owns the last fragment only if every newer content statement ending at the size is sizing too.
That is worth making **the writer's policy: every content statement the cut states that ends at its id's size is sizing**.
It costs no byte, and it moves the size statement onto the newest statement of the tail, which releases the previous one at once instead of keeping its leaf alive; the measurements below assume it.
But a reader cannot rely on another writer's policy, so the field stays the record.

### Zeros are stated, and implicit ones never occurred

**The coverage rule makes every zero below the size a statement, and kladde's own containers already write every byte they grow.**
A vector push writes past the end, a map writes the whole slot it grows by, and a derived enum writes zeros where a smaller variant leaves bytes unused.
In the nine runs below, no growth left a range unwritten, no `Grow` was stated, and at the end of each run no zero range was owned by a `Shrink` or a `Tombstone` either: today's design never once used an implicit zero, nor a `Shrink`'s denial of an exposed range.

The use case [Allocations](../spec/allocations.md#content-semantics) names, a vector of a thousand empty strings as one `Grow`, survives as one `Zero*`, one byte longer.
What would cost more is a sparse allocation, a large range grown and then written in scattered places: each gap is a `Zero`, a few bytes, and each `Zero` is stated again when its page is retired, where an implicit zero was free.

**So make an unmatched probe below the size a format violation**, which the load sweep finds as a gap in its envelope, at no cost.
`ZeroByDefault` then disappears from memory, and with it the one fragment variant without an owner that [every code path must remember](../impl/in-memory-state.md#consequences-the-implementation-must-honour).
The alternative keeps implicit zeros legal, so that a writer may omit zeros where no physically present statement can match, which it can know cheaply only for an id that no physically present statement names, `mentions == 0`; it would keep `ZeroByDefault`, for a saving no measured workload would see.

## What it removes

### From the format and its rules

- `Grow`, and `Shrink` with its content claim.
- A `Tombstone`'s match on every probe.
- The anchor, `anchor_epoch`, and the maximum rule for the size.
- [Why the resize is split in two](../spec/address-table.md#statement-types), whose reasons were the maximum rule's.
- The robustness note on `epoch > anchor_epoch`, and the delta-encoding invariant for `Grow`.

### From the in-memory state

- **One size statement per allocation instead of an anchor and a grow witness.**
- **Two kinds of pin, as today, but simpler ones**: `F`, for each fragment a statement owns, and `S`, held by an existing id's size statement until a newer statement states the size or the id is freed, and by a non-existent id's tombstone until `mentions` falls to 1, as today.
  Today's `A` has two kinds of holder and five release conditions.
- **A zero fragment is owned by a `Zero`, and only by a `Zero`**, and there is no `ZeroByDefault`.
- **A re-allocated id's tombstone dies at re-allocation**: it is not the newest statement, matches no probe, and states no size that counts.
  Today it stays the anchor of the new incarnation.

### From loading

**The sweep tracks the newest size-stating statement as it tracks the newest statement, and clips at the size once the group is done.**

```rust
fn resolve_id(state: &mut State, id: Id, stmts: &mut Peekable<Merge>, emitted: &mut Vec<Run>) -> Result<()> {
    let mut open = MaxHeap::new();      // content statements only
    let (mut newest, mut sizing, mut mentions) = (None, None, 0);
    let (mut pos, mut current, mut run_start) = (0, None, 0);
    let first = emitted.len();
    loop {
        while open.peek().is_some_and(|e| e.end <= pos) { open.pop(); }
        while stmts.peek().is_some_and(|s| s.id == id && s.start() <= pos) {
            let s = stmts.next().unwrap();
            mentions += 1;
            newest = newest_of(newest, s);
            if s.states_size() { sizing = newest_sizing(sizing, s)?; } // a tie is a violation
            if s.is_content() { open.push(s.epoch, s.end(), s); }
        }
        let winner = open.peek();
        if winner != current {
            if pos > run_start { emitted.push(Run { id, start: run_start, owner: current }); }
            (current, run_start) = (winner.copied(), pos);
        }
        let next = stmts.peek().filter(|s| s.id == id).map(|s| s.start());
        if next.is_none() && winner.is_none() { break; }
        pos = min(next.unwrap_or(u32::MAX), winner.map_or(u32::MAX, |w| w.end));
    }
    let size = sizing.map_or(0, |s| s.size());
    clip(emitted, first, run_start, size)?;  // drop runs at or past `size`; a gap below it is invalid
    finish(state, id, &emitted[first..], mentions, size, newest, sizing) // slots and pins, after the clip
}
```

What goes: the `Grow` branch and its `beats` test, [the section explaining why `Grow` needs it](../impl/address-table-operations.md#why-grow-needs-its-own-rule), unbounded match ranges for `Shrink` and `Tombstone`, the anchor read off the winner at termination, and [the argument that `run_start` is the size](../impl/address-table-operations.md#why-run_start-is-the-size).
What comes: the size is known only once the group is done, so the runs past it are dropped then, and slots and pins are given out after that rather than on first ownership.
A group's runs are emitted into a vector anyway, so this costs no copy.

### From flushing and consolidation

- **A growth takes its exposed range as pending zeros**, unless the flush writes it; [`grow_size_to`](../impl/address-table-operations.md#grow_size_toid-n-and-shrink_size_toid-n) no longer chooses between an anchor-owned and an unowned zero range.
- **`set_anchor`, `set_grow_witness`, the death tests, and the anchor's and witness's release become one `set_size_statement`.**
- **[What the cut states for a touched id](../impl/address-table-operations.md#what-the-cut-states-for-a-touched-id) shrinks from five rows to three rules**:
  every content statement the cut states that ends at its id's size is sizing;
  an id that is new, was resized, or whose size statement lies in a retired page, and has no such statement, gets `Size(id, size)`;
  an id freed, or not existing and with its tombstone in a retired page, gets `Tombstone(id)` as today, the latter while `mentions > 0`.
- **[`replacement_anchor`](../impl/address-table-operations.md#replacement_anchorid--the-one-correctness-obligation), with its four cases, becomes the second rule.**
  A retired size statement is still an obligation, since dropping the newest size statement readmits an older one, but it is a pin like any other, recorded in one field, and its replacement never conflicts with anything the cut states.
- **The fillers' conflict is gone**: every statement the cut could state for an id's size states the current size, and the cut states one.
  The eligibility check that [keeps nearly empty leaves out](consolidation-from-statements.md#the-cuts-fillers-refuse-every-leaf-that-names-an-id-the-flush-touched) has nothing left to guard.
- **A shrink's statement is exact**: it is needed whenever the size changes, since the newest size wins, and it is free whenever the flush states a content statement ending at the new size.
  The bit that [Liveness declines to pay for](../impl/liveness.md#emission-when-a-resize-must-write-a-statement) is not needed.

### From liveness

[Liveness](../impl/liveness.md) loses five of its sections: [`Grow` is locally decidable](../impl/liveness.md#grow-is-locally-decidable-and-shrink-is-not), [the anchor pin is not substitutable](../impl/liveness.md#the-anchor-pin-is-not-substitutable), [a `Shrink` can raise the size](../impl/liveness.md#a-shrink-can-raise-the-size-not-only-lower-it), [emission](../impl/liveness.md#emission-when-a-resize-must-write-a-statement), and [the residual](../impl/liveness.md#the-residual).
The droppability rule becomes `pins == 0` over `F` and `S`, with [the last-tombstone rule](../impl/liveness.md#the-last-tombstone) unchanged.
Pins rise after creation only when a split gives one statement a second fragment, never because a growth exposes a range an old `Shrink` wins.

### From kladde-rs's list of gaps

**Five of the six gaps that kladde-rs's `implementation-notes.md` records in this machinery could not arise, and the sixth only in part.**

- A `Grow` above a `Tombstone` anchor must survive `n <= anchor.n`: no `Grow`, no anchor.
- A `Grow` stays the grow witness at load whenever the size is 0: a `Size(id, 0)` is the size statement like any other.
- A growth must not resolve through an anchor the header's take retires: a growth always takes its exposed range.
- A grow witness in a retired page must be released when another statement witnesses the size: binding a new size statement releases the old one.
- The cut's fillers state only ids the flush has not touched: the conflict they avoid cannot arise.
- The last-tombstone rule must not fire when the tombstone itself leaves: what went wrong, a re-allocated id finding no tombstone to anchor on, cannot happen, since re-allocation anchors on nothing; restating a non-existent id's tombstone when its page is retired remains.

All six are gaps in the implementation documents that surfaced only once kladde-rs implemented and tested them, as [the leaf pile-up](consolidation-from-statements.md) did.

## What it costs, and what is new

- **Zeros the application never wrote cost a statement**, a `Zero` of a few bytes per gap, stated again whenever its page is retired; none occurred in the runs below.
- **The coverage rule is a writer's obligation that no reader can check**, of the same silent kind as today's replacement-anchor obligation, but in one place: every growth takes its exposed range.
  A differential test should grow allocations over shrunk statements still present in leaves, since only those make a missed range visible.
- **The load sweep must clip at the size and give out slots afterwards**, a step it does not have today.
- **A `Size` is still invisible in the fragment map**, so a page rewrite must still ask whether the page holds an id's size statement; but the question is one field and one comparison, with no case distinction on what kind of statement it is.
- **A size statement may own no fragment while pointing at data**: a sizing `Ref` from another writer, shadowed at the tail by a newer plain statement, stays live through `S` while no fragment reads its bytes, so its data page's coverage does not count them, and the page may be retired and reused under it.
  Nothing reads those bytes, since the statement wins no probe, and a reader already must not check the addresses of statements that win nothing, since dead statements point into reused pages too; kladde-rs's own files never contain one, by the writer's policy above.
- **`Inline` payloads are capped at 125 bytes**, which binds only a threshold above that.
- **The specification's case for zero-by-default content moves**: identical bytes across implementations hold trivially when every byte is stated, and deleted data not reappearing is the coverage rule's to guarantee.

What it leaves alone: existence and the last-tombstone rule, per-page epochs, and the cost of describing a splice, which restates every statement behind the splice point; only the `Shrink` beside a deleting splice goes.

## Measured on kladde-svg's drawings

**On every drawing and layout, today's design writes `Shrink`s by the thousands, mostly where the proposal would write nothing, and keeps a third of its live statements alive for the size alone in two of the three layouts.**
The runs are those of [the leaf pile-up](consolidation-from-statements.md#the-finding): kladde-rs `795b3b9`, svg-bench's focused edits, 31,613 edits of the tiger, 41,130 of the coat of arms, and 177,831 of the world map, a journal of 4 pages, and main's consolidation; scratch counters observed the cut and the state after every commit without changing what was written.

| drawing | layout | `Shrink`s written | of all statements written | of their framing bytes | free under the proposal | live `Shrink`s | of live statements | live statements released |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tiger | base | 18,156 | 17 % | 12 % | 88 % | 849 | 36 % | 36 % |
| tiger | packed | 16,666 | 19 % | 15 % | 94 % | 596 | 28 % | 29 % |
| tiger | small | 3,829 | 8 % | 4 % | 99 % | 53 | 9 % | 9 % |
| coat of arms | base | 27,281 | 16 % | 11 % | 84 % | 2,213 | 35 % | 37 % |
| coat of arms | packed | 26,752 | 20 % | 15 % | 90 % | 2,146 | 37 % | 39 % |
| coat of arms | small | 9,621 | 8 % | 4 % | 95 % | 252 | 19 % | 20 % |
| world map | base | 115,910 | 11 % | 7 % | 61 % | 9,497 | 32 % | 31 % |
| world map | packed | 156,058 | 19 % | 14 % | 76 % | 10,121 | 37 % | 38 % |
| world map | small | 49,519 | 4 % | 2 % | 86 % | 612 | 11 % | 11 % |

- **"`Shrink`s written"** counts every `Shrink` the cuts stated, for shrinks and as replacement anchors; most are replacements, of anchors the header carries and states again every flush, 25,206 of the 27,281 on the coat of arms in its base layout.
  Framing bytes leave out inline payloads.
- **"Free under the proposal"** is the share of them that a cut stated together with a content statement ending at the id's size, which the proposal would make sizing instead; the rest would be `Size` statements, of the same length.
  It is a lower bound on the saving, since under the proposal the size statement would mostly sit on a content statement already, and fewer replacements would arise; the bytes saved come to at least 2 to 14 % of the framing the cuts wrote.
- **"Live statements released"** counts, after the last commit, the `Shrink` anchors whose id's last fragment is owned by a statement at least as new, which ends at the size and would be sizing, together with the tombstones still anchoring an id that was allocated again since; it is an estimate from the final state, not a run under the proposal.
  Between 74 and 100 % of the live `Shrink`s are of the first kind, and on the world map in its base layout, 2,113 of the 2,297 live tombstones are of the second.
- **The layout matters through the number of allocations**: the small layout keeps short strings and vectors inline, so it has 84 to 666 allocations where the others have 1,168 to 12,573, and fewer anchors among its statements.

## Before and after: the worked traces

**The [worked trace](../impl/address-table-operations.md#worked-trace) of allocation 7 shows what the proposal costs, since it grows an allocation without writing the range it exposes, which the drawings above never do: six statements written instead of five, none of them unnecessary, and none kept alive by the size alone once something newer states it.**

| flush | operation | today | under the proposal |
| --- | --- | --- | --- |
| 3 | allocate 7, write 1000 bytes | `Ref(7,0,1000,P1)`@3 | `Ref*(7,0,1000,P1)`@3, the size statement: pins `F` + `S` |
| 5 | truncate to 10 | `Shrink(7,10)`@5, the anchor | `Size(7,10)`@5, the size statement; `Ref*@3` keeps `[0,10)` and loses `S` |
| 9 | grow to 60, write `[50,60)` | `Ref(7,50,10,PB)`@9; `[10,50)` resolves through `Shrink@5`, which gains an `F` pin | `Zero(7,10,40)`@9 and `Ref*(7,50,10,PB)`@9; `Size@5` loses `S` and owns nothing, so it is dead |
| 10 | resize to 55 | `Ref(7,50,5,PB)`@10 and the unnecessary `Shrink(7,55)`@10 | `Ref*(7,50,5,PB)`@10, which states the size; and `Zero(7,10,40)`@10, as the header states everything again |
| 10, instead | resize to 30 | `Shrink(7,30)`@10, now necessary, and indistinguishable from the case above without a bit per allocation | `Zero*(7,10,20)`@10 if the header holds the `Zero`; `Size(7,30)`@10 if a leaf holds it, which keeps `[10,30)` |

At flush 9 the proposal writes a `Zero` that today's design does not, and the header writes it again at flush 10, where the proposal saves the `Shrink`.
In exchange, the `Shrink@5` that today's design keeps alive in a leaf for its zeros is dead under the proposal from flush 9 on, and whether flush 10 needs a size statement of its own is decided by what the flush states, not by a bit no structure keeps.

**The [re-allocation trace](../impl/address-table-operations.md#re-allocation) of allocation 12 keeps no statement of the old incarnation alive.**
Allocation 12 is `Ref*(12,0,100,P1)`@3, freed at epoch 5, recycled at flush 7 at 100 bytes with `[0,10)` written, and truncated to 50 at flush 9.

| statement | today, after 7 / after 9 | under the proposal, after 7 / after 9 |
| --- | --- | --- |
| `Ref(12,0,100,P1)`@3, sizing under the proposal | 0 / 0 | 0 / 0 |
| `Tombstone(12)`@5 | 2 = `A` + `F` / 1 = `F` | 0 / 0 |
| `Ref(12,0,10,PX)`@7 | 1 / 1 | 1 / 1 |
| `Grow(12,100)`@7, or `Zero*(12,10,90)`@7 | 1 = witness / dead | 2 = `F` + `S` / 1 = `F`, if a leaf holds it |
| `Shrink(12,50)`@9, or `Size(12,50)`@9 | — / 1 = `A` | — / 1 = `S` |

Today the tombstone's `F` pin is what keeps the old incarnation's bytes out of `[10,50)`; under the proposal the `Zero*` that the growth stated does it, and it is the new incarnation's own statement.

## What it means for the other drafts

- **[Consolidation from the statements up](consolidation-from-statements.md)**: its task 3 infers a size statement for an id exactly when the size changed or the size statement went with a victim, a local test, so the size summary and its three ways to settle it are not needed.
  Its proposed spec change, statements that keep their epoch, loses most of its motivation, which was the replacement anchor, the `Shrink` in place of a `Grow`, and the size summary.
  It would still let a rewrite copy statements without restating them, which is now a question of cost alone.
- **[The `Move` operation](move-op.md)**: a donated suffix costs a `Ref` and a `Size` instead of a `Ref` and a `Shrink`, the same bytes.
- **[Variable-size values](variable-size.md)**: a deleting splice no longer pays a `Shrink`, since the restated tail states the size; what a splice costs otherwise is the next step, not this one.

## Documents to change if adopted

- **[Address table](../spec/address-table.md)**: the statement types, the rules of one epoch, the resolution, the grammar and the decoding steps, the `Inline` bound, and the case for zero-by-default content; a name for the coverage rule among the writer's rules.
- **[Allocations](../spec/allocations.md)**: a zero-sized allocation is a `Size(id, 0)`; id recycling is safe because the new incarnation states every byte below its size, not because the tombstone matches every probe; re-allocation in one flush needs no `Shrink`; and the paragraph on zeroing, which cites the always-emitted shrink.
- **[Liveness](../impl/liveness.md)**, **[Address-table operations](../impl/address-table-operations.md)**, and **[In-memory state](../impl/in-memory-state.md)**: as listed above.
- **[Consolidation](../impl/consolidation.md)**, **[Consolidator state](../impl/consolidator-state.md)**, **[Id recycling](../impl/id-recycling.md)**, **[The flush](../impl/flush.md)**, and the [Implementation](../impl/index.md) overview: the passages on anchors, grow witnesses, and `ZeroByDefault`.
- **[kladde-rs's store](../rust/store.md)**: the niche optimization's savings, and the reason `pins` is 32 bits.

## Open questions

- **Whether a writer may leave zeros implicit for an id that no physically present statement names**, which keeps `ZeroByDefault` and saves a `Zero` per gap only for sparse allocations; no workload measured here has one.
- **Whether to rename the content statements' `size` field to `len`** in the specification, as this draft does, now that "ends at the size" is a rule of its own.
- **Whether the rotating window should state a `Size` again when it restates from the page holding one**, as it [replaces an anchor today only where that pays](../impl/address-table-operations.md#rewrite_key_rangelo-hi-dirty); with the writer's policy above, a `Size` survives only until the id's tail is next stated, so it may never matter.
