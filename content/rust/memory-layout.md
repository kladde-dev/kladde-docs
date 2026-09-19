---
title: Memory layout
---

The Rust-specific half of the [in-memory structures](../impl/in-memory-state.md): concrete types, niches, and the alignment trade-offs behind them.

None of this changes what the structures *mean*; it is about making them small without making them awkward.
All of it is unmeasured, and recorded so that a later design change does not break it needlessly.

## The statement slab

The language-agnostic shape is a slab of `{ page_or_next, pins }` plus a parallel array of framing lengths.
The Rust realisation:

```rust
struct StatementSlab {
    records: Vec<StatementRecord>,  // 8 bytes per slot, naturally aligned
    framing_len: Vec<u8>,           // <= 21, see the spec's bounds
    free_head: u32,                 // first free slot, or 0 for "none"
}

struct StatementRecord {
    page_or_next: u32,
    pins: u32,
}

struct StatementRef(NonZeroU32);    // slot 0 is never handed out
```

**Why two parallel arrays rather than one packed record.**
Putting `framing_len: u8` inside the record makes it 9 bytes packed, or 12 with the natural trailing padding.
`#[repr(packed)]` gets the 9, but every field access must then be by value — `&rec.pins` on a packed field is undefined behaviour, which is awkward for the one field that is touched constantly — and a 9-byte stride puts records across cache lines irregularly.

Splitting gets the same 9 bytes per slot with no packing at all: the hot array is exactly 8 bytes and naturally aligned, and `framing_len` moves to a byte array it shares an index with.
That is also better for locality, since `framing_len` is read only when a statement dies while `pins` is touched on every fragment gained or lost.

**Why slot 0 is never handed out.**
It costs eight bytes once and buys two things.
It gives the free list a terminator, which nothing else could: page numbers and slot indices both use the whole `u32` range, so no value is spare.
And it makes `StatementRef` a `NonZeroU32`, so `Option<StatementRef>` is four bytes rather than eight — which saves twelve bytes per allocation, across the allocation map's `anchor` and `grow_witness` and the recyclable set's `tombstone`.

**Why `pins` is `u32` and not `u16`.**
`pins = F + A`, where `A <= 1` per statement — one statement is either the anchor or the grow witness, never both, since the first is a `Shrink`/`Tombstone` and the second a `Grow`.
`F` is the one that does not fit: `F <= payload_size` holds only for `Ref`, `Inline` and `Zero`, which win probes inside their own extent.
A `Shrink` or `Tombstone` wins the gaps *between* newer statements, anywhere in `[n, size)` or `[0, size)`, so its `F` is bounded by the allocation's fragment count and nothing smaller: `Shrink(id, 0)`, then a grow to 1 MiB and 100 000 scattered writes, leaves it owning about 100 000 fragments.

**A debug-only generation array** is worth keeping in mind.
The argument that stale `StatementRef`s cannot arise rests on every release site being correct; a parallel `Vec<u32>` of generations, checked in debug builds only, turns a violation into an assertion rather than silent corruption, at no release cost.

## A more compact `Fragment`

The safe facade is easy to work with and wastes two bytes:

```rust
use std::mem::MaybeUninit;

/// Safe facade: `size_of::<Fragment>() == 12`
enum Fragment {
	Bytes { offset: PageOffset, statement: StatementRef, page: u32 },
	ZeroExplicitly { statement: StatementRef },
	ZeroByDefault,
}

/// Compact internal representation for the B-tree: `size_of::<CompactFragment>() == 10`
#[repr(packed(2))]
struct CompactFragment {
    tag_or_offset: u16,
    statement: MaybeUninit<u32>,
    page: MaybeUninit<u32>,
}

struct Id(std::num::NonZeroU32);
struct StatementRef(std::num::NonZeroU32); // slot 0 is never handed out, see above

/// Invariant: never takes value 0xffff or 0xfffe (those offsets can't hold data due to the CRC)
struct PageOffset(u16);

impl From<CompactFragment> for Fragment {
    fn from(cf: CompactFragment) -> Self {
        match cf.tag_or_offset {
            0xffff => Fragment::ZeroByDefault,
            0xfffe => Fragment::ZeroExplicitly {
                statement: unsafe {
                    StatementRef(NonZeroU32::new_unchecked(cf.statement.assume_init()))
                }
            },
            offset => Fragment::Bytes {
                offset: PageOffset(offset),
                page: unsafe { cf.page.assume_init() },
                statement: unsafe {
                    StatementRef(NonZeroU32::new_unchecked(cf.statement.assume_init()))
                }
            },
        }
    }
}

impl From<Fragment> for CompactFragment {
    fn from(f: Fragment) -> Self {
        match f {
            Fragment::Bytes { page, offset, statement } => CompactFragment {
                tag_or_offset: offset.0,
                statement: MaybeUninit::new(statement.0.get()),
                page: MaybeUninit::new(page),
            },
            Fragment::ZeroExplicitly { statement } => CompactFragment {
                tag_or_offset: 0xfffe,
                statement: MaybeUninit::new(statement.0.get()),
                page: MaybeUninit::uninit(),
            },
            Fragment::ZeroByDefault => CompactFragment {
                tag_or_offset: 0xffff,
                statement: MaybeUninit::uninit(),
                page: MaybeUninit::uninit(),
            },
        }
    }
}
```

The discriminant is stolen from the page-offset field, using two values that a real offset can never take because [the page framing](../spec/file-format.md#page-framing) puts a CRC there.

## Where the integer widths come from

Every width below is forced by [the specification's bounds](../spec/address-table.md#bounds), not chosen:

| type | width | because |
| --- | --- | --- |
| `Id` | `NonZeroU32` | allocation ids are 32 bit; zero is reserved so `Option<Id>` is free |
| `PageOffset` | `u16` | page sizes are at most 64 KiB |
| `AllocationOffset` | `u32` | allocation sizes are bounded by `2^32` |
| page number | `u32` | page numbers are 32 bit |
| `StatementRef` | `NonZeroU32` | at most `2^32 - 1` statements in a file, and zero is the free niche |
| `framing_len` | `u8` | a statement's framing is at most 21 bytes |

A language without niche optimisation pays four to eight bytes more per optional reference and is otherwise unaffected.
