---
title: Transactions
---

The Rust surface over the [transaction and batch machinery](../impl/transactions-and-batches.md): RAII types, and what happens when a panic unwinds through one.

## RAII types

The store's `begin_transaction`, `end_transaction`, `begin_batch` and `end_batch` are **private**, because they are dangerous: it is very easy to forget an end call, which prevents every subsequent mutation from ever being persisted.

The public API provides them through RAII types instead:

- `Kladde::transaction()` returns a `Transaction`, and `Transaction::commit(self)` ends it;
- `Kladde::batch()` returns a `Batch`, and `Batch::end(self)` ends it;
- `Batch::transaction()` opens a transaction inside the batch — the nesting [the design is arranged around](../impl/transactions-and-batches.md#recommendations).

Each offers `guard()`, so mutations go through the scope rather than around it, and `commit` and `end` return a `Result`, since ending a scope appends to the journal.
Because the scope object borrows the `Kladde` mutably and an inner scope borrows the outer one, a scope cannot be ended while a scope nested inside it is still open: the interleaving errors of the [state table](../impl/transactions-and-batches.md#transitions-in-detail) do not compile.

Dropping a scope without ending it ends it, and a failure to end it there — where no error can be returned — poisons the store, since the in-memory mutations have then already happened and the journal does not hold them.

**There is no `Transaction::cancel`.**
Cancelling the transaction's effect on the *file* would be possible, but its effects on the in-memory representation are always immediate, and redoing them would mean reloading the entire affected value from the file.

## Behaviour on unwind

`Drop` branches on `std::thread::panicking()`, because the two scopes differ:

- **`Batch`: end normally, no poisoning.**
  Every transaction boundary inside the batch was, by the ordering discipline, a valid state.
  Committing the completed ones loses nothing, and discarding them would throw away durability the application already believes it has.
- **`Transaction`: discard the in-flight operations and poison the store.**
  Committing a half-built transaction would persist exactly the invalid intermediate state that transactions exist to prevent.
  But not committing leaves the file and the in-memory representation disagreeing, for the same reason there is no `cancel`, and poisoning is what stops that divergence from being silently observable.

"Poisoned" means *every subsequent store call returns an error*, not *panic* — panicking during unwind aborts the process.
Recovery is to drop the `Kladde` and reopen the file, which then holds the last completed transaction.
So a panic loses the in-flight transaction, a crash loses the in-flight transaction, and both leave the file at the last valid state.

## Open questions

- **Is a long-lived batch a footgun?**
  Batches delay persistence until they are ended, so holding one across a long computation quietly defeats kladde's main feature.
  Whether the API makes that tempting in common usage patterns is worth checking before it is frozen.
