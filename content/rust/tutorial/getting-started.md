---
title: Getting started
---

## The mental model

A backed data structure lives in two places at once.

**In memory**, it is an ordinary Rust value: a `PersistableVec<T>` really does hold a `Vec<T>`, and reading from it is a plain memory access.
Nothing about reading touches the file.

**In the file**, it is a compact binary representation that lags slightly behind — the *snapshot* — plus a *journal* of changes not yet folded into it.
A mutation updates the in-memory value immediately and appends to the journal before returning.

So: reads are as fast as the non-backed equivalent, writes cost one small append, and the bulk representation is brought up to date periodically rather than on every change.

The name comes from this shape.
A *Kladde* is a merchant's rough day-book — transactions scribbled down in order as they happen — later transcribed into the clean main ledger.

## Your first backed value

<!-- kladde-example: name=journal file=src/main.rs mode=run deps=kladde,kladde-types
after:
  mod chaining;
  mod nesting;
  mod reading_back;
-->
```rust
use kladde::Kladde;
use kladde::Persistable;
use kladde_types::{PersistableString, PersistableVec};

#[derive(Persistable)]
struct Journal {
    owner: PersistableString,
    entries: PersistableVec<PersistableString>,
}

fn main() {
    let mut journal = Kladde::new(Journal {
        owner: PersistableString::from("ada"),
        entries: PersistableVec::new(),
    });

    journal
        .guard()
        .entries_mut()
        .push(PersistableString::from("first entry"));

    println!("{} entries", journal.get().entries.len());
}
```

Three things are worth noticing.

**`Kladde<T>` is the root.**
It pairs your root value with a backend.
Everything reachable from it is backed; a value you construct outside it is not.

**Reading goes through `get()`**, which hands you `&T` — the plain value.
From there you use ordinary methods: `len()`, `iter()`, indexing.
No guard, no backend, no cost beyond the read itself.

**Writing goes through `guard()`**, which hands you a `JournalGuard`.
Every field of a derived type gets a `_mut()` accessor on its guard, which reborrows the same backend one level deeper.
So `journal.guard().entries_mut()` is a `PersistableVecGuard`, and `push` on it both mutates the vector and records the change.

## Why the guard exists

In Python, `journal.entries.append(x)` could persist automatically, because Python lets a library intercept attribute access.
Rust does not.
There is no hook that fires when you assign to a field.

The guard is the substitute.
It is an RAII token — exactly the relationship `MutexGuard` has to `Mutex` — that proves you have exclusive, recording access, and it carries two things a plain `&mut` could not: the backend to record into, and the *location* in the file that this particular field occupies.

This is why you cannot mutate a backed value through a plain `&mut`.
There is no such method.
Persistence is not something you can forget, because the only mutating API is the one that records.

## Nesting

Guards compose to arbitrary depth, and each level reborrows the same backend:

<!-- kladde-example: name=journal file=src/nesting.rs
before:
  use crate::Journal;
  use kladde::Kladde;
  use kladde_types::PersistableString;
  fn nesting(journal: &mut Kladde<Journal>) {
after:
  }
-->
```rust
let mut guard = journal.guard();
let mut entries = guard.entries_mut();
entries.push(PersistableString::from("second"));
entries.push(PersistableString::from("third"));
```

Or in one chain, when you only need one mutation:

<!-- kladde-example: name=journal file=src/chaining.rs
before:
  use crate::Journal;
  use kladde::Kladde;
  fn chaining(journal: &mut Kladde<Journal>) {
after:
  }
-->
```rust
journal.guard().owner_mut().set("grace");
```

Hold a guard only as long as you need it.
It borrows the root, so nothing else can touch the structure while it is alive — which is exactly the property that makes it safe for a flush to happen only between complete mutations.

## Reading back

<!-- kladde-example: name=journal file=src/reading_back.rs
before:
  use crate::Journal;
  use kladde::Kladde;
  fn reading_back(journal: &mut Kladde<Journal>) {
after:
  }
-->
```rust
let restored = journal.load();
assert_eq!(restored.entries.len(), 1);
```

`load()` reconstructs a fresh value from what is actually stored, rather than returning the in-memory one.
It is mostly useful in tests, where it is the round-trip check: mutate, flush, load, compare.

Once real file storage exists, `Kladde::open` will do the same thing from a path.

## What is not there yet

`Kladde::new` takes a value and keeps it in memory.
There is no `Kladde::open(path)` — real file storage is not implemented, so a `Kladde` currently lives only as long as the process.

Everything above the storage layer works today; the storage layer is the gap.
See [Durability](durability.md) for what the intended guarantees are.
