---
title: Write-phase state
---

What an implementation tracks between flushes, and why it is derived from the journal rather than kept alongside it.

## The log is the sole authority

> It is a totally ordered sequence of every mutation.
> Everything else is derived from it and may be discarded and rebuilt.

This is the one structural rule of the write path.
The alternative — a snapshot of the changed content kept beside the log — would have to agree with the log about the order of every change, and the sequence's positions make order decisive: a splice at position 10 followed by a write at position 20 does something else than the two in the other order, since the splice moves what the write names.
So the flush folds the log, and nothing else, into the new sequence.

## Two derived structures

**The sequence's length**, as every record recorded so far leaves it, so that bounds checks during the write phase are `O(1)`: a record whose range does not lie inside the sequence is refused before it is recorded, and records nothing.
It also checks the fold: the fold's sequence must come out exactly this long, a consistency check that costs one comparison per flush.

**The cuts of the journal**: for each cut, oldest first, its length, whether it was pasted, and which journal it belongs to — the epoch of the journal its record is appended to, or will be — so that a paste can be checked before it is recorded: it must name a cut that is not pasted yet, and its length gives the paste's.
A flush drops the cuts that the records it folded made; cuts that records past the committed cursor made stay, since those records go to the next journal.

**The pastes that may fall back**: a paste recorded in one transaction names a cut that an earlier transaction made, and it is a `Paste` only if it is appended to the journal that made the cut.
So the buffer keeps, beside each such paste, what its `Splice` would hold — the value's encoding, or the value to encode it from — until its transaction is appended, when [the flush triggers](transactions-and-batches.md#flush-triggers) decide which of the two it becomes ([cuts](values.md#moving-a-value-between-containers)).

**The fold's output** — the new sequence as runs of committed bytes and new bytes — is built at flush from the log alone, consumed by the mapping onto spans, and dropped; see [the fold](flush.md#the-fold).

## After a flush, and after a rollback

An explicit flush can run while a transaction is still open, with its records buffered past the committed cursor, and an automatic one holds back the transaction that did not fit ([flush triggers](transactions-and-batches.md#flush-triggers)).
The length covers those records too, so it is right after the flush as it was before; the cuts those records made belong to the next journal, and stay.

A transaction that fails halfway and is rolled back moves the buffer's end back to where it began, and with it the length and the list of cuts, to what they were at its start.
