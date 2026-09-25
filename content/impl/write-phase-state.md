---
title: Write-phase state
---

What an implementation tracks between flushes, and why it is derived from the journal rather than kept alongside it.

## The log is the sole authority

> It is a totally ordered sequence of every mutation.
> Everything else is derived from it and may be discarded and rebuilt.

This is the one structural rule of the write path, and it is worth stating as a rule because the obvious alternative — keeping a *state snapshot* beside an *operation log* — fails in a way that is easy to miss.

A snapshot keyed by id is unordered, last-writer-wins, and self-annihilating: allocate-then-free collapses to nothing.
A log is ordered and append-only.
If both exist and no order relates them, then any operation that invalidates an earlier log entry must reach into the log and repair it by hand — and the failures that produces are not all annihilation failures:

- **`alloc → write → free` cannot replay.**
  The allocate and free annihilate in the snapshot, so the id is never claimed, while the buffered write survives in the log and replays against an id that was never created.
- **A write followed by a shrinking resize corrupts a neighbour**, if replay does not bounds-check: a write buffered while the allocation was large replays at an offset now outside it.
- **And the case with no annihilation at all:** a value that allocates a child and writes the child's id into its own header emits a `Write` the log keeps and, for the child, a `Resize` the log does not.
  Recovery replays the log, finds a live pointer in the parent, and that pointer names an allocation nothing ever created.

The last one is the reason [the journal records existence and size](../spec/journal.md#record-kinds) as well as content, rather than leaving geometry to a snapshot beside it.

## Two derived structures

Both are derived, and it matters that they are.

- **Write-phase geometry** — per-id size and existence, so that bounds checks and size queries during the write phase are `O(1)`.
  Discarded at flush.
- **The fold's output** — geometry *and* content, built at flush from the log alone, consumed by the placement phase, then dropped.
  See [The flush](flush.md#phase-a--the-fold).

The fold recomputes the geometry the write phase already had, and **that redundancy is the point**: comparing them on every flush is a consistency check that all three failures above would have tripped.
It is asymptotically free, since the fold is already linear in log length and the comparison is linear in touched ids.

## One id, one lifetime

> **An id returns to the recyclable pool only at flush**, after the frees are applied.

This is a precondition, not an optimization.
The log and both derived structures are keyed by id, so if an id could be recycled mid-transaction then

```
Resize(id, 8), Write(id, …), Free(id), Resize(id, 16), Write(id, …)
```

would place two allocation lifetimes under one key.
The fold cannot emit two claims for one key, and the first write would be attributed to the second allocation.

It also makes the `Freed → New` transition unreachable rather than merely discouraged, which is what keeps the state machine below finite.

Note that this is a *within-transaction* rule and does not conflict with [recycling an id as soon as its tombstone is committed](id-recycling.md): the tombstone is committed by the flush, which is exactly when the id returns to the pool.

## The geometry state machine

Five states, of which two are "absent" and must be distinguished:

| state | tracked? | live in the file? | flush action |
| --- | --- | --- | --- |
| `Absent/live` | no | **yes** — claimed earlier, untouched this transaction | nothing |
| `Absent/dead` | no | no — never minted, or freed and flushed | nothing |
| `New(S)` | yes | no | allocate |
| `Resized(S)` | yes | yes | resize |
| `Freed` | yes | maybe | free **if live**, then recycle the id |

`New` and `Resized` are distinct because they produce different statements at flush, not because they carry different data.

### Transitions

| operation | from | to | note |
| --- | --- | --- | --- |
| `alloc` | `Absent/dead` | `New(S)` | the only source state |
| `write` | any live state | **unchanged** | content only; this map is geometry |
| `resize` | `New(S)` | `New(S′)` | |
| | `Absent/live` | `Resized(S′)` | |
| | `Resized(S)` | `Resized(S′)` | |
| | `Freed`, `Absent/dead` | — | error |
| `splice` | as `resize` | as `resize` | size change plus content operations |
| `free` | `Absent/live`, `Resized(S)` | `Freed` | |
| | `New(S)` | `Freed` | **not `Absent`** |
| flush | `New`, `Resized` | `Absent/live` | map cleared |
| | `Freed` | `Absent/dead` | ids recycled here, and only here |

`write` earns a row precisely because it is a no-op on the map.
If `write` ever needs to touch geometry, something has gone wrong.

**Freeing a `New` entry goes to `Freed`, not `Absent`.**
Going to `Absent` loses the information that the id must be recycled, which is an id leak.
Making the flush's free lookup-guarded lets one state serve both cases without a fourth variant.

Most error cells are unreachable if the API consumes an owned handle on free.
The escape hatch is any operation that can mint a second owner, so those cells *are* reachable through a duplicate handle and do need runtime checks.
Query order is therefore **this map first, the committed state second**; never trust a typestate alone.

### Repopulation after a flush

Because an explicit flush can occur while a transaction is still open, with its operations buffered past the committed cursor, this map no longer describes the same point in the operation sequence as the flush does.
It must be **repopulated** from the remaining operations after the flush rather than simply cleared.
See [Transactions and batches](transactions-and-batches.md#consequence-for-write-phase-geometry) for exactly which transitions this affects and why the ordering is worth keeping explicit.
