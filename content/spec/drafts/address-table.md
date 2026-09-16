# Address Table

## On-disk representation

### Logical level

On disk, the address table is a list of *statements*, written in one or more pages, where each statement implicitly inherits its page's `epoch`.
Statements from all live pages are merged, resolving conflicts by recency, see [[#conflict resolution across epochs]].

#### Statement types

| Statement                           | Existence of `id` | Size of `id`                                                     | Content of `id`                                                                    | Usage note                                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------- | ----------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Ref(id, offset, size, address)`    | exists            | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` equals `file[address, address + size)`. | `[address, address + size)` must be within a single `Data` page. As a *writer invariant* (readers never depend on it), every byte in a `Data` page is referenced by at most one live `Ref`; this is not needed for correctness but keeps data-page coverage a plain counter instead of a refcount. Relaxing it later would enable `O(1)` allocation clones and is a format-compatible policy change, see [[#Design directions and trade-offs]].                                                                                                                                                                  |
| `Undefined(id, offset, size)`       | exists            | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` is `Undefined`.                         | Kladde is allowed to hand out arbitrary data for `Undefined` regions, and consolidation may rewrite `Undefined` regions with `Ref` or `Inline` with arbitrary data (useful for data types that have long sequences of repeated small `[data] [undefined] [data] [undefined] ...` patches, which could happen for arrays of enums with slightly unbalanced payload sizes per variant). |
| `Shrink(id, n)`                     | exists            | `>= n`, and **anchors** the size: extents below this statement's epoch drop out | Anything at or beyond `n` is `Undefined`.                                          | The size-reducing half of the old `Size` statement. Its content claim is dormant while the allocation stays at `n`, and activates if the allocation later grows past `n` — which is exactly what stops a truncated tail from resurfacing.                                                                                                                                              |
| `Grow(id, n)`                       | exists            | `>= n`                                                           | **Nothing.** Matches no probe.                                                     | The size-increasing half of the old `Size` statement, carrying no content claim. Growing never needs to deny anything: every statement that could cover a probe at or past the old size has an epoch below the anchor and is therefore already denied by it. `Grow(id, 0)` is how an existent zero-sized allocation is stated.                                                        |
| `Tombstone(id)`                     | does not exist    | `0`                                                              | All bytes are `Undefined`.                                                         | Only has an effect if an older live page states existence of `id` and no newer live page states existence of `id` and fully overwrites any size and content remaining from the old incarnation.                                                                                                                                                                                       |
| `Inline(id, offset, size, payload)` | exists            | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` equals `payload`.                       | Small-size optimization. Requires `1 <= size <= 251` — one less than before the `Grow`/`Shrink` split, since the payload length is encoded as an offset past the last statement tag. Zero-sized payloads stay unrepresentable, and nothing needs them: `Grow(id, 0)` states an existent zero-sized allocation.                                                                        |

#### No conflicts within each epoch

Statements in the same `epoch` must not conflict with each other:

- no two statements in the same `epoch` may make contradicting statements about the existence of an `id`;
- no two statements in the same `epoch` may name any byte of content twice, not even if the two statements assign the same value to that byte.
  This includes `Undefined`: an assignment of `Undefined` to a given byte — whether by an `Undefined`, `Shrink`, or `Tombstone` statement — conflicts with any other assignment to the same byte, including a second `Undefined` assignment.
- At most one `Shrink` and at most one `Grow` statement per `id` is allowed per epoch, and never both: a flush knows whether it is growing or shrinking.
- Multiple `Ref`, `Undefined`, and `Inline` statements for pairwise disjoint ranges within the same allocation are allowed.
  They don't conflict in regards of the size of the allocation because they all merely bound the size of the allocation from below.
- However, a `Ref`, `Undefined`, or `Inline` statement conflicts with a `Shrink(n)` statement in the same `epoch` if its lower bound `offset + size` is larger than `n`, and this is not allowed in a single `epoch`.
  A `Grow` never conflicts with anything, since it claims no content and only bounds the size from below.

#### Conflict resolution across epochs

Two statements with different `epoch` are allowed to contradict each other.
In this case, the newer statement (the one with the higher `epoch`) takes precedence, but only as far as it contradicts the older statement.
Any part of the older statement that doesn't contradict the newer statement survives, and this is resolved at the granularity of the allocations existence, its size, and the value of each of its content bytes:

- **Existence:** allocation `id` exists if the statement with highest `epoch` that mentions `id` is `Ref`, `Undefined`, `Shrink`, `Grow`, or `Inline`.
  It does not exist if the statement with highest `epoch` that mentions `id` is `Tombstone(id)` or if no statement mentions `id`.
- **Size:** let `anchor_epoch` be the largest `epoch` of any `Shrink(id, n)` **or** `Tombstone(id)` statement (or `anchor_epoch = -1` if the id has neither), and call the statement at that epoch the *anchor*, whose `n` is `0` when it is a `Tombstone`.
  The size of allocation `id` is then the maximum over:
	- the anchor's `n`, if the id has an anchor;
	- the `n` field of every `Grow(id, n)` statement with `epoch > anchor_epoch`; and
	- all values `offset + size` of all `Ref(id, offset, size, ...)`, `Undefined(id, offset, size)` and `Inline(id, offset, size, ...)` statements with `epoch > anchor_epoch`.
  The maximum is over a non-empty set whenever the allocation exists, since existence requires at least one statement mentioning `id` and forbids the newest of them from being a `Tombstone`.
  A **non-existent** allocation falls out of the same rule with size `0` and needs no separate case: its newest statement is a `Tombstone`, which is therefore its anchor, and nothing lies above it.
  (Existent zero-sized allocations are still distinguished from non-existent ones — by the **Existence** rule above, not by this one.)
  Note that only `Shrink` and `Tombstone` anchor the epoch; a `Grow` contributes a lower bound and nothing else, which is why dropping one can never readmit older extents.
  A `Tombstone` anchors at `n = 0` and denies every probe, so a re-allocated id is described entirely by the statements above its tombstone.
  Also note that, under the "[[#no conflicts within each epoch]]" rule, the predicates `epoch > anchor_epoch` above could be relaxed to `epoch >= anchor_epoch` without changing semantics; we choose the stricter formulation `epoch > anchor_epoch` here to be more robust to buggy writers that violate the "non conflict within each epoch" rule.
- **Content at a given offset `probe`:** defined by the latest statement that sets the value of this content, i.e., the statement with highest `epoch` that matches any of the following patterns:
	- `Tombstone(id)`;
	- `Shrink(id, n)` where `n <= probe`; or
	- `Ref(id, offset, size, ...)`, `Undefined(id, offset, size)`, or `Inline(id, offset, size, ...)` where `offset <= probe < offset + size`.
  A `Grow` statement never matches, which is the whole of the difference between the two halves of the old `Size`.
  There must be at most one winner because at most one statement matching the above criteria is allowed per `epoch` (see [[#no conflicts within each epoch]]).
  Given the winning statement, the content of allocation `id` at offset `probe` is defined as follows:
	- If there is no matching statement or if the winning statement is of type `Tombstone`, `Shrink`, or `Undefined`: the content of allocation `id` at offset `probe` is `Undefined`.
	- if the winning statement is `Inline(id, offset, size, payload)`: the content of allocation `id` at offset `probe` is `payload[probe - offset]`.
	- If the winning statement is `Ref(id, offset, size, address)`: the content of allocation `id` at offset `probe` is `file[address + (probe - offset)]`.

### Physical level

There is only a single `kind` for address table pages: `kind == AddressTable`.
The header page is always an `AddressTable` page.
Every `AddressTable` page contains zero or more references to other `AddressTable` pages followed by zero or more statements.
References between `AddressTable` pages form a tree rooted at the header page.

```
address_table_page := num_children:varint child_ref{num_children} num_statements:varint statements:statement{num_statements}
child_ref          := page_number:varint  ; the page number (not the start address of the page)
statement          := id_delta (tagged_ref | tagged_undefined | tagged_shrink | tagged_tombstone | tagged_grow | inline)
id_delta           := varint              ; increment from id of last statement (for first statement in page: id)
tagged_ref         := 0:byte offset:varint size:varint address:varint
tagged_undefined   := 1:byte offset:varint size:varint
tagged_shrink      := 2:byte n:varint
tagged_tombstone   := 3:byte              ; tombstones have no payload apart from the id
tagged_grow        := 4:byte n:varint

; inline is optimized for lots of small payloads. size_tag = size + 4 >= 5 can't clash.
; size >= 1, so zero-sized Inline payloads stay unrepresentable; max size is 255 - 4 = 251.
inline             := size_tag:byte offset:varint payload:byte{size_tag - 4}
```

Notes on the physical format:

- Statements within a page are sorted by `(id, offset)`, so `id_delta` is never negative; a further statement about the same id has `id_delta == 0`.
- The root `AddressTable` page *is* the current header slot (pages 0/1, alternating per the CoW design), so before the payload described by this grammar it carries the file-header fields — magic, format version, page size, epoch, journal-segment pointer — all covered by the header CRC.
- A `child_ref` deliberately stores only a page number.
  The child's own framing carries its epoch, and invariant I1 of the CoW design guarantees that a page referenced by a valid header is present and intact, so a per-child expected-epoch field would only duplicate the end-to-end assertion that the child's own `epoch` field already provides.
- Nothing in the format constrains *which* page a statement lives in or how the tree is shaped; those are writer policies, specified in [[#Maintenance]].

## In-memory representation

The in-memory state after a bulk read is the page images themselves plus three indexes over them.
Content is never copied out of the page images: `Ref` and `Inline` content both have a file address (an `Inline` payload lives inside an address-table page, but it is bytes at a known address all the same), so every resolved fragment points into the in-memory copy of the file, and `Undefined` needs no bytes at all.

**1. The fragment map**, which everything else hangs off: a B-tree `(id, offset) → Fragment`, where each entry describes the resolved content from `offset` up to the next key with the same `id`:

```rust
struct Fragment {
    source: Source,                  // address into a Data or AddressTable page, or Undefined
    statement: Option<StatementRef>, // the live statement this fragment survives from;
                                     // None when the range resolves to Undefined by default
}

struct StatementRecord {     // one per statement that still has a reason to live
    page: PageNumber,        // where its encoding lives
    framing_len: u16,        // encoded size, excluding any Inline payload
    pins: u16,               // reasons this statement must stay; see below
}
```

A fragment stores no length — its extent runs to the next key for the same id, or to `size` for the last one — so a hole in the map would not describe a gap but silently extend the preceding fragment, and the "greatest key `<= probe`" lookup would answer with a neighbour's bytes.
The fragments of an existing id therefore **exactly partition `[0, size)`**, ranges that resolve to `Undefined` by default included; those are the entries whose `statement` is `None`, and they pin nothing, which is exactly right, since a range that resolves by default depends on no statement staying alive.
[[address-table-walkthrough#Preliminaries]] specifies the map in full.

Content bytes are deliberately *not* counted here; [[#What counts as a live byte]] charges them to whichever page physically holds them, which is what makes `Inline` payloads accounted per byte without any per-statement counter.

`pins` is a single refcount over heterogeneous holders, and a statement is live exactly while `pins > 0`.
Two things take a pin, and which of them a statement can hold depends on its kind:

1. **`F`** — each **fragment** resolving to the statement. Any kind.
2. **`A`** — the resolved size depends on this statement. Held by the **anchor** (the newest `Shrink` or `Tombstone`), because dropping it moves `anchor_epoch` and readmits older extents; and by a `Grow(id, n)` with `n == size`, because it may be the sole witness of that size.

So a content statement holds only `F`, a `Grow` holds only `A`, and a `Shrink` or `Tombstone` holds `A` and `F`.
One rule governs both of the latter: **a `Shrink` or `Tombstone` may be dropped iff it is not the anchor and owns no fragment**, derived in [[#What counts as a live byte]].

**A tombstone is a matcher, and therefore an ordinary anchor.**
It is a `Shrink(id, 0)` that also denies existence, and the in-memory resolver represents it exactly as [[#Conflict resolution across epochs]] does: it matches every probe, so it wins — and owns as fragments — whatever ranges a re-allocated incarnation leaves uncovered.
While the id does not exist its size is 0, so no probes exist and it owns nothing; it is then the anchor, and that alone keeps it alive.
An earlier draft inverted this into an epoch floor, discarding every statement at or below `tombstone_epoch`, so that a tombstone could never own a fragment and needed a denial pin instead.
The two framings resolve identically — a statement can only win by outranking every matcher, and the tombstone matches all of them — but the matcher framing is what lets one droppability rule cover `Shrink` and `Tombstone` alike, and it costs no fragment-map entry, only a pin, since an uncovered range has an entry either way.
A resolver may still walk each id's statements newest-first and stop at the first tombstone; that is an implementation shortcut now, not a separate model.

The name is deliberate: this is a refcount whose holders are of different kinds, so `liveness` would read as a boolean or an enum, and `refcount` would say nothing about what is doing the referring.
"Pin", in the buffer-manager sense of *something prevents this from being reclaimed*, is exactly the relationship, and it stays accurate across both reasons.
A single counter is still the right shape, because a `Shrink` or a re-allocated id's `Tombstone` holds `A` and `F` simultaneously and no consumer ever asks which kind a pin is; two fields would encode a distinction that is never read.

This is the draft's `(id, offset) → Option<Address>` B-tree and its `(id, offset) → Statement` B-tree collapsed into one structure: the fragment map *is* the resolved view of [[#conflict resolution across epochs]], computed once during the bulk read and maintained incrementally afterwards, so epochs never need to be consulted again at run time.
Content queries resolve `(id, offset)` in one lookup, `O(log F)` for `F` live fragments; sequential reads iterate from there.

**2. The allocation map**: a hash map `id → AllocationMeta { size, fragment_count, statement_bytes, mentions, anchor, grow_witness }`.
`size` answers size queries in `O(1)`.
`fragment_count` and `statement_bytes` measure the allocation's *description overhead* relative to `size`, which drives defragmentation in [[#Maintenance]].
`anchor` holds the id's newest `Shrink` **or** `Tombstone` and `grow_witness` the `Grow` with `n == size`, if any, so the `A` pin can be moved as either changes hands; the two are separate slots because both can be pinned at once, for different reasons — the anchor because dropping it readmits older extents, the witness because it may be the sole statement achieving the size.
No separate `tombstone` slot is needed: a tombstone *is* an anchor, and an older tombstone superseded by a newer one is droppable on sight under the rule above, since the newer one matches every probe and outranks it.
`mentions` counts the statements naming this id that are **physically present** in live address-table pages — *not* the ones that are live in the resolution sense.
That distinction is the whole point: a statement shadowed into irrelevance still sits in its page, and still resurrects if the thing shadowing it disappears, so it must keep being counted until a page rewrite actually drops it.
It is incremented when a statement naming the id is written or read at open, and decremented when one is dropped during consolidation or when its page is reclaimed.
It has exactly one reader: **a tombstone anchor releases its `A` pin when `mentions` falls to 1**, since the tombstone is then the id's last physically present statement and the id reads as non-existent without it.
Until that point the tombstone must outlive every older statement mentioning the id, or dropping it would let one of them decide existence again and resurrect the allocation — and no other structure sees those statements.
`mentions` does **not** gate id recycling ([[#Tombstones and id recycling]]).

**A tombstoned id keeps no `AllocationMeta` entry.** The flush that frees an id removes it and moves the only two fields a non-existent id still reads — `mentions` and a reference to its tombstone — into the id allocator's recyclable set, which is tracking that id anyway.
The rest is either constant (`size` 0, `fragment_count` 0, `grow_witness` `None`) or already recorded in the tombstone's own `StatementRecord` (`statement_bytes`, its two framing bytes).
A page rewrite that decodes a statement therefore looks its id up in the allocation map first and in the recyclable set if it is not there; the tombstone reference is what the `A`-pin release has to reach, what a consolidator consults before re-emitting a tombstone it has decoded, and what becomes `anchor` if the id is allocated again.
Within the recyclable set `mentions` only falls, since nothing writes a statement naming a non-existent id, and at 0 both fields are spent and the entry is a bare recyclable id.

See [[address-table-walkthrough#Preliminaries]] for a field-by-field account of when each of these is written and read.

**3. Page tables**: for every page its kind, epoch, and live-byte counter (`Data`: referenced bytes; `AddressTable`: per [[#What counts as a live byte]] below).
Pages are bucketed by live fraction (a handful of buckets suffices) together with an age mark, making victim selection `O(1)` rather than a priority queue's `O(log P)`, with `O(1)` bucket moves as counters change.
A free list tracks reusable pages, under the two-generation quarantine of the CoW design (a page freed by commit `E` becomes writable in flush `E + 2`).

Two derived structures complete the picture, both rebuilt at open and never persisted: the **id allocator** (next fresh id, plus recyclable ids — those that do not exist and have a committed tombstone, per [[#Tombstones and id recycling]], each carrying the `mentions` count and tombstone reference that its `AllocationMeta` entry left behind), and the **eviction clock** over the statements currently buffered in the header page ([[#Maintenance]]), which records how many flushes each has gone untouched.

Maintenance of all of the above costs `O(log F)` per fragment created or destroyed, and a flush creates or destroys at most a small multiple of the statements it writes.

### What counts as a live byte

A page's live-byte counter — its `coverage`, in the vocabulary of [[cow#Coverage accounting]] — is what makes it a consolidation victim, so it has to fall as the page's contents become useless.
`AddressTable` pages need no separate mechanism for this: **one rule covers both page kinds**, and it is the rule data pages already follow.

**Content bytes are charged to the page that physically holds them.**
Every fragment names a `source` address, so when a fragment is destroyed, decrement the coverage of the page that address falls in, by the fragment's length.
A `Ref`'s fragments point into a `Data` page; an `Inline`'s fragments point into the `AddressTable` page carrying the payload; an `Undefined` fragment points nowhere and costs nothing.
An inline payload is therefore just content that happens to live in a table page, and it gets per-byte accounting for free — no per-statement payload counters, no special case, the same line of code.
Splitting a fragment changes nothing, since both halves still cover the same bytes.

**Framing bytes are charged to the statement's own page, and released in one step** when the statement loses its last reason to live.
The two charges cover disjoint byte ranges of the encoding, so they cannot double-count.

Per-byte accounting for inline payloads is what the design actually needs, and it is worth saying why, because statement-granularity liveness looks adequate until it isn't.
For a `Ref`, partial shadowing strands only ~10 bytes of framing in the address-table page, while the content bytes it no longer reaches are tracked exactly in the data page's counter, which duly falls.
For an `Inline` there is no data page: the payload *is* the content, so charging it all-or-nothing would let shadowing half of a 200-byte inline strand 100 bytes that no counter in the system ever notices, and a page holding sixty mostly-shadowed inlines would report itself nearly full and never be cleaned.
Routing the charge through `fragment.source` avoids that without treating inlines specially at all.

**Framing is released when the statement's `pins` reach zero**, and which pins a statement can hold is what the rest of this section is about.

| Statement kind | Can be pinned by | Content bytes, and where charged |
| --- | --- | --- |
| `Ref` | fragments (`F`) | a `Data` page; released per fragment |
| `Inline` | fragments (`F`) | its own `AddressTable` page; released per fragment |
| `Undefined` | fragments (`F`) | none |
| `Shrink` | fragments (`F`) **and** anchor (`A`) | none |
| `Grow` | size witness (`A`) only — never fragments | none |
| `Tombstone` | fragments (`F`) **and** anchor (`A`), exactly as a `Shrink` | none |

**Content statements need only fragment pins**, because a content statement's denial is exactly co-extensive with what it defines.
If a `Ref` covering `[a, b)` has lost every fragment, then every byte of `[a, b)` is covered by something strictly newer, and that newer thing also outranks everything the `Ref` was denying — so dropping it can resurrect nothing.
The size formula is safe too: covering `[a, b)` requires some newer statement to reach offset `b`, so the id's size cannot shrink when the `Ref` goes.

**A `Shrink` needs no denial pins either**, which is the least obvious entry in the table.
A `Shrink(id, n)` may be dropped iff it is **not the anchor and owns no fragment**:

- it cannot affect the size, since a non-anchor `Shrink` has an epoch below `anchor_epoch` and the `max` term ranges only over `Grow` bounds and `Ref`/`Undefined`/`Inline` extents above the anchor;
- it cannot affect content, since it wins exactly the probes in `[n, size)` that nothing newer covers, and those are precisely its `F` pins;
- and a later *grow* cannot expose anything either, because the anchor always has `n <= size` and so already denies every probe from `size` upward.

**A `Grow` is simpler still: it holds only `A`, and its liveness is locally decidable.**
Matching no probe, it can never own a fragment, so it never becomes a fragment-pinned stray.
It is dead as soon as `size > n`, since some other term then achieves the maximum; as soon as `n <= anchor.n`, since the anchor's own term then achieves it; and as soon as it falls below `anchor_epoch`, which tombstoning also causes, since the tombstone anchors above it.
All three tests are `O(1)` against `AllocationMeta`.
Because a `Grow`'s bound strictly exceeded the size at emission, and any later decrease moves the anchor above it, the bounds of the `Grow` statements above the anchor are distinct and **at most one `Grow` per allocation can be alive** — the one with `n == size`.
Dropping a `Grow` also carries no [[address-table-walkthrough#(v) Consolidate `L2`, the leaf holding the anchor — break 1]] hazard, because it does not anchor the epoch and so can only lower the `max`, never readmit older extents.

The `A` pin carries the whole burden for the case that matters, and it is not substitutable.
With `Grow(id, 100)` at epoch 5 and `Ref(id, 0, 10)` at epoch 9 and nothing older, `[10, 100)` resolves to `Undefined` *by default*, so the statement owns no fragment and denies nothing, yet dropping it would shrink the id from 100 to 10.
More starkly: an allocation created and never written has `Grow(id, n)` as the *only evidence it exists*, and when `n` is 0 there is not even a default-`Undefined` range to appeal to — `Grow(id, 0)` is precisely how an existent zero-sized allocation is stated.

**A `Shrink` still holds two kinds of pin at once**, which is what keeps a single counter the right shape.
With `Ref(id, 0, 1000)` at epoch 3, `Shrink(id, 10)` at epoch 5 and `Ref(id, 50, 10)` at epoch 9, the id's size is 60 and the range `[10, 50)` resolves *through* the `Shrink` — so it holds `F` and `A` together.
Nothing in the design ever asks **which kind** of pin a statement holds; every consumer asks only whether any remain.
Two fields would therefore encode a distinction that is never read, at two bytes per live statement, while inviting the bug where a predicate checks one field and forgets the other.

**A tombstone takes the same two pins a `Shrink` does**, and the same rule retires it: not the anchor, owns no fragment.
It claims *every* probe, so when its id is allocated again beneath it, it wins whatever ranges the new incarnation leaves uncovered and takes an `F` pin for each — and those pins are load-bearing, since dropping it would let the previous incarnation's statements win those probes.
While the id does not exist its size is 0, so no probes exist and it owns nothing; it is then the newest `Shrink`-or-`Tombstone`, hence the anchor, and `A` alone keeps it alive.
An older tombstone superseded by a newer one is neither, so it is droppable on sight, which is why `AllocationMeta` needs no tombstone list and no tombstone slot.

**The one thing the two pins do not decide is the last tombstone**, and this is where `mentions` earns its place.
A tombstone that is its id's newest statement is the anchor, so `A` holds it — correctly, because dropping it while any older statement mentioning the id is still *physically present* would let that statement decide existence again and resurrect the allocation.
The count must be over physically present statements, not resolution-live ones: a statement with zero pins that has not yet been swept out of its page resurrects just as well.
So a tombstone anchor **releases `A` when `mentions` falls to 1**: it is then the id's last statement, the id reads as non-existent without it, and `pins == 0` is once again an exact droppability test.

**A possible extension, not part of this proposal: retiring a tombstone anchor by counting.**
An id's live statements are exactly the owners of its fragments together with `anchor` and `grow_witness`, since `F` and `A` are the only pins there are.
Walking the id's fragments and collecting the distinct owners therefore yields a live count, and comparing it with `mentions` says whether any *dead* statement naming the id is still physically present.
For a tombstone that test is exact, by the lemma that nothing older than a tombstone is live ([[address-table-walkthrough#Findings]] item 3): if no dead mention remains then nothing older than the tombstone remains at all, so dropping it could change neither existence nor the size, and it could retire while still the anchor — which the `mentions == 1` rule above never permits for an id that has been re-allocated, since the new incarnation's own statements hold `mentions` above 1.
For a `Shrink` the same test would be unsound, and instructively so: statements older than a `Shrink(id, n)` stay *alive* below `n` and carry their extents with them, so `Shrink(7,10)`@5 of [[address-table-walkthrough#(v) Consolidate `L2`, the leaf holding the anchor — break 1]] would pass the test while `Ref(7,0,1000,P1)`@3 is live with an extent of 1000, and dropping the anchor would take the size from 60 to 1000.
The cost is `O(fragment_count)` per check, which is why it is left out for now; it is worth revisiting if tombstones of re-allocated ids turn out to linger in practice.

This produces a cascade that clears itself without any special pass.
Statements shadowed by a tombstone contribute no content bytes, so their pages sink toward zero live fraction and become victims; consolidating those pages drops the statements, which decrements `mentions`; when it reaches 1 the tombstone goes dead, sinking *its* page's live fraction, which eventually recycles it too.
Nothing has to reason about epochs to make this happen — only about counters.

**When to emit, and the residual.**
A **grow** emits a `Grow(id, S_new)` only when nothing the flush itself writes reaches offset `S_new` — an exact, `O(1)` test, since everything not written by this flush is bounded by the old size.
The common append pattern, growing *and* writing at the new end, therefore emits nothing at all.
A **shrink** always emits a `Shrink(id, S_new)`: old extents above the anchor may exceed the target, and deciding whether any survives would need the id's *physically present* statements, which no structure reaches — the fragment map indexes only live fragments, `StatementRecord` exists only for statements with a reason to live, and `mentions` is a count rather than a list.
A superfluously emitted `Shrink` has a short life: it becomes the anchor, holds `A` and nothing else, and dies at the next shrink, which takes `A` from it — unless an intervening grow handed it fragments over `[n, size)`, in which case it was not superfluous for long.

**A `Shrink` can raise the size, not only lower it.**
`Shrink(id, n)` asserts `size >= n` — as anchor it contributes `n` to the `max` — as well as denying content at or past `n`.
Relative to its absence it lowers the size by excluding older extents and raises it through its own bound, and which effect dominates depends on what else is physically present.
Consolidation can remove the older extents it was excluding while the statement itself stays, after which only the raising effect remains: with `Shrink(id, 30)`@10 as anchor and `Ref(id, 0, 10, P)`@11 as the only other content statement, the size is 30 with the `Shrink` and 10 without it, so an otherwise identical file lacking the statement holds a *smaller* allocation.
This is not a defect — the statement then behaves exactly as a `Grow(id, 30)` would — but a consolidator cannot rewrite it as one, since it cannot verify that nothing older still reaches past 30; the name records the operation that emitted the statement, not its steady-state role.
The same blindness has a mirror image: a dead, superfluous `Shrink` still sitting in a leaf beneath the current anchor makes that anchor load-bearing, because dropping the anchor would re-anchor at the dead one and readmit its bound into the `max` ([[address-table-walkthrough#(ii) Resize to 55 bytes]]).

The residual is confined to `Shrink`.
An anchor can be redundant — with `Shrink(id, 100)`@5 and `Ref(id, 0, 100, X)`@9, dropping it would leave the size at 100 either way — and the `A` pin keeps it alive regardless.
Non-anchor `Shrink` statements can be redundant and alive too, whenever they own a fragment over a range that would read `Undefined` anyway, so the over-count is not bounded at one per allocation.
What bounds it is that the live set is contained in the running-minimum-bound chain read newest-first: a `Shrink` can own a fragment only if its bound is strictly below every newer one's, so any shrink to bound `b` permanently retires every `Shrink` with bound `>= b`, and a later grow never revives one.
Whether a given link of that chain is a stray or load-bearing depends on whether anything older still reaches its window — the same question the emission test cannot answer.
Building such a chain now requires *alternating* shrinks and grows rather than a run of plain resizes, because grows no longer plant content claims — which is the main thing the `Grow`/`Shrink` split buys.
See [[address-table-walkthrough#How stray `Size` statements accumulate and are pruned]] for the derivation.
And the counters are policy, not correctness: an over-count only delays cleaning, an under-count only cleans a page earlier than ideal, and neither can make a resolved read wrong.

[[address-table-walkthrough]] traces all of this through a concrete allocation and stress-tests it against sequences designed to break it.

## Maintenance

### Goals, and the one currency behind them

Everything below optimizes a single currency: **pages written now versus pages occupied — and rewritten again — later.**
The individual goals — few live address-table pages, few `Data` pages written per flush, few live `Data` pages — are all instances of it, and so is their apparent tension, which dissolves once statements can shadow *parts* of older statements: writing one large `Ref` for a bulk write and overwriting only the bytes a later flush actually changes are not alternatives.
The small overwrite simply shadows a slice of the large `Ref`, which stays live for everything else.

What accumulates instead is **description debt**: after many small overwrites, an allocation is described by one big `Ref` plus dozens of patches, which costs address-table bytes, fragment-map memory, and read assembly.
Paying that debt down — rewriting the patched region as one fresh `Ref` into fresh `Data` pages — is just another way of spending the same write budget.

So the design has exactly one knob, the per-flush **maintenance budget** in pages beyond the flush's own dirty content, and one queue of candidates competing for it, ranked by `bytes reclaimed / bytes written`:

1. **Data-page consolidation** — relocate the live bytes of low-live-fraction `Data` pages into the fill of pages being written anyway; reclaims dead data bytes.
2. **Address-table consolidation** — rewrite the live statements of low-live-fraction `AddressTable` pages, gathered by id range and emitted sorted and dense; reclaims dead statement bytes and restores delta-encoding density.
3. **Allocation defragmentation** — rewrite a heavily patched region of one allocation as a single fresh `Ref` (for `[data] [undefined] [data]` stipple, as a single `Ref` with arbitrary bytes in the don't-care gaps, as the `Undefined` usage note permits); reclaims statement bytes *and* the partially dead data behind the patches, and is the only candidate type that also shrinks the in-memory fragment map.

All three emit the same kind of output — fresh pages plus fresh statements — so they compose with the flush's ordinary work.
Ranking needs no cleverness: type-1 and type-2 candidates come from the live-fraction buckets in `O(1)`; type-3 candidates from an analogous bucket structure over `statement_bytes / size` in `AllocationMeta`.
The budget itself is the policy: spend at least enough to keep the reusable-page supply ahead of consumption (the pacing obligation of the CoW design), at most a constant number of pages per flush, highest ratio first.

On the requested complexity bound: strictly logarithmic *per flush* is unattainable, because a flush must at least record what it changed.
What the design achieves is the attainable form: `O((k + s) · log F)` in-memory work for a flush dirtying `k` ranges and rewriting `s` statements under the budget, `O(1)` victim selection per candidate, and page writes bounded by `dirty data pages + c + depth` for budget `c` — with the common small flush writing **no** address-table page beyond the header, as follows.

### The header as write buffer

The root of the address table is the header page, and the header page is rewritten by every flush no matter what.
That page is free real estate, so the design uses it as the address table's **write buffer**: every statement a flush produces lands in the header first, at zero additional page writes.
A flush that touches three small allocations spends a few bytes in a page it was writing anyway; that is the entire address-table cost of a small flush, and — via `Inline` — the entire cost of a small flush over small allocations, period.

Because the header is rewritten each flush, its statements are re-stamped with the current epoch every time.
That is sound under one discipline: **the header states resolved truth**.
Carried-over statements are merged with the flush's new ones first, and only surviving fragments are re-emitted, narrowed to their surviving ranges; a statement whose every fragment has been shadowed is dropped rather than carried.
Re-stamping resolved truth at a higher epoch is idempotent under [[#conflict resolution across epochs]], and the rules of [[#no conflicts within each epoch]] hold by construction, because resolved truth cannot contradict itself.

This discipline also means header-resident `Inline` statements never fragment: a partial overwrite of one is merged into a single re-emitted `Inline` covering the union, so a small allocation under active rewrite keeps exactly one statement no matter how often it is touched.
Fragmented inlines can therefore only arise in *evicted* statements — a leaf-resident inline partially shadowed by a later flush — which is precisely the case the per-byte payload accounting of [[#What counts as a live byte]] exists to expose.
A worthwhile refinement, at the writer's discretion: when a flush partially shadows an evicted inline, pull the survivor back into the header as one merged `Inline` rather than leaving a patch behind.
That costs at most the payload's length in header bytes, kills the leaf statement outright instead of stranding part of it, and turns a partial shadow into a full one — cheap here precisely because the payload is small by construction.

When the header overflows, the flush **evicts** statements into one fresh leaf page: the coldest statements — untouched for the most flushes, per the eviction clock — sorted by id and delta-encoded.
Hot statements stay in the header, so allocations under active rewrite generate no leaf garbage at all; a statement reaches a leaf only once it has stopped changing, which is exactly when writing it to a durable resting place is cheap.
This is generational: the header is the young generation, the leaves are the old one, and the assumption doing the work — most garbage is young — is the same one behind generational collectors and LSM memtables.

### The shape of the tree

The tree above the leaves exists only so that the root can *find* them, and that changes what kind of tree it should be.
It is a **directory, not a search structure**: every lookup is served by the in-memory fragment map, nothing ever descends the tree by key, so there is no ordering invariant to maintain across pages — and a B-tree's ~25 % expected slack is precisely the price of maintaining that invariant under inserts, so dropping the invariant drops the slack.
Interior pages are packed full, in arrival order; at each level only the newest interior page is partially filled.

- While everything fits in the header: zero extra pages — the draft's case (i), and the steady state of every small file.
- Overflow spills to leaves referenced directly from the header, with statements continuing to share the header alongside the `child_ref`s — case (ii).
  At ~3 bytes per `child_ref`, a header can reference on the order of a thousand leaves, i.e. a few MiB of statements, before this stops sufficing.
- Beyond that, one layer of interior pages (header → interiors → leaves) reaches roughly a million leaves; deeper is not going to be needed.

Structural updates are copy-on-write path copies, but the paths are short and the root is free: adding or removing a leaf rewrites at most `depth − 1` pages besides the leaf itself, the root being rewritten anyway.
Because interior pages are plain arrays of page numbers, removing a leaf's entry compacts its interior page for free during that rewrite — the opportunistic consolidation the draft asked for falls out of CoW by itself.
A depth change is a single-page event: when the header's child list outgrows its budget, move the whole list into one fresh interior page and reference that instead.

One soft property is worth cultivating without depending on it: eviction and consolidation both emit leaves sorted by id, so leaves *tend* to cover coherent id ranges, which keeps type-2 consolidation windows aligned with page boundaries and keeps delta encoding dense.
Nothing breaks as this degrades; consolidation restores it.

### Tombstones and id recycling

A `Tombstone(id)` of an id that has **not** been re-allocated is needed exactly as long as some *physically present* statement in a live address-table page still names `id` — otherwise the freed allocation would resurrect — and it can be dropped by any rewrite of the page holding it once that stops being true.
It is the newest statement for its id, hence the anchor, so it holds `A` until `mentions` falls to 1 and its `pins` reach zero ([[#What counts as a live byte]]).
Neither condition is persisted; both are properties of the physically present statement set, re-derived at open from the same bulk read that builds the fragment map, and maintained in `O(1)` per statement written or dropped thereafter.

A `Tombstone(id)` of a **re-allocated** id is judged on this allocation's own terms instead, by the rule that governs every anchor: it stays while it is the anchor or owns a fragment, and goes as soon as a newer `Shrink` supersedes it and content covers the ranges it was winning.
Nothing in cold pages delays that, which is the practical difference between this model and the denial-pin model it replaces.

**Recycling an id does not wait for either.**
An id becomes reusable as soon as a tombstone for it is committed, whatever `mentions` says, so a handful of dead statements stranded in cold pages can never consume the id space.
This is safe because the tombstone matches every probe and outranks every statement below it: the new incarnation's statements are written above it, and the old incarnation can therefore never win a probe, no matter how much of it survives physically.
An earlier draft required `mentions == 0` before recycling, which coupled id availability to the cleaning of unrelated pages — the hostage effect — for no correctness benefit.

Re-allocating a tombstoned id does **not** retire the tombstone, which is worth stating because the opposite is the intuitive guess.
The tombstone is exactly what keeps a smaller re-allocation from exposing the tail of the old one, and it says so in the ordinary way: it owns the uncovered ranges as fragments, and its `F` pins hold it there until something newer covers them.

Two obligations come with recycling this early:

- **The newest tombstone must not be dropped while an older incarnation's statements can still win a probe.**
  With `Ref`@3, `Tombstone`@5, `Ref`@7 and `Tombstone`@9, dropping `Tombstone`@9 would readmit `Ref`@7's bytes — and the anchor rule prevents it, since `Tombstone`@9 is the newest `Shrink`-or-`Tombstone` and holds `A`.
  `Tombstone`@5, by the same rule, is neither the anchor nor an owner of fragments, so it is droppable on sight.
- **`AllocationMeta` for a recycled id is rebuilt from its recyclable-set entry**, which carries `mentions` and the tombstone — the latter becoming the new incarnation's `anchor` — while `size`, `fragment_count` and `statement_bytes` restart from the statements the re-allocating flush writes.
  The entry is removed from the recyclable set at that point, and a later free inserts it again.

One case needs no tombstone at all: freeing and re-allocating an id **within a single flush**.
A tombstone and the new incarnation's statements would make contradicting existence claims in one epoch, which [[#No conflicts within each epoch]] forbids.
The flush should instead emit statements that fully cover the new extent, which denies the old incarnation on its own — a bare `Undefined(id, 0, n)` suffices when no content is written.

## Design directions and trade-offs

**Sub-allocation statements versus whole-allocation entries.**
The predecessor draft ([[cow]]) used one statement per allocation, arguing that per-chunk statements lacked stable keys.
This draft keys statements by *byte offset*, which is stable under overwrites and resizes — the operations that dominate — and the predecessor's spill machinery disappears outright: a large allocation's statements split across pages naturally, because no single statement ever needs to span one.
The price is middle insertion: a splice that shifts content shifts the offsets of everything behind it, so all later statements of that allocation must be restated, `O(fragments behind the splice point)` in address-table bytes.
The current non-CoW design physically moves the tail *bytes* for the same operation, so this is no regression against the status quo — but it is a genuine regression against the predecessor's chunk-sequence idea, which spliced in `O(1)` statements at the cost of that spill machinery.
If splice-heavy containers ever matter, the open path is a per-allocation indirection (a rope of ranges) layered *on top of* offset-keyed statements for exactly the allocations that need it; the format does not block it.

**Unordered directory versus on-disk search tree.**
Unordered is justified entirely by the bulk-read premise: nothing ever locates an id by descending pages.
This is the decision to revisit if `Section`s later require loading only part of a file; the cheap retrofit is optional id-range fences on `child_ref`s, which make the directory coarsely searchable without changing its maintenance, and full B-tree ordering would buy nothing beyond that.

**Epoch per page versus per statement.**
Per page is cheaper and imposes exactly one discipline — rewrites emit resolved truth — which consolidation requires anyway.
Per-statement epochs would spend bytes on every statement to relax a constraint the design already satisfies.

**The byte-referenced-once invariant** (the open question in the `Ref` row).
Not required for read correctness; keeping it makes data-page coverage a plain counter and consolidation locally decidable.
Relaxing it enables `O(1)` copy-on-write clones of allocations and deduplication, at the price of refcounted coverage and clone-aware consolidation.
Since it is a writer invariant invisible to readers, choosing the strict form now costs nothing later.

**Inline threshold.**
The encoding fixes the ceiling at 252 bytes (one-byte tag arithmetic); the policy threshold belongs far below it.
Below roughly the encoded size of a `Ref` (~10 bytes), inlining is strictly better — the pointer would be as large as the data.
Between there and the ceiling it is a trade: inline payloads ride the header for free while hot but inflate the address table when cold, whereas `Ref` plus data bytes are written once but occupy a data-page slot and keep a page partially live.
A starting policy: inline everything up to ~64 bytes, plus anything whose eviction would leave a `Data` page holding only scraps; then measure.
Note the accounting asymmetry that argues for keeping the threshold low: an inline's payload is charged to the address-table page that holds it and is only reclaimed by rewriting that page, whereas a `Ref`'s content is charged to a data page that consolidation can clean independently of the statement describing it.
This is what makes small allocations first-class: below the threshold, an allocation lives its entire life — creation, every update, deletion — as a few bytes of statements inside pages the flush was writing anyway, with no page-granularity amplification anywhere, which is the cost profile that a dynamically typed guest language, allocating by the thousands, needs.

**The single ranked queue.**
One budget with ratio-greedy ranking is the simplest policy that is not obviously wrong, and it inherits the known failure mode of ratio-greedy cleaners: costs that current ratios cannot see get starved — e.g., description debt on a cold allocation that will be read forever but never written again never looks urgent.
If that bites, the established fix is to fold the invisible cost into the ranking (LFS's cost-benefit policy does the analogous thing with segment age); the queue structure stays.

## Related work

| System | Shared ideas | Where kladde diverges, and why |
| --- | --- | --- |
| **LSM trees** (LevelDB, RocksDB) | The header-as-write-buffer is a memtable; eviction is an L0 flush; ratio-driven consolidation is compaction; cold/hot separation at eviction. | LSMs keep runs sorted and leveled so point lookups work without full residency; kladde is fully resident after the bulk read, so it needs no sort order, no levels, no bloom filters — one tier of dense, unordered leaves. |
| **LFS and SSD FTLs** | Never overwrite; live-fraction victim selection; cost-benefit (sparse *and* old) cleaning. | Their cleaning unit is a large fixed segment; kladde's is a page, and its leaves can be re-cut freely because the directory keeps no order. |
| **LMDB** | CoW pages, alternating root, page-reuse quarantine by transaction age. | LMDB's B-tree is sorted because readers descend it in the mapped file; kladde never descends, so it trades away the ordering along with its ~25 % slack and rebalancing writes. |
| **btrfs, ZFS** | CoW everything, checksums everywhere; btrfs *inline extents* and ZFS *bonus buffers* store small data directly in metadata — the precedent for `Inline` as a first-class citizen. | Both maintain on-disk search structures for partial access to data far larger than memory; kladde's bulk-read premise removes the requirement those structures exist to serve. |
| **ext4, XFS** | Extent-shaped `Ref`s; small-file inlining (`inline_data`); description debt fought by defragmentation. | In-place journaling and fixed metadata locations rather than CoW shadowing. |
| **MVCC stores** (PostgreSQL) | Epoch shadowing is tuple versioning; tombstone lifetime is dead-tuple visibility; consolidation is `VACUUM`. | MVCC retains versions for concurrent readers; kladde retains them only across the commit boundary, so its vacuum needs no visibility horizon beyond the two-header rule. |
