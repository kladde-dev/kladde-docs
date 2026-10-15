---
title: Porting kladde to C++ and into Inkscape
---

What a second implementation, [kladde-cpp](../cpp/), and its first application, the Inkscape fork inkscape-kladde, found out about kladde: where the specification left a second implementer guessing, where C++ made the design harder than Rust does, and what it took to make an existing application durable.

**The format held up: kladde-cpp, written from the specification and kladde-rs's store, reads and writes files that kladde-rs reads and writes, in both directions, and the typed layers agree on fingerprints once both name an enum's variants alike.**
The obstacles were elsewhere.
The specification was silent on a dozen points where two implementers could each be right and disagree, which the port turned into rules; C++ cannot enforce the exclusive access that Rust's handles rely on, so the C++ handles carry rules the compiler does not check; and Inkscape needed long-lived handles and a way to observe its document rather than replace it, both of which kladde-cpp now has.

The commits measured are kladde-rs `5d9ff9f` on its branch `cpp`, kladde-cpp `e8a3e61`, and inkscape-kladde `f81602b406`, and for the journal budget sized to the file, kladde-rs `9b08ed5`, kladde-cpp `ac37fb4`, and inkscape-kladde `ad3a6cf1a4` (branch `main`, the typed schema) and `b1409f8da9` (branch `untyped`, the XML schema), and for closing into a consolidated file, kladde-rs `02e9cfb`, kladde-cpp `c4ebcb2`, and inkscape-kladde `c6b731e204` (`main`) and `b93c50aa61` (`untyped`); the commands are at the end.

## What was built

