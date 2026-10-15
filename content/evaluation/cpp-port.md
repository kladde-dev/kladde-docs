---
title: Porting kladde to C++ and into Inkscape
---

What a second implementation, [kladde-cpp](../cpp/), and its first application, the Inkscape fork inkscape-kladde, found out about kladde: where the specification left a second implementer guessing, where C++ made the design harder than Rust does, and what it took to make an existing application durable.

**The format held up: kladde-cpp, written from the specification and kladde-rs's store, reads and writes files that kladde-rs reads and writes, in both directions, and the typed layers agree on fingerprints once both name an enum's variants alike.**
The obstacles were elsewhere.
The specification was silent on a dozen points where two implementers could each be right and disagree, which the port turned into rules; C++ cannot enforce the exclusive access that Rust's handles rely on, so the C++ handles carry rules the compiler does not check; and Inkscape needed long-lived handles and a way to observe its document rather than replace it, both of which kladde-cpp now has.

The port's findings are of kladde-rs `5d9ff9f` on its branch `cpp`, kladde-cpp `e8a3e61` and inkscape-kladde `f81602b406`; [the measurements](#measurements) are of kladde-cpp `c40ff8f` and inkscape-kladde `6452817ae3` (branch `main`, the typed schema) and `04e6ea0d31` (branch `untyped`, attribute values as text), with kladde-rs `f2f29a7` for the canonical SVG; the commands are at the end.

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
*What it does not cost:* durability is the same, since each change is recorded before the call that made it returns, and the file is the same, since it holds the same schema either way.
The [typed SVG schema](#what-the-typed-schema-took) faces the same choice, and would face it harder: Inkscape's objects (`SPObject` and its subclasses) are built from the XML tree and keep their own state, so a typed schema would be a third copy, or a rewrite of the object layer.

**Three details needed care:**

- **Inkscape changes a document's tree while building it**, before anything can observe it: it adds a named view and version attributes as it creates a document from a file.
  So attaching the observer first compares Inkscape's tree with the file's and writes the difference, and any later event for a node the file does not know rewrites the file from the tree, a fallback that the tests never reach.
- **Saving must not overwrite the kladde file with SVG.**
  A backed document's filename is its kladde file, so the title bar names it; the commands bar and the File menu offer no *Save*, and Ctrl+S only flushes the file, while *Save As* still writes SVG wherever the user chooses, and an imported `.svg` is left as it was.
- **Only the GUI imports.**
  Opening an `.svg` in the GUI creates `foo.kladde` next to it, asking first if that file exists; the command line, which Inkscape uses for conversions and exports, opens files as before, so that `inkscape --export-type=png foo.svg` leaves no `foo.kladde` behind.

**The idle flush fits Inkscape's main loop**: after each recorded change, a low-priority idle callback calls `flush_if_journal_exceeds(0.5)`, at the same priority Inkscape gives its own deferred work, so that a flush, and its `fsync`, falls between gestures rather than in one.
[The seal](../spec/journal.md#seals) and the commit on a worker thread are in kladde-rs and kladde-cpp, off by default, until the demo shows whether the idle flush suffices.

## Measurements

**inkscape-kladde costs Inkscape about 5 µs and 19 bytes of journal per edit, and a sixth more memory; it keeps a drawing in a file of 1.2 to 2.2 times its encoding while the drawing is edited, which for the coat of arms and the world map is no larger than the drawing's canonical SVG, and right after the import in a file of 1.03 to 1.4 times its encoding.**

`test_kladde-backing-bench` imports three drawings of [kladde-rs's corpus](tools.md) — the tiger (67 KB of SVG), the coat of arms of Spain (193 KB) and the world map (1.1 MB) — and makes a drawing program's edits through Inkscape's XML tree: drags of twenty moves of one element (eight in ten), style changes, reorders, duplicates and deletions.
It runs the same edits on the same drawing without a backing, for what Inkscape spends on them itself, and checks that the closed file reopens as the document was.
The fork stores a drawing in a typed schema, on its branch `main`: each element's attributes are one ordered list of entries, each a typed value — lengths, paints, colors with their written form, transform lists, path data with its layout, points, style declarations, decimal numbers, embedded data as its bytes — or the attribute's text, where formatting the typed value would not reproduce Inkscape's text byte for byte; 98.4 to 99.8% of the drawings' attributes end up typed.
The numbers are the typed schema's, at the journal budget sized to the file that kladde uses by default, unless they say otherwise; [the XML schema](#ablation-attribute-values-as-text) of the branch `untyped`, which keeps every attribute value as text, serves as an ablation.
Each number is one run on a shared machine of four cores, and a range spans a run of 10,000 edits and one of 50,000; the heap is `mallinfo2`'s, before and after.

| drawing | SVG | document's heap | backing's heap | edit, plain | edit, backed | journal per edit | file after the import (encoding) | closed after 10,000 / 50,000 edits (encoding) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tiger | 67 KB | 4.2 MB | 0.64 MB | 2.0–3.0 µs | 7.4–7.6 µs | 19 B | 60 KB (42 KB) | 112 / 136 KB (52 / 65 KB) |
| coat of arms | 193 KB | 5.9 MB | 1.12 MB | 2.1–2.2 µs | 7.8–7.9 µs | 20 B | 112 KB (96 KB) | 196 / 284 KB (113 / 149 KB) |
| world map | 1.1 MB | 29 MB | 4.4 MB | 2.1 µs | 8.0–10.5 µs | 19 B | 612 KB (594 KB) | 712 / 900 KB (604 / 642 KB) |

**An edit costs about 5 µs more, and stays that cheap however long the journal grows.**
The journal is appended without an `fsync`, and the flush that folds it, once every 400 to 500 edits, is amortized over them; the world map's 10.5 µs came from a run of 10,000 edits whose slowest move took 23 ms, an outlier its run of 50,000 did not repeat.
An early version spent time quadratic in the journal's length, since each append copied the journal's list of cuts, which every attribute change extends; both implementations now append in constant time.
Saving the whole drawing as SVG, which an application without kladde does on every save, takes 1.2 to 12.7 ms here, and writes the whole file.

**The second copy of the document costs a sixth of what Inkscape holds for it**: 0.64 MB beside the tiger's 4.2 MB, and 4.4 MB beside the world map's 29 MB, since Inkscape's tree, its objects, their styles and their rendering structures dwarf an encoding that holds each node's name, attributes and children once.

**Payload pages halve the file right after the import.**
Importing is one splice of the whole encoding, whose bytes [payload pages](../impl/transactions-and-batches.md#payload-pages) write once, into pages that the flush adopts as data pages.
Without them, the bytes went into the journal first and then into data pages, and the journal's pages became holes below them: kladde-cpp `c4ebcb2` left 100 KB, 204 KB and 1204 KB for the same three imports, where `c40ff8f` leaves 60 KB, 112 KB and 612 KB.
After thousands of edits the difference is gone: closed after 10,000 edits, the files were 112 KB, 200 KB and 696 KB without payload pages, since consolidation drains the import's holes along the way.

**While a drawing is edited, its file stays within 1.2 to 2.2 times its encoding**, which holds the journal, the pages a flush keeps for the header before it, and those a flush frees but can reuse only after the next one; closing flushes and truncates, so a closed file is about as large as the open one was.
The drawing's canonical SVG is the drawing as the edits leave it, saved by Inkscape and written again by [kladde-svg](tools.md) with less whitespace, shortest decimals and shortened path data; it takes 89 / 115 KB, 212 / 286 KB and 1107 / 1192 KB after 10,000 / 50,000 edits.
Next to it, the kladde file is 1.2 to 1.3 times as large for the tiger, whose fixed overhead weighs most beside its small content, 0.9 to 1.0 times for the coat of arms, and 0.6 to 0.8 times for the world map, whose numbers and path data the typed schema stores more compactly than their text.

**Opening a kladde file takes 1.3 to 2.3 times as long as parsing the SVG**: 17 to 21 ms for the tiger against 11 to 12 ms to parse it, 31 to 46 ms for the coat of arms against 19 to 20 ms, and 127 to 147 ms for the world map against 93 to 99 ms.
Most of the time goes to building Inkscape's document from the tree, as from a parsed SVG; formatting the typed values back into Inkscape's text adds a little, since with values kept as text a file opens in 0.9 to 1.8 times the parsing time.

### What the typed schema took

**What the typed schema needed, beyond keeping attribute values as text, says more than its numbers**:

- **An existing application's formatting must be stored as data.**
  kladde-svg normalizes what it parses, and would have lost nearly everything Inkscape writes; reproducing it took decimal numbers rather than floats, a color's written form, a transform's optional arguments, and how a path's text separates its numbers — and Inkscape's own `10e-4` and `-0` still fall back to text.
- **A `Mut`'s whole-value `set` records the whole encoding**, where `std::string`'s records only the changed middle, so changing one number of a long path rewrote the path; the branch diffs the encodings itself, and kladde-cpp should offer it for every type, as [the specification guarantees](../spec/index.md) for Text and Opaque.
- **A `std::variant` with many alternatives of one type** — `x`, `y`, `width` and twenty more are all lengths — cannot be reached through `if_holds<A>`, which needs an index-based form, and its names must be written out alike.
- **A `std::vector` of varint numbers is refused**, since they are not fixed-size, so short lists that are only ever replaced whole carry a `kladde::Vector`'s index.
- **Adding a typed attribute changes the root fingerprint**, so that files written before no longer open; growing a typed schema over time needs [evolution](../spec/schema/evolution.md), which neither implementation has yet.
- **Embedded data is held as its bytes**: a `data:` URI with base64 keeps its media type and decoded bytes, three quarters of its text, and only the line breaks inside the base64 do not read back; none of the three drawings embeds data.
- **Under the observer, a typed copy is a third copy of the document**, beside Inkscape's XML strings and its objects; a typed schema pays off fully only where it replaces the application's own model, which in Inkscape would mean rewriting its object layer.

### The journal's budget

**kladde sizes the journal's budget to the file, which keeps these files within 1.2 to 2.2 times their encoding at a flush of about a millisecond every 400 to 500 edits; a fixed budget of 256 pages, which almost never flushes, lets the tiger's file grow to 16 times its encoding.**
The budget is 2% of the sequence in whole pages, at least 2 and at most 10 (`Options::journal_budget_pages` left empty, in both implementations), which gives the tiger and the coat of arms 2 pages and the world map 3.
The runs below are the benchmark above, with `KLADDE_JOURNAL_BUDGET_PAGES` set for the fixed budgets; the ratios are to the encoding after the edits, and the latencies are those of each single move of a drag, which in the benchmark includes any flush the move triggers, since nothing flushes at idle there.

| budget | drawing | closed after 10,000 / 50,000 edits | flushes per 10,000 edits | mean edit | drag move, 99.9th percentile | slowest move |
| --- | --- | --- | --- | --- | --- | --- |
| sized to the file (2 pages) | tiger | 112 KB (2.2×) / 136 KB (2.1×) | 23 | 7.4–7.6 µs | 1.0–1.1 ms | 3.2 ms |
| sized to the file (2 pages) | coat of arms | 196 KB (1.7×) / 284 KB (1.9×) | 25 | 7.8–7.9 µs | 1.1 ms | 4.3 ms |
| sized to the file (3 pages) | world map | 712 KB (1.2×) / 900 KB (1.4×) | 20 | 8.0–10.5 µs | 1.3–1.7 ms | 23 ms |
| 1 page | tiger | 108 KB (2.1×) / 140 KB (2.2×) | 47 | 9.1–10.5 µs | 0.9–1.1 ms | 3.9 ms |
| 1 page | coat of arms | 220 KB (1.9×) / 268 KB (1.8×) | 50 | 10.9–12.1 µs | 1.1–1.2 ms | 4.2 ms |
| 1 page | world map | 744 KB (1.2×) / 896 KB (1.4×) | 48 | 9.9–10.7 µs | 1.0–1.2 ms | 4.3 ms |
| 4 pages | tiger | 128 KB (2.5×) / 156 KB (2.4×) | 11 | 7.4–9.8 µs | 1.5–1.6 ms | 4.0 ms |
| 4 pages | coat of arms | 188 KB (1.7×) / 288 KB (1.9×) | 12 | 7.5–8.1 µs | 0.4–1.6 ms | 4.1 ms |
| 4 pages | world map | 692 KB (1.1×) / 888 KB (1.4×) | 11 | 7.2–7.7 µs | 0.1–1.9 ms | 8.8 ms |
| 256 pages | tiger | 272 KB (5.2×) / 1040 KB (16×) | 0 | 5.2–6.2 µs | 46–54 µs | 0.9 ms |
| 256 pages | coat of arms | 340 KB (3.0×) / 1144 KB (7.7×) | 0 | 5.0–5.2 µs | 35–36 µs | 4.2 ms |
| 256 pages | world map | 836 KB (1.4×) / 1660 KB (2.6×) | 0 | 5.1–6.4 µs | 42–49 µs | 3.5 ms |

The world map's slowest move of 23 ms at the budget sized to the file came from its run of 10,000 edits, whose run of 50,000 peaked at 3.0 ms.
At 256 pages, the 50,000 edits never filled the journal, which is why no move stalled for long there; the next edit would have folded a journal of a megabyte, as [the ablation](#ablation-attribute-values-as-text) shows, whose larger records fill it once in 50,000 edits.

**The budget trades a few long stalls for many short ones, and the short ones are the better deal for an editor.**
A median move costs 3.1 µs at every budget: the move itself, recorded in the journal.
With 256 pages, a move almost never meets a flush, but the one that does folds a journal of a megabyte and stalls for 46 to 77 ms, three to five frames; with 4 pages, one move in seven hundred meets a flush of a few pages, which takes one to two milliseconds and fits within a frame.
The flushes here run inside the moves because the benchmark has no idle time; in the GUI, the idle flush moves most of them between gestures, and does so better the smaller the journal it has to fold.
The times come from a disk whose `fsync` is fast; on a slow disk, every flush costs more, and the small budget's frequent flushes cost proportionally more than its few large ones save, which is the case for [the commit on a worker thread](../impl/transactions-and-batches.md#a-flush-off-the-callers-thread).

**Hence a budget that follows the file, between a floor and a ceiling, rather than a fixed one.**
A budget of 1 page gains nothing in size over 2 to 4 here, and flushes two to four times as often; above a few pages, the files grow with the journal and its fallback while the flushes saved bring little, since each flush is also a chance to consolidate.
[Transactions and batches](../impl/transactions-and-batches.md#the-journals-budget) gives the rule and its reasons.

### Ablation: attribute values as text

**Keeping every attribute value as text, as the branch `untyped` does, makes the encoding 1.5 to 1.8 times larger, the files 1.5 to 1.7 times larger and an edit's journal a fifth larger; in exchange it takes two thirds of the typed schema's memory and a third to a half of its import time, and opens a little faster.**
The branch is `main` with the XML schema in place of the typed one, and the runs are the same.

| | tiger | coat of arms | world map |
| --- | --- | --- | --- |
| encoding, typed / text | 42 / 64 KB | 96 / 172 KB | 594 / 1071 KB |
| backing's heap, typed / text | 0.64 / 0.41 MB | 1.12 / 0.77 MB | 4.4 / 2.7 MB |
| import, typed / text | 6.7 / 3.1 ms | 8.8–8.9 / 3.4–4.5 ms | 44–49 / 15–19 ms |
| opening, typed / text (parsing the SVG) | 17–21 / 15–20 ms (11–17 ms) | 31–46 / 28–31 ms (18–20 ms) | 127–147 / 112–125 ms (93–122 ms) |
| journal per edit, typed / text | 19 / 23–24 B | 20 / 23–24 B | 19 / 23–24 B |
| mean edit, typed / text | 7.4–7.6 / 7.1 µs | 7.8–7.9 / 7.2–7.4 µs | 8.0–10.5 / 6.4–7.2 µs |
| file after the import, typed / text | 60 / 80 KB | 112 / 188 KB | 612 / 1092 KB |
| closed after 10,000 / 50,000 edits, typed | 112 / 136 KB | 196 / 284 KB | 712 / 900 KB |
| closed after 10,000 / 50,000 edits, text | 164 / 232 KB | 300 / 460 KB | 1192 / 1544 KB |
| canonical SVG after 10,000 / 50,000 edits | 89 / 115 KB | 212 / 286 KB | 1107 / 1192 KB |

**With values as text, a kladde file is 1.1 to 2.0 times the canonical SVG, where the typed schema's is 0.6 to 1.3 times it**: both hold the drawing's numbers, but as text they keep Inkscape's whitespace and digits, which the canonical SVG drops and the typed schema never stores.
Per edit, the two cost the same within the noise of the machine, though a median move costs 3.1 µs typed against 2.6 µs as text, which is parsing and formatting the attribute's value.
The journal budget behaves as with the typed schema, the world map getting 5 pages for its larger encoding: at the budget sized to the file the files stay within 1.1 to 2.1 times their encoding, at a flush every 300 to 900 edits.
At 256 pages its larger records fill the journal once in 50,000 edits, and the move that meets that flush stalls for 46 to 77 ms while it folds a megabyte of journal, where no move at the budget sized to the file took longer than 4.1 ms.

### In the GUI

**In the GUI, under a virtual display** (inkscape-kladde `f81602b406`, with the XML schema), opening `tiger.svg` created `tiger.kladde` beside it and left the SVG as it was; flipping and rotating the whole drawing through Inkscape's actions, then killing Inkscape without saving, left a file that the command line exports with the flip and the rotation in place.
The *File ▸ New* dialog, which asks where the new document's file goes, was not exercised, since no test drives a file chooser.

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
# (created_KB is the file right after the import, file_KB and closed_KB the file before and after closing, edited_KB the encoding after the edits)
KLADDE_BENCH_STEPS=50000 KLADDE_SVG_CORPUS=…/kladde-rs/crates/kladde-svg/corpus/drawings ./bin/test_kladde-backing-bench
# … with a fixed budget, and keeping the edited drawings for their canonical SVG's size (kladde-rs's svg-roundtrip, column canon_bytes)
KLADDE_JOURNAL_BUDGET_PAGES=4 KLADDE_BENCH_SAVE_DIR=edited KLADDE_SVG_CORPUS=… ./bin/test_kladde-backing-bench
cargo run --release -p kladde-svg --bin svg-roundtrip -- --csv canon.csv edited
```
