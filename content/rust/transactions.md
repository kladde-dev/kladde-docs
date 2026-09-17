---
title: Transactions
---

The Rust surface over the [transaction and batch machinery](../impl/transactions-and-batches.md): RAII types, and what happens when a panic unwinds through one.

## RAII types

The backend's `begin_transaction`, `end_transaction`, `begin_batch` and `end_batch` are **private**, because they are dangerous: it is very easy to forget an end call, which prevents every subsequent mutation from ever being persisted.

The public API provides safe access through RAII types `Transaction` and `Batch`, created by `.transaction()` and `.batch()` on the backend and consumed by `Transaction::commit(self)` and `Batch::end(self)`, both returning a `Result`.
`Drop` calls the corresponding method and unwraps.

This is also what makes the interleaving errors in the [state table](../impl/transactions-and-batches.md#transitions-in-detail) unrepresentable: a scope cannot be ended while a scope nested inside it is still open, because the inner guard still borrows.

**There is no `Transaction::cancel`.**
Cancelling the transaction's effect on the *file* would be possible, but its effects on the in-memory representation are always immediate, and redoing them would mean reloading the entire affected value from the file.

## Behaviour on unwind

**Status: aspirational, deferred.**
This is the intended design and is not required for a first implementation.
Until it exists, the interim contract is the documentation-only version of the same thing: unwinding across a live `Transaction` leaves the in-memory state unusable, and the application must drop the `Kladde<T>` and reopen the file.

If caller code panics while holding one of these, the two cases differ, and `Drop` should branch on `std::thread::panicking()`:

- **`Batch`: end normally, no poisoning.**
  Every transaction boundary inside the batch was, by the ordering discipline, a valid state.
  Committing the completed ones loses nothing, and discarding them would throw away durability the application already believes it has.
- **`Transaction`: discard the in-flight operations and poison the backend.**
  Committing a half-built transaction would persist exactly the invalid intermediate state that transactions exist to prevent, so not committing is clearly right for the *file*.
  But it leaves the file and the in-memory representation disagreeing, for the same reason there is no `cancel`: the in-memory mutations already happened and cannot be rolled back.
  Poisoning is what stops that divergence from being silently observable.

"Poisoned" means *every subsequent backend call returns an error*, not *panic* — panicking during unwind aborts the process.
Recovery from poisoning is to drop the whole `Kladde<T>` and reopen the file, which is well defined: the file holds the last completed transaction.

This makes the panic path no worse than the crash path, which is a property worth being able to state: a panic loses the in-flight transaction, a crash loses the in-flight transaction, and both leave the file at the last valid state.

## Open questions

- We should provide a simple way to turn any guard into one that holds a `Transaction` or `Batch` instead of the backend itself, or that replicates their RAII behaviour.
  How to do that generically is not settled.
- A possible alternative or addition: let the user choose immediate, transaction, or batch mode at the time they create a guard, so that defaulting to `Batch` for a short sequence is easy.
  This might be implementable by default-implemented methods on `Persistable`.
- **Is a long-lived batched guard a footgun?**
  Batches delay persistence until they are dropped or ended, so holding one across an await point or a long computation quietly defeats kladde's main feature.
  Whether the API makes that tempting in common usage patterns, and whether anything can make it harder, is worth checking before the API is frozen.