- **The schema layer**, from [the specification](../spec/schema/), checked against the [fingerprint vectors](../spec/schema/fingerprints.md#vectors).
- **The store**, ported from kladde-rs's `kladde-store` module by module, consolidation included, and tested as kladde-rs tests it: against a plain byte vector after every operation ([the differential oracle](../impl/flush.md#correctness-the-differential-oracle)), with simulated power cuts at every write since the last `fsync`, and with kladde-rs's consolidation thresholds.
- **The typed layer**, header-only, as [the draft](../drafts/cpp.md) designed it, with the departures that [kladde-cpp's page](../cpp/index.md#where-kladde-cpp-departs-from-the-draft) lists.
- **The Inkscape demo**: documents backed by a kladde file in an XML schema, with no save button.

## Two implementations, one format

**Files are format-compatible in both directions, but not byte-identical.**
Fifty seeds of a random workload with many flushes, consolidation, cuts and pastes, and an unflushed journal, written by one implementation, read the same in the other, which then edits them and hands them back.
The bytes differ because kladde-rs iterates hash sets where the order picks consolidation's victims, and kladde-cpp sorts there instead; [the fold](../spec/journal.md#folding) and consolidation are free to choose, so both files are right.

**The typed layers agree on a document only once the C++ type names an enum's variants as the Rust type does.**
The cross-implementation test writes a drawing — a struct holding text, a vector of shapes, a map, an option and an integer — from each language and reads it in the other.
The shapes are a Rust enum `Shape { Circle { r }, Poly(Vec<Point>), Label(String), Empty }`, and the C++ type `std::variant<Circle, std::vector<Point>, std::string, kladde::Unit>`; thanks to [variants that hold one value](../spec/schema/type-descriptors.md#enum), the two have the same structure, descriptor for descriptor, and the encodings agree byte for byte.
But the fingerprints differed: a variant's name is part of the fingerprint, and a `std::variant` names its alternatives after their types — `Value`, `Text` — where Rust names them `Poly` and `Label`.
`KLADDE_VARIANT_NAMED` now gives the C++ type the names the file uses, and with it each implementation opens the other's file.

*What it means for the design.*
Names in the fingerprint are deliberate: [evolution](../spec/schema/evolution.md) reconciles by names, so two types that differ only in names are different types.
The cost shows in a language whose sum type has no names of its own, which C++'s `std::variant` is, and will be in Python's unions and TypeScript's too: a program that shares files with another must spell out the other's names.
That is a fair price, and kladde-cpp asks it only where a program shares files across languages; within one, the names it derives are stable.

## What the specification left open

**Each point below is one where kladde-rs and kladde-cpp, both following the documents, could have disagreed**, and most were settled while this was written.

**Settled in the specification:**

- **A schema table must be canonical to be read.**
  kladde-rs accepted a varint longer than it needed to be, enum variants in any order (re-sorting them, so that re-encoding changed the bytes) and two variants with one discriminant; kladde-cpp refused all three.
  [Canonical encoding](../spec/schema/canonical-encoding.md#descriptor-encoding) now requires the refusal, so that a table that decodes re-encodes to its own bytes, and kladde-rs refuses them too.
- **An Opaque's parameters are not containment**, so a type may reach itself through one; and **an enum whose every variant holds itself is legal**, if useless ([rules of nesting](../spec/schema/type-descriptors.md#rules-of-nesting)).
- **A `Move` makes no cut** that a `Paste` could name ([cuts](../spec/journal.md#cuts)).

**Still open**, for [the conformance suite](../spec/conformance.md) to decide:

- whether page 1 of a file that has only ever had one header must be a header page, as [the file format](../spec/file-format.md#header-pages) says "if present", or may be zeros, as both implementations leave it at creation;
- whether a header whose epoch has the wrong parity for its slot, or whose `journal_pointer` is below 2, makes the file invalid or is ignored, as both implementations ignore it;
- whether varints in journal records and table pages may be longer than they need to be, as both implementations accept;
- whether a trailer that matches its CRC but names page 0, page 1 or a page already in the journal ends the journal, as both implementations treat it, silently dropping what follows, or makes the file invalid;
- whether a transaction of no records is valid, and whether a file with a `format_version` other than 0 is refused.

**Where kladde-rs and the reference algorithms disagree**, kladde-cpp followed kladde-rs, and the disagreements are now in [the implementation notes](../impl/implementation-notes.md): the seed of the rotating walk, what the header evicts, who takes the cut's fillers, the order in which the budget loop applies its floors, the depth of the span table, the fold's length check, and the bound on a `Ref`.

## What C++ made harder

**The store ported line by line; the typed layer did not.**
The store is positions and bytes, and [the reference algorithms](../impl/) describe it closely enough that a port follows kladde-rs's structure, with one mechanical change: every fallible function returns `kladde::expected<T>`, and `KLADDE_TRY` does what Rust's `?` does.
The typed layer is where the languages part.

### Handles that nothing checks

**Rust's borrow checker enforces two rules that C++ handles can only document.**

- *A handle is valid until something is written through a handle that is not itself or made from it.*
  In Rust a `Mut` borrows its parent exclusively, so a stale one does not compile; in C++ a stale `kladde::Mut` writes at the wrong position.
  This is the iterator-invalidation rule C++ programmers already keep, and the debug checking the draft proposed — a stack of live handles in the store — is not built yet.
- *A handle must not move while a handle made from it lives*, since the child refers to its parent's place.
  Rust's borrows pin the parent for free; in C++, `auto [a, b] = c.parts();` is fine and returning a handle by value is fine, by guaranteed copy elision, but moving a handle into a container while its children are in use is not.
- *A cut is pasted at most once.*
  kladde-rs's `CutId` is neither `Copy` nor `Clone`, so the type system enforces it; kladde-cpp's is a plain struct, and the store's check of it holds only while the cut's journal lasts.

### Describing types without a derive

**One description per type, written by a macro today and by reflection later, drives everything else, as the draft proposed, and it works.**
`KLADDE_STRUCT(Contact, email, phones, starred)` expands to a `constexpr` description found by argument-dependent lookup, and to a mixin that gives `Mut<Contact>` the methods `email()`, `phones()` and `starred()`.
Two compiler problems cost time, both now in the code as comments:

- a `static constexpr auto` member initialized through argument-dependent lookup inside a constrained partial specialization crashes GCC 13 with an internal error, the compiler that Inkscape builds with on Ubuntu 24.04; a `static constexpr` function avoids it;
- a static member of `durable<T>` cannot be computed by calling `durable<T>`'s own functions, which are not defined until the class is complete, so the sizes and flags that follow from a struct's fields are computed in a helper class of their own.

**Recursive types need care that Rust's lazy constant evaluation does not.**
`kladde::Box<T>` cannot report `T`'s fixed size, since a recursive type reaches itself through a box and asking would instantiate the type inside itself; it reports none, which costs nothing on file and refuses only a `std::vector` of boxes.

### Long-lived handles

**Inkscape needs handles that live as long as the node they point to, and kladde-cpp's `kladde::List` and `kladde::Ref` give them**, as [Values and their positions](../impl/values.md#handles-that-outlive-edits-elsewhere) describes.
Each element of a list is an *anchor* that knows its list and its index; a list knows the anchor of the value that holds it, and finds its own offset inside that value by walking the value's fields up to itself, each of which knows its encoded size in constant time.
So a write through a `Ref` finds its place in `O(depth · log m)`, and its change of size climbs the same path, updating each list's Fenwick tree.

One restriction falls out: **a `List` may sit inside structs, variants, options, boxes and other lists, but not inside a `kladde::Vector`, a `std::vector` or a `kladde::Map`**, whose indexes would not hear of a change made through a `Ref`; the compiler refuses it.
A document tree, whose children are lists all the way down, never needs it.

## What Inkscape needed

**inkscape-kladde observes Inkscape's XML tree and mirrors every change into the kladde file, rather than replacing the tree's nodes with durable ones, as the draft proposed.**
The draft's `KladdeNode` would have implemented `Inkscape::XML::Node` over the durable tree, so that the document lives in memory once.
But Inkscape's nodes are garbage-collected objects of the class `SimpleNode` and its subclasses, created through the document's factory methods and copied, compared and merged by code that assumes them; replacing them touches the XML layer's every user.
A subtree observer on the document node, by contrast, hears every change — attributes, content, insertions, removals, reorderings and renamings — and needs nothing else of Inkscape.

*What the observer costs.*
The document is held twice in memory, once as Inkscape's tree and once as kladde values, and a change is applied twice; [the measurements below](#measurements) put a number on it.
*What it does not cost:* durability is the same, since each change is recorded before the call that made it returns, and the file is the same, since it holds the XML schema either way.
The [typed SVG schema](#a-typed-svg-schema) faces the same choice, and would face it harder: Inkscape's objects (`SPObject` and its subclasses) are built from the XML tree and keep their own state, so a typed schema would be a third copy, or a rewrite of the object layer.

**Three details needed care:**

- **Inkscape changes a document's tree while building it**, before anything can observe it: it adds a named view and version attributes as it creates a document from a file.
  So attaching the observer first compares Inkscape's tree with the file's and writes the difference, and any later event for a node the file does not know rewrites the file from the tree, a fallback that the tests never reach.
- **Saving must not overwrite the kladde file with SVG.**
  A backed document's filename is its kladde file, so the title bar names it and *Save* flushes it; *Save As* still writes SVG wherever the user chooses, and an imported `.svg` is left as it was.
- **Only the GUI imports.**
  Opening an `.svg` in the GUI creates `foo.kladde` next to it, asking first if that file exists; the command line, which Inkscape uses for conversions and exports, opens files as before, so that `inkscape --export-type=png foo.svg` leaves no `foo.kladde` behind.

**The idle flush fits Inkscape's main loop**: after each recorded change, a low-priority idle callback calls `flush_if_journal_exceeds(0.5)`, at the same priority Inkscape gives its own deferred work, so that a flush, and its `fsync`, falls between gestures rather than in one.
[The seal](../spec/journal.md#seals) and the commit on a worker thread are in kladde-rs and kladde-cpp, off by default, until the demo shows whether the idle flush suffices.

## Measurements

**Backing a document costs Inkscape 3 to 5 µs and about 24 bytes of journal per edit, and a tenth more memory; a fixed journal budget of 256 pages, though, makes the open files far larger than the drawings, which is why kladde sizes the budget to the file ([below](#the-journals-budget)), and closing a drawing replaces its file with the smallest one that holds it ([further below](#closing-into-a-consolidated-file)).**

`test_kladde-backing-bench` imports three drawings of [kladde-rs's corpus](tools.md) — the tiger (67 KB of SVG), the coat of arms of Spain (193 KB) and the world map (1.1 MB) — and makes a drawing program's edits through Inkscape's XML tree: drags of twenty moves of one element (eight in ten), style changes, reorders, duplicates and deletions.
It runs the same edits on the same drawing without a backing, for what Inkscape spends on them itself, and checks that the closed file reopens as the document was.
Times are means on one shared machine of four cores; the heap is `mallinfo2`'s, before and after.
The runs in this table used a fixed journal budget of 256 pages, and closed the file with `close()`, a flush followed by truncation.

| drawing | SVG | document's heap | backing's heap | edit, plain | edit, backed | journal per edit | file after 50,000 edits | closed by a flush |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tiger | 67 KB | 4.2 MB | 0.42 MB | 2.9 µs | 7.0 µs | 23 B | 1.26 MB | 1.28 MB |
| coat of arms | 193 KB | 5.9 MB | 0.78 MB | 4.0 µs | 6.3 µs | 25 B | 1.54 MB | 1.56 MB |
| world map | 1.1 MB | 29 MB | 2.8 MB | 2.4 µs | 6.3 µs | 24 B | 2.44 MB | 2.48 MB |

**An edit costs a few microseconds more, and stays that cheap however long the journal grows.**
Over 10,000 edits the backed document took 4.8 to 7.7 µs per edit, and over 50,000, 6.3 to 7.0 µs: none of it waits for the disk, since the journal is appended without an `fsync`, and the one flush in 50,000 edits is amortized over them.
The first measurement found the cost growing with the journal, from 7 µs per edit to 18 to 34: each append copied the journal's list of cuts, which every attribute change extends, so a journal cost time quadratic in its length; both implementations now append in constant time.
Saving the whole drawing as SVG, which an application without kladde does on every save, takes 2.4 to 20 ms here, and writes the whole file.

**The second copy of the document costs a tenth of what Inkscape holds for it**: 0.42 MB beside the tiger's 4.2 MB, and 2.8 MB beside the world map's 29 MB, since Inkscape's tree, its objects, their styles and their rendering structures dwarf an encoding that holds each node's name, attributes and children once.

**With a budget of 256 pages, the files are many times the drawings**: the tiger's grew to 1.26 MB over 50,000 edits, nineteen times its SVG, and closing it by a flush left it there.
A budget of 256 pages, or 1 MiB, lets a journal grow to fifteen times the tiger's live size before a flush folds it, and the pages a flush frees become reusable only after the next one, so a small file is mostly journal and its fallback.
Files of 50 KB to 10 MB are what kladde is meant for, so this is the regime it must serve well; the idle flush does not help, as its threshold is a share of the same budget.
[The journal's budget](#the-journals-budget) below measures smaller budgets, and the one sized to the file that both implementations use by default.

### A typed SVG schema

**Typing the attributes makes the encoding 34 to 45% smaller and an edit's journal a fifth smaller, at 1.5 to 1.8 times the XML schema's memory and twice its import time.**
inkscape-kladde's branch `main` keeps each element's attributes as one ordered list of entries, each a typed value — lengths, paints, colors with their written form, transform lists, path data with its layout, points, style declarations, decimal numbers — or the attribute's text, where formatting the typed value would not reproduce Inkscape's text byte for byte; 98.9 to 99.8% of the drawings' attributes end up typed.

| | tiger | coat of arms | world map |
| --- | --- | --- | --- |
| encoding, XML / typed | 64 / 42 KB | 172 / 96 KB | 1071 / 594 KB |
| backing's heap, XML / typed | 0.42 / 0.64 MB | 0.78 / 1.15 MB | 2.8 / 5.2 MB |
| journal per edit, XML / typed | 23 / 19 B | 25 / 20 B | 24 / 19 B |

Per edit, the two cost the same within the noise of the machine.
**What the typed schema needed, beyond the XML one, says more than its numbers**:

- **An existing application's formatting must be stored as data.**
  kladde-svg normalizes what it parses, and would have lost nearly everything Inkscape writes; reproducing it took decimal numbers rather than floats, a color's written form, a transform's optional arguments, and how a path's text separates its numbers — and Inkscape's own `10e-4` and `-0` still fall back to text.
- **A `Mut`'s whole-value `set` records the whole encoding**, where `std::string`'s records only the changed middle, so changing one number of a long path rewrote the path; the branch diffs the encodings itself, and kladde-cpp should offer it for every type, as [the specification guarantees](../spec/index.md) for Text and Opaque.
- **A `std::variant` with many alternatives of one type** — `x`, `y`, `width` and twenty more are all lengths — cannot be reached through `if_holds<A>`, which needs an index-based form, and its names must be written out alike.
- **A `std::vector` of varint numbers is refused**, since they are not fixed-size, so short lists that are only ever replaced whole carry a `kladde::Vector`'s index.
- **Adding a typed attribute changes the root fingerprint**, so that files written before no longer open; growing a typed schema over time needs [evolution](../spec/schema/evolution.md), which neither implementation has yet.
- **Under the observer, a typed copy is a third copy of the document**, beside Inkscape's XML strings and its objects; a typed schema pays off fully only where it replaces the application's own model, which in Inkscape would mean rewriting its object layer.

### The journal's budget

**kladde sizes the journal's budget to the file, and for these drawings that keeps the files, open or closed by a flush, within 1.1 to 3.4 times their live encoding, where a fixed budget of 256 pages lets them reach 2.3 to 26 times; with the typed schema, such a file is within 0.6 to 1.3 times the size of the drawing's canonical SVG.**
The budget bounds what a crash leaves, and what `close()` leaves; inkscape-kladde closes into a consolidated file instead, whose size no budget affects ([below](#closing-into-a-consolidated-file)).
The budget is 2% of the sequence in whole pages, at least 2 and at most 10 (`Options::journal_budget_pages` left empty, in both implementations), which gives the tiger and the coat of arms 2 pages and the world map 3 (typed) or 5 (XML).
It costs a flush of one to two milliseconds every 300 to 900 edits, and 3 to 6 µs more per edit on average than 256 pages, which almost never flush here.
The runs below are the benchmark above, with `KLADDE_JOURNAL_BUDGET_PAGES` set for the fixed budgets, on inkscape-kladde's branch `untyped`; the live size is the document's encoding before the edits, and the latencies are those of each single move of a drag, which in the benchmark includes any flush the move triggers, since nothing flushes at idle there.
The canonical SVG is the drawing as the edits leave it, saved by Inkscape and written again by [kladde-svg](tools.md) with less whitespace, shortest decimals and shortened path data; the edits, and so the canonical SVG, are the same at every budget and in both schemas.

| budget | drawing (live) | canonical SVG after 10,000 / 50,000 edits | file closed by a flush after 10,000 / 50,000 edits | flushes per 10,000 edits | mean edit | drag move, 99.9th percentile | slowest move |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sized to the file (2 pages) | tiger (64 KB) | 89 KB / 115 KB | 168 KB (2.6×) / 220 KB (3.4×) | 28 | 7.8–8.2 µs | 1.0–1.2 ms | 4.8 ms |
| sized to the file (2 pages) | coat of arms (172 KB) | 212 KB / 286 KB | 328 KB (1.9×) / 468 KB (2.7×) | 31 | 8.5–10.3 µs | 1.2–1.5 ms | 4.2 ms |
| sized to the file (5 pages) | world map (1071 KB) | 1107 KB / 1192 KB | 1196 KB (1.1×) / 1544 KB (1.4×) | 11 | 9.1–12.9 µs | 0.1–2.3 ms | 34 ms |
| 1 page | tiger | 89 KB / 115 KB | 156 KB (2.4×) / 208 KB (3.3×) | 58 | 9.8–11.6 µs | 1.0 ms | 23 ms |
| 1 page | coat of arms | 212 KB / 286 KB | 328 KB (1.9×) / 440 KB (2.6×) | 61 | 10.0–10.6 µs | 1.0 ms | 4.7 ms |
| 1 page | world map | 1107 KB / 1192 KB | 1276 KB (1.2×) / 1536 KB (1.4×) | 59 | 11.0–11.7 µs | 1.1 ms | 5.6 ms |
| 4 pages | tiger | 89 KB / 115 KB | 164 KB (2.6×) / 236 KB (3.7×) | 14 | 6.7–10.1 µs | 1.4–2.1 ms | 3.2 ms |
| 4 pages | coat of arms | 212 KB / 286 KB | 292 KB (1.7×) / 484 KB (2.8×) | 15 | 7.6–7.9 µs | 1.6 ms | 4.5 ms |
| 4 pages | world map | 1107 KB / 1192 KB | 1188 KB (1.1×) / 1536 KB (1.4×) | 14 | 8.2–9.1 µs | 1.3–2.1 ms | 5.6 ms |
| 256 pages | tiger | 89 KB / 115 KB | 408 KB (6.4×) / 1276 KB (20×) | 0 (1 in 50,000) | 4.3–6.4 µs | 46–61 µs | 50 ms |
| 256 pages | coat of arms | 212 KB / 286 KB | 664 KB (3.9×) / 1564 KB (9.1×) | 0 (1 in 50,000) | 4.8–5.8 µs | 42–43 µs | 48 ms |
| 256 pages | world map | 1107 KB / 1192 KB | 2172 KB (2.0×) / 2480 KB (2.3×) | 0 (1 in 50,000) | 5.2–6.5 µs | 46–51 µs | 65 ms |

The table above is the XML schema's; in it, a kladde file closed by a flush stays 1.1 to 1.9 times the canonical SVG at the budget sized to the file.
The world map's slowest move of 34 ms came from one run of 10,000 edits, whose 50,000-edit run peaked at 6.6 ms.
**The typed schema shows the same pattern, with files a quarter to a half smaller in bytes**, on its branch `main` with the same changes, and with the ratios to its smaller encodings a little higher:

| budget | drawing (live) | canonical SVG after 10,000 / 50,000 edits | file closed by a flush after 10,000 / 50,000 edits | flushes per 10,000 edits | mean edit | drag move, 99.9th percentile | slowest move |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sized to the file (2 pages) | tiger (42 KB) | 89 KB / 115 KB | 112 KB (2.7×) / 136 KB (3.2×) | 23 | 7.9–8.5 µs | 1.0–1.2 ms | 2.8 ms |
| sized to the file (2 pages) | coat of arms (96 KB) | 212 KB / 286 KB | 196 KB (2.0×) / 272 KB (2.8×) | 25 | 8.8–9.6 µs | 1.3 ms | 6.5 ms |
| sized to the file (3 pages) | world map (594 KB) | 1107 KB / 1192 KB | 704 KB (1.2×) / 888 KB (1.5×) | 20 | 9.4–11.1 µs | 1.8–1.9 ms | 3.8 ms |
| 1 page | tiger | 89 KB / 115 KB | 120 KB (2.9×) / 144 KB (3.4×) | 47 | 12.1–15.2 µs | 1.2–1.4 ms | 3.4 ms |
| 1 page | coat of arms | 212 KB / 286 KB | 200 KB (2.1×) / 256 KB (2.7×) | 49 | 16.0–16.1 µs | 1.5–1.6 ms | 7.3 ms |
| 1 page | world map | 1107 KB / 1192 KB | 748 KB (1.3×) / 872 KB (1.5×) | 48 | 13.2–15.2 µs | 1.5 ms | 3.9 ms |
| 4 pages | tiger | 89 KB / 115 KB | 124 KB (3.0×) / 152 KB (3.6×) | 11 | 7.7–8.5 µs | 1.6–1.7 ms | 4.0 ms |
| 4 pages | coat of arms | 212 KB / 286 KB | 192 KB (2.0×) / 304 KB (3.2×) | 12 | 8.5–9.7 µs | 0.2–1.8 ms | 5.0 ms |
| 4 pages | world map | 1107 KB / 1192 KB | 852 KB (1.4×) / 904 KB (1.5×) | 11 | 9.5–9.9 µs | 0.2–2.2 ms | 4.6 ms |
| 256 pages | tiger | 89 KB / 115 KB | 312 KB (7.4×) / 1080 KB (26×) | 0 | 5.3–5.9 µs | 45–64 µs | 0.9 ms |
| 256 pages | coat of arms | 212 KB / 286 KB | 432 KB (4.5×) / 1264 KB (13×) | 0 | 5.8–6.3 µs | 75–87 µs | 4.7 ms |
| 256 pages | world map | 1107 KB / 1192 KB | 1428 KB (2.4×) / 2272 KB (3.8×) | 0 | 5.8–6.3 µs | 49–55 µs | 3.5 ms |

**At the budget sized to the file, a typed kladde file closed by a flush is about the size of the canonical SVG**: 1.2 to 1.3 times it for the tiger, whose journal and fallback pages weigh most beside its small content, and 0.6 to 0.95 times it for the coat of arms and the world map, whose numbers and path data the typed schema stores more compactly than their text.

A typed edit journals about 19 bytes rather than 24, so the same budget flushes a fifth less often, and at 256 pages its 50,000 edits never fill the journal, which is why no move stalls for long there; the next one would fold a journal of a megabyte, as the XML schema's did.
A median move costs 3.4 to 3.5 µs, against the XML schema's 2.8, which is the parsing and formatting of the attribute's value.

**The budget trades a few long stalls for many short ones, and the short ones are the better deal for an editor.**
A median move costs 2.8 µs at every budget: the move itself, recorded in the journal.
With 256 pages, a move almost never meets a flush, but the one that does folds a journal of a megabyte and stalls for 50 to 65 ms, three or four frames; with 4 pages, one move in seven hundred meets a flush of a few pages, which takes one to two milliseconds and fits within a frame.
The flushes here run inside the moves because the benchmark has no idle time; in the GUI, the idle flush moves most of them between gestures, and does so better the smaller the journal it has to fold.
The times come from a disk whose `fsync` is fast; on a slow disk, every flush costs more, and the small budget's frequent flushes cost proportionally more than its few large ones save, which is the case for [the commit on a worker thread](../impl/transactions-and-batches.md#a-flush-off-the-callers-thread).

**Hence a budget that follows the file, between a floor and a ceiling, rather than a fixed one.**
A budget of 1 page gains little in size over 2 to 4, and flushes two to four times as often; above a few pages, the files grow with the journal and its fallback while the flushes saved bring little, since each flush is also a chance to consolidate.
[Transactions and batches](../impl/transactions-and-batches.md#the-journals-budget) gives the rule and its reasons.

**Opening a kladde file takes as long as parsing the SVG**: 23 to 27 ms for the tiger against 16 to 22 ms to parse it, and 113 to 176 ms for the world map against 138 to 144 ms; the time goes to building Inkscape's document from the tree, as from a parsed SVG.

**In the GUI, under a virtual display**, opening `tiger.svg` created `tiger.kladde` beside it and left the SVG as it was; flipping and rotating the whole drawing through Inkscape's actions, then killing Inkscape without saving, left a file that the command line exports with the flip and the rotation in place.
The *File ▸ New* dialog, which asks where the new document's file goes, was not exercised, since no test drives a file chooser.

### Closing into a consolidated file

**Closing a drawing into a fully consolidated file leaves its encoding and 10 to 16 KB more, whatever the journal's budget: as large as the canonical SVG with the XML schema, within 1.00 to 1.08 times it, and 0.55 to 0.72 times it with the typed one; closing the document takes 6 to 61 ms.**
inkscape-kladde closes every drawing with `close_consolidated()`, which [replaces the file with the smallest file that holds it](../impl/flush.md#closing-into-a-consolidated-file); a crash never gets there, so only an ordinary close pays for the rewrite.
The runs are the benchmark above, at the budget sized to the file, on both branches; the open file is the file before closing, about what `close()` would have left, and the closing time is that of destroying Inkscape's document, its own teardown included.

| schema | drawing | canonical SVG after 10,000 / 50,000 edits | encoding | open file | closed file | closing |
| --- | --- | --- | --- | --- | --- | --- |
| XML | tiger | 89 / 115 KB | 86 / 113 KB | 160 / 228 KB | 96 / 124 KB (1.08× / 1.08×) | 9 / 12 ms |
| XML | coat of arms | 212 / 286 KB | 209 / 284 KB | 328 / 468 KB | 220 / 296 KB (1.04× / 1.03×) | 12 / 20 ms |
| XML | world map | 1107 / 1192 KB | 1096 / 1188 KB | 1196 / 1544 KB | 1112 / 1204 KB (1.00× / 1.01×) | 53 / 61 ms |
| typed | tiger | 89 / 115 KB | 52 / 65 KB | 112 / 144 KB | 64 / 76 KB (0.72× / 0.66×) | 6 / 10 ms |
| typed | coat of arms | 212 / 286 KB | 113 / 149 KB | 196 / 288 KB | 124 / 160 KB (0.58× / 0.56×) | 9 / 15 ms |
| typed | world map | 1107 / 1192 KB | 604 / 642 KB | 688 / 900 KB | 620 / 656 KB (0.56× / 0.55×) | 45 / 38 ms |

**The closed file is the encoding, two header pages, the padding of the last data page and the pages' framing**, so the journal and the fallback pages that make the open file 1.1 to 1.9 times larger are gone.
With the XML schema, the closed file is as large as the canonical SVG, since both are essentially the drawing's text; the typed schema's encoding is the smaller one, so a typed kladde file ends up at a little over half the size of the canonical SVG, for the coat of arms and the world map.
Closing takes about a frame for the tiger and the coat of arms, and two to four frames for the world map, whose 1.1 MB the close writes again; the rewrite is linear in the drawing, and the budget is what keeps the open file, and what a crash leaves, small in the meantime.

## Bugs the port found in kladde-rs

The port read kladde-rs's store line by line, and found five things that are wrong rather than different; [the implementation notes](../impl/implementation-notes.md#where-the-implementation-falls-short) record each.

- ⚠️ **A single write of 4 GiB or more corrupts the journal**, since a record's length is kept as a `u32`.
- **Arithmetic on values read from the file is unchecked**, so a file whose CRCs match but whose numbers do not panics or wraps rather than being refused.
- **`FileStorage` takes no lock**, so two processes can open one file.
- **A cut that escapes a transaction rolled back by `Store::atomically` can paste another record's bytes.**
- **`TypeTable::fixed_size` shared one memo between plain sizes and fixed forms**, so a type met inside a `Fixed` and outside one got one size for both; fixed in `d19a613`.
- **The first version of [the seal](../spec/journal.md#seals) could replay a lost journal after a power cut**, since the page it named was cleared without an `fsync` before the seal was appended; porting it found this, and both implementations and the specification now sync first.
- **An append copied the journal's list of cuts**, making a journal quadratic in its length, as [the measurements](#measurements) found; fixing it took kladde-rs's `svg-bench` from 136,000 to 330,000 edits a second on the tiger, and from 90,000 to 238,000 on the coat of arms.

And it confirmed one already known: ⚠️ a file whose span table outgrows the header's references to leaves, at some two million spans, no longer opens, in kladde-rs and in kladde-cpp alike.

## Reproducing

```sh
# kladde-cpp: the three test suites, under AddressSanitizer and UBSan
cmake -S kladde-cpp -B kladde-cpp/build -G Ninja -DCMAKE_BUILD_TYPE=Debug -DKLADDE_SANITIZE=ON
cmake --build kladde-cpp/build
kladde-cpp/build/kladde-schema-tests && kladde-cpp/build/kladde-store-tests && kladde-cpp/build/kladde-typed-tests

# The typed cross-implementation test, in both directions
cargo run -p kladde-types --example cross_impl -- write rs.kladde
kladde-cpp/build/kladde-cross write cpp.kladde
kladde-cpp/build/kladde-cross check rs.kladde
cargo run -p kladde-types --example cross_impl -- check cpp.kladde

# The Inkscape demo's tests and measurements, in inkscape-kladde's build directory
ninja test_kladde-backing test_kladde-backing-bench
./bin/test_kladde-backing
# (closed_KB is the file closed into a consolidated file, file_KB the file before closing, edited_KB the encoding after the edits)
KLADDE_BENCH_STEPS=50000 KLADDE_SVG_CORPUS=…/kladde-rs/crates/kladde-svg/corpus/drawings ./bin/test_kladde-backing-bench
# … with a fixed budget, and keeping the edited drawings for their canonical SVG's size (kladde-rs's svg-roundtrip, column canon_bytes)
KLADDE_JOURNAL_BUDGET_PAGES=4 KLADDE_BENCH_SAVE_DIR=edited KLADDE_SVG_CORPUS=… ./bin/test_kladde-backing-bench
cargo run --release -p kladde-svg --bin svg-roundtrip -- --csv canon.csv edited
```
