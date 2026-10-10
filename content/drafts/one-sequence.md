---
title: The file as one sequence
---

**Status: a proposal for a redesign of every layer above the pages, not adopted.**
A kladde file would hold no allocations.
Its content would be one byte sequence: the root value's encoding, in a packed format that the schema defines, with every value inline in its parent.
On file, the sequence is a **chain of spans**, statements that each show a run of its bytes and name the span that follows.
In memory, the store keeps a rope over the spans, and each container keeps where its children start.
A data type sees only its own **region** of the sequence: it overwrites bytes in it and splices it, and it neither knows where the region lies nor can reach anything outside it.
Pages, epochs, the two header slots, the flush protocol and copy-on-write stay as they are, and so does the application's API, except that the choice between packed and slotted containers goes.

**The design is meant to make two quantities independent of the file's size: the bytes of live pages per byte of the sequence, and the work per modification that keeps them so.**
A splice, an overwrite and a move each state a constant number of spans, wherever they land and however long the content around them is.
A page rewrite restates what is live in its page and nothing else.
And the description costs a few bytes per span, where today it costs a pointer and statements per allocation.
So live pages exceed the sequence by the description's share, the bytes per span statement over the bytes per span, plus the garbage a cleaner leaves.
A policy holds both at targets, at a cost per modification that is constant in bytes written and logarithmic in time.
With spans of 128 bytes where the sequence is hot and of a page where it is cold, and pages kept 90 % live, a large file's live pages come to about 1.12 times its sequence ([the arithmetic](#a-worked-example)).

**What goes**: allocations, their ids, pointers, ownership, freeing, size statements, tombstones, per-byte shadowing, the coverage rule, the fragment map and the statement slab, small values, and the distinction between packed and slotted places.
**What it costs**: containers keep positions in memory; offsets after a value whose size depends on its value are computed at run time; an overwrite inside a long span splits it; spans need a compaction policy; moving a value between containers encodes it again; a schema change rewrites the sequence; and partial loading needs an idea of its own.

## The design in brief

### Four layers, three of them new

| layer | today | proposed |
| --- | --- | --- |
| what an application sees | `Persistable` values, guards, transactions | unchanged, except that every container is packed |
| what a data type sees | an inline slot at a `Location`, allocations it owns, pointers, `store`, `load` and `free` | a region of the sequence: `encode`, `decode`, and writes and splices inside the region |
| the journal | records on allocations: `Free`, `Resize`, `Write`, `Splice`, `Copy`, `Move` | records on the sequence: `Write`, `Splice`, `Move` |
| the address table | statements keyed by `(id, offset)`, the newest winning per byte | span statements keyed by span id, the newest winning per span, ordered by links |
| the store, in memory | fragment map, statement slab, allocation map, page table | span table, rope, page table |
| pages and durability | | unchanged |

