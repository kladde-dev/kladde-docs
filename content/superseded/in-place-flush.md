---
title: The in-place flush
---

**Superseded.** Replaced by [the copy-on-write protocol](../spec/durability.md).

The flush design that wrote updated allocations over their old locations, and the machinery that was needed to make that safe.

## What it was, and what it needed

A flush applied the folded journal by writing each allocation's new content to its address — in place where the size allowed, relocating where it did not.
Because executing a flush **destroyed bytes that re-executing it would need**, three mechanisms were required, none of which exists any more:

**Conflict detection.**
A flush that relocates allocation `A` into space that allocation `B` still occupies, while some other action still needs to read `B`, has a read/write conflict.
Finding those conflicts meant lowering the schedule from ids to addresses and checking overlaps.

**Write-ahead copies.**
Where a conflict could not be scheduled away, the overwritten source had to be copied somewhere safe first, so that a re-run could still find it.

**Restartability analysis.**
A flush interrupted partway through had left the file in a state that was neither the old one nor the new one, so recovery had to determine how far it had got and what remained safe to redo.

On top of that, page-granularity **collateral damage** to untouched neighbours — "passenger corruption", where a torn write damages bytes the flush never meant to touch — could at best be *detected*, never prevented, because those bytes were inside pages the flush was legitimately writing.

## Why it went

Under [the reuse rule](../spec/durability.md#the-reuse-rule) a flush writes only to pages that no recoverable state references.
That single change removes all four problems at once:

- there are no read/write conflicts, because nothing a flush writes is something anything still reads;
- there are no write-ahead copies, because nothing is destroyed;
- there is no restartability analysis, because a crashed flush is simply re-run against an untouched previous state;
- there are no untouched neighbours in the write path at all, so passenger corruption stops being a category rather than being detected.

The fold survived unchanged — it is [still there](../impl/flush.md#phase-a--the-fold) — and everything downstream of it in the old flush design is simply gone.

This was the single largest argument for the redesign, and it is worth stating as a general lesson rather than a local one: **the machinery removed was bespoke, and the machinery that replaced it is well-trodden.**
Never-overwrite plus cleaning is Rosenblum's log-structured file system; the alternating header commit is LMDB's meta pages; copy-on-write with checksums is ZFS, WAFL and btrfs.
Their failure modes are understood and bounded; the conflict/hoisting apparatus's were not.

## What kladde does differently from those systems

Worth recording, because it is what made the adoption cheap.

Kladde **reads in bulk and looks nothing up on disk**, and every difference that follows cuts toward simplicity: no on-disk search tree, a flat rewritten root instead of path-copied interior nodes, whole-world validation available for free at open, and a single `fsync` per flush where LMDB needs two — because kladde can afford to let the header lag one write behind and lean on journal replay, which a random-access database cannot.

## What was given up

Stated in full at the time of adoption, and still accurate:

1. **Cleaning debt.** Every dirty piece is written once at flush and, on average, a fraction of a time again when consolidation later relocates its page-mates or itself. The worst case is uniform random small writes across a large file — the workload that hurts every log-structured system.
2. **Space overhead is a policy outcome, not a constant.** An early conjecture that it would be a constant was wrong: garbage accrues in proportion to write traffic and drains in proportion to consolidation effort.
3. **Loss of contiguity.** An allocation's bytes must be gathered at open and scattered at flush, and its description grows as it fragments.
4. **A minimum flush of several pages** even for a tiny transaction, where the in-place design might touch fewer. The crossover favours copy-on-write as soon as writes scatter at all, but a workload of frequent single-byte flushes pays more.
5. **Full trust in `fsync`**, leaned on harder: a single barrier, with the header issued unsynced.
6. **Both header pages corrupting simultaneously** is unrecoverable without a scan. They are single-sector writes at opposite ends of a two-page span, never written in the same flush, so this requires two independent failures; replicating headers at two more fixed locations would cost two extra page writes per flush and is probably not worth it.

The one workload that regresses — high-frequency tiny flushes — is measurable, and batching flushes attacks it directly.
