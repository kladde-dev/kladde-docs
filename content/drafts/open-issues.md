---
title: Open issues
---

Two lists, both maintained by audit rather than by memory: where the documents outside [Superseded](../superseded/) disagree with each other, and what has to be settled before a first Rust prototype can be built.

The prototype in question is one that can **create a file, open it, check the schema, hand out a `Kladde<T>`, accept mutations and record them in the journal, and flush**.
[Segments](segments.md) are deferred, and so is anything that only affects how well it performs.

## Contradictions

Ordered most to least severe, where severity means *how likely this is to make someone write the wrong code*.

### 1. The journal is two different things

[Transactions and batches](../impl/transactions-and-batches.md#flush-triggers) treats the journal as a **fixed-capacity allocation**: it can overflow, it must be "grown" by being freed and re-allocated at a larger size, and an elaborate five-rule trigger policy exists to guarantee that growth only ever happens while it is empty.

[Durability](../spec/durability.md#the-flush-protocol) and [File format](../spec/file-format.md#pages) describe it as a **chain of `Journal` pages**, beginning at a page the header names, drawn from the same reusable-page pool as everything else.

These cannot both be true, and under the page model the entire policy answers a question that does not arise: "growing the journal" is taking one more reusable page.
The capacity question does not disappear — it becomes "how many pages may a segment consume before a flush is forced" — but it is a different and much simpler question, and the `committed`/`ready`/`transaction` cursor machinery is answering the old one.

This is first because it is the part a prototype has to build first, and because a reader could reasonably implement either.

### 2. Journal pages cannot satisfy the page framing

[Page framing](../spec/file-format.md#page-framing) requires **every** non-header page, `Journal` kind included, to carry a `content_size` and a `crc` over its content.
The same document then exempts journal appends from the whole-page-write rule, and [Durability](../spec/durability.md#what-is-guaranteed) has transactions appended incrementally without an fsync.

A page that is appended to incrementally cannot keep a valid CRC over its content unless every append rewrites the whole page — which is exactly what the exemption exists to avoid.
So either `Journal` pages are exempt from the framing as well as from the write rule, or appends do rewrite the page.
The documents say neither.

Note this also touches the recovery argument: [How much of the framing is load-bearing](../spec/file-format.md#how-much-of-the-framing-is-load-bearing) says only the header CRCs and the journal's *chained transaction* CRCs are load-bearing, which reads as though journal page CRCs are expected not to be meaningful — but it never says so.

### 3. The durability promise is stated unqualified where users read it

[Durability](../spec/durability.md#what-is-guaranteed) is careful: under a power cut, transactions issued after the last completed flush survive only up to a valid journal prefix, "minus whatever the operating system never wrote out", because journal appends are not fsynced.

The two places an application author actually reads make the unqualified promise:

- the [hub page](../index.md) — "an append-only on-disk journal that is durable by the time the mutating call returns";
- the [tutorial](../rust/tutorial/durability.md#what-a-crash-costs) — "**Nothing that has been acknowledged.** If `push` returned, the element is in the file."

Both are true for an application crash and false for a power cut.
This is severe not because it is subtle but because it is a promise, and because the tutorial is where someone decides whether kladde is safe enough for their use.

### 4. Framing is per-record in one place and per-transaction in another

[Journal](../spec/journal.md#framing) and [Conformance](../spec/conformance.md) frame **transactions**, with a chained epoch-salted CRC.
The [tutorial](../rust/tutorial/durability.md#what-a-crash-costs) frames **records**: "Every record carries a length prefix and a checksum."

A format-level disagreement, and the two imply different recovery code.
The tutorial's version also predates the chaining, so it describes recovery as truncating a torn record rather than as keeping the longest valid prefix.

### 5. `mentions` is claimed to have one reader and has at least three

[Liveness](../impl/liveness.md#the-last-tombstone) states flatly: "This is the only reader of `mentions`", meaning the release of a tombstone anchor's pin at `mentions == 1`.
[In-memory state](../impl/in-memory-state.md) repeats it as "one thing only".

But [Id recycling](../impl/id-recycling.md#the-refinements) reads it twice — to prefer ids whose tombstone is already dead, and as the tiebreak that argues for taking high-`mentions` ids first — and [Liveness](../impl/liveness.md#ranking)'s own ranking section reads it again, to score "dead statements whose removal would let a tombstone retire".

The claim matters because it is the argument for how cheap `mentions` is.
With three readers the field is harder to remove than the text suggests, and anyone auditing whether it can be dropped will reach the wrong conclusion.

### 6. Reclamation is optional and also required

[Allocations](../spec/allocations.md#reclamation-is-not-part-of-this-specification) says whether an implementation reclaims garbage is unconstrained, and that one which never consolidates is conforming.

[Guarantees](../spec/index.md#guarantees-an-implementation-must-provide) says "reclamation is incremental by construction" as though it were a property the spec asserts, and [Headroom](../spec/durability.md#headroom) makes it a **must** that an implementation reserve enough slack "that a nearly-full disk cannot deadlock the very consolidation that would free space" — a requirement phrased around a mechanism the spec elsewhere declines to require.

The resolution is probably that *incrementality* is required of whatever reclamation an implementation does, while *doing any* is not — but as written the three passages disagree.

### 7. Conversion is gone, and the flush still reasons about it

Sizedness conversion was removed along with sizedness, taking the `Convert` record with it ([Pointers](../rust/pointers.md#what-sizedness-was-and-why-it-is-gone)).

[The flush](../impl/flush.md#frees-first-is-a-preference-not-an-edge) still uses it as the motivating example in three load-bearing places: the three-cycle that forces frees-first to be a priority rather than an edge, the [content-blind fast path](../impl/flush.md#the-content-blind-fast-path)'s flag condition, and the [implementation order](../impl/flush.md#implementation-order)'s step 5.

The arguments themselves survive — `Alloc(B); Copy(A→B); Free(A)` has the same shape as the conversion it describes — so this is stale vocabulary attached to live reasoning rather than a collapsed argument.
It is listed here because a reader who checks whether conversion exists will conclude the section is obsolete, which it is not.

### 8. "Five layers", six rows

[The layers](../spec/index.md#the-layers) says a kladde file is described by five layers and then tabulates six.
Trivial, and only listed because the table is the first thing a new implementer reads.

### 9. Which "read" the complexity bound governs

[Guarantees](../spec/index.md#guarantees-an-implementation-must-provide) requires "read any byte of any allocation" in `O(log F)`.
The [hub page](../index.md) says reads never touch the file and cost what a native in-memory access costs.

These describe different operations — the storage-level read that a tool or a `Copy` performs, versus the application-level read of a backed value — but the spec table does not say which, and the two answers differ by a logarithm.

### 10. Grouping is "free" but constrained

[What is fixed and what is free](../spec/index.md#what-is-fixed-and-what-is-free) lists batching — "how an implementation groups application operations into transactions" — as explicitly not fixed.
[Transactions](../spec/journal.md#transactions) then requires that a record occurring outside any transaction be recorded as a transaction of its own.

That is a real constraint on grouping: an implementation may not leave a record ungrouped.
The intent is clearly "how you *batch* is free, but everything lands in some transaction"; the wording does not say so.

## Blockers for a first prototype

Ordered most to least difficult.
Difficulty here means *design work plus implementation risk*, not lines of code — several large items are only large.

Items marked **deferrable** are not blockers; they are listed so that the line between them and the blockers is explicit.

### 1. The journal's on-file representation — unspecified

Nothing outside "there is a `Journal` page kind" and "the header names a segment's start page" exists.
A prototype needs all of:

- how the pages of one segment are **chained** — a next-page pointer in each page, a contiguous run, or a list the header carries — and how recovery follows the chain without trusting anything unvalidated;
- where a transaction's bytes sit within a page, and whether a transaction may **span** pages (it must, for a transaction larger than `MAX_PAGE_CONTENT`);
- the **framing**: prefix width, checksum algorithm, and how the chained epoch-salted CRC is computed and verified;
- the resolution of [contradiction 1](#1-the-journal-is-two-different-things) and [contradiction 2](#2-journal-pages-cannot-satisfy-the-page-framing), both of which sit squarely here.

This is first because it is genuinely undesigned, and because every other part of the write path depends on its answer.

### 2. The byte encoding of journal records

[Record kinds](../spec/journal.md#record-kinds) gives the record set and their semantics, and nothing about their bytes: no tags, no field widths, no varint discipline.
Both append and replay need it, and replay is what makes the whole design crash-safe, so this cannot be improvised and fixed later.

### 3. Bootstrapping — creating a file is nowhere described

"Create new files" is in the prototype's scope, and no document describes the initial state.
Open: what epoch a fresh file starts at and which header slot it occupies; what the first header's address-table payload contains; how the root allocation and the schema-table allocation come into existence before there is an address table to describe them; whether creation is a degenerate flush or a distinct path.

It is third rather than first only because it is small once the two above are settled.

### 4. File extension is not covered by the durability argument

[The reuse rule](../spec/durability.md#the-reuse-rule) permits "extend the file", and [I1](../spec/durability.md#the-two-invariants) says a header is issued only after an `fsync` covering everything it references.

But extending a file changes **metadata**, and on common filesystems the new length is not durable merely because the data blocks were fsynced — it can require fsyncing the directory, or an explicit allocation step.
A power cut can therefore leave a valid header referencing a page beyond the file's recovered length, which breaks I1 in exactly the way the design says cannot happen.

Small to fix, easy to miss, and it invalidates the central invariant if missed — which is why it ranks above items with far more code.

### 5. The header's byte layout

[Header pages](../spec/file-format.md#header-pages) lists the fields and says "exact widths, ordering, and the reservation of space for future roots are TBD".
Blocking for both creation and open, and it is the one structure whose layout can never change, so the reservation decision has to be made now rather than discovered later.

### 6. The pointer encoding inside allocations

[Pointer encoding](../spec/allocations.md#pointer-encoding) is TBD: width, null representation, alignment.

This blocks the very first derived struct with a container field, because storing a `PersistableVec` means writing a pointer into the parent's bytes.
The reference implementation's choice — 32-bit, zero reserved for null — is probably just right, but it needs to be *decided*, since it is a format-level fact that readers depend on.

### 7. The in-memory address table

The largest body of new code, and the only large item that is purely implementation: [Address-table operations](../impl/address-table-operations.md) already gives pseudocode for load, resolve, read, the fragment-map primitives, apply-a-statement, resize, free, page rewrite and eviction.

Risk is concentrated in two places — the [partition invariant](../impl/in-memory-state.md#the-fragments-of-an-existing-id-exactly-partition-0-size) of the fragment map, where a bug returns a neighbour's bytes rather than failing, and the [anchor replacement](../impl/address-table-operations.md) obligation on page rewrites, where a bug silently resurrects truncated data.
Both deserve assertions from the first commit rather than tests added later.

### 8. The fold, plus its differential oracle

[The flush](../impl/flush.md#phase-a--the-fold) is fully designed.
A prototype needs only the naive in-order replayer and the piece-table fold that must agree with it — but [the oracle comes first](../impl/flush.md#correctness-the-differential-oracle), and building it in the wrong order is the documented way to get this wrong.

The [scheduler](../impl/flush.md#phase-c--ordering) is explicitly *not* needed: hoisting collapses the graph, and the recommendation is to skip it until a workload proves otherwise.

### 9. Recording every mutation

The current `JournaledWriteBackend` [does not](../rust/index.md), which is the one defect that is not a performance matter.
Design work is zero — [the log is the sole authority](../impl/write-phase-state.md) — but every container and the derive macro have to be revisited to confirm each mutation actually emits its records, and nothing mechanically checks this yet.

### 10. Freeing

[Freeing](../rust/freeing.md) is designed and not built, so today a dropped or overwritten value orphans its allocation.

A prototype **can** ship leaking — it is harmless against a file that is being exercised rather than kept — but it interacts with the prototype's other goals: a leak makes the file grow, which exercises consolidation, which the prototype is otherwise entitled to skip.
Worth deciding deliberately rather than by omission.

### 11. Choosing values for things that only need *a* value

Each of these blocks the prototype only in the sense that a number must be typed:

- **page size** — take 4 KiB and defer the 16 KiB measurement;
- **the `Inline` threshold** — the [documented starting policy](../impl/flush.md#the-inline-threshold) of ~64 bytes;
- **journal capacity per segment** — whatever falls out of item 1;
- **consolidation constants** — see below.

### Deferrable, and why

- **Data-page consolidation.** The [reverse-index gap](../impl/consolidation.md#finding-the-referrers-of-a-data-page) is unresolved, but a prototype may consolidate address-table pages only, which are self-describing, or nothing at all. Garbage accumulates; nothing breaks.
- **Checkpoint versus commit.** [Undecided](../spec/journal.md#checkpoint-versus-commit), and the prototype can keep the documented assumption that a fold is a commit, which is what gives atomicity for free.
- **Schema evolution.** The prototype checks the schema, which means comparing the root fingerprint and failing closed. [Resolution](../spec/schema/evolution.md) is a separate and much larger feature.
- **The `Move` operation.** [A sketch](move-op.md), and nothing in the prototype's scope needs it.
- **Non-owning references**, the atomic-group escape hatch, application versioning, and concurrency. All marked TBD and none reachable from the prototype's feature list.
- **Unwind behaviour.** [Poisoning on panic](../rust/transactions.md) is aspirational; the interim contract — reopen the file — is adequate for a prototype.