The schema changes too: every type gets the packed encoding that [variable-size values](variable-size.md) proposes, containers are described structurally, and a type records whether its encoding has a fixed size.

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
03 4D 61 70                  title: 3 bytes of text, "Map"
02                           shapes: 2 elements
00 00 00 80 3F               Circle (variant 0), r = 1.0
01 02                        Path (variant 1), 2 points
00 00 00 00 00 00 00 00        (0.0, 0.0)
00 00 80 3F 00 00 80 3F        (1.0, 1.0)
```

Nothing in it is a pointer, a size of an allocation, or a byte of padding; a container states how many elements it holds, and every value is decoded front to back from the schema.

**Where each value lies is known only in memory, and only relative to its container.**
The points are fixed-size, eight bytes each, so the path finds point `i` at `1 + 8i` within its own region and keeps nothing.
The shapes are not, so the vector keeps where each of its elements starts, here at 1 and 6 within its region.
The drawing keeps nothing either: its title starts at 0, and its shapes start where the title ends, which the title's in-memory value says.

**Renaming the drawing is one record, and two span statements.**
Setting the title to "World map" splices 10 bytes over the first 4, `Splice(0, 4, 09 "World map")`, and every byte behind it moves by 6 without being mentioned.
On file, the sequence was one span, #1, showing 28 bytes at some address `a` in a data page.
Afterwards it is two: a new span #2 holds the 10 new bytes inline and names #1 as its successor, and #1 is stated again to show 24 bytes at `a + 4`; the header names #2 as the first span.
The same splice costs the same two statements wherever it lands, in a sequence of 28 bytes or of 28 million.

## Fundamental decisions

### 1. One sequence, holding the root value's packed encoding

**The file's content is the encoding of the root value, with every value inline in its parent; there is no allocation and no pointer.**

- **The description stops depending on the data model.**
  Today every allocation costs a pointer in its parent, statements naming its id, and a size, so the description grows with the number of values, and the [variable-size draft](variable-size.md#the-problem) found allocations of short strings and short lists to be most of them.
  With one sequence, the description depends only on how the store cuts the sequence into spans, which the store chooses and the data model never sees.
- **The unit of description and the unit of the data model stop pulling against each other.**
  Today a cheap description wants few allocations, so values move inline into long ones, as kladde-svg's small layout does; and a cheap splice wants short allocations, since a splice [restates the tail](splices.md#what-a-splice-costs-today) of its allocation.
  Separating the two removes the trade-off rather than choosing a point on it.
- **Everything that exists because of allocations goes with them.**
  Ownership, the free walk, publishing a pointer before freeing what it replaced, tombstones, id recycling and leaks all exist because content lives in allocations that pointers reach.
  Removing a value is now one splice, whatever it held, and nothing can leak, since nothing can be unreachable.
- **The file is its own serialization.**
  A tool decodes the whole content from the schema, with no pointer graph to follow and no opaque container to stop at.

It gives up reaching a value on file without decoding what lies before it, which kladde never does, since it [reads in bulk](../impl/index.md#the-shape-of-an-implementation).
And positions now change whenever something before them changes size, so something has to track them, which is decision 4.

### 2. Containers state counts, and nothing states its own size

**A container's encoding begins with its number of elements, a string's with its number of bytes, and no value states its encoded size.**
A value that changes size then changes no byte outside itself; even its container's count changes only when an element comes or goes.
Were every container to state its size in bytes instead, a change of size anywhere would change the stated size of every container around it, up to the root.
The price is that skipping a value means decoding it, which the load does anyway, and a reader of a foreign schema does too, by [its parse plan](variable-size.md#evolution).
Opaque types are the exception: nothing can decode them, so a variable-size opaque value is prefixed with its size in bytes.

### 3. Fixed-size types stay distinct

**A type whose every value encodes to the same number of bytes is fixed-size, the schema says which types are, and positions inside and among fixed-size values are computed rather than kept.**
Floats, bytes, `bool`s, fieldless enums with fewer than 128 variants, and structs, tuples and arrays of fixed-size types are fixed-size in their packed encoding.
Integers wider than a byte pack as varints and are not, nor are strings, sequences, and enums whose variants differ in size.
A wrapper, `Fixed(T)`, gives any type whose encodings have a largest size its **fixed encoding**: integers at their full width, a `char` in four bytes, an enum padded to its largest variant, and a struct as its fields' fixed encodings.

The distinction buys three things, all in memory:

- a struct's fields up to its first variable-size field sit at offsets the compiler folds, as every field does today;
- a container of fixed-size elements finds element `i` at `i` times the element's size, and keeps no index of where its elements start;
- a fixed-size value never changes size, so writing it is always an overwrite in place, and nothing above it has to hear about it.

The choice is the author's, per field and per element type.
Packed encodings save bytes on file; fixed ones save memory and time in containers that hold many values, and keep a field that changes often, such as a counter, from changing size whenever it crosses a power of 128.
Unlike the variable-size draft's places, nothing is inherited: a type is fixed-size or it is not, and `Fixed(T)` applies to all of `T`.

### 4. A value is a region, and its container knows where it is

**A data type's guard holds a region of the sequence: where the value starts, resolved when the guard is made, and how long it is, with writes and splices at offsets relative to that start, checked against its length.**
A container hands each child a subregion and keeps, in memory, where each child starts relative to itself: by multiplication for fixed-size children, by an index otherwise.
A child that changes size reports the change to its container, which updates its index and reports its own change to its container in turn, up to the root.
The guard chain, made from the root down for each mutation as it is today, adds the relative offsets up into the start of the value it reaches.

So no value stores where it lies, and neither does the store: the store knows bytes, the container knows its children, and the chain composes the two.
The alternative, a global index of anchors in the store, one for every value whose position anyone may ask for, would cost a node of an order-statistics tree per value, where a container's index costs four bytes per variable-size element and nothing per fixed-size one.
It would also teach the store about values, which nothing else requires it to know.
kladde-rs's `variable-size` branch already builds the mechanism for packed values, as `Place`, `Link` and `Node` in kladde-persist, with an index of element starts in the packed vector.

Making a guard then costs a lookup per level of nesting, `O(depth · log fanout)` in all, and so does reporting a change of size.

### 5. The order of the sequence lives in links between spans

**On file, the sequence is a chain of spans: a span is a statement that shows a run of the sequence's bytes — bytes in a data page, bytes in the statement itself, or zeros — and names the span after it, and the header names the first.**
This is the piece table that [the splices draft](splices.md#d-linked-spans-over-backings-the-recommendation) recommends for long allocations, applied to the only sequence there is.
The draft compares it with the alternatives, measured on kladde-svg's drawings; with one sequence, each alternative fares worse than it did there:

| order recorded by | a splice states | a move states | what else it needs |
| --- | --- | --- | --- |
| offsets, as today | everything behind it, `O(n)` | everything between its ends | nothing |
| chunks of bounded size, ordered by key | a chunk's tail | the moved run | relabeling when two adjacent keys leave no room between them |
| dense position keys | one or two spans | the moved run | a tombstone per removed span, kept while older statements of its key exist |
| a tree of spans on file | the path to the root, `O(log n)` | two paths | nothing |
| **links** | **one to three spans** | **three to six spans** | a chain walk at open, and a rope in memory |

A move, which today's design makes cheap between allocations with [`Move`](move-op.md), stays cheap only with links, and costs at most six statements, however many spans it moves.
Cutting a run out at its start and its end and inserting it at its destination splits at most three spans, one at each of those places.
Each split states its left part again, with a shorter length, and its right part as a new span over the same bytes.
The three left parts are also exactly the spans whose successors change: the one before the run now links to what followed the run, the run's last span to what follows the destination, and the span before the destination to the run's first span.
Where a place is the sequence's start, the header's first span changes instead, which costs no statement.
Every span inside the run keeps its statement, since neither its bytes nor its successor change, and the reach rule of [decision 6](#6-one-statement-per-span-the-newest-wins-reachability-decides-the-rest) asks nothing of it, since the chain reaches it before and after.
Where a place already falls between two spans, its split goes, and a move between such places states three spans.
A splice is the same argument with one place fewer: one to three statements.
A tree's random access, the reason to keep a rope on file, is worth nothing to a file that is read whole; the rope belongs in memory, where it finds the span that holds a position in `O(log k)` for `k` spans.

### 6. One statement per span; the newest wins; reachability decides the rest

**A span's newest statement is its whole description: no statement shows part of a span, and a span is part of the sequence exactly while the chain reaches it.**
Liveness is then one bit per statement: a statement is live if it is the newest of a span the chain reaches.
There are no fragments, no pins that count them, no size statements, no tombstones and no count of mentions, and dropping any other statement can never change what the file holds.
A page rewrite restates each live span in it under the span's own id, and no statement elsewhere changes its meaning.

A writer needs one rule, the analogue of today's [coverage rule](../spec/address-table.md#the-coverage-rule): **a span that the chain reaches at the end of an epoch but did not reach at the end of the epoch before is stated in that epoch.**
Without it, a writer could link in a span id whose newest statement a page rewrite had dropped, and an older statement of the same id would show its stale bytes.
With it, span ids can be recycled at once.

The price is that an overwrite cannot shadow part of a long span: it splits the span, or writes it again whole, which [choice A](#a-overwrites-split-rewrite-or-shadow) weighs.

### 7. The schema and the implementation's state are chains of their own

**The header names three chains: the sequence, the schema, and the implementation's private state, which is today's [consolidator state](../spec/file-format.md#the-consolidator-state).**
The journal edits only the sequence.
The schema changes only when a file is created or upgraded, which a flush does before the application gets the file, and the private state is written only by flushes, so no journal record needs to say which chain it edits.
The sequence's positions then start at the root value's first byte, and a file whose fingerprint matches never decodes its schema.

The schema could instead be a prefix of the sequence, which would work as well; it would offset every position by the schema's length, and a reader would decode the schema to find where the root begins.
Chains cost nothing extra in the span format, so the schema takes one, and so would [segments](#segments-later).

## Choices without a clear winner

### A. Overwrites: split, rewrite, or shadow

**Three ways to overwrite bytes inside a span, of which the format should offer the first two and leave the third out until measurements ask for it.**

- **Split**: cut the span in three around the written bytes.
  It costs three statements the first time a place is written, and one for every later overwrite of the same bytes, which restates the middle span.
  It leaves the description fragmented until [compaction](#b-how-short-spans-may-stay) merges the pieces.
- **Rewrite**: write the whole span again with the new bytes.
  It costs one statement and the span's length in data bytes, and fragments nothing; it suits short spans, and is costly for long ones.
- **Shadow**: a second kind of statement that overwrites part of a span by offset, as today's content statements overwrite part of an allocation.
  It costs one statement and writes no other byte, and it brings back per-byte resolution, fragments inside spans, and liveness that counts them: most of what decision 6 removes.

A writer would rewrite a span shorter than a threshold and split a longer one, so that a drag that moves a point many times splits its span once and restates an inline span of eight bytes at every later flush.
Shadowing is the option to revisit if overwrites inside long, cold spans turn out to dominate a workload; today's design pays one statement for them, and splitting pays three, once.

### B. How short spans may stay

**Two policies bound the description, and they combine: one bounds it everywhere at a constant cost per edit, the other drives it toward nothing where the sequence has gone cold.**

- **Bounded spans**, the discipline of a B-tree's leaves without the interior nodes: no two neighbouring spans are shorter than `L` together.
  After each flush, the spans around each edited place that break the rule are written again as one, which costs at most `L` bytes per boundary removed and never cascades, since a merged span is longer than either neighbour was.
  The mean span is then at least `L / 2` long, everywhere and at all times.
- **Merging by temperature**: a run of spans whose content has not been written for a while is written again as spans of up to a page.
  Each boundary is removed once, after the edits that made it, and a cold span of a page costs about six bytes, 0.15 % of what it shows.
  It needs a temperature per region of the sequence, as today's [heat and `last_written`](../impl/in-memory-state.md#3-the-allocation-map) give one per allocation.

The first gives the worst-case bound in [the guiding principle](#the-guiding-principle-overhead-and-work-independent-of-size), the second the low overhead of a large file that is mostly cold.
What `L` should be, and what counts as cold, are for measurements: a larger `L` makes the description smaller and each edit dearer.
Neither touches the format, which allows spans of any length from one byte to a page.

### C. What a container keeps to find its children

**A container of variable-size elements has four ways to keep where they start, and the frequent operation decides between them.**

| index | finding an element | a size change | an insertion | memory per element |
| --- | --- | --- | --- | --- |
| an array of starts | `O(1)` | `O(m)` | `O(m)` | 4 bytes |
| a Fenwick tree of sizes | `O(log m)` | `O(log m)` | `O(m)` | 4 bytes |
| a counted B-tree | `O(log m)` | `O(log m)` | `O(log m)` | about 8 bytes |
| a start every 64 elements | `O(64)` | `O(m / 64)` | `O(m)` | 1/16 byte |

The `variable-size` branch's packed vector keeps an array of starts.
A Fenwick tree is the better default for kladde-types' vector, since a size change of an element, typing into a string inside it, say, is the frequent case and costs `O(m)` in the array, while an insertion costs `O(m)` in the `Vec` that holds the elements anyway.
A struct keeps no index at all: it adds up its fields' encoded sizes after its first variable-size field, and every container caches its own total, so that adding up never recurses.

### D. Moving a value from one container to another

**Moving a value within one container is one `Move` record and three to six span statements, whatever its size; moving it between containers has no answer yet as good.**
Today a value that owns allocations moves by writing its pointers, and what they point to stays where it is.
In one sequence, every value is its bytes, so restacking a group, reordering a list, or sorting it are `Move`s inside their container, and moving a subtree to another parent is the question:

- **Encode it again**: remove the value, and insert its encoding where it goes.
  The journal and the flush carry all of its bytes; it is simple, and costs `O(size)`.
- **A move at a common ancestor**: a guard method on the container that holds both places.
  Rust's borrows forbid holding guards on two places at once, so the ancestor would have to take both paths, which is awkward to type and to derive.
- **Cut and paste**: `cut` moves the value's bytes into a scratch chain and returns a detached value; `paste` moves them in where it goes.
  It costs two moves of three to six statements each, however large the value, and needs a chain number in `Move` records, and a rule for a detached value that is dropped without being pasted, whose bytes the next transaction or the next open would splice out of the scratch chain.

Encoding again is the starting point; cut and paste is worth building once a workload moves large subtrees between parents.

## The guiding principle: overhead and work independent of size

**The live pages exceed the sequence by three terms — the description, the garbage, and the page framing — and each is held at a target that does not depend on the sequence's length, at a cost per modification that does not either.**

### Where the bytes of live pages go

Let `n` be the length of the sequence (with the schema's and the private state's, which are small), `k` the number of spans the chains reach, `c` the mean encoded size of their statements without inline payloads, and `L̄ = n / k` the mean length of a span.
The live pages hold:

- every byte of the sequence once, in a data page or as an inline payload in a table page, and zeros nowhere: at most `n`;
- `k` statements: `k · c = n · c / L̄`;
- garbage `G`: bytes of data pages that no reached span shows, and dead statements in table pages;
- the [page framing](../spec/file-format.md#page-framing), 0.37 %, and whatever a writer leaves unfilled at the ends of pages.

So live pages come to about `n · (1 + c / L̄ + G / n + 0.004)`.

### Each term, and what keeping it costs

**The description, `c / L̄`, is set by the span policy.**
[Bounded spans](#b-how-short-spans-may-stay) keep `L̄ ≥ L / 2`, so `c / L̄ ≤ 2c / L`: with statements of 8 bytes and `L = 256`, at most 6 %.
Each modification creates a constant number of span boundaries, at most two for a splice or an overwrite and three for a move, and removing one costs at most `L` bytes written again and one statement, so the policy costs `O(L)` bytes per modification, amortized.
Merging cold regions into spans of a page lowers the term for them to about 0.15 %, once per region and cooling.

**The garbage, `G / n`, is set by the cleaner's target.**
Holding pages at a mean live fraction `u` leaves `G ≤ (1 / u − 1) · n`, 11 % at `u = 0.9`.
In steady state, garbage arrives no faster than bytes are written, and a cleaner that takes the emptiest page first takes one at most `u` full, so each byte of garbage it reclaims costs it at most `u / (1 − u)` live bytes moved, 9 at `u = 0.9`, plus their spans restated, `c / L̄` per byte.
That is the classic bound for log-structured cleaning (Rosenblum and Ousterhout, "The Design and Implementation of a Log-Structured File System", SOSP 1991), and uniformly random overwrites are its worst case; skewed workloads clean far more cheaply, which is what today's ripeness and survivor sorting exploit.

**Everything else is constant or logarithmic.**
A splice, an overwrite and a move state a constant number of spans; a moved byte restates its span and nothing else, since no key changes; the rope finds and edits spans in `O(log k)`; a guard finds its value in `O(depth · log fanout)`; victims come from bucket queues in `O(1)`, and their spans from a reverse index in `O(1)` each; and the table pages' directory changes `O(depth)` pages per leaf added or removed, amortized over the statements a leaf holds, as today.

So the overhead is `c / L̄ + G / n + 0.004`, set by `L` and `u`, and the work per modification is `O(w + L)` bytes for `w` bytes written and `O(log n)` time, none of it growing with the file.

### Why today's design cannot

**Today the description is paid per allocation and a splice per allocation's tail, so keeping either small makes the other grow.**
A small description wants values inline in few allocations, and then each allocation is long, and a splice into one restates or rewrites everything behind it: on kladde-svg's small layout of the world map, [24 bytes per edit](splices.md#what-a-splice-costs-today) restated from leaves and 16 written again, growing with the length of the lists.
Cheap splices want short allocations, and then each value pays its pointer and statements, a cost that grows with the number of values rather than with their bytes.
No policy escapes that, because the format ties the unit of description to the unit of the data model; the proposal unties them.

### A worked example

Arithmetic, not a measurement: a sequence of 100 MB, of which 1 MB is being edited.

| part | spans | statement bytes | share of `n` |
| --- | --- | --- | --- |
| 99 MB cold, in spans of a page | 24,300 | 146 KB, at 6 bytes each | 0.15 % |
| 1 MB hot, in spans of 128 bytes on average | 8,200 | 82 KB, at 10 bytes each | 0.08 % |
| garbage, pages kept 90 % live | | | 11 % |
| page framing | | | 0.4 % |
| **live pages** | | | **about 112 %** |

At `u = 0.85` the garbage takes 18 %, and the cleaner moves at most 5.7 bytes per byte written instead of 9.
In either case, it is the cleaner's target, and not the description, that decides where between 10 and 20 % the file lands.

## The design in detail

### The encoding of the sequence

**Every type has a packed encoding, which a reader decodes front to back from the type's descriptor; a type whose encodings have a largest size also has a fixed one, which `Fixed(T)` selects.**

| kind | packed encoding | fixed-size when packed? |
| --- | --- | --- |
| `u8`, `i8`, `bool` | one byte | yes |
| `u16`, `u32`, `u64` | unsigned LEB128 | no |
| `i16`, `i32`, `i64` | zigzag, then LEB128 | no |
| `f32`, `f64` | IEEE-754, little-endian | yes |
| `char` | UTF-8 | no |
| Struct | its fields' encodings, concatenated | if all its fields are |
| Enum | the discriminant as LEB128, then the variant's fields | if every discriminant is below 128 and every variant encodes to the same size |
| `Array(T, n)` | `n` elements | if `T` is |
| `Sequence(T)` | the number of elements as LEB128, then the elements | no |
| `Text` | the number of bytes as LEB128, then UTF-8 | no |
| Opaque | its bytes, prefixed with their number as LEB128 unless the type declares a fixed size | if it declares one |
| `Fixed(T)` | `T`'s fixed encoding: integers at full width, little-endian, a `char` in 4 bytes, an enum padded with zeros to its largest variant, a struct as its fields' fixed encodings | yes |

`Fixed(T)` is defined for every `T` without a `Sequence`, a `Text`, or a variable-size opaque type inside it, and it applies to all of `T`.
A map is a `Sequence` of entries in the order its library keeps them, an `Option` an enum, and a recursive type nests inline, a tree as sequences of trees.
The canonical form is the variable-size draft's: minimal varints, valid UTF-8, and zeros as padding.

**The descriptors change accordingly.**
`Sequence`, `Text` and `Fixed` are new kinds, the reserved `Array` kind takes the encoding above, and an opaque descriptor gains its size mode.
The reserved `Pointer` kind is not needed, nor are the variable-size draft's `Small`, `Packed` and `Slotted`: with nothing to spill into, every short string and short list is inline, as a small value's content is there, and so is every long one.
Containers are described structurally, which settles [the container problem](../spec/tooling.md#the-container-problem) by its first option, and the fixed-size property is derived from the descriptors, so a reader and a tool compute it as a writer does.

### The journal

**Three records, on positions in the sequence:**

```
record  := write | splice | move
write   := 1:byte position:varint bytes
splice  := 2:byte position:varint removed:varint bytes
move    := 3:byte from:varint length:varint to:varint
bytes   := length:varint byte{length}
```

- `Write` overwrites `[position, position + length)`.
- `Splice` replaces `removed` bytes at `position` with `bytes`, shifting what follows.
- `Move` cuts `[from, from + length)` and inserts it at `to`, a position in the sequence without the cut bytes.

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

**One kind of statement, in three forms:**

| form | shows |
| --- | --- |
| `Ref(id, length, address, next)` | `length` bytes at `address`, inside one data page |
| `Inline(id, payload, next)` | its payload, 1 to 126 bytes |
| `Zero(id, length, next)` | `length` zeros |

`id` is the span's id, `next` the id of the span after it, or none at the end of a chain.
Zeros are rare in packed encodings, but `Fixed` values and large preallocated arrays have them, and they cost the format a tag.

**Resolution:**

1. A span id's statement in the highest epoch is its description; an epoch holds at most one per id.
2. Each chain runs from the span its header field names, following `next`, and holds its spans' content in that order; a header field of 0 names an empty chain.
3. A file is invalid if a chain names an id that no statement describes, reaches a span twice, or shares a span with another chain.

**Rules for writers:**

- at most one statement per span id and epoch;
- the reach rule of [decision 6](#6-one-statement-per-span-the-newest-wins-reachability-decides-the-rest): a span reached at the end of an epoch and not at the end of the one before is stated in that epoch;
- no two reached spans show the same byte of a data page, a writer invariant that readers do not depend on, as [today](../spec/address-table.md#design-directions).

### Encoding of statements

```
; `content` field of a table page:
table_page := child_ref* 0:byte statement*
statement  := id_delta:varint tag:byte body next?
; bit 0 of tag: 1 if the next span is id + 1, and no `next` field follows;
;               0 if a `next` field follows the body
; tag >> 1 = 0:        Ref     body := length:varint address_delta:varint
; tag >> 1 = 1:        Zero    body := length:varint
; tag >> 1 = 2..127:   Inline  body := payload:byte{(tag >> 1) - 1}
next       := varint   ; 0: the chain ends here; else zigzag(next - id)
```

Statements in a page are sorted by id, so `id_delta` is at least 1.
`address_delta` is the zigzag of the address minus where the previous `Ref` in the page ended, or minus 0 for the first.
`next` never needs to name the span itself, which would be a cycle, so 0 is free to end the chain.

A span written together with its neighbours — by a merge, by evacuation, or when the content was new — gets the next id, and its data follows theirs in the page, so it takes 5 bytes: one each for the id delta, the tag and the address delta, and two for the length.
A span with a successor elsewhere takes one to three more, and an inline span two plus its payload.
A statement's framing is at most 22 bytes.

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

- **The span table** is an array indexed by span id: what the span shows — a page and an offset, an inline payload's place in a table page, or zeros — its length, its successor, and the table page that holds its live statement, about 16 bytes per span.
- **The rope** is a counted B-tree over the sequence's spans in order, each entry a span id and its length, each node the total length below it.
  It finds the span that holds a position, splits one, and cuts and inserts runs, each in `O(log k)`.
  The schema's and the private state's chains are short and written only by flushes, so a list serves them.
- **The page table** is as today: kind, epoch, coverage, and buckets for victim selection.
  A data page's coverage counts the bytes reached spans show in it; a table page's, the live statements' bytes and their inline payloads.
- **The reverse index** lists, for each data page, the spans that show bytes in it, for [evacuation](../impl/consolidation.md#finding-the-referrers-of-a-data-page).
- **The span id allocator** hands out the lowest free id, and runs of consecutive free ids where a flush states consecutive spans.

The fragment map, the statement slab, the allocation map, the recyclable set, pins and `mentions` all go.
A statement is live exactly while it is the span table's statement for a span the rope holds, so liveness needs no counter at all.

### Opening a file

1. Read the headers and take the governing one, as today.
2. Read every table page the header reaches, and keep, for each span id, the statement of the highest epoch, in the span table; no sorting is needed.
3. Walk each chain from its head, building the rope, and refuse the file on a missing id, a span reached twice, or a span two chains share; count each reached span's statement and data toward its pages' coverage.
4. Read the data pages the reached spans show.
5. Decode the sequence through a cursor that follows the rope: the root type's `decode` reads front to back, and every container builds its index of children as it goes.
6. Replay the journal.

That is `O(statements + n)`, as today, and simpler: no sweep resolving bytes across epochs, no clipping at sizes, no tombstones.

### The flush

**The fold is today's, over one sequence instead of one per allocation.**
It keeps a piece table over the sequence, starting as one piece, the committed sequence; `Write`, `Splice` and `Move` edit it, and at the end it lists the new sequence as committed ranges, at the positions they had after the last flush, and new bytes in the arena.

**The flush then maps pieces to spans.**
A committed range is shown by the spans that showed it, found through the rope; where a range begins or ends inside a span, the span is split, its left part stated again with a shorter length and its right part a new span over the same bytes.
New bytes become new spans, inline below the [threshold](../impl/flush.md#the-inline-threshold) and placed in data pages above it.
Every span is linked to the one that now follows it, and every span whose content, length or successor changed is stated, with every new one.
A splice that lands in content the same flush wrote costs nothing beyond it, as [today](../impl/flush.md#what-falls-out-unasked) for an allocation written in the same flush.

**Around each edited place, the span policy then merges what it wants merged**, [choice B](#b-how-short-spans-may-stay), reading the bytes of committed spans from their data pages.
Consolidation, placement, [the header as write buffer](../impl/flush.md#the-header-as-write-buffer) and the commit follow as today, with heat kept per span, and evicted runs of spans contiguous in id.

### Consolidation

**The three debts of [today's picture](../impl/consolidation.md#the-high-level-picture) remain, and each gets simpler.**

- **Page fragmentation of data pages**: evacuating a victim writes the bytes of its reached spans into new pages and states each span again with its new address; nothing else changes, since no span's id does.
  Neighbouring short spans in a victim can be merged in the same pass, at no extra write.
- **Page fragmentation of table pages**: a page rewrite states each live span in it again, under its own id; there is no size to state again and no tombstone to carry, so the [one correctness obligation](../impl/address-table-operations.md#stating-the-size-again--the-one-correctness-obligation) of a rewrite goes.
- **Description fragmentation** becomes a property of regions of the sequence rather than of allocations: runs of short spans, found where edits happen and by a rotating walk over the rope, which replaces [the walk over the fragment map](../impl/consolidation.md#the-rotating-window).
  Merging a run is [description defragmentation](../impl/consolidation.md#description-defragmentation-rides-the-rotating-window) without the zeros it materializes today, since no span has gaps: it pays when the statement bytes it retires exceed `μ` per byte it writes.
  The walk can also give a run of spans consecutive ids, so that their successors become implicit, which plays the part today's window plays for delta encoding.
- **Outer fragmentation**, truncation, [compaction mode](../impl/consolidation.md#compaction-mode) and [pacing](../impl/consolidation.md#pacing) stay as they are.

### The API for application authors

**Unchanged in shape: `Kladde<T>`, guards, the derive, transactions and batches.**
What changes is the menu of containers.
There is one vector, one string and one map, all packed, and no small or packed variants beside them; an author who wants a fixed-size element or field writes `Fixed<u32>` or `Fixed<Shape>`.
Vectors gain `move_element`, which reorders without writing an element's bytes.
`remove` still returns the value, and `delete` is `remove` that drops it: both splice its bytes out of the file, and neither has anything to free.

### The API for data types

**A data type encodes, decodes, and mutates its region; it never allocates, frees, or knows a position.**

```rust
pub trait Persistable: Sized {
    /// `Some(n)` if every value encodes to `n` bytes.
    const FIXED_SIZE: Option<usize>;

