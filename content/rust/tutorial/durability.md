---
title: Durability and flushing
---

What is guaranteed when, and what a crash actually costs you.

## The two-stage model

A mutation does two things before it returns.

1. It appends records describing the byte-level change to the **journal**.
2. It updates the in-memory value.

Separately and later, the journal is **folded**: the records are applied to the file's compact form, written to fresh pages, and a new journal is started.

Durability comes from the journal, not from the fold.
A mutation that has returned survives an application crash whether or not a fold has happened, because its record is in the file — the operating system writes it out even if your process dies.
A *power* cut is the case where the fold matters; see [below](#what-a-crash-costs).

## What a crash costs

**After an application crash: nothing that returned `Ok`.**
If `push` returned `Ok`, the element is in the file, and `Kladde::open` finds it.

**After a power cut: at most what came after the last flush.**
Journal appends are not forced to disk one by one, so a power cut can take the ones the operating system had not written out yet.
Everything folded by a completed flush survives, and what survives of the rest is always a prefix: never a later mutation without the earlier ones.

**A partial mutation is never visible.**
Each mutation is one transaction, and so is each transaction you open, so recovery replays all of it or none of it: a push that splices in an element and rewrites the vector's count is never half there.

**A crash during a fold costs the fold, not the data.**
The fold never overwrites live data in place; it writes to unused space and commits with one small atomic write.
A crash before that commit leaves the old state intact and the half-written new data inert, and the journal still holds everything, so the next open simply redoes the fold.

## Flushing

You do not normally have to think about this.

Flushing is **automatic**: a mutation that would not fit in what is left of the journal's budget folds the journal before it returns.
Application authors mutate state and never call anything.

An explicit `flush()` remains available for the cases where you want the fold to happen now — before a long idle period, say, since a flush is also what makes everything before it survive a power cut.

**An interactive application calls `flush_if_journal_exceeds(0.5)` from its idle callback.**
It flushes only if the journal holds at least that fraction of its budget, and returns whether it did, so it costs next to nothing when there is little to fold.
Flushing while the user pauses keeps the automatic flush, whose `fsync` blocks the mutation that triggers it, out of the middle of a gesture such as a drag.
Keep the fraction well above zero: a flush ends the journal, and with it every [cut](containers.md#vect-and-durablevect) that is still waiting to be pasted, which is then encoded again when it is pasted.

```rust
if journal.flush_if_journal_exceeds(0.5)? {
    // folded while the user paused
}
```

**When the user closes a document, call `close_consolidated()`.**
It replaces the file with the smallest file that holds the value, in full pages and with an empty journal: it writes that file beside the old one, syncs it, and renames it over the old one, so a crash while closing leaves either file, each with every mutation that returned `Ok`.
It takes time linear in the value's encoded size, rather than in what changed since the last flush:

```rust
journal.close_consolidated()?;
```

`close()` is quicker, and leaves a larger file: it flushes and then truncates the file after its last live page, which does not shrink it to its live pages, since free pages below that one stay in the file.

What flushing is *not* for: making your data survive an application crash.
The journal already did that.

## When the disk fails

A failed `fsync` means the operating system may have dropped writes it had already accepted, so kladde **poisons** the `Kladde`: every later call returns `Error::Poisoned`.
Drop it and open the file again; it then holds every mutation that returned `Ok`.

## Transactions

Every mutation is atomic on its own.
When several belong together — money leaving one account must arrive in another — make them one transaction, and a crash keeps all of them or none:

<!-- kladde-example: name=accounts file=src/main.rs mode=run deps=kladde -->
```rust
use kladde::{Kladde, Durable};

#[derive(Durable)]
struct Accounts {
    checking: i64,
    savings: i64,
}

fn main() -> kladde::Result<()> {
    let mut accounts = Kladde::new(Accounts { checking: 100, savings: 0 });

    let mut tx = accounts.transaction();
    let mut both = tx.get_mut();
    let AccountsParts { mut checking, mut savings } = both.parts();
    checking.set(70)?;
    savings.set(30)?;
    tx.commit()?;

    assert_eq!(accounts.get().savings, 30);
    Ok(())
}
```

Each mutation still changes the in-memory value when it is made, and `commit` appends them to the journal together.
Dropping a transaction commits it too, unless a panic is unwinding: then its mutations are discarded, since they may be half a change, and the `Kladde` is poisoned, since the in-memory value already holds them.
There is no way to cancel a transaction, for the same reason.
[Transactions](../transactions.md) has the details.

## Long runs of mutations

A single transaction is held in memory until it ends, because the fold only ever folds complete transactions.
So keep transactions short, and use a batch for a long run of mutations that are each valid on their own:

<!-- kladde-example: name=batch file=src/lib.rs deps=kladde,kladde-types
before:
  use kladde::Kladde;
  use kladde_types::DurableVec;
  fn import(log: &mut Kladde<DurableVec<u64>>) -> kladde::Result<()> {
after:
  Ok(())
  }
-->
```rust
let mut batch = log.batch();
for reading in 0..100_000 {
    batch.get_mut().push(reading)?;
}
batch.end()?;
```

A batch appends its mutations to the journal in a few large pieces rather than one by one, which saves writes, and the automatic flush can fold between the pieces.
It is not a transaction: a crash can keep some pieces and lose the later ones, though never part of a mutation, nor of a transaction opened inside the batch with `batch.transaction()`.
