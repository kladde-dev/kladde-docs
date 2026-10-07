---
title: Sparse leaves, and consolidation from the statements up
---

**Status: a finding, half resolved, and a direction for a redesign; high priority.**
The consolidation policy of [Consolidation](../impl/consolidation.md), as kladde-rs implements it, keeps address-table leaves far emptier than data pages, so that 2 to 15 % of a file's live pages are leaves it would not need if they were as full as its data pages.
Until [the newest size won](../spec/address-table.md#conflict-resolution-across-epochs), it was 4 to 33 %, since leaves that held almost nothing stayed in the file for dozens of flushes; the new size rule ended that, by letting the statements that kept them alive die, and by removing the reason the cut refused them.
The cause of what remains is not a tuning constant but the way the policy interleaves three decisions, which page to retire, which statements to write, and where to put them, so that each constrains the others: the new size rule removed two of those constraints, and the third, a fill floor on retiring leaves, keeps them half empty.
The second half of this draft starts over from [the logical level of the address table](../spec/address-table.md#logical-level) and keeps the three apart.

## The finding

**Leaves stay far emptier than data pages, and most of them sit between a fifth and a half full: the cut's fillers take the sparsest into the room left in its last page, and the budget loop, which holds the rest to a fill floor, takes few of them.**
On kladde-svg's drawings, with svg-bench's edits keeping to one part of the drawing at a time, a journal of 4 pages, and main's consolidation at its default churn floor, at kladde-rs `e88fd47` on its branch `defrag-shifts`, the address-table pages average 44 to 60 % full, against 73 to 82 % for the data pages, and 40 to 67 % of them are less than half full at any time:

| drawing | layout | table pages | pages needed | fill | leaves under half full | under a tenth full | data pages under half full | share of live pages |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tiger | base | 13.4 | 7.2 | 54 % | 51 % | 0 % | 4.3 % | 34 % |
| tiger | packed | 6.3 | 3.8 | 60 % | 40 % | 1 % | 7.1 % | 31 % |
| tiger | small | 0.8 | 0.2 | 21 % | 88 % | 44 % | 5.0 % | 5 % |
| coat of arms | base | 30.5 | 15.9 | 52 % | 53 % | 0 % | 1.9 % | 31 % |
| coat of arms | packed | 17.0 | 9.3 | 55 % | 48 % | 0 % | 3.3 % | 32 % |
| coat of arms | small | 3.4 | 1.6 | 48 % | 66 % | 4 % | 1.8 % | 7 % |
| world map | base | 183.8 | 81.4 | 44 % | 67 % | 0 % | 0.3 % | 34 % |
| world map | packed | 103.0 | 50.6 | 49 % | 58 % | 0 % | 0.4 % | 28 % |
| world map | small | 18.3 | 8.7 | 48 % | 65 % | 0 % | 0.3 % | 6 % |

"Table pages" is their mean count, "pages needed" what their live statements would fill, "fill" the ratio of the two, and the last column the table pages' share of the live pages.
The means are over the rows svg-bench records every 1000 edits, and the shares over histograms of every live page's fill taken at the same flushes, by scratch instrumentation that did not change what was written.
Were the table pages as full as each run's own data pages, the files would carry 2 to 15 % fewer live pages, and 8 to 15 % fewer on the coat of arms and the world map in their base and packed layouts.
The small tiger is the exception to the ranges above: its statements need a fifth of a page, which the header mostly holds, and it has one leaf at a time, or none.

**Nearly empty leaves no longer stay: on every run but the small tiger, at most 4 % of the leaves are under a tenth full, and in the base and packed layouts at most 3 % are under a fifth.**
What stays is the band between a fifth and a half full, which holds 38 to 65 % of the leaves in the base and packed layouts.
The world map in those layouts shows that part alone: its leaves were never nearly empty, and the new size rule raised their fill by 1 and 3 points only.

### Before the newest size won

**At kladde-rs `795b3b9`, the same branch before it moved to the newest size rule, nearly empty leaves piled up: on the tiger and the coat of arms, 5 to 92 % of the leaves were under a tenth full, and leaves as full as the data pages would have saved 4 to 33 % of the live pages.**
The same runs, then and now:

| drawing | layout | table pages, then | fill, then | under a tenth full, then | live pages since |
| --- | --- | --- | --- | --- | --- |
| tiger | base | 15.8 | 48 % | 12 % | −6 % |
| tiger | packed | 13.1 | 32 % | 40 % | −25 % |
| tiger | small | 8.8 | 3 % | 92 % | −32 % |
| coat of arms | base | 37.2 | 46 % | 5 % | −6 % |
| coat of arms | packed | 24.8 | 42 % | 10 % | −12 % |
| coat of arms | small | 9.1 | 19 % | 62 % | −11 % |
| world map | base | 203.6 | 43 % | 0 % | −4 % |
| world map | packed | 122.4 | 46 % | 1 % | −5 % |
| world map | small | 24.0 | 38 % | 16 % | −2 % |

The data pages were as full then as now, so the live pages saved are the leaves'.
The edited tiger that the variable-size evaluation, on the kladde-docs branch `defrag-shifts`, reports closing with 41 table pages for 2,265 statements likely showed the same effect.

**On the small tiger, the leaves piled up for a dozen flushes and then vanished at once.**
Its table pages climbed to 11 to 19 at the peaks of the cycle, holding between 100 and 2,800 bytes of live statements in all; almost no table page was rewritten until compaction mode turned on, and that mode then rewrote 11 to 18 of them within one or two flushes, after which the count fell to one or two and started climbing again.
Now it holds at most two leaves at any flush.

## Why leaves stay empty

Three causes kept leaves nearly empty until the newest size won, and one of them, the fill floor, keeps them half empty still.

### The budget loop holds a table offer to the data pages' fill floor

**[The budget loop](../impl/consolidation.md#the-budget-loop) takes a page rewrite only when the restatements of its victims would fill a whole leaf to `1 − θ`, 95 %, as it takes data victims only when their survivors fill a whole data page.**
kladde-rs' `implementation-notes.md` records it: "a budgeted page rewrite opens no page of its own … the budget counts it as a page all the same, and holds it to the fill floor like a data offer."

For a data page the floor has a reason: evacuating victims into a page the flush opens for them costs that page, and a page that closes half empty is a page wasted.
For a table page it has none: a page rewrite opens no page, since what it restates joins the flush's dirty set and [the cut](../impl/consolidation.md#one-dirty-set-and-why-statements-are-derived-last) lays it out among everything else the flush states.
Its cost is the restatements' bytes, which [the churn floor](../impl/consolidation.md#the-churn-floor-is-a-parameter-not-an-identity) already weighs against the page it frees.

The floor turns the cheapest victims into the hardest to take: an offer is made of whole leaves at most half live, best score first, and needs 95 % of a leaf in restatements.
A file whose leaves hold less than that in all, such as the small tiger, can never pass it, however empty its leaves are.
Where the leaves hold more, an offer passes only where whole leaves happen to add up to 95 % of a leaf: a leaf between two-fifths and a half full restates into 50 to 62 % of one, by the estimate of 1.25 times its coverage, so two of them do not fit together, and one alone fails the floor.

**What the floor refuses waits for the cut's fillers, which take leaves only into the room left in the cut's last page, sparsest first.**
That room was 1.6 to 2.3 KB on average in these runs, enough for one leaf up to 31 to 45 % full, so the sparsest leaves go soon, and those fuller than the room allows wait for an offer that passes, which is the band the finding shows.
The fillers rewrote 52 to 96 % of the leaves rewritten in each run, and the budget loop met a table offer that passed the churn floor but not the fill floor 87 to 1,283 times per run.

**Held to the churn floor alone, leaves come out nearly as full as data pages.**
A diagnostic run, not a proposal, judged table offers by the churn floor alone, on the same drawings, layouts, and edits.
On every run but the small tiger, leaves came out 65 to 70 % full, with 14 to 26 % of them under half full, and fuller leaves would have saved only 0 to 5 % of the live pages, down from 2 to 15 %.
The files carried 1 to 11 % fewer live pages, 11 % on the world map in its base layout, and the flushes wrote 3 to 8 % more bytes per edit, restating leaves that the floor had kept waiting.

### Leaves were kept alive for the size alone

**Before the newest size won, much of what kept nearly empty leaves alive was statements that fixed sizes, and those could not die while anything older about their id was physically present.**
On the tiger and the coat of arms at `795b3b9`, 37 to 56 % of the live statements in leaves under a tenth full were `Shrink`s and `Tombstone`s, and none a `Grow`; on the small tiger it was 95 %, and 97 % of its nearly empty leaves, counted at every flush, held nothing else live.
Under the old size rule, a `Shrink` stayed its id's anchor, pinned, until a newer `Shrink` or `Tombstone` replaced it, since without it the extents of older statements still in leaves would raise the size again; and a re-allocated id's tombstone stayed the anchor of its new incarnation.

Under the new rule, the newest statement that states the size wins, so an older one loses its pin as soon as a newer one states the size, and the cut makes every content statement that ends at its id's size sizing, so that happens whenever the cut states the id's tail.
A re-allocated id's tombstone dies once the cut states the new incarnation's size.
A leaf that held only such statements then falls to zero coverage, and the cut unlinks it without rewriting anything.
That alone, with the fillers' check below still in place, cut the small tiger's table pages from 8.8 to 1.9 on average, and the packed tiger's from 13.1 to 8.5.

### The cut's fillers refused every leaf that named an id the flush touched

**The leaves that the budget loop does not take are meant to go as [fillers](../impl/consolidation.md#the-page-rewrite): whole table victims that fit the room of the cut's last page, which costs nothing.**
That room was ample: in 8000 edits of the small tiger, logged at every flush, it was 800 to 3,500 bytes in nearly every one, and every nearly empty leaf would have fit.
But kladde-rs took a filler only if the flush touched none of the ids the leaf names, and almost none qualified: on the small tiger, of up to 13 leaves under a quarter full, 0 were eligible in every flush, and on the coat of arms in its base layout, 0 to 2 of 2 to 14.

The check read a leaf's ids by decoding every statement still encoded in it, dead ones included.
So a leaf with 4 to 25 bytes of live statements still named 74 to 251 ids, while each flush of the tiger touches 43 to 48 of its roughly 120 ids, and some named id was always among them.

Since kladde-rs `def217a`, the fillers take any leaf the flush has not rewritten already.
Neither that nor the dying size statements suffices alone: with the new size rule but the check, 3 to 17 % of the leaves of the tiger and the coat of arms in their base and packed layouts were still under a tenth full, and 66 and 47 % in their small layouts.

The check guarded against stating an id's size twice in one epoch, which the new size rule lets the cut avoid without it; nor could the dead statements it read ever be part of a conflict.
The next two sections show both.

### What the check guarded against

**A filler's victim can hold the size statement of an id the cut has already laid out statements for, and the cut must then state that id's size again, while [one epoch holds at most one size statement per id](../spec/address-table.md#no-conflicts-within-each-epoch).**
Content restatements cannot conflict, since a filler restates only fragments its victim still owns, which the flush has not taken.
And the size statement need not: the cut decides which of its statements states each id's size only once the fillers are known, since [the sizing bit](../impl/address-table-operations.md#what-the-cut-states-for-a-touched-id) changes no statement's encoded length, and adds a `Size` to the last page only for an id none of whose content statements ends at its size.
So the cut can take any leaf as a filler, and kladde-rs dropped the check in `def217a`, once it had moved to these rules.
Under the old size rule, the check had a reason: the statement an id needed depended on which pages survived, a `Grow` if its anchor stayed and a `Shrink` to replace the anchor if a filler retired it, and one epoch could not hold both, so a filler chosen after the layout could change a statement laid out already.

### Dead statements cannot block a rewrite

**Dropping a dead statement never changes what the file resolves to; it only releases pins that newer statements hold on its account.**
A statement is dead when no fragment resolves through it and it is not its id's size statement, [`pins == 0`](../impl/liveness.md#the-droppability-rule), and the droppability rule is exactly that dropping such a statement leaves the resolution unchanged.
What a dead statement can still do is keep a newer statement alive, as an example shows:

- At epoch 4, allocation 8 holds 40 bytes, stated by `Ref*(8, 0, 40, c)` in leaf `L3`.
- At epoch 6, the application freed it: `Tombstone(8)`, in leaf `L4`.

`Ref*@4` is now dead, but physically present, and so it holds the tombstone: `mentions(8)` counts both statements, and [a tombstone keeps its pin](../impl/liveness.md#the-last-tombstone) until `mentions` falls to 1, since without it `Ref*@4` would be 8's newest statement, and 8 would exist again with its old 40 bytes.
Dropping `L3` drops `Ref*@4`, `mentions(8)` falls to 1, and the tombstone may retire: nothing resurfaces, which is the order the pins enforce, the denied statement first and its denier after.

**Where the flush touched the id, a dead statement can at most make one of the flush's own statements unnecessary.**
If the flush frees an id and states its `Tombstone`, and a filler drops the last older statement naming the id, the new tombstone has nothing left to deny: a redundant statement of a few bytes, which the next rewrite of its page drops, never a wrong one.
If the flush allocates 8 anew, the new incarnation states its size and every byte below it, and dropping `Ref*@4` changes nothing the cut has decided.

So the dead statements a leaf still encodes are no reason to keep it.
The check read them because it learned a leaf's ids by decoding the page, which lists dead statements beside live ones, rather than by asking which of the leaf's statements are live.

### Compaction mode collected them, in bursts

**[Compaction mode](../impl/consolidation.md#compaction-mode) offers the highest live page ahead of every victim, whatever its fill, and an offer holding it passes both floors.**
Once free pages below the tail pass a quarter of the file, the mode works down the file's tail, page by page; the nearly empty leaves that the other paths left behind went with it, and once the mode switched off, they piled up again.
That was the sawtooth on the small tiger, and why its file held 8.8 table pages on average for 0.2 pages' worth of statements; with no nearly empty leaves left for the mode to collect, it holds 0.8.

### Data pages are not hit the same way

**Data pages almost never fall below half full: 0.3 to 7.1 % of them, against 40 to 88 % of the leaves.**
They escape the fill floor: a data offer that whole victims leave short takes one more victim and [cuts its survivors at the page boundary](../impl/consolidation.md#the-budget-loop), carrying the rest into the next page, where a table offer can take whole leaves only.
And [free filling](../impl/consolidation.md#victims-are-pulled-one-at-a-time) takes any whole data victim that fits the room of any data page the flush writes, where the fillers have the room of one leaf.
Their histogram's one feature, 14 to 25 % of them between 50 and 60 % full, is the default churn floor's own threshold, which takes victims at most half live.

But the same interleaving shapes them: a data victim is taken only if its survivors fit the room of a page being written, or fill a budgeted page to 95 % together with other victims, so which pages are cleaned depends on how their survivors pack, not only on what cleaning them is worth.
Nothing measured here shows that doing harm on data pages; the redesign below removes it on both sides alike.

## The underlying error

**The policy makes three decisions in one interleaved pass, which page to retire, which statements to write, and where to put them, and lets each constrain the others.**

- Whether a leaf is retired depends on whether its restatements fill a page, which is a question about where they go.
  This is the fill floor, and it remains.
- Which leaves the cut could retire depended on the statements it had already laid out.
  The fillers' check, which enforced it, is gone.
- The statements for an id were decided before the set of retired pages was final, so a later victim could contradict them.
  Since the newest size wins, the cut settles which of its statements states each id's size once the fillers are known, and content restatements never could conflict.

Each of these was locally sensible, and the later ones were added to keep the interleaving correct, but together they made the cheapest victims unreachable.
The new size rule removed the two that guarded correctness, and the one left is a matter of policy alone: it no longer keeps the cheapest victims out, but it still makes the next cheapest wait.
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
  If `M` says it exists, `N` needs at least one statement for it whenever `S_keep`'s newest statement naming it is a tombstone, or none names it; the size rule below provides one.
- **Content.**
  For every byte below the id's size in `M`, the winner among `S_keep`'s statements, the newest that matches, either already gives `M`'s content, or `N` must cover the byte with a `Ref`, `Inline`, or `Zero`.
  A byte whose winner sat in a retired page has no winner left among those that won it before, so it is covered again unless an older surviving statement happens to give the same content.
- **Size.**
  The size is what the id's newest size statement states, so `N` needs one exactly when `M`'s size differs from what `S_keep`'s newest size statement states — because the id was resized, or that statement went with a victim, or the id is new.
  It is the content statement in `N` that ends at the size, made sizing, or `Size(id, size)` if there is none.

**Such an `N` always exists, and never breaks the rules of one epoch.**
The plain solution states, for every id whose resolution under `S_keep` differs from `M` at all, `Tombstone(id)` if it no longer exists, and otherwise content statements covering all of `[0, size)`, the last one sizing: it states the size, and the content statements win every probe below it.
Those are one size statement per id and content statements on disjoint ranges below the size, which is all that [one epoch's rules](../spec/address-table.md#no-conflicts-within-each-epoch) ask.
The inference above only leaves out of that solution what `S_keep` already says, so it obeys the rules too, and no choice of victims can make the vocabulary fall short.

**Inferring the smallest `N` needs nothing beyond the fragment map and each id's size statement.**
The content half is local: the fragment map already records which statement each byte resolves through, and so which page, and a byte needs a statement in `N` only when that page is retired or the byte changed.
The size half is local too: each id has one size statement, and it needs stating again only when the size changed or its page is retired.

**Laying `N` out is then a packing problem alone.**
`N` is final when layout starts, so the header can take the hottest statements, as [it does now](../impl/flush.md#the-header-as-write-buffer), and the leaves the rest, in key order or sorted by temperature, every page full but the last, with nothing to revisit.
The room the last page has left is not filled by choosing more victims after the fact; if it should be used, task 1 can plan for it, since the restatements of a table victim, nearly empty or not, cost about what its live statements take.

### A spec change that would make it simpler still: statements keep their epoch

**Let each statement carry its own epoch, so that consolidation can move a statement to a new page without changing what it means.**
Today a statement's epoch is [its page's](../spec/address-table.md#the-idea), so a statement moved to a new page is re-stamped as newest: it now outranks everything it used to lie beneath, and the writer must work out what else to state so that the rest still resolves the same way.
That re-stamping is why retiring a table page means deriving its restatements rather than copying them: a restated statement changes its rank, so the cut must state content as resolved truth, merged with whatever else the flush states, and state the size and the tombstone again where the victim held them.

With an epoch per statement, retiring a table page is copying its live statements, unchanged, into the flush's pages.
They resolve exactly as before, since no two statements change their order, and the dead ones are left behind, which by [the droppability rule](../impl/liveness.md#the-droppability-rule) changes nothing either.
Evacuating a data page copies its referrers the same way, each with its new address and its old epoch, since the bytes it points to are the same.
Only the flush's own changes are stated at its epoch, so task 3's inference is needed only for the ids the application touched, against a state the writer knows in full, and the size statements in the header stop moving every flush.

What it would cost and change:

- **Bytes.**
  A page would hold one or more runs, each of one epoch, given as a varint below the page's own, and each sorted by `(id, offset)`, as a page is now; a page the flush writes from its own changes alone is one run, as today, and a page of copied statements has a run per epoch it copies from.
- **The rule of one epoch**, [no conflicts within it](../spec/address-table.md#no-conflicts-within-each-epoch), would speak of statements of one epoch wherever they lie; a writer keeps it by retiring the page it copies from in the same commit, so that the copy and the original never both count.
- **Open's merge** would merge runs instead of pages; it [already relies](../impl/address-table-operations.md#three-facts-about-the-physical-format-that-decide-the-algorithm) on each run having one epoch and being sorted by key, and only the number of runs grows.
- **Validation** would add that no statement's epoch exceeds its page's.
- **What estimates read from a page's epoch**, the age of its content, would come from its statements instead, which is finer than today.

No rule of one epoch needs relaxing for consolidation's sake, with or without this change.

### What this would remove

- **The fill floor on table offers**, since retiring a table page opens no page; its cost is the bytes task 3 infers.
- **The cut's fillers**, since the statements are inferred once the victims are known, and nothing laid out is ever revisited.
- **The distinction between free filling, budgeted pages, and compaction mode as ways to choose victims**: one ranking chooses every victim, and the room left in pages is a packing matter.

### Open questions

- **Whether an epoch per run pays for itself**: its bytes against the restatements and re-stamped header statements it saves, which svg-bench's drawings could measure.
- **How victims are ranked once packing no longer constrains them**, and how much a flush may retire, which bounds both its work and how far the file can fall behind; this is where ripeness, survivor classes, and the churn floor's price of space come back in.
  The diagnostic run without the fill floor on table offers traded 3 to 8 % more bytes per edit for 1 to 11 % fewer live pages.
- **Whether the rotating window survives**: it restates a key range for description density, which in this frame is a victim too, a range rather than a page, ranked by what its restatement saves.
