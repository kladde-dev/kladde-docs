---
title: kladde-rust
---

The Rust implementation of kladde, and the reference implementation of the [specification](../spec/).

Everything here is **specific to Rust** and should be expected to differ in a port.
The [file format](../spec/) and the [algorithms](../impl/) are shared; the trait shapes, the mutation-capture mechanism, the macros, and the memory layout are not.

## Two audiences

**If you want to use kladde in an application**, start with the [tutorial](tutorial/).
It assumes you know Rust and nothing about kladde, and works up from a first program to writing your own persistable types.

**If you want to work on kladde**, or port it, read the [specification](../spec/) and [implementation notes](../impl/) first — they carry the parts that are not about Rust — and come back here for the rest.

## What it is

A Rust workspace providing backed data structures: containers and derived types whose mutations are recorded durably as they happen.

<!-- kladde-example: name=notes file=src/main.rs mode=run deps=kladde,kladde-types
before:
  fn main() {
after:
  }
-->
```rust
use kladde::Kladde;
use kladde::Persistable;
use kladde_types::{PersistableString, PersistableVec};

#[derive(Persistable)]
struct Notes {
    title: PersistableString,
    lines: PersistableVec<PersistableString>,
}

let mut notes = Kladde::new(Notes {
    title: PersistableString::from("today"),
    lines: PersistableVec::new(),
});

notes.guard().lines_mut().push(PersistableString::from("wrote a doc"));
```

The `push` updates the in-memory vector and records the change.
There is no save call.

## The central problem

Rust has no way to intercept a field assignment.

Every comparable system relies on exactly such a hook: Python has `__setattr__`, Smalltalk has message dispatch, Swift has property wrappers.
Those systems make persistence syntactically invisible — `self.foo = bar` just works — because the language hands them a place to stand.

Rust hands them nothing.
So kladde-rust makes the recording point **explicit and unforgeable** instead: the only mutating API is the one that records, and it is reached through a `Guard`.

Everything idiosyncratic about this layer follows from that one constraint, and it is the single largest thing a port should expect to redesign.
A Python implementation should use property hooks and have no guards at all.

## The documents

| document | contents |
| --- | --- |
| [Crate layout](crates.md) | the workspace, the dependency direction, and where the seam falls |
| [Pointers](pointers.md) | the four handle types, and the ownership discipline they enforce |
| [Persistable and guards](persistable-and-guards.md) | the central traits, and why mutation needs a guard at all |
| [Containers](containers.md) | how the built-in containers are laid out and why |
| [The derive macro](derive-macro.md) | what it generates, and why guards are generated per type |
| [Freeing](freeing.md) | recursive reclamation — designed, not built |
| [Transactions](transactions.md) | the RAII types, and what happens on unwind |
| [Schema binding](schema-binding.md) | how a Rust type declares its descriptor |
| [Memory layout](memory-layout.md) | the Rust-specific half of the [in-memory structures](../impl/in-memory-state.md) |
| [Tutorial](tutorial/) | for application authors |

## Status

Not feature-complete, and the parts are at very different stages.
The design documents describe the system as intended and mark where the implementation falls short.

| area | status |
| --- | --- |
| Schema descriptors, encoding, fingerprints | implemented and specified |
| Schema binding to Rust types | implemented |
| Containers, derive macro, guards | implemented against an in-memory backend |
| `Persistable`, `Location` | implemented |
| Storage abstraction | implemented (in-memory only) |
| Journal | **known broken** — see below |
| Address table, copy-on-write pages | designed, not built |
| Real file storage | not started |
| Crash consistency | designed, not built |
| Freeing / reclamation | designed, not built |
| Schema evolution | designed, not built |

The previous heap — a relocatable heap over a flat address space, with incremental compaction — *was* implemented, measured and tuned, and has since been [superseded](../superseded/) by the page-oriented design.

### The journal, concretely

`JournaledWriteBackend` keeps **two** records of a transaction with no order relating them: a `pending: HashMap<Pointer, Size>` state snapshot, and a `journal: Vec<(Pointer, Size, Vec<u8>)>` operation log.
So any operation that invalidates an earlier log entry must reach into the log and repair it by hand, and exactly one does — the sizedness conversion scans the log re-anchoring writes, and nothing else repairs anything.

Three failures follow, and they are symptoms of the one structural problem that [the log-as-authority model](../impl/write-phase-state.md) fixes:

1. **`alloc → write → free` panics at flush.**
   The allocate and free annihilate inside `pending`, so the id is never claimed; the buffered write survives in the other structure and its replay looks up an id the heap has never heard of.
2. **A write followed by a shrinking resize silently corrupts a neighbour.**
   The seek does no bounds check, so a write buffered while the allocation was large replays at an offset that now lies outside it.
   Reproduced: allocate A at 128 bytes, write `0xAA` at offset 64, shrink A to 64, allocate B at 8 bytes; after the flush, B reads back as `[170; 8]`.
3. **A sizedness conversion of an allocation claimed by an earlier flush loses its content.**
   The immediate path is *mint → allocate new → copy `min(old, new)` → free old*; the deferred path performs only the mint, and the copy is not deferred or approximated but absent.
   This one disappears outright with [sizedness](pointers.md#what-sizedness-was-and-why-it-is-gone).

None of these is the fundamental problem.
The fundamental problem is that **the log does not record every mutation**, which shows up without any annihilation at all — see the dangling-pointer case in [the record set](../spec/journal.md#record-kinds).
