---
title: Splices without restating the tail
---

**Status: a proposal for [the address table](../spec/address-table.md), comparing seven designs; it recommends linked spans over backings, with their compaction policy as the open question.**
Today a splice restates every statement behind its point, because statements are keyed by their offset in the allocation, and a splice changes the offsets behind it.
A design that states a splice in a constant number of statements must keep content statements at keys that no splice changes, and say elsewhere in which order their ranges follow each other.

Of the designs that do, the recommended one keeps an allocation's content in **backings**, offset spaces that no splice shifts, and orders it by **spans**, statements that show a range of a backing and name the span after it.
A splice states two or three spans, wherever it lands, and moves no content statement.
An allocation that was never spliced has one implicit span and is described exactly as today, at no cost.

**Measured on kladde-svg's drawings and on typing into a long string, spans would state 1 to 6 bytes per edit, where today's flushes restate 0.4 to 24 bytes per edit that lay in leaves, and write 1 to 16 bytes per edit of data again.**
The gain is large where edits keep splicing long, fragmented allocations: 3 to 7 times on the small layout's two larger drawings and the slotted world map, and 7 to 16 times for typing.
On kladde-svg's packed layout, where most splices land in a shape's own short lists, today's flush restates their tails in a statement or two, and spans gain little: nothing on the tiger and the coat of arms, and a third on the world map.

**The price is that spans accumulate.**
Every splice leaves two more, so a list that is spliced again and again is described by ever more spans until something compacts them: without compaction, the drawings end with up to 9,600 live spans, more than twice the 4,302 statements the small world map has in all.
Every design that states a splice in constant size has this price, in one form or another, since the order of the content has to be recorded somewhere; the designs differ in where, and in what compacts it.

## What a splice costs today

**A splice restates every fragment behind its point, since each fragment's key is its offset.**
kladde-rs, at `e88fd47` on its `defrag-shifts` branch, states a shifted piece of the fold as the fragments its bytes lie in, each at its new offset with its data where it was: a statement per fragment, and a data byte moved by none.
Runs of fragments whose statements would cost more than their bytes, at this flush and the shifts its history predicts, it writes again as one chunk instead (the kladde-docs branch `defrag-shifts` documents both in `impl/flush.md`, under "Shifted bytes: restated, or merged").

