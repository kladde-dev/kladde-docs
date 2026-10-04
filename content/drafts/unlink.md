---
title: Unlinking values
---

**Status: a proposal, not adopted.**

**Split today's transferring `remove` in two: a `remove` that frees what it takes out and returns a plain value, and an `unlink` that keeps the allocations, opens a transaction, and returns an `Unlinked` value that must be stored again or freed before that transaction ends.**
Moving a value then stays as cheap as it is today, since only the value's own encoding is written again.
Two mistakes that today's API lets through silently become impossible: dropping a taken-out value leaks its allocations in the file, and storing it into another file makes it alias that file's own allocations.

## What happens today

**Every container hands out removed values with their allocations intact.**
`remove` and `pop` on the three vectors, the small vector's internal `take`, and the hash map's `remove` all do, and so does the map's `insert` with the value it replaces.
Storing such a value again writes only its own encoding, because `prepare` leaves a container that has an allocation alone; a restack in kladde-svg writes the 38 bytes of a slotted node, however large its subtree.

**A value that is dropped instead leaks its allocations in the file**, as [Freeing](../rust/freeing.md#where-ownership-ends) says.
A handle cannot free itself, since it holds no reference to the backend, and that is deliberate: a reference in every handle would inflate every container.

**A value stored into another file corrupts it.**
A `UniquePointer` carries an id and nothing that names its file, and `prepare` skips any allocation a value already has, so the value's ids go into the other file unchecked, where they name that file's own allocations.
In an experiment, a string removed from file A and pushed into file B read as B's own first string once B was reopened, so two owners shared one allocation, and freeing either would destroy the other's content.

## The variants

**The strict variant fits a document editor best**: a move is atomic, and the value cannot outlive the edit that moved it.
The lenient variant is for values that must be held across transactions, as a clipboard holds cut content; nothing in the reference implementation needs that yet.

| | today | lenient `Detached` | strict `Unlinked` |
| --- | --- | --- | --- |
| a dropped value | leaks | is freed after the current transaction | is freed, and the transaction ends |
| storing it into another file | corrupts that file | fails with an error | fails with an error |
| a crash between taking out and storing again | leaks | leaks | cannot happen: the move is one transaction |
| how long the value may live | as long as the caller likes | as long as the caller likes | until its transaction ends, and no longer than the root guard it came from |
| what the value holds | nothing | a weak handle to a queue in the store | the backend handle |

Under either variant, `remove` frees: the value it returns has no allocations left, since every container's `free` clears its handles, and storing it creates them again, in any file.
That makes it the safe default, and the right one for content that is discarded; `unlink` is for content that moves.
`delete` becomes `remove` with its result ignored.

### The lenient variant: free on drop, later

`Detached<T, P>` holds the value and a `Weak` reference to a queue of ids in the store.
Dropping it walks the value's allocations into the queue, and the store frees them in a transaction of its own after the current one commits, and at `flush` and `close`; freeing an allocation that nothing points to is safe at any later time.
`free(self, backend)` frees at once and returns the error, and `into_inner(self)` hands out the bare value for a caller who takes over responsibility, as today's `remove` does.

The price is the queue, a shared handle the store does not have today, and the leak that remains when the process dies before the queue is drained.
A `Detached` dropped after its file is closed cannot reach the queue, and leaks too.

### The strict variant: a transaction from `unlink` to storing again

`unlink(i)` begins a transaction, splices the element out, and returns `Unlinked<'k, T, B>`, which holds the backend handle.
Storing it with `insert` or `push` ends the transaction; `free(self)` frees the value, ends the transaction and returns any error; dropping it frees the value and ends the transaction too, and poisons the store if either fails, as dropping a `Transaction` whose commit fails does.
The store's transactions nest already (`Store::begin_transaction` counts a depth), so several values can be unlinked at once, and an `unlink` inside a `Kladde::transaction` joins it.

**What the caller can do meanwhile is anything a guard can do**: every mutation made while an `Unlinked` lives joins its transaction, which is what makes the move atomic.
It needs no access to the backend through the `Unlinked`; the guards keep working.

**One change makes that true: guards must hold the backend handle by value.**
A guard holds `&'s B` with the same lifetime `'s` as its borrow of the value, and a child guard's `'s` is its parent's reborrow.
An `Unlinked` that kept such a reference would keep the parent's reborrow alive, and the borrow checker would refuse a second path from the root to the place it is stored again.
With a `B: Copy` handle by value — `&'k Store`, borrowed for as long as the root guard — the lifetime is the root's, and reborrowing the root again is allowed.
The change is mechanical: `backend: &'s B` becomes `backend: B` in every guard, in the containers and in what the derive generates.

**The usability cost is the one intended.**
An `Unlinked` cannot be kept in an application's own structures, nor across a flush: a flush folds only committed transactions, so an `Unlinked` held for long holds the journal open with it.
A caller who needs to hold content longer uses `remove`, and stores the value again as a copy.

The open transaction also leaves room for a later optimization: since the source and the destination are known before the transaction ends, a container could record a `Move` from where the bytes lie instead of encoding the value again, which pays once the flush restates moved bytes rather than writing them again.

## Accepting a value or an unlinked value in one method

**`insert` and `push` can take either without the caller saying which, through one trait with two implementations:**

```rust
pub trait IntoElement<T> {
    fn into_element(self) -> Element<T>;   // a plain value, or an unlinked one with its store and transaction
}
impl<T> IntoElement<T> for T { /* ... */ }
impl<T, B> IntoElement<T> for Unlinked<'_, T, B> { /* ... */ }

pub fn insert(&mut self, index: usize, value: impl IntoElement<T>) -> Result<(), Error>;
```

The two implementations cannot overlap, since no type is its own wrapper, and inference picks one from the argument's type.
In a test, `push(1)` on a list of `u32`, `push(String::from("x"))` and `push(Detached(…))` all compiled without annotations.

**What breaks is an argument whose type only the parameter fixes**: `push("a".into())` fails with "type annotations needed", since `.into()` could now target either implementation.
Four places in the documentation and the doc tests write that, and would name the type instead, as in `push(PersistableString::from("a"))`.
A separate method, `insert_unlinked`, would keep `.into()` working, at the price of a second name for every insertion.

## The narrower trait

**`Persistable::free` takes a full backend, `B: WriteBackend`, although a free walk only ever calls `free`.**
`WriteBackend` also requires `alloc`, `resize`, `write`, `splice`, `copy`, `move_range` and `atomically`, so the lenient variant's queue, to be handed to a free walk, would have to implement all of them as stubs that are never called.

A trait with `free` alone, say `Release<P>`, which `WriteBackend` extends, would let `free` take `&impl Release<P>` instead.
The queue would implement only that, and the signature would state what a free walk may do.
The change touches every hand-written `free` and what the derive generates, mechanically; only the lenient variant needs it.

## Open questions

- **Names.** `remove`, `pop` and the map's `remove` would free; `unlink`, `unlink_last` and the map's `unlink` would transfer.
  The map's `insert` would free the value it replaces and return it plain.
- **Whether the lenient variant is needed at all**, which depends on whether an application wants to hold content across transactions without copying it.
- **A leak check.** The lenient variant still leaks when the process dies before its queue drains, and files written today may hold leaks already; the [`Pointer` descriptor](../spec/schema/type-descriptors.md#pointer) would let a tool find allocations that nothing owns, and free them.