    fn encoded_len(&self) -> usize;
    fn encode(&self, out: &mut Vec<u8>);
    fn decode(input: &mut Decoder<'_>) -> Result<Self, Error>;

    type Guard<'r, B: WriteBackend>: Guard<Persistable = Self>;
    fn guard<'r, B: WriteBackend>(&'r mut self, region: Region<'r, B>) -> Self::Guard<'r, B>;

    fn describe(builder: &mut SchemaBuilder) -> TypeDescriptor;
}
```

A `Region` is the value's substring: `len()`, `write(at, bytes)`, `splice(at, removed, bytes)`, `move_bytes(from, len, to)`, and `child(at, len, node, index)`, which hands out a subregion whose changes of size are reported to `node`, the container's index, as the `variable-size` branch's `Link` and `Node` report them.
Every offset is relative to the region's start, and every method refuses one outside it.
A child borrows its parent region mutably, so the parent cannot write while a child exists, and a sibling cannot move it.

A push onto a vector of variable-size elements, for instance, splices the element in at the end and rewrites the count, in one transaction, and only then changes memory:

```rust
pub fn push(&mut self, value: T) -> Result<(), Error> {
    let n = self.vec.items.len();
    let mut element = Vec::new();
    value.encode(&mut element);
    self.region.transaction(|r| {
        r.splice(r.len(), 0, &element)?;
        r.splice(0, varint_len(n), &varint(n + 1))
    })?;
    self.vec.sizes.push(element.len());
    self.vec.items.push(value);
    Ok(())
}
```

`store` and `free` go, since there is nothing to allocate and nothing to release, and with them `Location`, `UniquePointer` and [the one hole](../rust/pointers.md#the-one-hole) through which `load` could mint a second owner.
The derive generates a field's guard at a constant offset up to the struct's first variable-size field, and at the sum of the encoded sizes before it after that.

The obligations of a hand-written implementation become: `FIXED_SIZE` and `encoded_len` match what `encode` writes; `encode` and `decode` are inverses; every guard method leaves its region holding the value's encoding when its transaction ends; and the descriptor matches the bytes.
The third is checkable, which today's ordering discipline is only by replay: a store in a test can keep the sequence in memory and compare a region with the value's encoding after every method.

### The built-in containers

- **`PersistableVec<T>`**: the count, then the elements; in memory, a `Vec<T>`, and for variable-size `T` a Fenwick tree of the elements' sizes, [choice C](#c-what-a-container-keeps-to-find-its-children).
  A push and an insertion are a splice and a count, a removal likewise, a reordering a `Move`, and a size change of an element a report that updates one entry of the tree.
- **`PersistableString`**: the length in bytes, then UTF-8; in memory, a `String`, or a rope for long text, which then needs no container of its own, since the file already takes splices in long text at constant cost.
- **`PersistableMap<K, V>`**: the count, then the entries in insertion order; in memory, a map from key to slot, slots in entry order with their sizes in a Fenwick tree, and removed slots kept as holes of size zero until the map compacts them, in memory only.
- **`PersistableBlob<T>`**: opaque, its serde payload prefixed with its length.

### Evolution

**Reading a foreign schema works as the variable-size draft describes for packed values; writing in it does not, so an upgrade rewrites the sequence.**
The per-type read plan becomes a parse of the writer's encoding, run during the load that decodes everything anyway.
But a guard that wrote in the writer's layout would have to encode every value by the writer's descriptors at run time, so the *retain* mode of [evolution](../spec/schema/evolution.md#what-is-settled) is out of reach, and the *upgrade* mode becomes the only one: a file opened with a new schema is rewritten whole, schema chain and sequence, in one flush before the application gets it.
That flush needs no journal: a crash before it commits leaves the old file, and the next open upgrades again.
It costs `O(n)` once per change of schema.

### Tooling and testing

- **A tool decodes the whole file from the schema chain**: every container is structural, there is no pointer graph to follow, and nothing can leak, so the leak checker of [conformance](../spec/conformance.md#the-suite) has nothing to look for.
- **A differential oracle becomes one comparison**: after every operation, the sequence must equal the canonical encoding of the in-memory root, byte for byte.
- **The store can be tested on its own** against a plain `Vec<u8>`: any series of `Write`, `Splice` and `Move`, flushes, reopens and simulated power cuts must leave the chain holding the vector's bytes.

### Segments, later

**Loading part of a file means skipping a subtree without decoding it and without reading its spans, and one sequence offers neither.**
A [segment](segments.md) would therefore be a chain of its own, named from the sequence by a reference: one coarse kind of pointer, reintroduced deliberately, for exactly the subtrees an application declares.
The format already has three chains, so a fourth kind changes the header's fields and a descriptor kind, not the span machinery; whether references to segments may move, and how their transactions relate to the sequence's, is as open as everything else about segments.

## What it costs

- **Positions in memory.**
  Every container of variable-size elements keeps an index, four bytes per element, where today's slotted containers keep none; fixed-size elements cost nothing, and the store's own structures lose their entries per allocation.
- **Offsets at run time.**
  A field behind a variable-size field, and an element of a container of variable-size elements, are found by a sum or a lookup instead of a constant.
- **Guards cost a walk.**
  Making one costs `O(depth · log fanout)` where today it costs a constant per level.
- **Overwrites inside long spans split them**, three statements the first time a place is written, against today's one.
- **Two records for every insertion and removal**, the element and the count, in one transaction.
- **Moving a value between containers encodes it again**, until [cut and paste](#d-moving-a-value-from-one-container-to-another) exists.
- **A schema change rewrites the sequence**, once, and the retain mode of evolution goes.
- **Partial loading needs segments**, which need a pointer of their own.
- **A rewrite of everything above the pages**: the address table, allocations and journal documents of the specification, most of the implementation documents, and the store, persist, types and derive crates of kladde-rs.

## Compared with today

| | today | proposed |
| --- | --- | --- |
| statements per splice | every fragment behind it in its allocation, or its bytes written again | one to three spans |
| moving a value within a container | the moved bytes, or [`Move`](move-op.md)'s statement edits | three to six spans, no bytes |
| statements per overwrite | one | three the first time, one after |
| description per value | a pointer, its statements, and its size | none; a few bytes per span of up to a page |
| what a page rewrite must also state | the sizes and tombstones of the ids it touches | nothing |
| liveness | pins over fragments, size pins, tombstones, `mentions` | the newest statement of a reached span |
| the store's memory | fragment map, statement slab, allocation map, page table | span table, rope, page table |
| removing a value | a splice, then a free walk over what it owns | a splice |
| field access when mutating | constant offsets | constant up to the first variable-size field |
| opening | a sweep resolving bytes across epochs | one pass over statements, a chain walk, and the decode |
| tooling | opaque containers, a pointer graph | a decode from the schema |

## How to get there

kladde-rs is a testbed, so the way to judge this is to build it on a branch, in four stages, each tested before the next:

1. **The store**: chains of spans, the three journal records, the fold, the flush, consolidation and open, behind a byte-sequence interface, tested against a `Vec<u8>` with reopens and simulated power cuts.
2. **Persist**: `Region`, `Node`, `encode`, `decode`, `FIXED_SIZE`, `Fixed<T>`, and the derive.
3. **Types**: the vector, the string, the map and the blob.
4. **kladde-svg on top**, measured against main's slotted, packed and small layouts: file size, bytes written per edit, flush time, open time, and memory.

The specification and the implementation documents would change only once those measurements hold.

## Open questions

- **The span policy's constants**: `L`, what counts as cold, and how hot spans and the header share the address table's room.
- **Where to split and where to rewrite** on an overwrite, and whether shadowing statements earn their way back for some workload.
- **The containers' indexes**: whether a Fenwick tree is the right default, and what an application with millions of variable-size elements should use.
- **Moving values between containers**: whether cut and paste is worth a scratch chain and a chain number in `Move`.
- **Memory for very large files**: the rope costs about 16 bytes per span plus the span table's 16; for a file of a gigabyte that is cold, spans of a page keep that near 8 MB, but a file edited everywhere at once would not be.
- **Opaque types**: whether declaring a fixed size is enough, or whether some want both modes.
- **Upgrades of large files**: whether rewriting the whole sequence at the first open with a new schema is acceptable, or whether some schema changes should be expressible as edits.
- **Segments**: how a reference to another chain looks in the schema, in the journal, and in a transaction.
