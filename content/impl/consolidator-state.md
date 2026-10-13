---
title: Consolidator state
---

What consolidation could carry from one session to the next in the [private chain](../spec/file-format.md#the-private-chain), and how it would tell whether what it finds there is still current.

**Status: designed, not built.**
kladde-rs writes nothing to the private chain yet: every session starts its budget from its default, its rotating walk at the sequence's start, and its heat from the pages' epochs.

## Why it exists

**Consolidation learns from many flushes, and a session that forgot what it had learned at every close would never get past the start.**
The damage concentrates in workloads of many short sessions, such as a command-line tool that a script invokes over and over, each time opening a file, flushing once, and exiting:

- the [budget](consolidation.md#the-churn-floor-is-a-parameter-not-an-identity) would restart from its default at every open, and never converge on its target fill;
- the [rotating walk](consolidation.md#merging-spans-rides-the-rotating-walk) would restart wherever open seeds it, rather than where the last session left off, and over many short sessions might never reach parts of the sequence;
- the [heat](in-memory-state.md#heat) of every span would be rebuilt from the epoch of the page that holds its entry, which the header re-stamps every flush, so every span stated in the header would look freshly written at every open, and the walk and the header's eviction would misjudge it.

## What it would hold

| part | what | size |
| --- | --- | --- |
| identification | the implementation, and the version of this layout | 8 bytes |
| `up_to_date` | the epoch of the flush that last brought the state up to date | 8 bytes |
| the budget | the per-flush budget the controller had reached | 4 bytes |
| the walk | the span at which the next rotating walk begins | 4 bytes |
| heat | for spans whose heat the file does not give exactly, the flush that last wrote their bytes | a snapshot, plus records appended since |

**Everything a flush puts in the state is known before its data pages are packed**, since it is written together with them.

**Heat is kept only where the file gets it wrong.**
A span's entry lies in a page whose epoch can only understate how long its bytes have gone unwritten, since content is never younger than the page that holds its entry; that fallback is exact for a span written once and never restated.
So a snapshot keeps mainly the spans whose entries consolidation or the header restated since their bytes were written.
Heat records name an epoch and the span ids last written in that flush, sorted and stored as varint deltas, a byte or two per span, so a flush appends one record, one contiguous write; once the appended records outgrow the snapshot, a flush rewrites everything as a fresh snapshot, keeping only each span's newest mention.

## Writing it

**Every flush brings the state up to date, together with its own writes**: it appends its heat record and rewrites the fixed fields.
The state is ordinary content of the private chain, committed with the flush that wrote it, so a crash can never leave it ahead of or behind the file it describes.

A flush that finds no state it recognizes as its own replaces it, since the private chain is the only place to keep one.

## Checking it

**A state is used entry by entry, as far as each entry can be shown current**, because a writer that kept the state without maintaining it may have changed the file since.

- **If `up_to_date` is the governing header's epoch, everything in the state is current.**
- **Otherwise, a span's heat is current if no entry of the span is newer than `up_to_date`**, since its bytes cannot change without a new entry at the epoch of the flush that changed them; one that fails the test falls back to its entry's page epoch.
  Span ids are recycled, so a span stated after `up_to_date` may be another span under an old id, which the same test catches.
- **The walk's position and the budget are starting points either way**: at worst the walk resumes elsewhere, and the controller corrects a budget that no longer fits.
- **A state that cannot be parsed is treated as absent**, like another implementation's.
  Nothing in it can affect content, so the worst a misread state can do is mislead the heuristics.

**A flush that brings a stale state up to date rewrites it as a fresh snapshot first**, so that records it could not vouch for do not become current by default.
