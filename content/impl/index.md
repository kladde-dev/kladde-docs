---
title: Implementation
---

The reference algorithms and data structures: what an implementation must maintain in memory to satisfy the [specification](../spec/), and how it maintains it.

Everything here is **language-agnostic**.
Pseudocode is written in a Rust-like notation, because `enum`s and pattern matching make the cases concrete and easy to reason about, but nothing here depends on Rust.
For implementations in other languages, it is recommended but not required to make substantially the same choices for, e.g., integer widths or fundamental data structures like balanced trees or hash maps, both of which follow from the [bounds the specification states](../spec/span-table.md#bounds).

What does *not* belong here is anything a different language would do differently: trait shapes or class hierarchies, memory layout, alignment, the mutation-capture mechanism.
Those decisions are documented in the language-specific documentations (currently only [kladde-rs](../rust/)).

## The documents

| document | contents |
| --- | --- |
| [In-memory state](in-memory-state.md) | the structures a loaded file is held in: the span table, the rope, the page table, the reverse index, and the span id allocator |
| [Opening a file](opening.md) | the steps from the headers to a decoded root value |
| [Values and their positions](values.md) | regions, what each container keeps to find its children, what a guard costs, the built-in containers, and moving values between containers |
| [Write-phase state](write-phase-state.md) | what is tracked between flushes, and why it is derived from the journal |
| [The flush](flush.md) | folding the journal into pages: the piece table, mapping pieces to spans, the span policy, placement, and the cut of the span table |
| [Consolidation](consolidation.md) | reclaiming garbage: victim selection, evacuation, the page rewrite, merging spans, and the budget |
| [Consolidator state](consolidator-state.md) | what consolidation could carry from one session to the next in the private chain |
| [Transactions and batches](transactions-and-batches.md) | grouping operations into transactions, and the buffering state machine |
| [Related work](related-work.md) | where the design comes from, and where it parts company |
| [Implementation notes](implementation-notes.md) | where kladde-rs departs from these documents, and what it found |

## The shape of an implementation

A kladde file is currently **read in bulk and never searched on disk**.

That single decision is what keeps this design far simpler than a database engine, and it is worth stating first because almost every other choice follows from it:

- On-file representations are optimized for compactness and cheap incremental edits, never for random access.
- There is no on-disk search tree, no B-tree page splitting, no path copying.
- The span table's [tree of child references](../spec/span-table.md#physical-format) is a *directory*, not a search structure: nothing ever descends it by key.
- The order of the content lives in the spans' links, which a reader walks once, at open; a tree over the spans is kept in memory only.
- Epochs are consulted exactly once, during the bulk read, and never again at run time.

What this costs is that opening a file is `O(entries + n)` for a sequence of `n` bytes, and that the whole sequence is decoded into memory.
What it buys is that every read is an in-memory read of the application's own values and every flush writes only deltas.

A future feature called *[segments](../spec/sequence.md#partial-loading-later)* would address the cost of bulk reads by letting application authors declare a subtree as a chain of its own, loaded lazily.

## Overhead and work independent of size

**The live pages exceed the sequence by three terms — the description, the garbage, and the page framing — and each can be held at a target that does not depend on the sequence's length, at a cost per modification that does not either; the file adds to them a number of pages that depends on what a flush writes, and not on the sequence's length either.**
This is what the design is for, and the algorithms on the following pages are chosen to keep it.

### Where the bytes of the file go

Let `n` be the length of the sequence (with the schema's and the private chain's, which are small), `k` the number of spans the chains reach, `c` the mean encoded size of their entries without inline payloads, and `L̄ = n / k` the mean length of a span.
The live pages hold:

- every byte of the sequence once, in a data page or as an inline payload in a table page, and zeros nowhere: at most `n`;
- `k` entries: `k · c = n · c / L̄`;
- garbage and slack, `G`: bytes of data pages that no reached span shows, dead entries in table pages, and room that a writer left empty;
- the [page framing](../spec/file-format.md#page-framing), 0.37 %.

So live pages come to about `n · (1 + c / L̄ + G / n + 0.004)`.

The file holds more pages than the live ones:

- the **fallback** pages, which the last commit stopped referencing, and which [the reuse rule](../spec/durability.md#the-reuse-rule) keeps until the next flush's `fsync`;
- the **reusable** pages that the next flush will write into, freed by the commit before the last;
- the **journal**'s pages, at most its budget;
- **holes**: further reusable pages below the last live page, which a fall of the live size leaves behind, and which later flushes fill, or [compaction mode](consolidation.md#compaction-mode) and truncation give back.

### Each term, and what keeping it costs

**The description, `c / L̄`, is set by the span policy.**
[Bounded spans](flush.md#the-span-policy) keep `L̄ ≥ L / 2` in the leaves, so `c / L̄ ≤ 2c / L`: with entries of 8 bytes and `L = 256`, at most 6 %, and the header's exempt spans add at most a page.
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

That is the classic bound for log-structured cleaning (Rosenblum and Ousterhout, "The Design and Implementation of a Log-Structured File System", SOSP 1991), and uniformly random overwrites are its worst case; skewed workloads clean far more cheaply, which is what age terms in victim selection try to exploit.
`θ` is a worst case too: [packing](consolidation.md#slack-is-cheaper-than-a-split--up-to-a-point) holds every page a flush writes but its last to at least `1 − θ` full and usually fills it, so the cost lies near the column for `θ = 0`.

**The pages around the live ones are set by what one flush writes.**
In steady state, a flush frees about as many pages as it writes, so with `p` pages written per flush, the file holds about `p` fallback pages and `p` reusable ones: `2p` pages besides the live ones, plus the journal's.
`p` is the flush's new data and table pages, plus the survivors its cleaning moves: about ten pages for a flush that supersedes a page of content at `u = 0.9` and `θ = 0`.
So a large file carries some 25 pages besides its live ones, about 100 KB, which is 0.1 % of a sequence of 100 MB and vanishes as `n` grows; for a file of a few pages, the same pages are most of the file.

**Everything else is constant or logarithmic.**
A splice, an overwrite and a move state a constant number of spans; a moved byte restates its span and nothing else, since no key changes; the rope finds and edits spans in `O(log k)`; a guard is made from its parent's with one lookup in the parent's index, and a change of size updates one entry per level ([what a guard costs](values.md#what-a-guard-costs)); victims come from bucket queues in `O(1)`, and their spans from a reverse index in `O(1)` each; and the table pages' directory changes `O(depth)` pages per leaf added or removed, amortized over the entries a leaf holds.

So the live pages' overhead is `c / L̄ + G / n + 0.004`, set by `L` and `u`, the file adds `2p` pages and the journal's, and the work per modification is `O(w + L)` bytes for `w` bytes written and `O(log n)` time in the store, plus the walk from the root to the modified value, which the application makes anyway.

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
[The file as one sequence, measured](../evaluation/one-sequence.md) shows what kladde-rs reaches on real edits.

## The mirror

**An implementation reads pages into its own buffers with ordinary file reads, rather than mapping the file, and reads only the pages the chains reach.**
At open it reads every table page the header reaches, walks the chains, and then reads the data pages the reached spans show, in page order, into a **mirror**, from which the decode of the sequence reads; [Opening a file](opening.md) gives the steps.

It might be tempting to load each data page lazily, the first time something reads from it.
Reading them eagerly turns scattered reads into one sequential pass over a set known in advance, and validates every reached page's CRC at open rather than at first touch — which may be long after the last backup that could have helped.
And it keeps the resident set a property of the *file*: nothing obliges a data type to read anything, and a tool that only compacts reads no content at all.

**Drop the data-page mirror once loading is done.**
After the load, application reads are served by the values' own in-memory representations and never touch the file, so a span's bytes in a data page are read again only when a flush moves them, merges them with their neighbours, or rewrites a short span an edit cut into — all rare, and all able to afford a fresh read.

**Table pages stay resident by default**, for a reason data pages do not share: `Inline` payloads live inside them, and every flush that restates an inline span copies its payload.
They are also small next to the sequence.
Nothing depends on it, though: only the header must stay, being the write buffer, and dropping the leaves after load costs one read per leaf a flush rewrites or restates from.

*Why not `mmap`.*
Mapping the file would let the operating system's page cache serve as the mirror, with pointer-dereference access and no second copy.
It is rejected for now because **I/O errors arrive as `SIGBUS` rather than as an error value**: a page that cannot be faulted in — a failing disk, or the file truncated by something else — takes the process down, and there is no portable way to catch it.
Should that trade ever be revisited, the mapping must be read-only, since kladde writes with ordinary writes and `fsync`, and mixing the two is only coherent by grace of the platform's unified page cache.

*Reading through a cursor.*
The decode reads the sequence through a cursor that follows the rope across span boundaries and hands out slices of the mirror and of the table pages, so no byte of the sequence is copied before a value's own decoder takes it.