**What that costs the file depends on where the restated statements lay.**
A statement that lay in [the header](../impl/flush.md#the-header-as-write-buffer) is restated by every flush anyway, so restating it at a new offset costs nothing more.
A statement that lay in a leaf is restated into the header, as hot content, where it displaces other statements into new leaves; that, and the bytes written again, are what a splice adds.

On svg-bench's edits of kladde-svg's three large drawings, with its default focused edits, a journal of four pages and main's consolidation:

| drawing | layout | table pages written | restated from leaves | restated from the header | written again, as data | all bytes written |
| --- | --- | --- | --- | --- | --- | --- |
| tiger | slotted | 19.0 | 1.3 | 3.3 | 1.5 | 128 |
| tiger | packed | 12.7 | 0.6 | 1.3 | 1.3 | 87 |
| tiger | small | 7.0 | 0.4 | 4.1 | 3.6 | 92 |
| coat of arms | slotted | 29.7 | 2.4 | 4.3 | 1.9 | 175 |
| coat of arms | packed | 16.0 | 1.2 | 1.7 | 1.8 | 109 |
| coat of arms | small | 15.4 | 6.0 | 5.1 | 5.2 | 130 |
| world map | slotted | 47.0 | 11.1 | 4.5 | 3.7 | 216 |
| world map | packed | 28.6 | 3.6 | 2.1 | 3.2 | 162 |
| world map | small | 39.8 | 24.3 | 6.2 | 15.5 | 214 |

All figures are bytes per edit; restated bytes are the statements' estimated encoded size, 9 bytes for a `Ref`, 4 for a `Zero`, and 4 plus the payload for an `Inline`.
The layouts are those of the variable-size evaluation on the kladde-docs branch `defrag-shifts`: slotted lists, packed lists of enums, and small values for every list.

**On the small layout's coat of arms and world map, the statements that splices take out of leaves amount to 39 and 61 % of the bytes of table pages written; elsewhere to 5 to 13 %, and 24 % on the slotted world map.**
Those are the runs in which ordinary edits splice long allocations.
The small layout keeps a shape's whole content inline in its parent's child list, so a drag, a recolor or an inserted path node changes the shape's size and splices the parent's list, which on the world map holds up to 39 KB.
In the packed and slotted layouts, those edits splice only the shape's own lists, which are short, and the long child lists are spliced by duplicates, deletes and restacks alone; the slotted world map's are the longest, at 38 bytes per child.

**Typing into a long string costs as much as the journal does.**
A scratch benchmark typed 100,000 keystrokes into one string of 64 KB or 512 KB: 85 % insert a character at the cursor, 10 % delete the one before it, 4.5 % move the cursor, nine times in ten within a window of a tenth of the text, which jumps every 5000 keystrokes, and 0.5 % paste 200 bytes.
Per keystroke, the flushes restated 6.6 and 18.4 bytes of statements from leaves, and wrote 7.5 and 17.9 bytes of table pages in all, where the journal took 13.3.
It is the workload piece tables were invented for, and the one in which a splice's cost grows with the length of what it splices.

## What every design has to answer

**Two questions separate the designs: where the order of an allocation's content is recorded, and what compacts that record when it grows.**
A statement keyed by offset records content and order at once, which is why a splice must restate it.
Every alternative splits the two, and each records the order in its own way:

| design | order is recorded by | a splice states | what accumulates |
| --- | --- | --- | --- |
| A. offsets, restated or merged (today) | each statement's offset | every statement behind its point | fragments, which merging writes again as one |
| B. chunked containers | the container's chunk index | the statements behind its point within one chunk | chunks |
| C. spans placed by offset | each span's offset | every span behind its point | spans |
| D. **linked spans over backings** | each span's successor | two or three spans | spans |
| E. spans keyed by dense positions | each span's key | one or two spans, and a tombstone per span removed | spans, tombstones, and key length |
| F. a tree of spans | each node's list of children | the path from a span to the root | spans |
| G. shift statements | the epochs of the statements and the shifts between them | one shift | shifts |

**What accumulates must be compacted, and compaction costs about what eager restating saved, unless it runs rarely or cheaply.**
That is the trade-off between write amplification and space amplification that log-structured storage makes between updating in place and compacting later: today's design restates eagerly and keeps the description compact, and every other design defers the work to compaction.
The designs that defer win where an allocation is spliced many times between compactions, and a policy that compacts too eagerly gives the gain back.

## The designs

### A. Offsets, restated or merged: today

**Every statement keeps the allocation's offset as its key, a splice restates what lies behind its point, and runs that cost more to describe than to write are written again.**
It needs no change to the format, and the merge rule already keeps short allocations cheap: on kladde-svg's packed layout, splices cost 1.9 to 6.8 bytes per edit, about what the recommended design D states there.
It also keeps the description compact: a merged list is one `Ref` per page, however often it was spliced.

Its cost grows with what lies behind the splice point, in statements or in bytes, so it is worst for long allocations spliced in many places, which are exactly what packed and variable-size values produce as documents grow: 14.8 bytes per edit on the slotted world map, 39.8 on the small one, and 19.4 per keystroke typing into 512 KB.
In memory, every splice re-keys the fragment map's entries behind its point, `O(fragments behind)` tree operations per flush.

### B. Chunked containers: keep allocations short

**A container that may grow long stores its elements in chunks of bounded size, each an allocation of its own, under an index of the chunks; a splice shifts only the tail of one chunk.**
It needs no change to the format, and it bounds the cost of a splice by the chunk's size rather than the container's, in the table and in memory alike.
It is a rope at the level of the data type, which [the specification's design directions](../spec/address-table.md#design-directions) already consider for clones.

**It bounds the cost rather than removing it, and it puts the work into every container instead of into the store once.**
Packed vectors, small vectors, strings and maps would each need chunking, the derive macro would need to know about it, and the [type descriptors](../spec/schema/type-descriptors.md) would have to describe chunked layouts for readers in other languages.
Every chunk is an allocation, with its id, its size statement and its pointer in the index, which is what the small layout set out to remove.
A move of an element between chunks writes its bytes again, and values kept inline inside an element still shift everything behind them within its chunk.

A container could instead leave slack between its elements, so that a splice shifts only to the next gap, as a packed memory array does; that brings back the padding that packing removed.

### C. Spans placed by offset

**Content statements move to backings, offset spaces that no splice shifts, and the allocation is described by spans, each stating at which offset of the allocation a range of a backing begins.**
Spans resolve by offset as content statements do today, newest wins, so the format gains one level and no new kind of rule, and an overwrite never touches a span.

**But a splice restates every span behind its point, and spans accumulate with every splice, so the replay states more than today on every run measured: 5 to 71 bytes per edit on the drawings, and 27 to 31 per keystroke.**
It wins only where an allocation has far more fragments than spans, and only with compaction eager enough to give the gain back.
It is rejected.

### D. Linked spans over backings: the recommendation

**Content statements move to backings as in C, and each span names the span after it instead of stating its offset; a splice restates the span it lands in and states the new ones, two or three statements wherever it lands.**
That is a piece table, kept in the file: the spans are its pieces, and the backings its buffers.
It is the per-allocation indirection that [the specification's design directions](../spec/address-table.md#design-directions) name as the open path for splice-heavy containers, with the order kept as links where they sketch a rope.
An overwrite is one content statement in the backing that holds the bytes, shadowing as today, so the two most frequent edits of a drawing editor, overwrites and splices, both cost a constant number of statements.

**Identities are stable because they are not positions.**
A span is known by where its bytes begin, its backing and its offset there, which no splice anywhere in the allocation changes; the order lives in the successor links alone.
A design that numbered pieces in order would have to renumber them, or draw numbers from a dense order, which is design E.
And a piece that owned its offset space, with no start of its own, could only be split by moving the tail of its content into a new piece, restating it; so a span shows a range of a backing, and splitting it at any point states two spans and moves nothing.

**Existence follows from reachability, so a span that leaves needs no tombstone.**
A span is part of the allocation while the chain from the head reaches it; a span no longer reached is dead with its statements, and nothing can make it reached again except a statement of the epoch that does, by the rule below.
Dropping a dead span's statement, or a dead content statement in a backing no span shows, can therefore never change what the file resolves to.

**A page rewrite restates what is live in the page, each statement at its own key, and nothing else.**
No key changes when the page goes, so no statement elsewhere changes its meaning, and the rewrite has no obligation beyond the one it has today, stating the size again, which here is stating the head again.

**What it costs**: a second level of keys wherever a structure is keyed by offset; a walk of each spliced allocation's chain at open; a writer's choice per splice between restating and linking; and a compaction policy for the spans that accumulate.
[The design in detail](#the-recommended-design-in-detail) spells them out.

### E. Spans keyed by dense positions

**Spans are keyed by positions drawn from a dense order, in which another position always lies between two, as fractional indexing, Logoot and LSEQ draw them; the order of the keys is the order of the content, so spans sort and delta-encode as statements do today, and no chain has to be walked.**
A splice states the new spans at keys between their neighbours'.

**But nothing says that a span has left except a statement at its key**, a tombstone that must outlive every older statement physically present at that key, or an older span would come back.
That is the predicate over physically present statements that [the newest size rule](../spec/address-table.md#conflict-resolution-across-epochs) removed for sizes, now for every span a splice removes.
And keys grow where insertions keep landing in one gap: with midpoints in base 128, the replay's keys averaged 1.3 to 6.3 bytes on the drawings, up to 28, and 10 to 14 bytes when typing, up to 26, where LSEQ's strategies for choosing a key near one end of the gap would shorten them.
Labels of fixed width instead need relabeling, which order-maintenance algorithms bound to `O(log n)` labels per insertion amortized, restating spans again.
On bytes written, it states about what D states, 1.6 to 5.0 bytes per edit, because it saves the successor links and pays for tombstones and keys.

### F. A tree of spans

**The spans hang in a tree whose interior nodes list their children and the lengths below each, as a rope or a counted B-tree does; a splice restates its span and every node on the path to the root, to correct the lengths.**
The order is explicit, no chain is walked, and any offset is found in `O(log n)` without reading the rest.

**That random access is what a rope is for, and kladde does not need it in the file**: a file is read whole at open, and every read after that is served from memory.
What remains is a cost of `O(log n)` restated nodes per splice, each listing up to a node's fanout of children, where D restates `O(1)` spans; it is the path copying of a copy-on-write B-tree, whose cost statements exist to avoid.
The tree belongs in memory, as the index over a spliced allocation's spans, where it answers which span holds a given offset; D keeps it there.

### G. Shift statements

**A statement `Shift(id, at, removed, inserted)` says that the content behind `at` moved; content statements keep the offsets of their own epoch, and a reader replays each id's epochs in order, shifting older content as it goes.**
It writes least of all designs: the replay states 0.8 to 2.5 bytes per edit for the shifts on the drawings, and 0.3 per keystroke.
Shifts compact themselves through consolidation: a shift is needed exactly while some byte behind it, in today's offsets, is won by a statement older than it, since dropping it moves only such statements, and only to offsets at or behind its point; once consolidation has restated the content behind it, the shift is dead.
That test is decidable from live state, with the minimum epoch of the fragments behind each point.

**But the meaning of every statement depends on every newer shift of its id, which makes three things hard.**

- **A shift can never be restated at a newer epoch**, since it would then shift what lies between its old epoch and its new one.
  The header, which restates its content every flush, can therefore not carry a shift, and no page holding a live shift can be rewritten, unless every statement keeps its own epoch, the change [Sparse leaves](consolidation-from-statements.md#a-spec-change-that-would-make-it-simpler-still-statements-keep-their-epoch) considers for consolidation's sake.
  Without it, retiring a page that holds a live shift means restating every older fragment behind the shift: exactly today's cost.
- **Opening becomes a replay**: statements of different epochs use different offsets, so they cannot be merged by key in one sweep as today, and each spliced id is replayed epoch by epoch into a tree of fragments.
- **A shift cannot reorder**: within an epoch, an id's shifts form one monotone map of offsets, so moving content forward or back, as a restack does, writes the moved bytes again, 0.2 to 3.3 bytes per edit in the replay.

A design whose statements' meaning depends on statements elsewhere is what the newest size rule just removed for sizes; this one would bring it back for content.
It is not recommended.

## The recommended design in detail

### Statements

**Two kinds of statement are new; every statement of a never-spliced allocation stays as it is.**

| statement | meaning |
| --- | --- |
| `Backing(id, b)` | in this page, the content statements that follow, up to the next `Backing` or the next id, belong to backing `b` of `id`, `b >= 1` |
| `Span(id, b, start, len, next)` | allocation `id` shows `[start, start + len)` of its backing `b` here, followed by the span that begins at `next = (b′, start′)`, or by nothing |

**Backings** are an allocation's offset spaces: backing 0 is the one every allocation has, and the only one of an allocation that was never spliced.
A content statement belongs to backing 0 unless a `Backing` precedes it, so statements of backing 0 are encoded as today, at no extra byte.
A backing's number means nothing but identity; a writer numbers them per allocation and may reuse a number once no span shows it, by the same argument that lets it [reuse an id](../spec/allocations.md#id-recycling).

**A span is known by its key, `(id, b, start)`**, and its newest statement is its value.
The span keyed `(id, 0, 0)` is the allocation's **head**, and it alone may be empty, for an allocation whose first byte lies in another backing.

### Resolution

- **Existence**: as today.
- **Form and size**: the newest statement of an id that either states a size or is its head decides.
  A size statement makes the allocation **contiguous**: its content is backing 0 from offset 0 to that size, as today.
  A head makes it **spliced**: its content is the concatenation of the spans reached from the head, following each span's `next`, and its size their total length.
- **Content** at an offset of a backing: the newest content statement of that backing that covers it, as today.
- **A spliced allocation is invalid** if its chain names a key with no span statement, visits a span twice, shows a byte of a backing twice, or shows a byte that no content statement covers.

Shown twice is forbidden so that no two places of an allocation alias one byte, which keeps [the byte-referenced-once invariant](../spec/address-table.md#design-directions) meaningful without refcounts.

### Rules for writers

- **No conflicts within an epoch**: at most one statement per id that states a size or is its head, at most one statement per span key, and content statements per backing as today.
- **What becomes part of an allocation in an epoch is stated in that epoch**, which generalizes [the coverage rule](../spec/address-table.md#the-coverage-rule):
  a byte of a backing becomes part of a spliced allocation when a span reached at the end of the epoch shows it and none reached at the end of the epoch before did;
  and a span becomes part of it when it becomes reached.
  Relinking a span that was reached before needs no statement of it; reaching one that was not, or showing a byte that was not shown, does.

Without the second half, a span that left the chain could come back with an old statement: a span keyed `(id, 2, 40)` is cut out at epoch 5 and its newest statement, now dead, is dropped by a page rewrite; an older statement at the same key, from epoch 3, is still physically present; a writer that linked `(id, 2, 40)` again at epoch 9 without stating it would show epoch 3's span.

### Encoding

```
statement     := id_delta (tagged_ref | tagged_zero | tagged_tombstone | tagged_size
                           | tagged_span | tagged_backing | inline)
tagged_span   := 252:byte offset_delta:varint len:varint next_b:varint next_start:varint?
               ; next_b = 0: the last span, without next_start;
               ; else the next span begins at offset next_start of backing next_b - 1
tagged_backing := 253:byte b_delta:varint
inline        := tag:byte offset_delta:varint payload:byte{(tag - 4) >> 1}   ; 6 <= tag <= 251
```

Statements sort by `(id, backing, offset)`, a span at its `start`, before content statements at the same offset; a span moves the offset cursor by nothing, and a `Backing` resets it.
The tags need room: `Inline` payloads would take at most 123 bytes instead of 125, which frees tags 252 to 255, two of them for these statements.
A span takes 5 to 9 bytes, by the replay's estimate about 8.

### Open and memory

**Opening resolves each backing as today's sweep resolves an allocation, and walks each spliced allocation's chain once its statements are merged.**
The sweep already merges statements by key, and the key gains a backing; spans collect per id, and when an id's statements are done, its chain is walked from the head, each span's range taken from its backing's resolved runs, and everything the chain does not show left unpinned.
The checks that make a file invalid fall out of the walk: a missing key, a revisit, a byte shown twice, a shown byte that no run covers.

**In memory, the fragment map is keyed by `(id, backing, offset)`, and a spliced allocation keeps an index of its spans by position.**
The index is the rope of design F: a counted tree, which finds the span holding an offset and takes a splice in `O(log spans)`; for a few hundred spans, a vector with running totals, updated in `O(spans)` per flush, does as well.
No fragment is re-keyed by a splice, so a flush no longer does `O(fragments behind)` work in memory for each.
A reached span holds a pin of a third kind beside [`F` and `S`](../impl/liveness.md#pins), taken when the chain comes to reach it and released when it stops; the head holds `S` besides, as the statement that decides the allocation's form.

### Flushing

**[The fold](../impl/flush.md#phase-a--the-fold) is unchanged; the flush maps its pieces through the span index.**
Committed bytes taken from an old offset become the backing ranges that show them; new content goes into a backing of the flush's own, one per spliced allocation and flush, or onto the end of the backing whose end the allocation's tail shows; overwrites in place go to the backings that hold the bytes.
The flush states the spans whose length or successor changed, and the new ones.

**A writer may restate instead of linking, and should while the tail is short.**
An allocation stays contiguous, its splices restating its tail as today, until restating would cost more than the two or three spans a link costs; the measurements below show where that line runs: short packed lists stay contiguous, long lists and strings become spliced.

### Compacting spans

**A spliced allocation is compacted by writing runs of short spans again as one, and by flattening it: restating its content in backing 0, at the allocation's offsets, and stating its size, which makes it contiguous again.**
Writing short spans again costs their bytes and saves their statements, the trade today's merge rule already makes, but once instead of at every later shift.
Flattening costs a statement per content fragment the allocation has, today's restatement paid once instead of at every splice.

**A rule that flattens an allocation once its spans outnumber, by a margin, the content statements it had after its last flattening keeps its spans about as few as its content statements, and pays for each flattening with the splices before it.**
On the typing runs, it cut the live spans from about 8,000 to 1,786 and 1,460, for 0.8 and 1.1 more bytes per keystroke.
On the drawings it barely fired, because the replay keeps every content fragment an overwrite made, without the description defragmentation that kladde-rs runs; how it behaves there is for an implementation to measure.

### What it changes elsewhere

- **The newest size rule** holds for contiguous allocations as it is; for a spliced one, the head takes the size statement's place, and a size statement newer than the head makes it contiguous again.
- **[Stating the size again](../impl/address-table-operations.md#stating-the-size-again--the-one-correctness-obligation)**, a page rewrite's one obligation, extends to the head.
- **[Description defragmentation](../impl/consolidation.md)** works per backing, and gains flattening as a way to pay down a description.
- **The reverse index**, from a data page to the ranges that point into it, the dirty set, and [the consolidator state](../impl/consolidator-state.md) key ranges by backing as well as by id.
- **A move within an allocation**, as a restack, is a relinking that writes no data byte; [the `Move` operation](move-op.md) between allocations is unaffected.

## Measured

**All designs but C state far less than today where long allocations are spliced, and about as much where they are short; D pays for its constant cost with spans that accumulate.**

### How

**Scratch instrumentation of kladde-rs `e88fd47` recorded every flush's fold, the piece table of every allocation it touched, and what today's flush made of the shifted pieces, without changing what was written; a replay then derived from the same piece tables what each design would state.**
The runs are those of the table above, and the two typing runs.
The replay keeps each allocation as spans over backings, maps each piece table through them as the [flush above](#flushing) does, and counts the statements whose key or value changed, at their encoded size with varints.
For today, the bytes are what the flush restated from leaves and what it wrote again, from the instrumentation.

The replay is a model: it does not run consolidation, so it counts what flushes state, not where statements end up; and it models content fragments only as boundaries of writes, without description defragmentation.

### What the flushes state

Bytes per edit, or per keystroke for the typing runs; "+" separates statements from data written again:

| drawing | layout | today, from leaves + written again | D: spans | D, rewriting short spans | E: dense keys | G: shifts + moves written again | C: spans by offset |
| --- | --- | --- | --- | --- | --- | --- | --- |
| tiger | slotted | 1.3 + 1.5 | 2.1 | 2.1 + 0.0 | 1.6 | 0.8 + 1.0 | 5.1 |
| tiger | packed | 0.6 + 1.3 | 2.1 | 1.7 + 1.1 | 2.1 | 0.8 + 0.2 | 6.9 |
| tiger | small | 0.4 + 3.6 | 2.5 | 2.2 + 0.6 | 4.3 | 1.3 + 0.8 | 10.9 |
| coat of arms | slotted | 2.4 + 1.9 | 3.1 | 3.1 + 0.0 | 2.2 | 1.2 + 1.3 | 8.2 |
| coat of arms | packed | 1.2 + 1.8 | 2.9 | 2.5 + 1.5 | 2.5 | 1.1 + 0.3 | 8.9 |
| coat of arms | small | 6.0 + 5.2 | 3.5 | 3.4 + 0.4 | 4.3 | 1.9 + 2.1 | 18.5 |
| world map | slotted | 11.1 + 3.7 | 4.2 | 4.2 + 0.0 | 2.4 | 1.7 + 1.7 | 17.8 |
| world map | packed | 3.6 + 3.2 | 4.3 | 4.0 + 2.5 | 2.9 | 1.7 + 0.4 | 25.2 |
| world map | small | 24.3 + 15.5 | 5.8 | 5.7 + 0.7 | 5.0 | 2.5 + 3.3 | 71.2 |
| typing, 64 KB | | 6.6 + 1.3 | 1.1 | 1.0 + 0.2 | 2.3 | 0.3 + 0.0 | 26.7 |
| typing, 512 KB | | 18.4 + 1.0 | 1.2 | 1.2 + 0.1 | 1.9 | 0.3 + 0.0 | 30.6 |

"Rewriting short spans" writes a run of spans again as one where its spans and their content statements, counted at 14 bytes each, outweigh its bytes, by today's merge rule over spans.

- **D's cost does not grow with the allocation**: 1.1 to 5.8 bytes per edit on every run, where today's grows from 1.9 to 39.8 with the length and fragmentation of what is spliced.
- **On the packed layout, D gains little**: 2.1 and 2.9 bytes per edit on the tiger and the coat of arms, against today's 1.9 and 3.0, and 4.3 against 6.8 on the world map, since most splices there land in short lists, whose tails today's merge keeps at a statement or two.
  That is why the writer should keep short allocations contiguous.
- **G writes least**, and its moves written again cost as much again on the small layout; what it would cost in compaction the replay cannot say, since that depends on when consolidation restates the content behind each shift.
- **C is worse than today everywhere**, since spans accumulate and it restates all of them behind each splice.

### What accumulates

Live spans at the end of each run, against the statements today's file holds in all:

| drawing | layout | today's statements | D, live spans | in the largest allocation | rewriting short spans | and flattening |
| --- | --- | --- | --- | --- | --- | --- |
| tiger | slotted | 1,752 | 738 | 142 | 738 | 626 |
| tiger | packed | 1,585 | 818 | 224 | 512 | 459 |
| tiger | small | 459 | 841 | 270 | 656 | 569 |
| coat of arms | slotted | 3,857 | 1,855 | 263 | 1,855 | 1,752 |
| coat of arms | packed | 3,529 | 2,058 | 284 | 1,421 | 1,385 |
| coat of arms | small | 1,130 | 2,081 | 630 | 1,739 | 1,664 |
| world map | slotted | 17,966 | 8,953 | 305 | 8,953 | 8,413 |
| world map | packed | 15,944 | 9,381 | 426 | 6,719 | 6,373 |
| world map | small | 4,302 | 9,631 | 787 | 8,394 | 7,833 |
| typing, 64 KB | | 2,501 | 8,030 | 8,030 | 5,785 | 1,786 |
| typing, 512 KB | | 5,608 | 8,477 | 8,477 | 7,830 | 1,460 |

**Kept, spans would add about half again to the slotted and packed files' statements, and nearly triple the small layout's.**
The allocations with the most spans show why: one of 23 KB on the small world map ends with 787 spans, and one of 1.9 KB on the packed world map with 426, a span for every 4.5 bytes.
Rewriting short spans helps where elements are short, in the packed layout, and not where they are 38 bytes, in the slotted one.
Flattening paid off on the typing runs, at 0.8 to 1.1 bytes per keystroke, and fired too rarely on the drawings to judge, for the reason [above](#compacting-spans).

## What the literature does

**Text editors keep sequences as piece tables or ropes, in memory; file systems key extents by offset, and pay for inserting in the middle; collaborative editors give elements identities independent of position.**

- **Piece tables** describe a text as a sequence of pieces, each a range of an unchanging original buffer or an append-only one, so an edit changes the sequence and never the buffers; Bravo and Microsoft Word used them, Charles Crowley's "Data Structures for Text Sequences" (1998) compares them with gap buffers and others, and Visual Studio Code's piece tree (2018) keeps one in a balanced tree.
  Design D is a piece table whose sequence lives in the file as a linked list, and whose index lives in memory.
- **Ropes**, Boehm, Atkinson and Plass's "Ropes: an Alternative to Strings" (1995), are balanced trees over string fragments for fast concatenation and indexing, an in-memory structure; counted B-trees and finger trees serve the same purpose.
- **Extent trees of file systems** key a file's extents by offset, as today's statements do: inserting a range, as Linux's `FALLOC_FL_INSERT_RANGE` on ext4 and XFS does, shifts every extent behind it, the cost kladde pays today.
  Copy-on-write B-trees, as Ohad Rodeh's "B-trees, Shadowing, and Clones" (2008) describes them, copy the path to the root on every change, which is design F's cost.
- **Sequence CRDTs** give every inserted element an identity independent of its position, and order elements either by links to a neighbour, as RGA and Yjs do, or by dense position identifiers, as Logoot and LSEQ do; they keep tombstones for removed elements until every replica has seen the removal.
  A single writer needs none of their machinery for merging concurrent edits, which leaves links, design D, and dense keys, design E.
- **Order maintenance**, from Dietz and Sleator (1987) to Bender et al.'s "Two Simplified Algorithms for Maintaining Order in a List" (2002), keeps fixed-width labels in order by relabeling `O(log n)` of them per insertion, amortized.

## Open questions

- **The compaction policy**: when to rewrite short spans, when to flatten, and how much description a hot list may carry; it is the policy this design adds, and the one a test should probe for failure modes before anything else.
- **Where the line between restating and linking runs**, which the replay places between kladde-svg's packed lists and its long lists, but which a writer has to find per splice, from the tail it would restate.
- **Whether the head and the size statements should be one kind**: both decide an allocation's form and size, and the newest of them wins; a size statement could be read as a head over backing 0 that runs to the size, which would leave one rule where there are two.
- **What the in-memory index costs**, in memory and in time, against today's `O(fragments behind)` re-keying per splice, which this draft does not measure.
- **Whether shift statements' low write cost is worth a second look** once statements keep their epoch for some other reason; without that, they are not an option.
