---
title: Porting kladde to C++ and into Inkscape
---

What a second implementation, [kladde-cpp](../cpp/), and its first application, the Inkscape fork inkscape-kladde, found out about kladde: where the specification left a second implementer guessing, where C++ made the design harder than Rust does, and what it took to make an existing application durable.

**The format held up: kladde-cpp, written from the specification and kladde-rs's store, reads and writes files that kladde-rs reads and writes, in both directions, and the typed layers agree on fingerprints once both name an enum's variants alike.**
The obstacles were elsewhere.
The specification was silent on a dozen points where two implementers could each be right and disagree, which the port turned into rules; C++ cannot enforce the exclusive access that Rust's handles rely on, so the C++ handles carry rules the compiler does not check; and Inkscape needed long-lived handles and a way to observe its document rather than replace it, both of which kladde-cpp now has.

The commits measured are kladde-rs `5d9ff9f` on its branch `cpp`, kladde-cpp `e8a3e61`, and inkscape-kladde `f81602b406`; the commands are at the end.

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
The typed SVG schema, planned for the branch `typed`, faces the same choice, and would face it harder: Inkscape's objects (`SPObject` and its subclasses) are built from the XML tree and keep their own state, so a typed schema would be a third copy, or a rewrite of the object layer.

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

**Backing a document costs Inkscape 3 to 5 µs and about 24 bytes of journal per edit, and a tenth more memory; the files, though, are far larger than the drawings, because the journal's budget is sized for files far larger than these.**

`test_kladde-backing-bench` imports three drawings of [kladde-rs's corpus](tools.md) — the tiger (67 KB of SVG), the coat of arms of Spain (193 KB) and the world map (1.1 MB) — and makes a drawing program's edits through Inkscape's XML tree: drags of twenty moves of one element (eight in ten), style changes, reorders, duplicates and deletions.
It runs the same edits on the same drawing without a backing, for what Inkscape spends on them itself, and checks that the closed file reopens as the document was.
Times are means on one shared machine of four cores; the heap is `mallinfo2`'s, before and after.

| drawing | SVG | document's heap | backing's heap | edit, plain | edit, backed | journal per edit | file after 50,000 edits | closed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tiger | 67 KB | 4.2 MB | 0.42 MB | 2.9 µs | 7.0 µs | 23 B | 1.26 MB | 1.28 MB |
| coat of arms | 193 KB | 5.9 MB | 0.78 MB | 4.0 µs | 6.3 µs | 25 B | 1.54 MB | 1.56 MB |
| world map | 1.1 MB | 29 MB | 2.8 MB | 2.4 µs | 6.3 µs | 24 B | 2.44 MB | 2.48 MB |

**An edit costs a few microseconds more, and stays that cheap however long the journal grows.**
Over 10,000 edits the backed document took 4.8 to 7.7 µs per edit, and over 50,000, 6.3 to 7.0 µs: none of it waits for the disk, since the journal is appended without an `fsync`, and the one flush in 50,000 edits is amortized over them.
The first measurement found the cost growing with the journal, from 7 µs per edit to 18 to 34: each append copied the journal's list of cuts, which every attribute change extends, so a journal cost time quadratic in its length; both implementations now append in constant time.
Saving the whole drawing as SVG, which an application without kladde does on every save, takes 2.4 to 20 ms here, and writes the whole file.

**The second copy of the document costs a tenth of what Inkscape holds for it**: 0.42 MB beside the tiger's 4.2 MB, and 2.8 MB beside the world map's 29 MB, since Inkscape's tree, its objects, their styles and their rendering structures dwarf an encoding that holds each node's name, attributes and children once.

**The files are many times the drawings**: the tiger's grew to 1.26 MB over 50,000 edits, nineteen times its SVG, and closing left it there.
The journal's budget, 256 pages or 1 MiB, lets a journal grow to fifteen times the tiger's live size before a flush folds it, and the pages a flush frees become reusable only after the next one, so a small file is mostly journal and its fallback.
Files of 50 KB to 10 MB are what kladde is meant for, so this is the regime it must serve well, and it argues for the open question of [Transactions and batches](../impl/transactions-and-batches.md#open-questions): a budget that scales with the file, or a flush when the journal outgrows the sequence it describes, rather than a fixed one.
The idle flush does not help here, as its threshold is a share of the same budget.

**Opening a kladde file takes as long as parsing the SVG**: 23 to 27 ms for the tiger against 16 to 22 ms to parse it, and 113 to 176 ms for the world map against 138 to 144 ms; the time goes to building Inkscape's document from the tree, as from a parsed SVG.

**In the GUI, under a virtual display**, opening `tiger.svg` created `tiger.kladde` beside it and left the SVG as it was; flipping and rotating the whole drawing through Inkscape's actions, then killing Inkscape without saving, left a file that the command line exports with the flip and the rotation in place.
The *File ▸ New* dialog, which asks where the new document's file goes, was not exercised, since no test drives a file chooser.

## Bugs the port found in kladde-rs

The port read kladde-rs's store line by line, and found five things that are wrong rather than different; [the implementation notes](../impl/implementation-notes.md#where-the-implementation-falls-short) record each.

- ⚠️ **A single write of 4 GiB or more corrupts the journal**, since a record's length is kept as a `u32`.
- **Arithmetic on values read from the file is unchecked**, so a file whose CRCs match but whose numbers do not panics or wraps rather than being refused.
- **`FileStorage` takes no lock**, so two processes can open one file.
- **A cut that escapes a transaction rolled back by `Store::atomically` can paste another record's bytes.**
- **`TypeTable::fixed_size` shared one memo between plain sizes and fixed forms**, so a type met inside a `Fixed` and outside one got one size for both; fixed in `d19a613`.
- **The first version of [the seal](../spec/journal.md#seals) could replay a lost journal after a power cut**, since the page it named was cleared without an `fsync` before the seal was appended; porting it found this, and both implementations and the specification now sync first.
- **An append copied the journal's list of cuts**, making a journal quadratic in its length, as [the measurements](#measurements) found.

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
KLADDE_BENCH_STEPS=50000 KLADDE_SVG_CORPUS=…/kladde-rs/crates/kladde-svg/corpus/drawings ./bin/test_kladde-backing-bench
```
