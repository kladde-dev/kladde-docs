---
title: Consolidator state
---

What the reference implementation keeps in the [consolidator state](../spec/file-format.md#the-consolidator-state), how it keeps writing it cheap, and how it tells whether what it finds there is still current.

## Why it exists

**Consolidation learns from many flushes, and a session that forgot what it had learned at every close would never get past the start.**
The damage concentrates in workloads of many short sessions, such as a command-line tool that a script invokes over and over, each time opening a file, flushing once, and exiting:

- [Description defragmentation's age](consolidation.md#where-it-is-called-and-how-it-is-executed) would have to be rebuilt from the pages that hold each allocation's content, which is exact only for content that has never moved.
  The header rewrites the inline payloads it holds every flush, so every allocation with one there would look freshly written at every open, and in a small file, whose allocations are mostly inline in the header, description defragmentation would practically never run.
- Description defragmentation would lose, at every close, the candidates the latest walk had found, since a candidate is executed a flush after it is found; a session of one flush would find candidates and never execute any.
- The [budget](consolidation.md#the-churn-floor-is-a-parameter-not-an-identity) would restart from its default at every open, and never converge on its target fill.
- The [rotating window](consolidation.md#the-rotating-window) would restart wherever open seeds it, rather than where the last session left off.

## What it holds

| part | what | size |
| --- | --- | --- |
| identification | the implementation, and the version of this layout | 8 bytes |
| `up_to_date` | the epoch of the flush that last brought the state up to date | 8 bytes |
| the budget | the per-flush budget the controller had reached | 4 bytes |
| the window | the fragment-map key at which the latest flush's walk began | an `(Id, AllocationOffset)` |
| content ages | for each allocation, the flush that last wrote it | a snapshot, plus a byte or two per id written since |

**Everything a flush puts in the state is known before its data pages are packed**, since it is written together with them: what the flush knew when it began, and what its fold learned.
That is why the state records where the latest walk *began*: where a walk ends is decided at the cut, after packing.

### Content ages

**Content age is kept per allocation, the granularity [`last_written`](in-memory-state.md#3-the-allocation-map) has in memory, because it is the one age the file does not already record.**

- The other term of description defragmentation's age is a fragment's, the epoch of the statement that owns it, and every page records its epoch.
- Per page would be cheaper, but it would fail where it matters: the header holds the inline payloads of hot and cold allocations alike, so every allocation with one there would look as young as the youngest.
- Per fragment would cost an entry per statement, to cover a blind spot — a hot tail hiding a cold head in a large allocation — that the fragment term already covers.

**Ages are kept as records, each naming an epoch and the ids last written in that flush**, sorted and stored as varint deltas, which is a byte or two per id.
A flush appends its record, which is one contiguous write; an array indexed by id would cost a statement per entry, since the ids one flush writes are scattered.
An id's `last_written` is the highest epoch among the records that name it.

```rust
/// The content of the consolidator state, in this order.
struct ConsolidatorState {
    tag:        [u8; 8],                   // the implementation, and the version of this layout
    up_to_date: Epoch,                     // the flush that last brought the state up to date
    budget:     u32,                       // the per-flush budget the controller had reached
    window:     (Id, AllocationOffset),    // where the latest flush's walk began
    ages:       Vec<AgeRecord>,            // the snapshot, then one record per flush since
}

struct AgeRecord {
    epoch: Epoch,
    ids:   Vec<Id>,                        // sorted, and stored as varint deltas
}
```

**Once the appended records outgrow the snapshot, a flush rewrites everything as a fresh snapshot**, keeping only each id's newest mention, one record per epoch.
That keeps the state within about twice its compacted size, and costs at most two bytes rewritten per byte appended.
Between snapshots, each appended record is described by a statement of its own, until [description defragmentation](consolidation.md#description-defragmentation-rides-the-rotating-window) merges them, as it would for any allocation written piecemeal.

**A snapshot may leave allocations out, and an allocation without a record falls back to the youngest page that holds one of its fragments.**
That is the data page for bytes a `Ref` states, and the table page for an `Inline`'s payload or a `Zero`; `ZeroByDefault` fragments are in no page and are left out, and an allocation with no other fragment falls back to the youngest page that holds one of its statements.
Content is never younger than the page that holds it, so the fallback can only understate an age, which errs toward treating an allocation as recently written.
It is exact for content that was written once and never moved.
So a snapshot leaves out every allocation for which the fallback gives its `last_written` exactly, and keeps mainly two kinds: allocations whose bytes consolidation has moved since they were written, and those with inline content, which moves whenever its table page is rewritten — every flush, in the header.
An allocation it left out and consolidation moves afterwards looks younger than it is until the next snapshot takes it in, which errs the same safe way.

### The rotating window's position

**The state records where the latest flush's walk began, and open walks that window again, read-only, before the rotation resumes where the walk ends.**
The walk at open finds again the description defragmentation candidates that the latest walk found, which no flush has executed yet, and hands them to the session's first flush.
The previous session's last flush already restated as much of that window as its room allowed, so the first flush's own walk resumes where the one at open ended, as it would have within one session.
Without a usable state, open [seeds the position](consolidation.md#the-rotating-window) instead, and walks from there all the same.

### The budget

**The state records the per-flush budget the controller had reached**, so that a session resumes the controller rather than restarting it.

### What stays out

- **Description defragmentation's candidates**, which the walk at open finds again.
- **The eviction clock**, which open seeds from content ages: a fragment the header states starts with as many flushes untouched as its allocation has gone unwritten.
- **Whatever the file already records**: page epochs, each page's fill when it was written (its `content_size`), and every statement's page.

## Writing it

**Every flush brings the state up to date, together with its own writes.**
At the end of its fold, it appends its record and rewrites the fixed fields — `up_to_date` with its own epoch, the budget it is working with, and the position its walk will start from — which costs one contiguous append and one small range, a statement or two.
The state is ordinary content, committed with the flush that wrote it, so a crash can never leave it ahead of or behind the file it describes.

The first flush of a file creates the state and names it in the header.
So does a flush that finds no state it recognizes as its own: it replaces a foreign one, since the reference implementation has nowhere else to keep its own.

## Checking it

**A state is used entry by entry, as far as each entry can be shown current**, because a writer that kept the state without maintaining it may have changed the file since.

- **If `up_to_date` is the governing header's epoch, everything in the state is current.**
  That is the common case, since the reference implementation brings the state up to date in every flush.
- **Otherwise, an allocation's age is current if no statement naming the allocation is newer than `up_to_date`**, since content cannot change without a new statement at the epoch of the flush that changed it.
  The test is conservative, since a restatement looks like a write; an allocation that fails it falls back to the youngest page that holds one of its fragments, as one without a record does.
- **The window's position and the budget are starting points either way**: at worst the rotation resumes somewhere other than where it would have, and the controller corrects a budget that no longer fits.
- **A state the reference implementation cannot parse is treated as absent**, like another implementation's.
  Nothing in it can affect content, so the worst a misread state can do is mislead the heuristics.

**A flush that brings a stale state up to date rewrites it as a fresh snapshot first.**
Otherwise the records it could not vouch for would become current by default, the moment its own `up_to_date` covered the flushes they missed.

## What the ripeness draft would add

The [ripeness draft](../drafts/ripeness.md) estimates how fast each page still drains from that page's own losses, which a session would otherwise have to observe afresh.
It would add, for each page below `1 − θ`, `(page, epoch, coverage, s0, s1, rate, at, fast)`, appended as the fold drains pages, and the price `κ` its controller had reached.
A page's entry is current if the page's epoch still matches, and the coverage the page has lost since the entry was written is a loss that happened in the gap.
