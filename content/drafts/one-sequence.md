---
title: The file as one sequence
---

**Status: a proposal for a redesign of every layer above the pages, not adopted.**
A kladde file would hold no allocations.
Its content would be one byte sequence: the root value's encoding, in a compact binary format that the schema defines, with every value inline in its parent.
On file, the sequence is a **chain of spans**, each described by an entry that shows a run of its bytes and names the span that follows.
In memory, the store keeps a rope, a balanced tree over the spans that finds the one holding a given position, and each container keeps where its children start.
A data type sees only its own **region** of the sequence: it overwrites bytes in it and splices it, and it neither knows where the region lies nor can reach anything outside it.
Pages, epochs, the two header slots, the flush protocol and copy-on-write stay as they are, and so does the application's API, except that the choice between packed and slotted containers goes.

**The design is meant to make two quantities independent of the file's size: the bytes of live pages per byte of the sequence, and the work per modification that keeps them so.**
A splice, an overwrite and a move each state a constant number of spans, wherever they land and however long the content around them is.
A page rewrite restates what is live in its page and nothing else.
And the description costs a few bytes per span, where today it costs a pointer and statements per allocation.
So live pages exceed the sequence by the description's share, the bytes per span entry over the bytes per span, plus the garbage a cleaner leaves.
A policy holds both at targets, at a cost per modification that is constant in bytes written and logarithmic in time.
With spans of 128 bytes where the sequence is hot and of a page where it is cold, and pages kept 90 % live, a large file's live pages come to about 1.12 times its sequence, and the file adds a few flushes' worth of pages to them ([the arithmetic](#a-worked-example)).

**What goes**: allocations, their ids, pointers, ownership, freeing, size statements, tombstones, per-byte shadowing, the coverage rule, the fragment map and the statement slab, small values, and the distinction between packed and slotted places.
**What it costs**: containers keep positions in memory; offsets after a value whose size depends on its value are computed at run time; an overwrite inside a long span splits it; spans need a compaction policy; moving a value between containers without encoding it again holds a transaction open from the cut to the paste; writing in a foreign schema interprets that schema's descriptors at every write; and partial loading needs an idea of its own.

## The design in brief

### Four layers, three of them new

| layer | today | proposed |
| --- | --- | --- |
| what an application sees | `Persistable` values, guards, transactions | unchanged, except that containers no longer come in slotted and packed variants |
| what a data type sees | an inline slot at a `Location`, allocations it owns, pointers, `store`, `load` and `free` | a region of the sequence: `encode`, `decode`, and writes and splices inside the region |
| the journal | records on allocations: `Free`, `Resize`, `Write`, `Splice`, `Copy`, `Move` | records on the sequence: `Write`, `Splice`, `Move`, `Paste` |
| the address table, which becomes the span table | statements keyed by `(id, offset)`, the newest winning per byte | entries keyed by span id, the newest winning per span, ordered by links |
| the store, in memory | fragment map, statement slab, allocation map, page table | span table, rope, page table |
| pages and durability | | unchanged |

The schema changes too: every type has one encoding, without padding and with integers as varints, which [variable-size values](variable-size.md) proposes as the packed one; containers are described structurally; and a wrapper, `Fixed(T)`, gives a type a fixed-size encoding where an author wants one.

### An example

```rust
#[derive(Persistable)]
struct Drawing {
    title: PersistableString,
    shapes: PersistableVec<Shape>,
}

#[derive(Persistable)]
enum Shape {
    Circle { r: f32 },
    Path { points: PersistableVec<(f32, f32)> },
}
```

A drawing titled "Map" with a circle of radius 1 and a path from (0, 0) to (1, 1) is this sequence of 28 bytes:

```
4D 61 70 FF                  title: "Map", and the end of the text
02                           shapes: 2 elements
00 00 00 80 3F               Circle (variant 0), r = 1.0
01 02                        Path (variant 1), 2 points
00 00 00 00 00 00 00 00        (0.0, 0.0)
00 00 80 3F 00 00 80 3F        (1.0, 1.0)
```

Nothing in it is a pointer, a size of an allocation, or a byte of padding; a container states how many elements it holds, text ends in a byte that UTF-8 never contains, and every value is decoded front to back from the schema.

**Where each value lies is known only in memory, and only relative to its container.**
The points are fixed-size, eight bytes each, so the path finds point `i` at `1 + 8i` within its own region and keeps nothing.
The shapes are not, so the vector keeps where each of its elements starts, here at 1 and 6 within its region.
The drawing keeps nothing either: its title starts at 0, and its shapes start where the title ends, which the title's in-memory value says.

**Renaming the drawing is one record, and two span entries.**
Setting the title to "World map" splices 9 bytes over the first 3, `Splice(0, 3, "World map")`, and every byte behind it moves by 6 without being mentioned.
On file, the sequence was one span, #1, showing 28 bytes at some address `a` in a data page.
Afterwards it is two: a new span #2 holds the 9 new bytes inline and names #1 as its successor, and #1 is stated again to show 25 bytes at `a + 3`; the header names #2 as the first span.
A span is known by its id, not by its position, so had the splice landed in the middle of a sequence of many spans, every span behind it would have kept its entry although its bytes moved.
The same splice costs the same two entries wherever it lands, in a sequence of 28 bytes or of 28 million.

## Fundamental decisions

### 1. One sequence, holding the root value's encoding

**The file's content is the encoding of the root value, with every value inline in its parent; there is no allocation and no pointer.**

- **The description stops depending on the data model.**
  Today every allocation costs a pointer in its parent, statements naming its id, and a size, so the description grows with the number of values, and the [variable-size draft](variable-size.md#the-problem) found allocations of short strings and short lists to be most of them.
  With one sequence, the description depends only on how the store cuts the sequence into spans, which the store chooses and the data model never sees.
- **The unit of description and the unit of the data model stop pulling against each other.**
  Today a cheap description wants few allocations, so values move inline into long ones, as kladde-svg's small layout does; and a cheap splice wants short allocations, since a statement is keyed by its offset in its allocation, so a splice restates every statement behind it, or writes those bytes again.
  Separating the two removes the trade-off rather than choosing a point on it.
- **Everything that exists because of allocations goes with them.**
  Ownership, the free walk, publishing a pointer before freeing what it replaced, tombstones, id recycling and leaks all exist because content lives in allocations that pointers reach.
  Removing a value is now one splice, whatever it held, and nothing can leak, since nothing can be unreachable.
- **The file is its own serialization.**
  A tool decodes the whole content from the schema, with no pointer graph to follow and no opaque container to stop at.

It gives up reaching a value on file without decoding what lies before it, which kladde never does, since it [reads in bulk](../impl/index.md#the-shape-of-an-implementation).
And positions now change whenever something before them changes size, so something has to track them, which is decision 4.

### 2. Containers state counts, text ends in a terminator, and nothing states its own size

**A container's encoding begins with its number of elements, text ends in the byte `0xFF`, and no value states its encoded size.**
A value that changes size then changes no byte outside itself; even its container's count changes only when an element comes or goes, and text has no count to change.
Were every container to state its size in bytes instead, a change of size anywhere would change the stated size of every container around it, up to the root.

Text can end in a terminator because UTF-8 never contains `0xFF`, so no byte of text needs escaping.
An edit of text is then one splice, where a count would add a second record, and a second place where the chain is cut, to every keystroke.
Bytes, and sequences in general, keep their count, since every byte value may occur in them.
The price is that skipping a value means decoding it, which the load does anyway, and a reader of a foreign schema does too, by [its parse plan](variable-size.md#evolution).
Opaque types are the exception: nothing can decode them, so a variable-size opaque value is prefixed with its size in bytes.

### 3. Fixed-size types stay distinct

**A type whose every value encodes to the same number of bytes is fixed-size, the schema says which types are, and positions inside and among fixed-size values are computed rather than kept.**
Floats, bytes, `bool`s, fieldless enums whose discriminants are all below 128, and structs, tuples and arrays of fixed-size types are fixed-size.
Integers wider than a byte are varints and are not, nor are text, sequences, and enums whose variants differ in size.

**`Fixed(T)` is a type of its own, with the values of `T` and an encoding of fixed size.**
It lays out everything inside `T` at its largest: every integer at its full width, every `char` in four bytes, and every enum with its discriminant at a fixed width and its variant padded with zeros to the largest.
It exists for every `T` whose values have a largest encoding, which is every `T` without text, a sequence, a variable-size opaque type, or a recursion back to itself inside it.
So `Fixed` of a struct with a `u32` field takes four bytes for that field, and `Fixed(Option<u32>)` takes five bytes for every value, while `Option<Fixed<u32>>` is variable-size, one byte for `None` and five for `Some`, since `Fixed` applies to what it wraps and not to what wraps it.

The distinction buys three things, all in memory:

- a struct's fields up to its first variable-size field sit at offsets the compiler folds, as every field does today;
- a container of fixed-size elements finds element `i` at `i` times the element's size, and keeps no index of where its elements start;
- a fixed-size value never changes size, so writing it is always an overwrite in place, and nothing above it has to hear about it.

The choice is the application author's, per field and per element type.
Varints and unpadded enums save bytes on file; `Fixed` saves memory and time in containers that hold many values, and keeps a field that changes often, such as a counter, from changing size whenever it crosses a power of 128.
Unlike the variable-size draft's places, nothing is inherited: a type is fixed-size or it is not, and `Fixed(T)` applies to all of `T`.

### 4. A value is a region, and its container knows where it is

**A data type's guard holds a region of the sequence: where the value starts, resolved when the guard is made, and how long it is, with writes and splices at offsets relative to that start, checked against its length.**
A container hands each child a subregion and keeps, in memory, where each child starts relative to itself: by multiplication for fixed-size children, by an index otherwise.
A child that changes size reports the change to its container, which updates its index and reports its own change to its container in turn, up to the root.
The guard chain, made from the root down for each mutation as it is today, adds the relative offsets up into the start of the value it reaches.

So no value stores where it lies, and neither does the store: the store knows bytes, the container knows its children, and the chain of guards composes the two for as long as it lives.
The alternative, a global index of anchors in the store, one for every value whose position anyone may ask for, would cost a node of an order-statistics tree per value, where a container's index costs four bytes per variable-size element and nothing per fixed-size one.
It would also teach the store about values, which nothing else requires it to know.
kladde-rs's `variable-size` branch already builds the mechanism for packed values, as `Place`, `Link` and `Node` in kladde-persist, with an index of element starts in the packed vector.

**A guard is a cursor: it holds where its value starts, so the guard made from it looks its child up in the value's own index, starting there, and nothing above it is searched again.**
An application reaches a value through its ancestors' guards, one level at a time, here as today, so the walk from the root is one it makes anyway; what each step adds is one lookup in one container's index, and a change of size adds one update per level on the way back.
For fields and for fixed-size elements that is a constant; for variable-size elements it is the index's cost, `O(log m)` in a Fenwick tree over `m` elements, and an index that weighs elements by their encoded size makes a whole chain cost `O(depth + log n)` ([what a guard costs](#what-a-guard-costs)).

### 5. The order of the sequence lives in links between spans

**On file, the sequence is a chain of spans: a span shows a run of the sequence's bytes — bytes in a data page, bytes in its entry itself, or zeros — and names the span after it; an entry in the span table describes each span, and the header names the first.**
This is a *piece table*, the structure in which text editors have kept documents under editing since Xerox's Bravo, and in which Visual Studio Code keeps them today: a document is a list of pieces, each a run of some buffer, and an edit changes the list but never a buffer.
Here the buffers are data pages and entries, the pieces are spans, and the list is kept in the file, as the spans' successor links.
How this and the other layers compare with the systems they resemble is in [Related systems](#related-systems).

**A span is known by an id, not by a position, so an edit restates only the spans at the places where it cuts the chain.**
Today a statement is keyed by its offset in its allocation, so a splice changes the key of every statement behind it, and each of those must be stated again.
A span's id stays the same when bytes before it come or go, and the order of the sequence is recorded nowhere but in the links, so an edit changes what the spans at its cuts show and where they link, and nothing behind them.

A move cuts the chain at three places — the start and the end of the moved run, and its destination — and splits at most one span at each.
Each split states its left part again, with a shorter length, and its right part as a new span over the same bytes.
The three left parts are also exactly the spans whose successors change: the one before the run now links to what followed the run, the run's last span to what follows the destination, and the span before the destination to the run's first span.
Where a place is the sequence's start, the header's first span changes instead, which costs no entry.
Every span inside the run keeps its entry, since neither its bytes nor its successor change, and the reach rule of [decision 6](#6-one-entry-per-span-the-newest-wins-reachability-decides-the-rest) asks nothing of it, since the chain reaches it before and after.
Where a place already falls between two spans, its split goes, and a move between such places states three spans.
So a move, which today's design can state without writing bytes only between allocations, with [`Move`](move-op.md), states three to six spans here, however many it moves.
A splice is the same argument with one place fewer: one to three entries, counting the span of its new bytes.

**The links are kept on file and a tree over them in memory, because the file is read whole.**
A tree of spans on file would let a reader find a position without reading what lies before it, which kladde never does, and keeping the lengths in its interior nodes current would restate a path to the root at every edit.
In memory, the rope finds the span that holds a position in `O(log k)` for `k` spans, which is what the flush needs to map the journal's positions to spans.
[Splices without restating the tail](splices.md) weighs links against other ways to record the order of content.

### 6. One entry per span; the newest wins; reachability decides the rest

**A span's newest entry is its whole description: no entry shows part of a span, and a span is part of the sequence exactly while the chain reaches it.**
Liveness is then one bit per entry: an entry is live if it is the newest of a span the chain reaches.
There are no fragments, no pins that count them, no size statements, no tombstones and no count of mentions, and dropping any other entry can never change what the file holds.
A page rewrite writes the entry of each live span in it again, under the span's own id, and no entry elsewhere changes its meaning.
Writing a span's entry is called *stating* the span, and writing it again *restating* it.

A writer needs one rule, the analogue of today's [coverage rule](../spec/address-table.md#the-coverage-rule): **a span that the chain reaches at the end of an epoch but did not reach at the end of the epoch before is stated in that epoch.**
Entries of older epochs stay in their pages until those pages are rewritten, so without it, a writer could link in a span id whose newest entry a page rewrite had dropped as dead, and an older entry of the same id, still in a page nobody rewrote, would show its stale bytes.
With it, span ids can be recycled at once.

The price is that an overwrite cannot shadow part of a long span: it splits the span, or writes it again whole, which [choice A](#a-overwrites-split-rewrite-or-shadow) weighs.

### 7. The schema and the implementation's state are chains of their own

**The header names three chains: the sequence, the schema, and the implementation's private state, which is today's [consolidator state](../spec/file-format.md#the-consolidator-state).**
The journal edits only the sequence.
The schema changes only when a flush writes it — when a file is created or upgraded, before the application gets the file, or, with [capsules](#mixing-schemas-capsules), when a session adds a version of a type — and the private state is written only by flushes, so no journal record needs to say which chain it edits.
The sequence's positions then start at the root value's first byte, and a file whose fingerprint matches never decodes its schema.

The schema could instead be a prefix of the sequence, which would work as well; it would offset every position by the schema's length, and a reader would decode the schema to find where the root begins.
Chains cost nothing extra in the span format, so the schema takes one, and so would [segments](#segments-later).

## Choices without a clear winner

### A. Overwrites: split, rewrite, or shadow

**Three ways to overwrite bytes inside a span, of which the format should offer the first two and leave the third out until measurements ask for it.**

- **Split**: cut the span in three around the written bytes.
  It costs three entries the first time a place is written, and one for every later overwrite of the same bytes, which restates the middle span.
  It leaves the description fragmented until [compaction](#b-how-short-spans-may-stay) merges the pieces.
- **Rewrite**: write the whole span again with the new bytes.
  It costs one entry and the span's length in data bytes, and fragments nothing; it suits short spans, and is costly for long ones.
- **Shadow**: a second kind of entry that overwrites part of a span by offset, as today's content statements overwrite part of an allocation.
  It costs one entry and writes no other byte, and it brings back per-byte resolution, fragments inside spans, and liveness that counts them: most of what decision 6 removes.

**Decision:** a writer should rewrite a `Ref` span shorter than a threshold and split a longer one, so that a drag that moves a point many times splits its span once and restates an inline span of eight bytes at every later flush.
An `Inline` span it always rewrites, since splitting one restates its whole payload as well, in two or three entries instead of one.

Shadowing is the option to revisit if overwrites inside long, cold spans turn out to dominate a workload; today's design pays one statement for them, and splitting pays three entries, once.
Instead of full shadowing, a first step towards it could instead introduce entries for shrinking and/or relinking existing `Inline` spans, i.e., entries that only state an existing `Inline` span's size and/or next-pointer.
These entries should only be allowed to be issued for spans that are defined in an earlier epoch by an `Inline` entry, as their purpose would be to avoid having to restate the content of the `Inline` entry, which could be up to 126 bytes long.
They must also be restricted to spans that the chain reached at the end of the epoch before: a span that becomes reached needs a full entry, by [the reach rule](#6-one-entry-per-span-the-newest-wins-reachability-decides-the-rest), since an older `Inline` entry of its id, still in some page, could otherwise supply stale bytes.
The cost of these shrinking and/or relinking entries would be that they keep the existing `Inline` entry alive, which now contains a dead payload (in the case of a shrinking entry).
The rule would still be "newest wins", only that for `Inline` entries it would then apply separately for its backing content, size, and next-pointer.
So a span could have two live entries, and decision 6's "one entry per span" would become "at most two".
A page rewrite that writes the `Inline` entry again must state the span's current size and successor with it, since the restated entry is the newest and would otherwise win them back; that makes the shrinking or relinking entry dead, and drops the dead part of the payload.
A shrinking entry keeps a prefix of the payload, so it saves the left part of a split, and a cut at the end; the right part of a split still needs a span of its own, and its bytes stated again.
Relinking saves a restatement whenever an `Inline` span's successor changes, which every splice at its end causes.
For `Ref` entries, nothing would change since shrinking or relinking them by issuing a full new `Ref` entry is already cheap.
These shrinking and relinking entries should not be adopted until motivated by measurements.

### B. How short spans may stay

**Two policies bound the description, and they combine: one bounds it in the leaves at a constant cost per edit, the other drives it toward nothing where the sequence has gone cold; spans whose entries sit in the header are exempt from both.**

- **Bounded spans**, the discipline of a B-tree's leaves without the interior nodes: no two neighbouring spans whose entries lie in leaves are shorter than `L` together.
  A flush keeps the rule after its fold has been mapped to spans and before it places anything: it writes the spans that break the rule again as one, among its own new bytes, which costs at most `L` bytes per boundary removed and never cascades, since a merged span is longer than either neighbour was.
  The mean span in the leaves is then at least `L / 2` long, everywhere and at all times.
- **Merging by temperature**: a run of spans whose content has not been written for a while is written again as spans of up to a page.
  Each boundary is removed once, after the edits that made it, and a cold span of a page costs about six bytes, 0.15 % of what it shows.
  It needs a temperature per region of the sequence, as today's [heat and `last_written`](../impl/in-memory-state.md#3-the-allocation-map) give one per allocation.
- **The header is exempt**: spans of any length may keep their entries there.
  An edit site that every flush touches — typing, dragging a shape, moving a slider until a setting sounds right — splits its spans while it is hot, and merging them would write the same bytes again at every flush, only for the next edit to split them anew.
  The header can afford them, since it holds the hottest entries, is written by every flush anyway, and holds at most a page of entries, so the exemption adds at most a page to the description, however large the file.
  The bound takes effect where entries leave the header: when the header would overflow, the flush chooses its coldest runs of spans to evict to leaves, by heat as [today](../impl/flush.md#the-header-as-write-buffer), and merges each run's short spans as it evicts them, so their bytes are written again once, after they have gone cold.
  Since merging writes bytes, the flush makes that choice before it places anything, from entry sizes it estimates without encoding them, where today the cut makes it after placement.

The first gives the worst-case bound in [the guiding principle](#the-guiding-principle-overhead-and-work-independent-of-size), the second the low overhead of a large file that is mostly cold, and the exemption keeps the first from charging hot edits for merges that the next edit undoes.
What `L` should be, and what counts as cold, are for measurements: a larger `L` makes the description smaller and each edit more expensive.
Neither touches the format, which allows spans of any length from one byte to a page.

### C. What a container keeps to find its children

**A container of variable-size elements has five ways to keep where they start, and the frequent operation decides between them.**

| index | finding an element | a size change | an insertion | memory per element |
| --- | --- | --- | --- | --- |
| an array of starts | `O(1)` | `O(m)` | `O(m)` | 4 bytes |
| a Fenwick tree of sizes | `O(log m)` | `O(log m)` | `O(m)` | 4 bytes |
| a counted B-tree | `O(log m)` | `O(log m)` | `O(log m)` | about 8 bytes |
| a tree weighted by the elements' sizes | `O(1 + log(W / w))`, amortized | the same | the same | about 16 bytes |
| a start every 64 elements | `O(64)` | `O(m / 64)` | `O(m)` | 1/16 byte |

`m` is the number of elements, `W` the container's encoded size and `w` the element's, and the "memory per element" column assumes that sizes are recorded in 32-bit values, which is fine since larger containers should probably hold their content in `Segment`s once they land; until then, a container that would outgrow them fails cleanly, as [the bounds](../spec/address-table.md#bounds) require of every limit.
The `variable-size` branch's packed vector keeps an array of starts.
A Fenwick tree is the better default for kladde-types' vector, since a size change of an element, typing into a string inside it, say, is the frequent case and costs `O(m)` in the array, while an insertion costs `O(m)` in the `Vec` that holds the elements anyway.
The weighted tree finds an element that holds much of its container near the tree's root, which makes the lookups along a chain of guards add up to `O(depth + log n)` rather than a logarithm per level ([what a guard costs](#what-a-guard-costs)); it pays for deep nesting of large containers.
A struct keeps no index at all: it adds up its fields' encoded sizes after its first variable-size field, and every container caches its own total, so that adding up never recurses.

**Decision:** prefer a Fenwick tree over a weighted tree to save memory unless a specific container type justifies a different choice.
The runtimes discussed here are for calculations that happen typically at most a handful of times per mutation, and at the time the mutations happen (i.e., not during the flush), and are therefore of lower priority than memory savings for typical use cases of kladde.
Kladde is meant for interactive applications where mutations happen due to some user action and therefore on human time scales, so a millisecond delay per mutation is unnoticeable.
For bulk operations (e.g., pasting large and complex content from the clipboard), the API should make it the natural choice for application authors to first construct any large and complex data types outside of a guard and then bring them in as a whole, which should then encode them into a buffer and splice them in.
Even in automated edits (a script or macro that edits thousands of shapes in a vector illustrator app), the overhead of Fenwick tree lockups is expected to be negligible compared to the cost of journal appends (a Fenwick tree's lookup or update over a million elements is about twenty steps, well under a microsecond, so a script that edits thousands of shapes spends a few milliseconds on them in all).

### D. Moving a value from one container to another

**Moving a value within one container is one `Move` record and three to six span entries, whatever its size; between containers, cutting the value out and pasting it in within one transaction costs the same.**
Today a value that owns allocations moves by writing its pointers, and what they point to stays where it is.
In one sequence, every value is its bytes, so restacking a group, reordering a list, or sorting it are `Move`s inside their container, and moving a subtree to another parent is the question:

- **Encode it again**: remove the value, and insert its encoding where it goes.
  The journal and the flush carry all of its bytes; it is simple, and costs `O(size)`.
- **A move at a common ancestor**: a guard method on the container that holds both places.
  Rust's borrows forbid holding guards on two places at once, so the ancestor would have to take both paths, which is awkward to type and to derive.
- **Cut and paste across transactions**: `cut` moves the value's bytes into a scratch chain and returns a detached value; `paste` moves them in where it goes.
  It needs a chain number in `Move` records, and a rule for a detached value that is dropped without being pasted, whose bytes the next transaction or the next open would splice out of the scratch chain.
  Design of a feature like this is deferred until a need for it is proven — it should be relatively easy to implement a persistent clipboard area on the application layer (an `Option` or `Vec` of cut values as a field of the root data type, to and from which the app moves values using the mechanism of cut and paste within a transaction below).
  Since cut and paste moves rather than copies, a clipboard that pastes the same value more than once encodes it again for every paste but the last.
- **Cut and paste within a transaction**: the bytes a splice removes stay available until its transaction ends, and a later record of the same transaction pastes them elsewhere, naming them instead of carrying them.
  Nothing outlives the transaction, so nothing needs a chain, or a rule for values that are cut and never pasted.

**Decision:** cut and paste within a transaction, as below.
Encoding again stays the way to insert a value more than once, or long after it was removed, as a clipboard does.

**Every record that removes bytes, a `Write` or a `Splice` that removes some, makes a *cut* of them, which a later record of the same transaction may paste once, and which is gone when the transaction ends.**

- A fourth record, `Paste(position, removed, cut)`, replaces `removed` bytes at `position` with the bytes of a cut, named by how many cuts of the transaction were made after it: 0 for the latest.
  With `removed` equal to the cut's length it overwrites, and with 0 it inserts, so one record does what a stack of cuts would need two for.
- A cut is pasted at most once, so that no data byte is shown twice; a reader refuses a `Paste` that names a cut already pasted, or one its transaction did not make.
- A cut that nothing pastes is removed bytes, as after any splice: nothing frees it, and nothing can leak.
- A `Write` is a `Splice` that removes as many bytes as it inserts, a byte shorter in the journal, so a writer records a `Write` wherever the lengths agree; making its overwritten bytes a cut too keeps that choice from deciding whether the old value can be pasted elsewhere, as a field's `replace` or a change of variant would.
  The cost is that a `Paste` counts writes among the cuts made after its own, a varint byte more past 127 of them, and that the fold keeps a write's overwritten pieces until the transaction ends, as it keeps a splice's.
- Cuts belong to the journal's transaction, which [nesting and batching](../impl/transactions-and-batches.md#nesting) may make larger than the application's; naming them by distance keeps that invisible to the records.
- `Move(from, length, to)` is then `Splice(from, length, [])` followed by `Paste(to, 0, 0)`, and stays as the shorter way to write it.

Naming a cut by distance, rather than popping it off a stack, lets a transaction paste its cuts in any order, so two values that swap containers need no care about which is pasted first; with a stack, pasting the older cut would discard the newer, and a handle the application still holds would fail at run time.
The price is that the fold and the replay keep every cut of a transaction until the transaction ends, each as the list of the fold's pieces it removed, a few words per piece.

**The fold resolves a cut and its paste as a move**: the cut is the list of pieces that its record removed from the fold's piece table, and the paste inserts that list, so the flush states three to six spans for the two, however large the value, and writes none of its bytes.
Replay at open does the same.

**In kladde-rs**, a container's `cut(i)` splices element `i` out as `remove(i)` does, and returns a `Cut<T, B>`, with `B` the guards' handle on the store, `&Store`: the in-memory value, and the cut's number in the store's open transaction, which the `Cut` holds open, nested in any the application opened, for as long as it lives.

- Methods that take a value — `push`, `insert`, a field's `set` — take a `Cut<T, B>` too, record a `Paste` instead of the value's encoding, and move the value into memory.
  kladde-rs gives them methods of their own, `push_cut` and `insert_cut`, since one method taking either a value or a cut would need a conversion trait whose two impls Rust's coherence rules make fragile.
- A `Cut` dereferences to its value but gives no mutable access, since its bytes must stay the value's encoding until they are pasted.
- It is neither `Copy` nor `Clone`, and typed by its value's type, so it is pasted at most once, and a `Cut<Fixed<Shape>>` cannot go where a `Shape` goes.
- It borrows the store, not the guard it came from, so the application can drop the source's guard and make the destination's while it holds the cut.
- Dropping it unpasted ends the transaction it holds open, which removes the value for good; `commit` does the same and returns a `Result`.
  A drop cannot return the failure of its append, so it poisons the store instead, and the next call reports it, rather than panicking in `drop`.
- `remove` stays, for a value the application wants as a value, outside any transaction.
- Any guard method that splices a value out can return it as a cut: a field's `replace`, an `Option`'s `take`, or a change of an enum's variant, for the fields of the old variant.

kladde-rs implements cuts on the vector only, `PersistableVec`'s `cut`, `insert_cut` and `push_cut`; the map, fields, `Option` and enums neither make nor take them yet.

**What it costs:**

- The transaction stays open from the cut to the paste, so no flush can land in between, and the transaction's records stay in memory: a cut is for a move within one operation, such as dropping a dragged shape into another group, not for a clipboard.
- The store keeps the length of every cut of the open transaction, to check the positions of the records after it.
- Changing a value between its cut and its paste, say to adjust a shape's coordinates to its new group, would need records whose positions lie inside a cut rather than in the sequence; it is left out, since pasting first and changing the value then does the same.

Cap'n Proto has the same idea within one message: an *orphan* is an object disowned by one parent and adopted by another without copying, move-only, and zeroed if nothing adopts it.


## The guiding principle: overhead and work independent of size

**The live pages exceed the sequence by three terms — the description, the garbage, and the page framing — and each is held at a target that does not depend on the sequence's length, at a cost per modification that does not either; the file adds to them a number of pages that depends on what a flush writes, and not on the sequence's length either.**

### Where the bytes of the file go

Let `n` be the length of the sequence (with the schema's and the private state's, which are small), `k` the number of spans the chains reach, `c` the mean encoded size of their entries without inline payloads, and `L̄ = n / k` the mean length of a span.
The live pages hold:

- every byte of the sequence once, in a data page or as an inline payload in a table page, and zeros nowhere: at most `n`;
- `k` entries: `k · c = n · c / L̄`;
- garbage and slack, `G`: bytes of data pages that no reached span shows, dead entries in table pages, and room that a writer left empty;
- the [page framing](../spec/file-format.md#page-framing), 0.37 %.

So live pages come to about `n · (1 + c / L̄ + G / n + 0.004)`.

The file holds more pages than the live ones, as it does today, and nothing in this proposal changes them:

- the **fallback** pages, which the last commit stopped referencing, and which [the reuse rule](../spec/durability.md#the-reuse-rule) keeps until the next flush's `fsync`;
- the **reusable** pages that the next flush will write into, freed by the commit before the last;
- the **journal**'s pages, at most its budget;
- **holes**: further reusable pages below the last live page, which a fall of the live size leaves behind, and which later flushes fill, or [compaction mode](../impl/consolidation.md#compaction-mode) and truncation give back.

### Each term, and what keeping it costs

**The description, `c / L̄`, is set by the span policy.**
[Bounded spans](#b-how-short-spans-may-stay) keep `L̄ ≥ L / 2` in the leaves, so `c / L̄ ≤ 2c / L`: with entries of 8 bytes and `L = 256`, at most 6 %, and the header's exempt spans add at most a page.
Each modification creates a constant number of span boundaries, at most two for a splice or an overwrite and three for a move, and removing one costs at most `L` bytes written again and one entry, so the policy costs `O(L)` bytes per modification, amortized.
Merging cold regions into spans of a page lowers the term for them to about 0.15 %, once per region and cooling.

**The garbage and slack, `G / n`, are set by the cleaner's target.**
Holding pages at a mean live fraction `u` leaves `G ≤ (1 / u − 1) · n`, 11 % at `u = 0.9`.
In steady state, content that the application supersedes turns into garbage, and the cleaner must win back as much room as that content took.
A cleaner that takes the emptiest page first takes one at most `u` full, live fraction `v ≤ u`; writing its survivors into pages that it fills to at least `1 − θ` takes `v / (1 − θ)` of a page, so it wins back `1 − v / (1 − θ)` of one.
Each page of room won back then costs `v (1 − θ) / (1 − θ − v)` pages of survivors moved, plus their spans restated, `c / L̄` per byte, and the cleaner can keep up only while `u < 1 − θ`:

| `u` | `θ = 0` | `θ = 0.05` |
| --- | --- | --- |
| 0.85 | 5.7 | 8.1 |
| 0.9 | 9 | 17 |

That is the classic bound for log-structured cleaning (Rosenblum and Ousterhout, "The Design and Implementation of a Log-Structured File System", SOSP 1991), and uniformly random overwrites are its worst case; skewed workloads clean far more cheaply, which is what today's ripeness and survivor sorting try to exploit, so far with questionable success.
`θ` is a worst case too: [packing](../impl/consolidation.md#slack-is-cheaper-than-a-split--up-to-a-point) holds every page a flush writes but its last to at least `1 − θ` full and usually fills it, so the cost lies near the column for `θ = 0`, with each flush's last page as a boundary effect.

**The pages around the live ones are set by what one flush writes.**
In steady state, a flush frees about as many pages as it writes, so with `p` pages written per flush, the file holds about `p` fallback pages, freed by the last commit, and `p` reusable ones, freed by the commit before and waiting for the next flush: `2p` pages besides the live ones, plus the journal's.
`p` is the flush's new data and table pages, plus the survivors its cleaning moves, which the table above bounds: about ten pages for a flush that supersedes a page of content at `u = 0.9` and `θ = 0`.
So a large file carries some 25 pages besides its live ones, about 100 KB, which is 0.1 % of a sequence of 100 MB and vanishes as `n` grows; holes add to that only after the live size falls, until compaction mode gives them back.
For a file of a few pages, the same pages are most of the file, as they are today, since the reuse rule that sets them is the same.

**Everything else is constant or logarithmic.**
A splice, an overwrite and a move state a constant number of spans; a moved byte restates its span and nothing else, since no key changes; the rope finds and edits spans in `O(log k)`; a guard is made from its parent's with one lookup in the parent's index, and a change of size updates one entry per level, which size-weighted indexes bound by `O(depth + log n)` for a whole chain from the root; victims come from bucket queues in `O(1)`, and their spans from a reverse index in `O(1)` each; and the table pages' directory changes `O(depth)` pages per leaf added or removed, amortized over the entries a leaf holds, as today.

So the live pages' overhead is `c / L̄ + G / n + 0.004`, set by `L` and `u`, the file's adds `2p` pages and the journal's, and the work per modification is `O(w + L)` bytes for `w` bytes written and `O(log n)` time in the store, plus the walk from the root to the modified value, which the application makes anyway and which costs `O(depth + log n)` with size-weighted indexes; none of it grows with the file beyond the logarithm, and the depth only with how deeply the data model nests.

### Why today's design cannot

**Today the description is paid per allocation and a splice per allocation's tail, so keeping either small makes the other grow.**
A small description wants values inline in few allocations, and then each allocation is long, and a splice into one restates or rewrites everything behind it: on kladde-svg's small layout of the world map, flushes were [measured](splices.md#what-a-splice-costs-today) to restate 24 bytes of statements per edit that had lain in leaves and to write 16 bytes of data again, growing with the length of the lists.
Cheap splices want short allocations, and then each value pays its pointer and statements, a cost that grows with the number of values rather than with their bytes.
No policy escapes that, because the format ties the unit of description to the unit of the data model; the proposal unties them.

### A worked example

Arithmetic, not a measurement: a sequence of 100 MB, of which 1 MB is being edited.

| part | spans | entry bytes | share of `n` |
| --- | --- | --- | --- |
| 99 MB cold, in spans of a page | 24,300 | 146 KB, at 6 bytes each | 0.15 % |
| 1 MB hot, in spans of 128 bytes on average | 8,200 | 82 KB, at 10 bytes each | 0.08 % |
| garbage and slack, pages kept 90 % live | | | 11 % |
| page framing | | | 0.4 % |
| **live pages** | | | **about 112 %** |
| fallback and reusable pages, a flush writing 12 pages, and a journal of 4 | | | 0.1 % |
| **the file** | | | **about 112 %** |

At `u = 0.85` the garbage takes 18 %, and the cleaner moves at most 5.7 bytes per byte written instead of 9.
In either case, it is the cleaner's target, and not the description, that decides where between 10 and 20 % the file lands.

## The design in detail

### The encoding of the sequence

**Every type has one encoding, which a reader decodes front to back from the type's descriptor; `Fixed(T)` is a type of its own, whose encoding has a fixed size.**

| kind | encoding | fixed-size? |
| --- | --- | --- |
| `u8`, `i8`, `bool` | one byte | yes |
| `u16`, `u32`, `u64` | unsigned LEB128 | no |
| `i16`, `i32`, `i64` | zigzag, then LEB128 | no |
| `f32`, `f64` | IEEE-754, little-endian | yes |
| `char` | UTF-8 | no |
| Struct | its fields' encodings, concatenated | if all its fields are |
| Enum | the discriminant as LEB128, then the variant's fields | if every discriminant is below 128 and every variant is fixed-size with the same total size; this includes C-style enums if every discriminant is below 128 |
| `Array(T, n)` | `n` elements | if `T` is |
| `Sequence(T)` | the number of elements as LEB128, then the elements | no |
| `Text` | UTF-8, then the byte `0xFF` | no |
| Opaque | its bytes, prefixed with their number as LEB128 unless the type declares a fixed size | if it declares one |
| `Fixed(T)` | `T`'s values at a fixed size: integers at full width, little-endian, a `char` in 4 bytes, an enum's discriminant at the width its descriptor gives and its variant padded with zeros to the largest, a struct as its fields in that form | yes |

`Fixed(T)` is defined for every `T` without a `Sequence`, a `Text`, a variable-size opaque type, or itself inside it, and it applies to all of `T` (see [Decision 3](#3-fixed-size-types-stay-distinct) for examples).
A map is a `Sequence` of entries in the order its library keeps them, an `Option` an enum, and a recursive type nests inline, a tree as sequences of trees.
The canonical form is the variable-size draft's: minimal varints, valid UTF-8, and zeros as padding.

**Ending text in `0xFF` costs little.**
A decoder finds the end of text by scanning for the terminator, which it does while it checks the UTF-8 anyway, and it learns the length only at the end; text below 128 bytes takes the byte a count would have taken, and longer text saves the count's further bytes.
A terminator has to be a byte that the content cannot hold, so only text has one; bytes, and sequences of any other type, keep their count.

**The descriptors change accordingly.**
`Sequence`, `Text` and `Fixed` are new kinds, the reserved `Array` kind takes the encoding above, and an opaque descriptor gains its size mode.
kladde-rs gives the new kinds the tags 133, 134 and 135, leaves the pointer's 132 unused, writes the size mode as one varint, 0 for a length prefix and one more than the size otherwise, and keeps the enum descriptor's discriminant width, which only the fixed form uses.
Its rules of nesting are two: a `Fixed` wrapper holds a type that has a fixed form, and a type reaches itself only through an enum or a sequence, whose discriminant or count can end the recursion; a struct that holds itself through structs and arrays alone has no finite value.
The reserved `Pointer` kind is not needed, nor are the variable-size draft's `Small`, `Packed` and `Slotted`: with nothing to spill into, every short string and short list is inline, as a small value's content is there, and so is every long one.
Containers are described structurally, which settles [the container problem](../spec/tooling.md#the-container-problem) by its first option, and the fixed-size property is derived from the descriptors, so a reader and a tool compute it as a writer does.

### The journal

**Four records, on positions in the sequence, each position written as its distance from where the record before it left off:**

```
record  := head:varint (write | splice | move | paste)
; x := the head read as a zigzag-encoded signed integer
; kind := x & 3, and delta := x >> 2, shifting arithmetically
write   := bytes                          ; kind 0; position := cursor + delta
splice  := removed:varint bytes           ; kind 1; position := cursor + delta
move    := length:varint to_delta:varint  ; kind 2; from := cursor + delta,
                                          ;   to := from + to_delta read as zigzag
paste   := removed:varint cut:varint      ; kind 3; position := cursor + delta
bytes   := length:varint byte{length}
```

- `Write` overwrites `[position, position + length)`.
- `Splice` replaces `removed` bytes at `position` with `bytes`, shifting what follows.
- `Move` cuts `[from, from + length)` and inserts it at `to`, a position in the sequence without the cut bytes.
  To move `bc` between `d` and `e` in `abcde`, giving `adbce`, `from` is 1 and `length` 2, which leaves `ade`, and `to` is 2, between `d` and `e` there: `Move(1, 2, 2)` before delta-encoding.
- `Paste` replaces `removed` bytes at `position` with the bytes of a cut that an earlier `Write` or `Splice` of the same transaction removed, [choice D](#d-moving-a-value-from-one-container-to-another).

**A move of `B` behind `C` in `A B C D` is also a move of `C` in front of `B`, and which one a writer records changes nothing on file.**
The flush states spans from the sequence the fold ends with, which is the same either way, cut at the same three places, so it states the same spans; and the fold, whose pieces sit in a counted tree, cuts and inserts a run of them in `O(log)` steps however long it is.
A container records the move of the element it was asked to move.

**The cursor is where the record before left off**: the end of what a `Write`, `Splice` or `Paste` put in place, and the end of the moved run, at its destination, for a `Move`.
It starts at 0 at the start of each epoch's journal, which is always replayed from its start.

**Positions are relative because edits mostly land near the one before, and the kind rides in the same varint.**
A delta from −16 to 15 fits in the head's one byte with the kind, where an absolute position takes three bytes in a sequence of 50 KB and four in one of 100 MB, besides a byte for the kind; a jump across most of the sequence costs at most one byte more than an absolute position would, since zigzag adds one bit.
A keystroke is then a `Splice` of four bytes — the head, nothing removed, a length of one, and the character — and a transaction of nine with its length and checksum, where an absolute position and a tag byte made it 12 or 13.
A `Move`'s `to` counts from its `from`, since restacking moves bytes a short way.
Two bits leave no kind for a fifth record; a `Write` of no bytes, which changes nothing, is reserved as the escape to one.

A record's positions refer to the sequence as the records before it left it.
A record whose range does not lie inside the sequence is a writer's bug, and a reader refuses the file, as it refuses a transaction whose checksum matches but whose records do not decode; with one sequence there is nothing for a record to bring into existence, so no record needs a meaning outside it.
Transactions, their framing, the CRC chain, journal pages and recovery stay [as they are](../spec/journal.md).

`Free` and `Resize` have nothing left to act on.
`Copy` goes too: duplicating a value writes its encoding, and the flush would have to write a copy's bytes again anyway, to keep every data byte shown once; a `Copy` saved journal bytes only.

**The ordering discipline shrinks to its core: every transaction takes a sequence that decodes to one that decodes.**
Inside a transaction, anything goes; at its boundaries, the sequence must decode by the schema.
Nothing can dangle and nothing can leak, so the rules about publishing before freeing go.
A container whose edit changes two places — the element and the count, for a push — makes both in one transaction, and the conformance suite's check becomes: truncate the journal at every byte, replay, and decode.

### Spans on file

**One kind of entry, in three forms:**

| form | shows |
| --- | --- |
| `Ref(id, length, address, next)` | `length` bytes at `address`, inside one data page |
| `Inline(id, payload, next)` | its payload, 1 to 126 bytes |
| `Zero(id, length, next)` | `length` zeros |

`id` is the span's id, `next` the id of the span after it, or none at the end of a chain.
Zeros are rare in encodings without padding, but `Fixed` values and large preallocated arrays have them, and they cost the format a tag.

**Resolution:**

1. A span id's entry in the highest epoch is its description; an epoch holds at most one per id.
2. Each chain runs from the span its header field names, following `next`, and holds its spans' content in that order; a header field of 0 names an empty chain.
3. A file is invalid if a chain names an id that no entry describes, reaches a span twice, or shares a span with another chain.
   A reader must in particular check if any span is reached twice to protect against an infinite loop during opening.

**Rules for writers:**

- at most one entry per span id and epoch;
- the reach rule of [decision 6](#6-one-entry-per-span-the-newest-wins-reachability-decides-the-rest): a span reached at the end of an epoch and not at the end of the one before is stated in that epoch;
- no two reached spans show the same byte of a data page, a writer invariant that readers do not depend on, as [today](../spec/address-table.md#design-directions).

### Encoding of entries

```
; `content` field of a table page:
table_page := child_ref* 0:byte entry*
entry      := id_delta:varint tag:byte body next?
; bit 0 of tag: 1 if the next span is id + 1, and no `next` field follows;
;               0 if a `next` field follows the body
; tag >> 1 = 0:        Ref     body := length:varint address_delta:varint
; tag >> 1 = 1:        Zero    body := length:varint
; tag >> 1 = 2..127:   Inline  body := payload:byte{(tag >> 1) - 1}
next       := varint   ; 0: the chain ends here; else zigzag(next - id)
```

Entries in a page are sorted by id, so `id_delta` is at least 1.
`address_delta` is the zigzag of the address minus where the previous `Ref` in the page ended, or minus 0 for the first.
`next` never needs to name the span itself, which would be a cycle, so 0 is free to end the chain.

A span written together with its neighbours — by a merge, by evacuation, or when the content was new — gets the next id, and its data follows theirs in the page, so it takes 5 bytes: one each for the id delta, the tag and the address delta, and two for the length.
A span with a successor elsewhere takes one to three more, and an inline span two plus its payload.
An entry's framing is at most 22 bytes.

### The header

`root_allocation`, `schema_table` and `consolidator_state` become `sequence_head`, `schema_head` and `private_head`, the first span of each chain, four bytes each and 0 for an empty chain.
The private chain keeps the rules of the [consolidator state](../spec/file-format.md#the-consolidator-state): nothing in it affects the file's content, any writer may keep, replace or clear it, and it may be stale or another implementation's.
`root_fingerprint` stays, and with it [the fast path](../spec/schema/fingerprints.md#as-a-fast-path).

### Bounds

- Span ids are 32 bits and never 0, so a file holds at most `2^32 − 1` spans.
- A `Ref` shows at most `MAX_PAGE_CONTENT` bytes, an `Inline` 1 to 126, and a `Zero` at most `2^32 − 1`; no span is empty.
- The sequence is shorter than `2^48` bytes, so a position takes at most 7 bytes as a varint.

### In memory

**The store keeps a span table, a rope, the page table, and a reverse index.**

- **The span table** is an array indexed by span id: what the span shows — a page and an offset, an inline payload's place in a table page, or zeros — its length, its successor, and the table page that holds its live entry, about 16 bytes per span.
- **The rope** is a counted B-tree over the sequence's spans in order, each entry a span id and its length, each node the total length below it.
  It finds the span that holds a position, splits one, and cuts and inserts runs, each in `O(log k)`.
  The schema's and the private state's chains are short and written only by flushes, so a list serves them (unless having a single, reused, chain implementation based on a B-tree is easier).
  kladde-rs keeps the rope as an implicit treap instead, a randomized binary tree whose nodes know their parent and the total length below them, in the same arena type as the fold's piece table; it costs `O(log k)` expected steps, and a node of 40 bytes per span.
  The treap is there for ease of implementation: a split at a position and a join of two trees are all that the rope, the fold's piece table and a transaction's cuts need, so one arena type of under 400 lines serves all three, while splitting and joining whole B-trees is the intricate part of a counted B-tree.
  A counted B-tree with leaves of 32 entries would take about 15 bytes per span rather than 40, and descend three levels where the treap descends about 14 for the world map's 3,660 spans; neither difference shows in the files measured, but the B-tree is the better choice once memory per span matters, 240 MB against 640 for 16 million spans.
- **The page table** is as today: kind, epoch, coverage, and buckets for victim selection.
  A data page's coverage counts the bytes reached spans show in it; a table page's, the live entries' bytes and their inline payloads.
- **The reverse index** lists, for each data page, the spans that show bytes in it, for [evacuation](../impl/consolidation.md#finding-the-referrers-of-a-data-page).
- **The span id allocator** hands out the lowest free id, and runs of consecutive free ids where a flush states consecutive spans, from a bitmap of free ids with a summary bit per word, which finds either in a few scans of words.
  A free list threaded through the span table's empty entries would cost no memory of its own, but would hand ids out in the order they were freed, which scatters them, while low, consecutive ids are what keep id deltas short and successors implicit.
  The page table hands out reusable pages lowest first from an ordered set, as today, for a reason of the same kind: it keeps the file's live pages low, where truncation can follow.
  kladde-rs hands out the id after the predecessor's, if it is free, and the lowest free id otherwise, which keeps the spans a flush makes one after another consecutive without looking for a run.

The fragment map, the statement slab, the allocation map, the recyclable set, pins and `mentions` all go.
An entry is live exactly while it is the span table's entry for a span the rope holds, so liveness needs no counter at all.

**kladde-rs's span table takes 48 bytes per span, not 16**, since each record also holds the span's chain, its rope node, its heat, its entry's size and place in its table page, and its place in its data page's reverse list, which the store keeps in the span table rather than beside it.
With the rope's node, a span costs about 90 bytes in memory, against the 32 [the open questions](#open-questions) assume.

### Opening a file

1. Read the headers and take the governing one, as today.
2. Read every table page the header reaches, and keep, for each span id, the entry of the highest epoch, in the span table; no sorting is needed.
3. Walk each chain from its head, building the rope, and refuse the file on a missing id, a span reached twice, or a span two chains share; count each reached span's entry and data toward its pages' coverage.
4. Read the data pages the reached spans show, in the order of their page numbers, into a mirror, which step 5 then reads from and which is dropped after it, [as today](../impl/index.md#the-mirror).
   Reading them in the sequence's order instead, as step 5 needs them, would jump between pages wherever the sequence was ever edited; step 3 has already found which pages are needed, so step 4 reads them in one sequential pass, and checks every page's checksum at open.
5. Decode the sequence through a cursor that follows the rope: the root type's `decode` reads front to back, and every container builds its index of children as it goes.
   A recursive type nests as deeply as the file says, so the decode limits its depth, or does not recurse, and refuses a file beyond the limit rather than overflow its stack.
6. Replay the journal.

That is `O(entries + n)`, as today, and simpler: no sweep resolving bytes across epochs, no clipping at sizes, no tombstones.

**kladde-rs differs in three steps.**

- It writes a span table of two levels, the header and the leaves it names, and refuses at step 2 a leaf that names table pages of its own.
  The header's room for references to leaves, one or two bytes each, bounds a file to some 2,000 to 4,000 leaves, one to two million spans: a sequence of a few hundred MB in spans of the length the measurements find, and far more than any workload measured, the largest of which takes 21 to 26 leaves.
  ⚠️ Beyond the bound, a flush fails, and since opening flushes the replayed journal, the file no longer opens; kladde-rs's `later.md` records the fix, interior table pages.
- Its decode recurses without a limit on its depth.
- It replays the journal before the decode, by folding the recovered transactions in a flush at open, so that the decode reads the state after the last of them; the decode then needs no replay of its own.

### The flush

**The fold is today's, over one sequence instead of one per allocation, with its pieces in a counted tree.**
It keeps a piece table over the sequence, starting as one piece, the committed sequence; `Write`, `Splice`, `Move` and `Paste` edit it, and at the end it lists the new sequence as committed ranges, at the positions they had after the last flush, and new bytes in the arena.
Today's fold keys its pieces by their offset in their allocation, which a splice into a long sequence would re-key behind it; a counted tree, as the rope is over spans, takes each record in `O(log)` steps of the pieces the journal made, and keeps a transaction's cuts as lists of pieces until the transaction ends.

**The flush then maps pieces to spans.**
A committed range is shown by the spans that showed it, found through the rope; where a range begins or ends inside a span, the span is split, its left part stated again with a shorter length and its right part a new span over the same bytes.
New bytes become new spans, inline below the [threshold](../impl/flush.md#the-inline-threshold) and placed in data pages above it.
Every span is linked to the one that now follows it, and every span whose content, length or successor changed is stated, with every new one.
A splice that lands in content the same flush wrote costs nothing beyond it, as [today](../impl/flush.md#what-falls-out-unasked) for an allocation written in the same flush.

**Around each edited place, the span policy then merges what it wants merged**, [choice B](#b-how-short-spans-may-stay), reading the bytes of committed spans from their data pages.
Consolidation, placement, [the header as write buffer](../impl/flush.md#the-header-as-write-buffer) and the commit follow as today, with heat kept per span, and evicted runs of spans contiguous in id and merged to the bound as they leave the header.
kladde-rs's flush makes a run of 32 zeros or more among the new bytes a `Zero` span, evicts the header's coldest entries by heat alone rather than in runs contiguous in id, and merges each evicted span with its neighbours in the sequence, extending each run while a neighbour whose entry also goes to a leaf makes a pair shorter than the bound.
Its span policy has five constants, whose defaults [measurements](../evaluation/one-sequence.md#the-span-policy) chose: the bound `L`, `Options::span_min`, 64 bytes; the length below which a `Ref` cut by an edit is written again rather than split, `Options::rewrite_below`, 64 bytes; the inline threshold, 64 bytes; the flushes after which a span counts as cold for the walk, 16; and `μ`, 0.02.

### Consolidation

**The three debts of [today's picture](../impl/consolidation.md#the-high-level-picture) remain, and each gets simpler.**

- **Page fragmentation of data pages**: evacuating a victim writes the bytes of its reached spans into new pages and states each span again with its new address; nothing else changes, since no span's id does.
  Neighbouring short spans in a victim can be merged in the same pass, at no extra write.
- **Page fragmentation of table pages**: a page rewrite states each live span in it again, under its own id; there is no size to state again and no tombstone to carry, so the [one correctness obligation](../impl/address-table-operations.md#stating-the-size-again--the-one-correctness-obligation) of a rewrite goes.
- **Description fragmentation** becomes a property of regions of the sequence rather than of allocations: runs of short spans, found where edits happen and by a rotating walk over the rope, which replaces [the walk over the fragment map](../impl/consolidation.md#the-rotating-window).
  Merging a run of spans does what today's [description defragmentation](../impl/consolidation.md#description-defragmentation-rides-the-rotating-window) does, but materializes no zeros, since no span has gaps: it pays when the entry bytes it retires exceed `μ` per byte it writes.
  The walk can also give a run of spans consecutive ids, so that their successors become implicit, which plays the part today's window plays for delta encoding.
- **Outer fragmentation**, truncation, [compaction mode](../impl/consolidation.md#compaction-mode) and [pacing](../impl/consolidation.md#pacing) stay as they are.

kladde-rs implements none of the three refinements: its evacuation writes a victim's spans as they are, splitting the one that crosses the end of the page it fills rather than leaving the rest of the page empty; its walk merges runs of cold spans and renumbers nothing; and it keeps main's churn floor, budget controller and free filling unchanged, over pages of spans.

### The API for application authors

**Unchanged in shape: `Kladde<T>`, guards, the derive, transactions and batches.**
What changes is the menu of containers.
There is one vector, one string and one map, with no small, slotted or packed variants beside them; an author who wants a fixed-size element or field writes `Fixed<u32>` or `Fixed<Shape>`, and marks `Shape` `#[kladde(bounded)]`, as [the API for data types](#the-api-for-data-types) explains.
A `Box<T>` is persisted as the `T` it holds, with `T`'s encoding, descriptor and fingerprint, so boxing a field changes nothing on file; it is how a recursive type, such as a tree whose nodes hold their children inline, gets a size in Rust.
In kladde-rs a box's `FIXED_SIZE` is `None` whatever `T`'s, since a type that holds itself through a box would otherwise ask its own size of itself while the compiler evaluates it, which rustc refuses as a cycle (E0391) rather than reading it as "not fixed-size"; a vector of boxes then keeps an index it could do without, and nothing on file changes.
The derive cannot tell a recursive type from the tokens of one type, and a box is the one indirection among kladde's types that could pass a fixed size on, so answering `None` there cuts every cycle.
The guard of a `Fixed<T>` in kladde-rs replaces the value whole, an overwrite of its fixed size, since a guard of one of `T`'s fields would have to write that field in its fixed form, which takes a second guard for every type.
That is visible to the caller: the guard offers `set(Fixed<T>)` and reading, and no guard of a field of `T`.

Vectors gain `move_element`, which reorders without writing an element's bytes.
`remove` still returns the value, and `delete` is `remove` that drops it: both splice its bytes out of the file, and neither has anything to free.

### The API for data types

**A data type encodes, decodes, and mutates its region; it never allocates, frees, or knows a position.**

```rust
pub trait Persistable: Sized {
    /// `Some(n)` if every value encodes to `n` bytes.
    const FIXED_SIZE: Option<usize>;

    fn encoded_len(&self) -> usize;
    fn encode(&self, out: &mut Encoder<'_>);
    fn decode(input: &mut Decoder<'_>) -> Result<Self, Error>;

    type Guard<'r, B: Backend + 'r>: Guard where Self: 'r;
    fn guard<'r, B: Backend + 'r>(&'r mut self, region: Region<'r, B>) -> Self::Guard<'r, B>;

    fn describe_local(builder: &mut SchemaBuilder) -> TypeDescriptor;
}

/// A type with a fixed form, which `Fixed<Self>` holds.
#[diagnostic::on_unimplemented(message = "`{Self}` has no fixed form", /* ... */)]
pub trait Bounded: Persistable {
    const FIXED_FORM_SIZE: usize;
    fn encode_fixed(&self, out: &mut Encoder<'_>);
    fn decode_fixed(input: &mut Decoder<'_>) -> Result<Self, Error>;
}

pub struct Fixed<T: Bounded>(pub T);
```

That is the trait as kladde-rs implements it, which differs from the proposal's first sketch in three places.
`Fixed<T>` needs `T`'s fixed form, which a subtrait, `Bounded`, states, so that no method panics for a type without one, and `Fixed<PersistableString>` fails to compile where it is written, with a message that names the fix.
`Backend` is any `WriteBackend` that is `Copy`, such as `&Store`, so that a guard hands its backend to its children and to a cut by value.
And a guard's `Persistable` need not be `Self`, since a `Box<T>` uses `T`'s guard.

**The scalars, `Fixed`, and the `Option`s and tuples of `Bounded` types are `Bounded`, and a derived type is if it is marked `#[kladde(bounded)]`.**
The derive then requires every field's type to be `Bounded` too, failing at the type's definition otherwise, and bounds each type parameter by it, so that a generic type such as `Tagged<T>` is `Bounded` where its parameter is.
It cannot implement `Bounded` unasked, exactly where the fields' types allow it: for a type without parameters, rustc refuses an impl whose where-clause names a concrete type that lacks the trait (E0277) rather than dropping the impl, short of the unstable `trivial_bounds`.
Opting in marks only the types that `Fixed` wraps and those inside them, none in kladde-svg; opting out would mark every type that holds text, a container or itself, 6 of kladde-svg's 15 derived types.
`FIXED_SIZE` stays an `Option` on `Persistable`, since a vector decides by it whether to keep an index, which a subtrait could tell it only with specialization; a type with a fixed size and no fixed form is possible, and harmless, since nothing can wrap it in `Fixed`.
A second vector type, for elements of a subtrait of fixed-size types, would hold no index at all, but would leave the application the choice between two vectors that this proposal spares it, to save a field that can shrink to an empty pointer, 8 bytes.

`FIXED_FORM_SIZE` is the bound on a value's size that `Fixed<T>` needs, so `Persistable` declares no other maximum: `Fixed<T>` stores the fixed form, every integer at its full width, rather than `T`'s encoding padded to its longest, in which a field would move whenever a varint before it grew.
A fixed-size type's fixed form is its encoding byte for byte, except where an enum's integer `#[repr]` sets a discriminant wider than the one byte its encoding takes, which keeps the fixed form's size when variants are added: `#[repr(u32)] enum Pinned { A, B }` has `FIXED_SIZE` 1 and `FIXED_FORM_SIZE` 4.

`encode` appends the value's encoding to an `Encoder`, and `decode` reads it from a `Decoder`; they differ because writing goes into one buffer, while reading crosses spans.
An `Encoder` appends to a buffer it borrows, which is the payload of the journal record being written, whose length `encoded_len` has already given, so a value is encoded once, straight into its transaction, and it offers varints, zigzag and UTF-8 with its terminator.
In kladde-rs, the buffer is one of the value's own, which the store then copies into the record, a copy of the value's bytes that a store method taking an encoding closure would save.
A `Decoder` reads the sequence from the mirror through the rope, across span boundaries, without copying, and says where a decode failed.

A `Region` is the value's substring, with `len()`, `write(at, bytes)`, `splice(at, removed, bytes)`, `splice_value(at, removed, &value)`, which encodes the value into the record and returns its encoded length, and `move_bytes(from, len, to)`.
`splice` records a `Write` wherever the lengths agree, so a guard's `set` calls it whether or not the size changes, and `write` is the case whose lengths agree by construction; inside a transaction both return the cut of what they remove, as [choice D](#d-moving-a-value-from-one-container-to-another) has it.
kladde-rs adds `paste(at, removed, cut)`, and `transaction(f)`, which poisons the store if `f` fails, since the regions around may have heard of a size change by then; a guard checks what can fail before it opens one.
`splice_value` asks the value for `encoded_len` once, since the record states the length before the bytes, and its caller learns the length from its result rather than asking again.
Its method `child(at, len, node, index)` makes the region of one of the value's children: the `len` bytes at `at`, together with a note that they are the value's child number `index`, so that when the child changes size, its region tells `node`, the value's index of its children, which updates where the children behind it start and passes the change on to the value's own region.
The `variable-size` branch's `Link` and `Node` do this for packed values.
Every offset is relative to the region's start, and every method refuses one outside it.
A child made on its own borrows its parent region mutably, so the parent cannot write while the child exists, and no sibling can move it; children made together, by `parts` or `split_at_mut`, are in [what a guard costs](#what-a-guard-costs).

A push onto a vector of variable-size elements, for instance, splices the element in at the end and rewrites the count, in one transaction, and only then changes memory:

```rust
pub fn push(&mut self, value: T) -> Result<(), Error> {
    let n = self.vec.items.len();
    let len = self.region.transaction(|r| {
        let len = r.splice_value(r.len(), 0, &value)?;
        r.splice(0, varint_len(n), &varint(n + 1))?;
        Ok(len)
    })?;
    self.vec.sizes.push(len); // appends to the Fenwick tree, O(log m); nothing for fixed-size `T`
    self.vec.items.push(value);
    Ok(())
}
```

`store` and `free` go, since there is nothing to allocate and nothing to release, and with them `Location`, `UniquePointer` and [the one hole](../rust/pointers.md#the-one-hole) through which `load` could mint a second owner.
The derive generates a field's guard at a constant offset up to the struct's first variable-size field, and at the sum of the encoded sizes before it after that.

The obligations of a hand-written implementation become: `FIXED_SIZE` and `encoded_len` match what `encode` writes; `encode` and `decode` are inverses; every guard method leaves its region holding the value's encoding when its transaction ends; and the descriptor matches the bytes.
The third is checkable, which today's ordering discipline is only by replay: a store in a test can keep the sequence in memory and compare a region with the value's encoding after every method.

### What a guard costs

**Making a guard costs one lookup in its parent's index, however deep its value lies, because the parent's guard already holds where the parent starts.**
Every guard holds the start of its value, which it needs anyway, since journal records name positions in the sequence: an absolute start, unless an ancestor's guard was made together with its siblings, [as below](#guards-made-together).
An application reaches a value one level at a time, `drawing.guard().shapes_mut().get_mut(7)?`, here as today, and each step adds its child's start relative to the parent to the parent's start:

- a field before its struct's first variable-size field, or an element of a container of fixed-size elements, starts at a constant, or at `i` times the element's size;
- a field after it starts at the sum of the encoded sizes of the fields before it, a number of additions fixed by the struct's type, since each field's size is its in-memory value's or its container's cached total;
- an element of a container of variable-size elements starts where the container's index says, [choice C](#c-what-a-container-keeps-to-find-its-children).

The start a guard holds stays right for as long as the guard lives: the guard borrows its parent mutably, so nothing before its value can change size meanwhile, and its value's own changes of size move only what lies behind it.
Iterating needs no index at all: a guard that steps through a container's elements adds each element's size to the start of the one before.

#### Guards made together

**Guards made together, by `parts` or `split_at_mut`, ask for their start when they write instead of holding it.**
`parts` hands out a guard for every field of a struct, or of an enum's current variant, at once, and `split_at_mut` guards for two runs of a container's elements, so a sibling before a guard can change size while the guard lives.
Such a guard holds a link to its parent's index and its own number there, as the `variable-size` branch's `Link::In` does today for packed values, and computes its start whenever it writes, from the parent's start and the sizes the index keeps for the children before it.
The index keeps those sizes in cells, so that a sibling's report of a change of size can update it while the other guards still hold shared references to it.
A write to an earlier part that changes its size, followed by a write to a later part, then works out by itself: the first write's report updates the parent's index, and the second write computes its start from the updated index, so that its record names the shifted position.
Such a guard pays a lookup at every write rather than once; a count of changes kept in the index, compared with the count the guard saw last, would let it reuse its start until a sibling's size has changed.

kladde-rs implements `parts`, for derived structs and enums and for tuples, and not `split_at_mut`, nor the count of changes.
`split_at_mut` is left out only because the evaluation needs it nowhere, kladde-svg's edits reaching one element at a time, and `parts` exercises guards made together already; the vector's Fenwick tree lies in cells and answers any element's offset, so it would serve as the two runs' index as it is.
Its `parts` gives a field whose predecessors are all fixed-size a constant start, as an accessor would, since no sibling's change of size can move it; only the fields after the first variable-size one look their start up when they write.

**A guard made on its own below a guard made together holds its start relative to that guard, since that guard's own start can move while it lives.**
Between the two, every guard borrows the one above it mutably, so nothing between them can change size before it, and the relative start stays right; above them, a sibling's write can shift everything.
A write then computes its absolute start by climbing only the guards made together: at each, one lookup in the index of the parent it was made from, plus the relative starts of the guards made on their own in between, which are constants.

Unit tests in kladde-rs should test this behavior in nested setups: a tree where we take two sibling guards of a root type using `parts`, descend two levels further in each sibling guard (without `parts`), then take two siblings of each of the two guards, and then propagate two more levels down in each of the four resulting remote cousins.
A write in one of the remote cousins finds its start by climbing only the guards made by `parts`, up to the root, and adds the relative starts of the guards in between, into which the test descended without `parts`, as constants.
A write that changes its value's size, though, updates every level up to the root, the guards in between included, since each of their values grew or shrank with it; those reports pass through guards that the guards below them borrow, so every level keeps its cached total and its index in cells, as the indexes of guards made together do.
The tests should check both: that each cousin writes at the right position after other cousins have changed size, and that afterwards the indexes at every level, the ones in between included, match the in-memory values.
kladde-derive's test `remote_cousins_write_where_they_start_after_each_other_grows` builds this setup from structs, and checks that the file then holds the value's encoding, and reopens to it; kladde-types' randomized test checks every vector's index against its elements after edits through `parts` of nested vectors.

**A change of size walks the same chain back up**, since it changes the encoded size of every value around the one that changed: each ancestor updates its cached total and its index's entry for the child the change came from.
That is one update per level, at the index's cost, where today a slotted value never changes size and a change of an allocation's size concerns no other allocation.

**So the depth is not a cost of the design, and the logarithm per level is a choice of index.**
The costs fall at three different times.
Making a guard costs a lookup at each level it descends, once, when the guard is made; a write through it that keeps its value's size costs nothing more, as today; and a write that changes its value's size costs an update at each level above the value, at that write, which today costs nothing.
The chain from the root is walked to make the guards anyway, here and today, so a constant per level adds nothing that grows with the file.
What a level adds beyond a constant is its container's index: nothing for fields and for fixed-size elements, and for variable-size elements `O(1)` with an array of starts, at `O(m)` per change of size, or `O(log m)` with a Fenwick tree.
The one other cost per mutation is that of guards made together by `parts` or `split_at_mut`, which look their start up at every write, as described above.

**Indexes that weigh elements by their encoded size make the whole chain cost `O(depth + log n)`, as one tree over every value's position would, with no such tree.**
In a tree over a container's elements in which an element of `w` bytes, in a container of `W`, lies at depth `O(1 + log(W / w))` — a biased search tree (Bent, Sleator and Tarjan, "Biased Search Trees", SIAM Journal on Computing, 1985), or a splay tree, whose amortized cost of access is the same for any weights (Sleator and Tarjan, "Self-Adjusting Binary Search Trees", Journal of the ACM, 1985) — the lookups from the root, of `n` bytes, down to a value of `w` bytes cost `O(depth + log(n / w))` in all, since each level's `log(W / w)` cancels against the next level's.
Changes of size telescope the same way on the way back up.
Each guard is then a cursor into one tree of positions that the containers' indexes form together, and the guard made from it starts its search there.
That pays for deep nesting of large containers, a document tree whose nodes hold long lists of nodes, say; for nesting as shallow as kladde-svg's, a Fenwick tree per container is simpler.

### The built-in containers

- **`PersistableVec<T>`**: the count, then the elements; in memory, a `Vec<T>`, and for variable-size `T` a Fenwick tree of the elements' sizes, [choice C](#c-what-a-container-keeps-to-find-its-children).
  A push and an insertion are a splice and a count, a removal likewise, a reordering a `Move`, and a size change of an element a report that updates the element's size in the tree, `O(log m)` of its entries.
  kladde-rs keeps the tree's sums in 32-bit cells, appends to it and pops from it in `O(log m)`, and builds it again in `O(m)` after an insertion, a removal or a reordering elsewhere.
- **`PersistableString`**: UTF-8, then `0xFF`; in memory, a `String`, or a rope for long text, which then needs no container of its own, since the file already takes splices in long text at constant cost.
  kladde-rs keeps a `String`.
- **`PersistableMap<K, V>`**: the count, then the entries; in memory, an index map, as Rust's `indexmap` keeps one — the entries in a vector, in their order on file, and a hash table of their indices, so that each key is stored once — and for variable-size entries a Fenwick tree of their sizes.
  Removal swaps: the last entry moves into the removed one's place, by a `Move` that writes no bytes, the removed entry is spliced out, and the count goes down, so the file stays dense, with no tombstones, and the only index that changes in memory is the moved entry's.
  An insertion appends, and a lookup is a hash and an index.
  kladde-rs's hash table is its own, open addressing over 32-bit positions, probed linearly and at most half full, and describes an entry as a struct of a key and a value.
- **`PersistableBlob<T>`**: opaque, its serde payload prefixed with its length.

### Evolution

More detailed specification and implementation of schema evolution is deferred until an initial empirical evaluation reveals whether the proposal in this document should be kept in the first place.
kladde-rs writes the schema chain and the root fingerprint, and refuses to open a file whose fingerprint differs from the application's root type's.

**A change of schema is no more invasive here than today: reading a writer's schema works as it does, an upgrade writes again only the values whose encoding changed, and only the *retain* mode, writing in the writer's schema, gets more expensive, because of variable-size encodings rather than of one sequence.**

**Reading is unchanged.**
The schema chain holds the writer's descriptors and the header its root fingerprint; a reader whose fingerprint matches decodes natively, and one whose fingerprint differs resolves the writer's types against its own once, as [evolution](../spec/schema/evolution.md#what-is-settled) settles it, into a plan per type.
With variable-size encodings, the plan is a parse of the writer's encoding rather than a list of offsets, as [the variable-size draft](variable-size.md#evolution) found; it runs during the decode that the load does anyway.

**Neither design mixes schemas in one file.**
Today's header names one schema table and one root fingerprint, as this design's does, and the specification [leaves open](../spec/schema/evolution.md#what-is-tbd) whether a schema could ever be tagged per allocation or per region.
Until one is, the whole file decodes by one schema at every commit, here as there, so a session that opens a file with a different schema either upgrades everything whose layout changed before its first commit, or writes in the writer's schema all session.

**Upgrading costs the same order here as there: the bytes of what changed.**
Say a file was written while `Shape` had two variants, `Circle` and `Path`, and the application opening it now declares a third, `Rect`, and has given `Path` a new field.
At open, the reader decodes every shape by the plan into its own `Shape`, and then compares each shape's bytes in the file with that shape's encoding by its own schema.
A circle's are equal, since adding a variant changes none of the old variants' bytes, so it keeps them; a path's differ, since its encoding now holds the new field, so the upgrade splices the new encoding in over the old one.
Values whose type is the same in both schemas, such as the title, are not encoded at all; nor are the vector of shapes and the drawing, whose types differ only through `Shape`, so the comparison passes through them to their shapes.
Each splice states a constant number of spans besides its new bytes, and where changed values lie densely, as in a vector whose element type gained a field, [the span policy](#b-how-short-spans-may-stay) writes their container's region again as one span, as today's upgrade writes the vector's allocation again.
Nothing behind a changed value is restated, although all of it moves, since no position is stored on file.
The upgrade is a flush at open that folds these splices along with the new schema chain, before the application gets the file, and needs no journal: a crash before it commits leaves the old file, and the next open upgrades again.

**Retaining costs more, because of variable-size encodings.**
To write in the writer's schema, a guard needs the writer's offsets and the writer's encoding of each value it writes.
Today a slotted value's offsets follow from the writer's inline sizes, which the read plan holds; a variable-size value's depend on the writer's encoded size of every variable-size field before them, and those sizes, like the encoding itself, come only from interpreting the writer's descriptors on the in-memory value at each write.
The variable-size draft meets the same cost for its packed containers; this design gives every container variable-size encodings, so it meets it everywhere except inside `Fixed` values, whose offsets the writer's descriptors give as today's slotted ones do.
Upgrading is therefore the default, and retaining a mode for files that an older application must still read, which interprets descriptors at each write.

### Mixing schemas: capsules

**Keeping parts of a file in different versions of their schema, and upgrading each part on its own, is what capsules were meant to do, and one sequence makes them cheaper than allocations would.**
Capsules were sketched in kladde-rs's notes on schema evolution in July 2026, which kladde-rs `d18baa1` removed in September with the other notes of the relocatable heap; the specification's "per schema-boundary region" is what is left of them.
Three ideas made them up:

- **A `Capsule<T>` is a schema boundary**: a subtree that records which version of `T`'s schema it is written in, as an index into a table of the versions the file holds, each stored once and resolved once per session, however many capsules use it.
- **Fingerprints computed outside a capsule erase its inner type**, so the root fingerprint does not change when `T` does, and a stale capsule is found by comparing the table's fingerprints with the reader's, once per version, and reading each capsule's index.
- **Each capsule upgrades on its own**, at first touch, or keeps its version for as long as nothing writes it.

**Today a capsule would need an allocation of its own**, since a value whose size changed with its version would otherwise shift everything behind it in its parent's allocation, and an allocation per capsule is a pointer, statements and a size: the per-value cost this design removes.
In one sequence, a value's size concerns nothing but positions in memory, so a capsule is inline: its version's index as a varint, then `T`'s encoding in that version.
Upgrading one at first touch is a splice of its region, with the new index, in the same transaction as the write that touched it: a constant number of span entries, plus the capsule's bytes.
The capsule's guard is where that happens, since every guard inside it is made from it.

The schema chain then holds the table of versions in use.
A session whose types are missing from it adds them in the flush at open, before any capsule names them, and a count of capsules per version, built at open and kept in memory, says when a flush may drop a version from the table.
Nothing in the span format or the journal changes: capsules are a matter of the schema and of one wrapper type, and can come later.

### Tooling and testing

- **A tool decodes the whole file from the schema chain**: every container is structural, there is no pointer graph to follow, and nothing can leak, so the leak checker of [conformance](../spec/conformance.md#the-suite) has nothing to look for.
- **A differential oracle becomes one comparison**: after every operation, the sequence must equal the canonical encoding of the in-memory root, byte for byte.
- **The store can be tested on its own** against a plain `Vec<u8>`: any series of `Write`, `Splice`, `Move` and `Paste`, flushes, reopens and simulated power cuts must leave the chain holding the vector's bytes.
- The specification should eventually also define a canonical textual representation of kladde files, as well as canonical conversions to and from common human readable serialization formats like json.
  These text-based representations should only cover the schema and the sequence: not the private state, which any writer may keep, replace or clear without changing what the file holds, and not how either is split up into spans.

### Segments, later

**Loading part of a file means skipping a subtree without decoding it and without reading its spans, and one sequence offers neither.**
A [segment](segments.md) would therefore be a chain of its own, named from the sequence by a reference: one coarse kind of pointer, reintroduced deliberately, for exactly the subtrees an application declares.
The format already has three chains, so a fourth kind changes the header's fields and a descriptor kind, not the span machinery; whether references to segments may move, and how their transactions relate to the sequence's, is as open as everything else about segments.

## What it costs

- **Positions in memory.**
  Every container of variable-size elements keeps an index, four bytes per element, where today's slotted containers keep none; fixed-size elements cost nothing, and the store's own structures lose their entries per allocation.
- **Offsets at run time.**
  A field behind a variable-size field, and an element of a container of variable-size elements, are found by a sum or a lookup instead of a constant.
- **Guards look positions up, and changes of size walk back up.**
  Making a guard costs a lookup in its parent's index, and a change of size an update at each level above it, where today the one costs a constant and the other nothing: `O(log m)` per level of variable-size container with Fenwick trees, or `O(depth + log n)` for a whole chain with size-weighted indexes ([what a guard costs](#what-a-guard-costs)).
- **Overwrites inside long spans split them**, three entries the first time a place is written, against today's one statement.
- **Two records for every insertion into a sequence and every removal from it**, the element and the count, in one transaction; text, which has no count, needs one.
- **Moving a value between containers holds a transaction open** from its [cut to its paste](#d-moving-a-value-from-one-container-to-another), or encodes the value again.
- **Writing in a foreign schema interprets its descriptors at each write**, as it would for packed values today; upgrading costs what it costs today ([evolution](#evolution)).
- **Partial loading needs segments**, which need a pointer of their own.
- **A rewrite of everything above the pages**: the address table, allocations and journal documents of the specification, most of the implementation documents, and the store, persist, types and derive crates of kladde-rs.

## Compared with today

| | today | proposed |
| --- | --- | --- |
| description written per splice | every statement behind it in its allocation, or its bytes written again | one to three entries |
| moving a value within a container | the moved bytes, or [`Move`](move-op.md)'s statement edits | three to six spans, no bytes |
| moving a value between containers | its pointers written again, its allocations left where they are | a cut and a paste in one transaction: three to six spans, no bytes |
| description written per overwrite | one statement | three entries the first time, one after |
| description per value | a pointer, its statements, and its size | none; a few bytes per span of up to a page |
| what a page rewrite must also state | the sizes and tombstones of the ids it touches | nothing |
| liveness | pins over fragments, size pins, tombstones, `mentions` | the newest entry of a reached span |
| the store's memory | fragment map, statement slab, allocation map, page table | span table, rope, page table |
| removing a value | a splice, then a free walk over what it owns | a splice |
| field access when mutating | constant offsets | constant up to the first variable-size field |
| opening | a sweep resolving bytes across epochs | one pass over entries, a chain walk, and the decode |
| upgrading to a new schema | the allocations holding values whose layout changed, written again | the values whose encoding changed, spliced in again |
| writing in the writer's schema | offsets from the writer's inline sizes | offsets and encodings interpreted from the writer's descriptors |
| parts of a file in different schema versions | not supported; a capsule would take an allocation | not supported; a capsule would be inline |
| tooling | opaque containers, a pointer graph | a decode from the schema |

### Vocabulary

**Most of today's vocabulary of the layers above the pages goes, and this document already uses the replacements wherever it describes the proposal:**

| term | why it goes | instead |
| --- | --- | --- |
| allocation, allocation id, allocation size | there are none | the sequence, and a value's region |
| pointer, owning pointer, ownership, `UniquePointer`, `Location` | nothing points at anything | a region; a segment's reference, later |
| free, freeing, free walk, leak | nothing is allocated | removing, deleting: a splice |
| tombstone, size statement, sizing variant, `Size` | no size is stated | — |
| coverage rule | no byte becomes part of an allocation | the reach rule |
| fragment, fragment map, stipple, shadowing of part of a statement | no statement shows part of a span | span, run of spans; an entry replaces another whole |
| pin, `F`, `S`, `mentions`, statement slab, allocation map, recyclable set | liveness is one bit | live entry, reached span |
| `Resize`, `Free`, `Copy` | nothing to act on | — |
| slotted and packed, of places and encodings; inline size, `INLINE_SIZE` | a type has one encoding | fixed-size, variable-size, `Fixed(T)`, encoded size |
| small value, spilling | everything is inline | — |
| root allocation, schema table, consolidator state, as allocations | they are chains | the sequence, the schema chain, the private chain |
| description defragmentation | there is no stipple to fill | merging spans |

Notes on a few more specific terms:

- **"Address table" becomes "span table".**
  It no longer maps allocations to their bytes; it lists spans, and the in-memory span table is its resolved form, so one name serves both, as "address table" serves the pages and their resolution today.
  Its pages keep the kind `Table`.
- **"Statement" becomes "entry".**
  Each describes one span whole, and the newest replaces the others whole, as entries do in an LSM tree or in Bitcask's log; "statement" suggested claims about some bytes that combine with other claims about others, which they no longer do.
  "Entry" also names the in-memory span table's slots, but those are the same thing resolved, so the clash is mild.
  The verbs stay: writing a span's entry is *stating* the span, and writing it again *restating* it.
- **"Record" stays, and "operation" keeps a meaning of its own.**
  The specification already says "record": [the journal](../spec/journal.md#record-kinds) lists record kinds, and only the implementation documents call [their in-memory buffer](../impl/transactions-and-batches.md#one-buffer-and-three-cursors) `ops`.
  A record is what the journal holds, and an operation what an application's call does, which may take several records, as a push takes two; the buffer would become `records`.
- **"Span" and "piece" both stay.**
  "Piece" is the literature's name for an element of a piece table, which is what the fold keeps: transient, unnamed, over committed bytes and the arena.
  A span is a piece made persistent: it has an id, links to its successor, and outlives the flush.
  The closest established term for a run of a file's bytes on disk is a file system's *extent*, but an extent is keyed by its offset in the file, which is exactly what a span avoids, so the name would mislead.
- **"Backend" stays as well:** a region writes through a `WriteBackend`, which the store implements.

## Related systems

**This draft knows of no system that combines what the proposal does, but each of its layers has a close precedent, and the redesigns have moved kladde from object stores toward the storage of editors' documents.**
In those terms, the proposal is an editor's piece table kept on disk, linked rather than keyed by position, made crash-safe by shadow paging with an intent log, kept compact by a log-structured cleaner, and holding a serialization much like Avro's.

| layer | closest precedents | shared | different |
| --- | --- | --- | --- |
| the encoding | Avro's binary encoding | the schema in the file, no tags, no padding, varints, counts | edited in place; text ends in a terminator; `Fixed` |
| the sequence on file | Word's fast save; FAT's chains | an on-disk piece table; successors in a table | links instead of positions; byte-granular spans |
| the span table | LFS's inode map; Bitcask | a table of locations, newest wins, held in memory | nothing is searched on disk |
| commit and recovery | shadow paging; LMDB; WAFL and ZFS | a root written last after an `fsync`; a log replayed over it | no tree on disk |
| reclamation | Sprite LFS's cleaner | victims by utilization and age | pages instead of segments |
| state and durability | system prevalence; Redis | all state in memory, every change logged | byte records instead of commands; incremental checkpoints |
| moving values | Cap'n Proto's orphans | detach, adopt elsewhere, no copy | journaled, within a transaction |
| positions in memory | ropes and piece trees | a counted tree over pieces | — |
| order by links | Yjs | a linked list of items with ids, merged into runs | one writer: no merging, tombstones or history |

### The encoding: Avro's, nearly

**[Avro](https://avro.apache.org/docs/1.11.1/specification/)'s binary encoding is the closest precedent of the sequence's encoding, and its object container files carry their schema as kladde files do.**
An Avro record is its fields' encodings concatenated, with no tags and no padding; integers are zigzag varints, a union starts with its branch's index as a varint, and an array is its elements in blocks, each preceded by its count.
kladde's [evolution](../spec/schema/evolution.md#prior-art) already takes Avro's model of reconciling a writer's schema with a reader's.
The differences follow from mutation: Avro data is written once and read in sequence, while kladde edits values in place, so it keeps counts in one place rather than in blocks, ends text in a terminator so that an edit of text needs one splice, and offers `Fixed` for values that change often.
*Protocol Buffers* share the varints and drop padding too, but tag every field with its number, which makes evolution possible without a stored schema at the price of bytes per field; kladde, like Avro, stores the schema once instead.

### The sequence on file: a piece table, as Word's fast save kept one

**A piece table on disk is old: Microsoft Word's binary `.doc` format kept one for its *fast save*, which appended the changed text to the document's stream and wrote a new piece table over it.**
Bravo, at Xerox PARC in the 1970s, introduced piece tables for editing, and Visual Studio Code's piece tree (2018) keeps one in a balanced tree in memory, as the rope does here.
Word's table differs from the span table in three ways that matter.
It keys each piece by its character position (the `PlcPcd`, an array of positions and piece descriptors), so it is rewritten whole at every fast save, where links let kladde state only the spans at an edit.
It reclaims nothing until a full save rewrites the document, so deleted text stayed in fast-saved files, which leaked it to whoever received them.
And it gave no crash safety of its own.

**The span table's successors are *linked allocation*, as FAT keeps it, with byte-granular spans instead of clusters.**
FAT file systems, and Microsoft's compound files, the container of `.doc` files, keep each file or stream as a chain of fixed-size clusters, whose successors a table holds.
A chain of fixed-size clusters can take an insertion only at a cluster's boundary; a span can be split at any byte.
And linked allocation's classic weakness, reaching a byte by walking the chain to it, does not arise, since kladde walks each chain once, at open, and keeps a rope.
Today's address table is the other classic scheme, *extents* keyed by their offset in the file, as ext4's and XFS's extent trees keep them; inserting a range there, as Linux's `FALLOC_FL_INSERT_RANGE` does, shifts every extent behind it, which is today's cost of a splice.

### Storage: a log-structured file system for one file

**The pages, the span table, the cleaner and the two header slots are those of a log-structured file system, with pages for segments and one file in it.**
Sprite LFS (Rosenblum and Ousterhout, "The Design and Implementation of a Log-Structured File System", SOSP 1991, and TOCS 1992) writes everything to fresh segments, finds each file's inode through an *inode map* that is itself written to the log and held in memory, tracks each segment's live bytes in a *segment usage table*, cleans by moving live blocks out of segments chosen by utilization and age, and alternates between two checkpoint regions.
The span table is that inode map, coverage the segment usage table, ripeness the cost-benefit policy, and the header slots the checkpoint regions.
*Bitcask* (Basho, 2010) holds every key in memory, rebuilt at open from its log, lets the newest write of a key win, and merges files by writing their live values again: the span table, read at open, is a Bitcask keyed by span id.
*LSM trees* resolve a key's versions across runs by recency, as entries are resolved across epochs, but search on disk, which kladde never does.

**The commit is shadow paging with an intent log.**
System R's shadow pages (Lorie, "Physical Integrity in a Large Segmented Database", TODS 1977), LMDB's two meta pages, WAFL's consistency points with their log of requests in NVRAM (Hitz, Lau and Malcolm, USENIX Winter 1994), and ZFS's transaction groups with their uberblocks and intent log all write a new root last, after an `fsync`, over pages that no older root reaches, and replay a log over the last root after a crash.
kladde's flush is such a consistency point, its journal such an intent log, and its two header slots LMDB's meta pages; it has no tree on disk to copy paths in, since nothing is searched on disk.

### State and durability: a memory image with incremental checkpoints

**Keeping all state in memory, logging every change before it returns, and folding the log into a checkpoint is *system prevalence*, as Prevayler implements it and Martin Fowler describes it as a *memory image*; Redis does the same with its append-only file and its snapshots.**
kladde differs in two ways.
Its log holds byte effects rather than commands, so replaying it needs no application code and no schema; and its checkpoints are incremental, since a flush writes what changed, where those systems write whole snapshots or rewrite their whole log.

### Moving values: Cap'n Proto's orphans

**Cut and paste is Cap'n Proto's *orphans*: an object disowned by its parent and adopted by another in the same message, without copying, move-only, and zeroed if nothing adopts it.**
Here the cut lives in the journal's transaction rather than in a message's arena, and the flush turns it into a few links.

### Order by links: sequence CRDTs

**[Yjs](https://github.com/yjs/yjs) keeps a shared text as a linked list of items, each with an id and links to its neighbours, merges consecutive items of one writer into runs, and persists updates that it later merges into one, which is close to linked spans with ids.**
*Automerge* keeps the history of operations, in a compressed, columnar format.
Both merge concurrent edits from many writers, and so keep tombstones and history and compact by encoding the whole document again; a single writer needs none of that.

### Where the redesigns have led

**Each redesign moved kladde further from object stores and closer to document storage.**

1. **The first design** was a relocatable heap: allocations contiguous in the file, pointers between them, and compaction that moved whole allocations.
   That is a persistent heap, as PMDK's `libpmemobj` or Boost.Interprocess's mapped files keep one, and an object store, as ZODB's `FileStorage` (a log of object records, an index of object ids built at open, and packing) or Realm are.
2. **The design at the time this proposal was made** keeps allocations and pointers, but stores them as a log-structured file system stores files: pages written copy-on-write, an address table of extents keyed by offset, an intent log, and a cleaner.
3. **This proposal** keeps the file system and drops the objects: one file whose extents are linked, a piece table, holding a serialization as Avro would write it.

So kladde has converged on known parts, put together in a way this draft has not found elsewhere: a typed document held whole in memory, durable at every change through a byte-level journal, and saved as deltas into a linked piece table on copy-on-write pages.
The nearest whole is Word's fast save, without its rewrites of the piece table, its leaked deletions, and its lack of crash safety.

## How to get there

kladde-rs is a testbed, so the way to judge this is to build it on a branch, in four stages, each tested before the next:

1. **The store**: chains of spans, the four journal records, the fold, the flush, consolidation and open, behind a byte-sequence interface, tested against a `Vec<u8>` with reopens and simulated power cuts.
2. **Persist**: `Region`, `Node`, `encode`, `decode`, `FIXED_SIZE`, `Fixed<T>`, and the derive.
3. **Types**: the vector, the string, the map and the blob.
4. **kladde-svg on top**, measured against main's slotted, packed and small layouts: file size, bytes written per edit, flush time, open time, and memory.

The specification and the implementation documents would change only once those measurements hold.

Implementation should start from branch `defrag-shifts`, at `b1a7f90`.
It holds main's seven newest commits, ported, the whole `variable-size` branch, `text-bench`'s one commit (`7c5cdd6`) cherry-picked on top, and two commits of its own: `45cfa13`, which merges the fragments of bytes that splices shift again and again, and `e88fd47`, which keeps svg-bench's edits to one part of the drawing at a time.
The first is a store change that this redesign replaces anyway, and the second is what stage 4's measurements want.
The three `*-svg` branches have the same base, each with a ripeness policy on top, and `variable-size` and `text-bench` lack main's seven newest commits.

**kladde-rs's branch `one-sequence` builds the four stages on `b1a7f90`, in eleven commits from `cbb1617` to `d2073f0`**, which the sections above annotate where the implementation differs from the proposal.
Stage 2 is two commits, the schema's descriptors and persist, and the derive arrives with the facade; stage 4 ports `text-bench` too; `b2d0649` sets the span policy's defaults to what the measurements found, and `0792da3` makes a span table that outgrows its header fail the flush rather than loop.
The last two came after the measurements: `bde8218` makes writes cut what they overwrite and has `Region::splice` record a `Write` where the lengths agree, and `d2073f0` moves the fixed form into the subtrait `Bounded`.
Two things went that measured what this design removes: kladde-svg's packed and small layouts, since every list and string is inline in one sequence, which leaves one layout; and kladde-bench's synthetic scenarios, which allocated, resized and freed.
[The file as one sequence, measured](../evaluation/one-sequence.md) compares the branch with main's churn floor on the drawings and texts of [Cleaning edited drawings and texts](../evaluation/cleaning-edits.md).

## Open questions

- **The span policy's constants**: `L`, what counts as cold, and how hot spans and the header share the span table's room.
- **Where to split and where to rewrite** on an overwrite, and whether shadowing entries earn their way back for some workload.
- **The containers' indexes**: whether a Fenwick tree is the right default, what an application with millions of variable-size elements should use, and whether any data model nests deeply enough for size-weighted indexes to pay.
- **Moving values between containers**: whether a cut should ever outlive its transaction, which would need a scratch chain and a chain number in `Move`.
- **Memory for very large files**: the rope and the span table take about 32 bytes per span together; a cold gigabyte in spans of a page needs about 8 MB of them, but a gigabyte edited everywhere at once, in short spans, would need far more.
  kladde-rs takes about 90 bytes per span, [In memory](#in-memory) says why.
- **Opaque types**: whether declaring a fixed size is enough, or whether some want both modes.
- **Capsules**: whether to bring back schema versions per subtree, [inline](#mixing-schemas-capsules), so that a large file whose element type changed can upgrade a part at a time rather than every element at the first open.
- **Segments**: how a reference to another chain looks in the schema, in the journal, and in a transaction.
