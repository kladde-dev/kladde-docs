---
title: Getting started
---

## The mental model

A backed data structure lives in two places at once.

**In memory**, it is an ordinary Rust value: a `PersistableVec<T>` really does hold a `Vec<T>`, and reading from it is a plain memory access.
Nothing about reading touches the file.

**In the file**, it is a compact binary representation that lags slightly behind, plus a *journal* of changes not yet folded into it.
A mutation appends to the journal and then updates the in-memory value, before it returns.

So: reads are as fast as the non-backed equivalent, writes cost one small append, and the bulk representation is brought up to date periodically rather than on every change.

The name comes from this shape.
A *Kladde* is a merchant's rough day-book — transactions scribbled down in order as they happen — later transcribed into the clean main ledger.

## Your first backed value

<!-- kladde-example: name=journal file=src/main.rs mode=run deps=kladde,kladde-types
after:
  mod chaining;
  mod nesting;
-->
```rust
use kladde::{Kladde, Persistable};
use kladde_types::{PersistableString, PersistableVec};

#[derive(Persistable)]
struct Journal {
    owner: PersistableString,
    entries: PersistableVec<PersistableString>,
}

fn main() -> kladde::Result<()> {
    let path = std::env::temp_dir().join("getting-started.kladde");
    let mut journal = Kladde::create(
        &path,
        Journal {
            owner: PersistableString::from("ada"),
            entries: PersistableVec::new(),
        },
    )?;

    journal
        .guard()
        .entries_mut()
        .push(PersistableString::from("first entry"))?;
    println!("{} entries", journal.get().entries.len());
    journal.close()?;

    let journal = Kladde::<Journal>::open(&path)?;
    assert_eq!(journal.get().entries.len(), 1);
    Ok(())
}
```

Four things are worth noticing.

**`Kladde<T>` is the root.**
It pairs your root value with the file that backs it.
Everything reachable from it is backed; a value you construct outside it is not, until you store it into something that is.

**Reading goes through `get()`**, which hands you `&T` — the plain value.
From there you use ordinary methods: `len()`, `iter()`, indexing.
No guard, no file access, no cost beyond the read itself.

**Writing goes through `guard()`**, which hands you a `JournalGuard`.
Every field of a derived type gets a `_mut()` accessor on its guard, which reborrows the same backend one level deeper.
So `journal.guard().entries_mut()` is a `PersistableVecGuard`, and `push` on it both records the change and mutates the vector.
It returns a `Result`, because recording is I/O.

**`open` reads it all back.**
`close` folds the journal into the file first, but it does not have to: a file closed by a crash opens just the same, with every mutation that returned `Ok`.

`Kladde::new(value)` does the same as `create` with a file that lives only in memory, which is handy for tests and for trying things out.

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
  fn nesting(journal: &mut Kladde<Journal>) -> kladde::Result<()> {
after:
  Ok(())
  }
-->
```rust
let mut guard = journal.guard();
let mut entries = guard.entries_mut();
entries.push(PersistableString::from("second"))?;
entries.push(PersistableString::from("third"))?;
```

Or in one chain, when you only need one mutation:

<!-- kladde-example: name=journal file=src/chaining.rs
before:
  use crate::Journal;
  use kladde::Kladde;
  fn chaining(journal: &mut Kladde<Journal>) -> kladde::Result<()> {
after:
  Ok(())
  }
-->
```rust
journal.guard().owner_mut().set("grace")?;
```

Hold a guard only as long as you need it.
It borrows the root, so nothing else can touch the structure while it is alive.
