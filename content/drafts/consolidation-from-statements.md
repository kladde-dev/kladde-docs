---
title: Nearly empty leaves, and consolidation from the statements up
---

**Status: a finding, and a direction for a redesign; high priority.**
The consolidation policy of [Consolidation](../impl/consolidation.md), as kladde-rs implements it, keeps address-table leaves that hold almost nothing in the file for dozens of flushes, so that 4 to 33 % of a file's live pages are leaves it would not need if they were as full as its data pages.
The cause is not a tuning constant but the way the policy interleaves three decisions, which page to retire, which statements to write, and where to put them, so that each constrains the others.
The second half of this draft starts over from [the logical level of the address table](../spec/address-table.md#logical-level) and keeps the three apart.

## The finding

**Leaves that hold almost nothing are consolidated only when enough half-empty leaves happen to fill an offer with them: two rules keep them out on their own, and on a small table only compaction mode collects them, in bursts.**
On kladde-svg's drawings, with svg-bench's edits keeping to one part of the drawing at a time, a journal of 4 pages, and main's consolidation at its default churn floor, at kladde-rs `795b3b9`, the address-table pages average 3 to 48 % full, against 73 to 83 % for the data pages, and more than half of them are less than half full at any time:

| drawing | layout | table pages | pages needed | fill | leaves under half full | under a tenth full | data pages under half full | share of live pages |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tiger | base | 15.8 | 7.6 | 48 % | 55 % | 12 % | 4.5 % | 38 % |
| tiger | packed | 13.1 | 4.2 | 32 % | 73 % | 40 % | 7.4 % | 48 % |
| tiger | small | 8.8 | 0.2 | 3 % | 100 % | 92 % | 4.4 % | 34 % |
| coat of arms | base | 37.2 | 17.0 | 46 % | 61 % | 5 % | 1.4 % | 36 % |
| coat of arms | packed | 24.8 | 10.4 | 42 % | 66 % | 10 % | 2.8 % | 41 % |
| coat of arms | small | 9.1 | 1.7 | 19 % | 87 % | 62 % | 1.8 % | 17 % |
| world map | base | 203.6 | 87.4 | 43 % | 67 % | 0 % | 0.3 % | 37 % |
| world map | packed | 122.4 | 56.7 | 46 % | 60 % | 1 % | 0.4 % | 31 % |
| world map | small | 24.0 | 9.2 | 38 % | 73 % | 16 % | 0.2 % | 7 % |

"Table pages" is their mean count, "pages needed" what their live statements would fill, "fill" the ratio of the two, and the last column the table pages' share of the live pages.
The means are over the rows svg-bench records every 1000 edits, and the shares over histograms of every live page's fill taken at the same flushes, by scratch instrumentation that did not change what was written.
Were the table pages as full as each run's own data pages, the files would carry 4 to 33 % fewer live pages, and 14 to 18 % fewer on the coat of arms and the world map in their base and packed layouts.
The edited tiger that the variable-size evaluation, on the kladde-docs branch `defrag-shifts`, reports closing with 41 table pages for 2,265 statements likely shows the same effect.

**On the small tiger, the leaves pile up for a dozen flushes and then vanish at once.**
Its table pages climb to 11 to 19 at the peaks of the cycle, holding between 100 and 2,800 bytes of live statements in all; almost no table page is rewritten until compaction mode turns on, and that mode then rewrites 11 to 18 of them within one or two flushes, after which the count falls to one or two and starts climbing again.
On the world map, whose leaves hold more in all, leaves are rewritten in nearly every flush, the nearly empty ones among them, and on the coat of arms in about half of them; but in the base and packed layouts the leaves still average 42 to 46 % full, and this draft does not trace why.

## Why they pile up

### The budget loop holds a table offer to the data pages' fill floor

**[The budget loop](../impl/consolidation.md#the-budget-loop) takes a page rewrite only when the restatements of its victims would fill a whole leaf to `1 − θ`, 95 %, as it takes data victims only when their survivors fill a whole data page.**
kladde-rs' `implementation-notes.md` records it: "a budgeted page rewrite opens no page of its own … the budget counts it as a page all the same, and holds it to the fill floor like a data offer."

For a data page the floor has a reason: evacuating victims into a page the flush opens for them costs that page, and a page that closes half empty is a page wasted.
For a table page it has none: a page rewrite opens no page, since what it restates joins the flush's dirty set and [the cut](../impl/consolidation.md#one-dirty-set-and-why-statements-are-derived-last) lays it out among everything else the flush states.
Its cost is the restatements' bytes, which [the churn floor](../impl/consolidation.md#the-churn-floor-is-a-parameter-not-an-identity) already weighs against the page it frees.

The floor turns the cheapest victims into the hardest to take: an offer is made of whole leaves at most half live, best score first, and needs 95 % of a leaf in restatements.
A file whose leaves hold less than that in all, such as the small tiger, whose leaves hold at most 3 KB together, can never pass it, however empty its leaves are.
Where the leaves hold more, offers pass, and the nearly empty leaves ride along with the half-empty ones that fill them.

### The cut's fillers refuse every leaf that names an id the flush touched

**The leaves that the budget loop does not take are meant to go as [fillers](../impl/consolidation.md#the-page-rewrite): whole table victims that fit the room of the cut's last page, which costs nothing.**
That room is ample: in 8000 edits of the small tiger, logged at every flush, it was 800 to 3,500 bytes in nearly every one, and every nearly empty leaf would have fit.
But kladde-rs takes a filler only if the flush touched none of the ids the leaf names, and almost none qualified: on the small tiger, of up to 13 leaves under a quarter full, 0 were eligible in every flush, and on the coat of arms in its base layout, 0 to 2 of 2 to 14.

The check reads a leaf's ids by decoding every statement still encoded in it, dead ones included.
So a leaf with 4 to 25 bytes of live statements still names 74 to 251 ids, while each flush of the tiger touches 43 to 48 of its roughly 120 ids, and some named id is always among them.

The check guards against a real conflict, but too broadly in two ways: the conflict is an artifact of the order in which the cut decides things, and the dead statements could never be part of it.
The next two sections show both, each with an example.

### The conflict the check guards against

**Taking a leaf as a filler can require a `Shrink` for an id for which the cut has already decided to state a `Grow`, and [one epoch may not hold both](../spec/address-table.md#no-conflicts-within-each-epoch).**
`implementation-notes.md` gives it as the reason for the check, and an example shows it:

- At epoch 3, allocation 7 holds 1000 bytes, stated by `Ref(7, 0, 1000, a)` in leaf `L1`.
- At epoch 5, the application shrank it to 10 bytes: `Shrink(7, 10)` is its anchor, in leaf `L2`, where the header later evicted it.
- In the flush in progress, at epoch 9, the application grows it to 50 bytes and writes bytes 10 to 30.

After the fold, 7's fragment map holds `[0, 10)` through `Ref@3`, `[10, 30)` as fresh bytes, and `[30, 50)` as zero owned by `Shrink@5`, which denies `Ref@3` there.
By [what the cut states for a touched id](../impl/address-table-operations.md#what-the-cut-states-for-a-touched-id), the cut writes `Ref(7, 10, 20, b)` and, since the id grew and nothing it states reaches 50, `Grow(7, 50)`.
Resolved with `L1` and `L2`, that is right: the anchor is `Shrink@5`, so the size is `max(10, 50, 30) = 50`, `Ref@3`'s extent of 1000 lying below the anchor, and `[30, 50)` resolves to zero through the `Shrink`.

Now let `L2` be nearly empty, and the cut take it as a filler.
Dropping it drops 7's anchor, and with no replacement the size becomes `max(1000, 50, 30) = 1000`, and `[30, 1000)` reads `Ref@3`'s stale bytes: [the one correctness obligation](../impl/address-table-operations.md#replacement_anchorid--the-one-correctness-obligation) of a page rewrite.
Its replacement is `Shrink(7, 50)` at epoch 9, with the zeros the old anchor owned taken in place and stated as `Zero(7, 30, 20)`; but epoch 9 already holds `Grow(7, 50)`, and a `Shrink` beside it is illegal.

**The resolved state is expressible all the same: state the `Shrink` instead of the `Grow`.**
Epoch 9 then holds `Ref(7, 10, 20, b)`, `Zero(7, 30, 20)`, and `Shrink(7, 50)`, which break no rule of one epoch: one size statement, and content statements on disjoint ranges below 50.
They resolve to the same allocation: the anchor is now `Shrink@9`, so the size is its own 50, every extent at or below epoch 9 dropping out; `[0, 10)` still resolves through `Ref@3`, since `Shrink(7, 50)` matches only probes at or past 50; `[10, 30)` through `Ref@9`, and `[30, 50)` through `Zero@9`.
`Shrink(7, 50)` asserts everything `Grow(7, 50)` asserts, a size of at least 50, and anchors besides, so wherever an anchor must be replaced, a `Shrink` alone takes the `Grow`'s place.

**So the conflict comes from the order of the cut, not from the vocabulary of statements.**
The cut decides each touched id's size statement before it chooses its fillers, since the fillers are chosen to fit the room that its layout leaves, and it does not revisit a decision once the statement is laid out.
The same holds for a tombstone anchor: a filler that holds `Tombstone(8)` of an id the flush allocated again needs `Shrink(8, size)` as its replacement, where the cut may already have stated `Grow(8, size)`, and again the `Shrink` alone states both.

The possible fixes, within the current design:

- **Narrow the check to what can conflict**: ids whose anchor, grow witness, or tombstone the victim holds.
  Content restatements cannot conflict, since a filler restates only fragments its victim still owns, which the flush has not taken; `implementation-notes.md` already relies on this for the rotating window.
  A nearly empty leaf holds few live statements, and so few anchors, but how many leaves would then qualify is untested.
- **Revise the size statement instead of refusing**: when a filler holds the anchor of an id the cut stated a `Grow` for, replace the `Grow` with the `Shrink`, whose encoding takes the same bytes, and add the zeros the anchor owned to the last page's room.
- **Decide the statements after the victims**, which is what [the redesign below](#possible-solutions-from-the-statements-up) does, and which removes the question.

### Dead statements cannot block a rewrite

**Dropping a dead statement never changes what the file resolves to; it only releases pins that newer statements hold on its account.**
A statement is dead when no fragment resolves through it and it is neither its id's anchor nor the size's sole witness, [`pins == 0`](../impl/liveness.md#the-droppability-rule), and the droppability rule is exactly that dropping such a statement leaves the resolution unchanged.
What a dead statement can still do is keep a newer statement alive, as an example shows:

- At epoch 4, allocation 8 holds 40 bytes, stated by `Ref(8, 0, 40, c)` in leaf `L3`.
- At epoch 6, the application freed it: `Tombstone(8)`, in leaf `L4`.

`Ref@4` is now dead, but physically present, and so it holds the tombstone: `mentions(8)` counts both statements, and [a tombstone anchor keeps its pin](../impl/liveness.md#the-last-tombstone) until `mentions` falls to 1, since without it `Ref@4` would be 8's newest statement, and 8 would exist again with its old 40 bytes.
Dropping `L3` drops `Ref@4`, `mentions(8)` falls to 1, and the tombstone may retire: nothing resurfaces, which is the order the pins enforce, the denied statement first and its denier after.

**Where the flush touched the id, a dead statement can at most make one of the flush's own statements unnecessary.**
If the flush frees an id and states its `Tombstone`, and a filler drops the last older statement naming the id, the new tombstone has nothing left to deny: a redundant statement of a few bytes, which the next rewrite of its page drops, never a wrong one.
If the flush allocates 8 anew, the new incarnation's statements keep `mentions` above 1, and dropping `Ref@4` changes nothing the cut has decided.

So the dead statements a leaf still encodes are no reason to keep it.
The check reads them because it learns a leaf's ids by decoding the page, which lists dead statements beside live ones, rather than by asking which of the leaf's statements are live.

### Compaction mode collects them, in bursts

**[Compaction mode](../impl/consolidation.md#compaction-mode) offers the highest live page ahead of every victim, whatever its fill, and an offer holding it passes both floors and is rewritten without the fillers' check.**
Once free pages below the tail pass a quarter of the file, the mode works down the file's tail, page by page, and the nearly empty leaves that the other two paths left behind go with it; the mode then switches off, and they pile up again.
That is the sawtooth on the small tiger, and why its file holds 8.8 table pages on average for 0.2 pages' worth of statements.

### Data pages are not hit the same way

**Data pages almost never fall below half full: 0.2 to 7.4 % of them, against 55 to 100 % of the leaves.**
They have a path without a check: [free filling](../impl/consolidation.md#victims-are-pulled-one-at-a-time) takes any whole data victim that fits the room of a page the flush writes, whatever ids it holds.
Their histogram's one feature, 14 to 25 % of them between 50 and 60 % full, is the default churn floor's own threshold, which takes victims at most half live.

But the same interleaving shapes them: a data victim is taken only if its survivors fit the room of a page being written, or fill a budgeted page to 95 % together with other victims, so which pages are cleaned depends on how their survivors pack, not only on what cleaning them is worth.
Nothing measured here shows that doing harm on data pages; the redesign below removes it on both sides alike.

## The underlying error

**The policy makes three decisions in one interleaved pass, which page to retire, which statements to write, and where to put them, and lets each constrain the others.**

- Whether a leaf is retired depends on whether its restatements fill a page, which is a question about where they go.
- Which leaves the cut may retire depends on the statements it has already laid out.
- The statements for an id are decided before the set of retired pages is final, so a later victim can contradict them.

Each of these is locally sensible, and the later ones were added to keep the interleaving correct, but together they make the cheapest victims unreachable.
The same pattern makes the policy hard to reason about at all: the [budget loop](../impl/consolidation.md#the-budget-loop), free filling, the cut's fillers, the rotating window, and compaction mode each choose victims under different constraints, and each constraint comes from a decision made elsewhere.

## Possible solutions, from the statements up

This section ignores how consolidation and the flush after its fold work today, and starts only from what statements mean.

### What a writer must guarantee

**A file's address table is the set of statements in its live table pages, each at its page's epoch, and [their resolution](../spec/address-table.md#conflict-resolution-across-epochs) defines the fragment map: each id's existence, its size, and for each byte, the statement that wins it and so its content.**
Call the statements `S`, and the fragment map they resolve to `R(S)`.
Statements may contradict each other across epochs, since the newest wins at every probe; within one epoch, [they may not](../spec/address-table.md#no-conflicts-within-each-epoch).

A flush at epoch `e` starts from the file's statements `S` and from the map `M` that the application's writes since the last flush define: `R(S)` updated by the fold.
It ends with a new set of live pages, whose statements `S'` must satisfy `R(S') = M`, and whose data pages hold every byte `M` resolves into the file.

**Consolidation is the choice of which pages to give up, and what to write in their place, so that this still holds.**
That is three tasks, each of which needs only the results of the ones before it.

### Task 1: choose the victims

**Choose a set `V` of pages to retire, table and data pages alike, the current header always among them, by what each costs and what it gains, and nothing else.**
Retiring a data page costs the live bytes it holds, which must be written again elsewhere, and retiring a table page costs the statements that must be written because it is gone, which task 3 infers and which are usually its live statements' worth.
Either gains a page.

Nothing about packing or layout enters the choice, since any `V` is feasible: task 3 can always state the resolved map again, as shown below.
So a page whose live content is nearly nothing is the best victim there is, whichever kind it is, and is taken first; a page of dead statements costs nothing at all.
How to rank the rest, by a churn floor, by [ripeness](ripeness.md), or by any other estimate of what waiting saves, is a separate question, and so is how many pages a flush may retire, which bounds its work.
Returning space to the file system belongs here too: preferring the highest pages, as compaction mode does, is a ranking, not a mechanism of its own.

### Task 2: combine the victims with the fold and the header

**What survives is the statements in the pages not retired, `S_keep`, and the data bytes in the data pages not retired; everything else that `M` needs must be written.**

- **Data**: every byte of `M` that resolves into a retired data page, or into the flush's own new content, must be written into a new data page.
- **Statements**: a set `N` of statements at epoch `e` must be found such that `R(S_keep ∪ N) = M`.
  The retired header's statements are among those that went, so whatever it stated that is still true must be inferred again like anything else.

### Task 3: write the result

**Writing out is two independent policies: one packs the data bytes into new data pages, and one infers `N` and lays it out into new table pages.**

#### Data pages

The bytes to write are known once task 2 is done, so packing them is a bin-packing choice alone: in key order, as today, or sorted by temperature first, as [the survivor-classes draft](survivor-classes.md) proposes, so that content that dies soon and content that waits do not share pages.
Their addresses are what `N`'s `Ref` statements then point to.

#### Statements, by inference

**`N` is what a reader needs beyond `S_keep` to resolve to `M`, and it can be inferred id by id, by comparing what `S_keep` alone resolves to with `M`.**
`N`'s epoch `e` is newer than everything in `S_keep`, so every statement in `N` wins over every surviving one where both match.
For each id:

- **Existence.**
  If `M` says the id does not exist, `N` needs `Tombstone(id)` exactly when some statement in `S_keep` still names the id; otherwise nothing.
  If `M` says it exists, `N` needs at least one statement for it whenever `S_keep`'s newest statement naming it is a tombstone, or none names it; the size rule below provides one as `Grow(id, size)` when nothing else does.
- **Content.**
  For every byte below the id's size in `M`, the winner among `S_keep`'s statements, the newest that matches, either already gives `M`'s content, or `N` must cover the byte with a `Ref`, `Inline`, or `Zero`.
  A byte whose winner sat in a retired page has no winner left among those that won it before, so it is covered again unless an older surviving statement happens to give the same content.
- **Size.**
  With `N`'s content statements added, compute the size `S_keep ∪ N` resolves to.
  If it is `M`'s size, nothing more is needed; if it is smaller, `N` needs `Grow(id, size)`; if it is larger, because a surviving statement above the surviving anchor reaches further, or the anchor's own bound is larger, `N` needs `Shrink(id, size)`.
  A `Shrink` at epoch `e` anchors there, so every older extent drops out of the size, and it denies only probes at or past the size, so it changes no byte below it; it states the size the `Grow` would have stated, so at most one of the two is ever needed.

**Such an `N` always exists, and never breaks the rules of one epoch.**
The plain solution states, for every id whose resolution under `S_keep` differs from `M` at all, `Tombstone(id)` if it no longer exists, and otherwise `Shrink(id, size)` with content statements covering all of `[0, size)`: the `Shrink` makes `e` the anchor, so older extents cannot raise the size, and the content statements win every probe below it.
Those are one size statement per id and content statements on disjoint ranges below the size, which is all that [one epoch's rules](../spec/address-table.md#no-conflicts-within-each-epoch) ask.
The inference above only leaves out of that solution what `S_keep` already says, so it obeys the rules too, and no choice of victims can make the vocabulary fall short.

**Inferring the smallest `N` needs, per id, a summary of what the surviving pages state about its size.**
The content half is local: the fragment map already records which statement each byte resolves through, and so which page, and a byte needs a statement in `N` only when that page is retired or the byte changed.
The size half is not: it needs the newest surviving anchor, and the largest extent and `Grow` bound above it, among every statement physically present in the surviving pages, dead ones included, because a reader resolves those too.
[Liveness](../impl/liveness.md#emission-when-a-resize-must-write-a-statement) calls this the predicate no structure reaches, and declines to pay for the one bit that would decide it for shrinks.
Three ways to settle it:

- **State a `Shrink` whenever in doubt**: whenever any of an id's statements went with the victims, or the id shrank, state `Shrink(id, size)`, which fixes the size at `size` whatever else survives.
  A grow alone keeps the exact rule Liveness gives, a `Grow` when nothing the flush states reaches the new size, since everything that survives is bounded by the old size.
  It is always sound, costs about three bytes, and a redundant `Shrink` dies at the id's next shrink, as [the residual](../impl/liveness.md#the-residual) describes.
- **Keep the bit**: per id, whether any statement naming it above its anchor lies in a table page other than the header, which makes the shrink test exact, as Liveness sketches.
- **Keep the summary**: per id, its newest anchor among the live pages, and the largest extent and `Grow` bound above it, maintained as pages are written and retired; open already decodes every statement, so it can build the summary for free.

**Laying `N` out is then a packing problem alone.**
`N` is final when layout starts, so the header can take the hottest statements, as [it does now](../impl/flush.md#the-header-as-write-buffer), and the leaves the rest, in key order or sorted by temperature, every page full but the last, with nothing to revisit.
The room the last page has left is not filled by choosing more victims after the fact; if it should be used, task 1 can plan for it, since the restatements of a table victim, nearly empty or not, cost about what its live statements take.

### A spec change that would make it simpler still: statements keep their epoch

**Let each statement carry its own epoch, so that consolidation can move a statement to a new page without changing what it means.**
Today a statement's epoch is [its page's](../spec/address-table.md#the-idea), so a statement moved to a new page is re-stamped as newest: it now outranks everything it used to lie beneath, and the writer must work out what else to state so that the rest still resolves the same way.
That re-stamping is the whole of what makes retiring a table page hard: the replacement anchor, the `Shrink` in place of a `Grow`, and the size summary task 3 needs for victims all exist because a restated statement changes its rank.

With an epoch per statement, retiring a table page is copying its live statements, unchanged, into the flush's pages.
They resolve exactly as before, since no two statements change their order, and the dead ones are left behind, which by [the droppability rule](../impl/liveness.md#the-droppability-rule) changes nothing either.
Evacuating a data page copies its referrers the same way, each with its new address and its old epoch, since the bytes it points to are the same.
Only the flush's own changes are stated at its epoch, so task 3's inference is needed only for the ids the application touched, against a state the writer knows in full, and the anchors in the header stop moving every flush.

What it would cost and change:

- **Bytes.**
  A page would hold one or more runs, each of one epoch, given as a varint below the page's own, and each sorted by `(id, offset)`, as a page is now; a page the flush writes from its own changes alone is one run, as today, and a page of copied statements has a run per epoch it copies from.
- **The rule of one epoch**, [no conflicts within it](../spec/address-table.md#no-conflicts-within-each-epoch), would speak of statements of one epoch wherever they lie; a writer keeps it by retiring the page it copies from in the same commit, so that the copy and the original never both count.
- **Open's merge** would merge runs instead of pages; it [already relies](../impl/address-table-operations.md#three-facts-about-the-physical-format-that-decide-the-algorithm) on each run having one epoch and being sorted by key, and only the number of runs grows.
- **Validation** would add that no statement's epoch exceeds its page's.
- **What estimates read from a page's epoch**, the age of its content, would come from its statements instead, which is finer than today.

No rule of one epoch needs relaxing for consolidation's sake, with or without this change: the `Grow` and `Shrink` that may not share an epoch were never both needed, as [the example above](#the-conflict-the-check-guards-against) shows.

### What this would remove

- **The fill floor on table offers**, since retiring a table page opens no page; its cost is the bytes task 3 infers.
- **The cut's fillers, and their eligibility check**, since the statements are inferred once the victims are known, and nothing laid out is ever contradicted.
- **The distinction between free filling, budgeted pages, and compaction mode as ways to choose victims**: one ranking chooses every victim, and the room left in pages is a packing matter.
- **The [replacement-anchor obligation](../impl/address-table-operations.md#replacement_anchorid--the-one-correctness-obligation) as a separate rule**, since inferring the size statement covers it: an id whose anchor went with a victim either resolves to the same size without it, and needs no size statement, or to a larger one, and `N` states a `Shrink`; the zeros the anchor owned are bytes whose winner went, which the content rule covers.

### Open questions

- **What the size summary costs**, against stating a `Shrink` whenever in doubt, on files where many ids' anchors sit in leaves; with statements that keep their epoch, the question shrinks to the ids the application touched.
- **Whether an epoch per run pays for itself**: its bytes against the restatements, replacement anchors, and re-stamped header statements it saves, which svg-bench's drawings could measure.
- **How victims are ranked once packing no longer constrains them**, and how much a flush may retire, which bounds both its work and how far the file can fall behind; this is where ripeness, survivor classes, and the churn floor's price of space come back in.
- **Whether the rotating window survives**: it restates a key range for description density, which in this frame is a victim too, a range rather than a page, ranked by what its restatement saves.
- **Why leaves stay 42 to 46 % full even on the large drawings**, where the budget loop does rewrite them; the fill floor and the fillers' check are the likely causes, but this draft has not traced it.
