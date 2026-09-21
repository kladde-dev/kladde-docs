---
title: Address table
---

The address table maps allocation ids to their existence, size, and content.
It is the only structure in a kladde file that is interpreted rather than merely pointed at, and it is what makes a flush write a small delta rather than rewriting the world.

## The idea

The table is a list of **statements**, written into pages of kind `AddressTable`, where each statement implicitly inherits its page's [epoch](file-format.md#epochs).
Statements from all live pages are merged, and contradictions are resolved by recency.

*Why statements rather than entries.*
An entry-per-allocation table has to be rewritten wherever an allocation's description changes, which under copy-on-write means rewriting a whole page to change a few bytes.
Statements are small, independent, and shadowing: a flush appends what changed, and nothing it does not mention is affected.
The cost is that a reader must merge, which is paid once at open rather than per access.
The [superseded whole-entry design](../superseded/whole-entry-address-table.md) records what the alternative looked like and why it was abandoned.

## Logical level

### Statement types

| Statement | Existence of `id` | Size of `id` | Content of `id` | Usage note |
| --- | --- | --- | --- | --- |
| `Ref(id, offset, size, address)` | exists | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` equals `file[address, address + size)`. | `[address, address + size)` must be within a single `Data` page. As a *writer invariant* (readers never depend on it), every byte in a `Data` page is referenced by at most one live `Ref`; this is not needed for correctness but keeps data-page coverage a plain counter instead of a refcount. See [Design directions](#design-directions). |
| `Zero(id, offset, size)` | exists | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` is zero. | Costs no data bytes, so it is how a range that currently holds data is set back to zero without writing zeros anywhere. Consolidation may rewrite a `[data] [zeros] [data] …` stipple as one `Ref` or `Inline` over a range it fills with actual zeros — useful for data types with unbalanced variant payloads. |
| `Shrink(id, n)` | exists | `>= n`, and **anchors** the size: extents below this statement's epoch drop out | Anything at or beyond `n` is zero. | The size-reducing half of a resize. Its content claim is dormant while the allocation stays at `n`, and activates if the allocation later grows past `n` — which is exactly what stops a truncated tail from resurfacing. |
| `Grow(id, n)` | exists | `>= n` | **Nothing.** Matches no probe. | The size-increasing half, carrying no content claim. Growing never needs to deny anything: every statement that could cover a probe at or past the old size has an epoch below the anchor and is therefore already denied by it. `Grow(id, 0)` is how an existent zero-sized allocation is stated. |
| `Tombstone(id)` | does not exist | `0` | All bytes are zero. | Like `Shrink(id, 0)` except that it also asserts non-existence. If a later epoch asserts existence again, the tombstone's effect on content remains: ranges the new incarnation leaves uncovered still resolve through it. |
| `Inline(id, offset, size, payload)` | exists | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` equals `payload`. | Small-size optimization. Requires `1 <= size <= 251`, since the payload length is encoded as an offset past the last statement tag. Zero-sized payloads stay unrepresentable, and nothing needs them: `Grow(id, 0)` states an existent zero-sized allocation. |

*Why the resize is split in two.*
A single `Size(id, n)` statement would do two jobs — anchoring the size, and claiming that everything at or past `n` is zero — and those two are needed in opposite directions.
A shrink needs both; a grow needs only the size, because the territory it exposes is already denied by the last shrink.
A grow-emitted `Size` would therefore carry a *dormant* content claim that activates later, when the allocation grows past its bound, which is a durable source of statements that survive without doing anything.

*Why unwritten content is zero rather than unspecified.*
An earlier design left it undefined — a reader "must not assume anything", and an implementation could hand out whatever was physically there.
Specifying zero costs nothing in storage, since a range that resolves to zero by default occupies no bytes either way, and it buys three things.
It makes two conforming implementations return **identical bytes** for the same file, which the rest of the format already insists on for canonical encodings and fingerprints, and which lets a conformance corpus state an expected value for every byte rather than masking holes out.
It stops deleted data from **reappearing**: a consolidator filling a hole with "arbitrary data" fills it in practice with whatever is adjacent in the buffer it is packing, which is other allocations' content.
And it gives [schema evolution](schema/evolution.md) its default for free — a field added to a struct reads as zeros from every file written before it existed.

What it gives up is narrow: a fold may no longer elide a shrink immediately followed by a growth, because the re-grown region must now read as zeros rather than as whatever survived.

### No conflicts within each epoch

Statements in the same `epoch` must not conflict with each other:

- no two statements in the same `epoch` may make contradicting statements about the existence of an `id`;
- no two statements in the same `epoch` may name any byte of content twice, not even if the two statements assign the same value to that byte.
  This includes zeroing: an assignment of zero to a given byte — whether by a `Zero`, `Shrink`, or `Tombstone` statement — conflicts with any other assignment to the same byte, including a second zero assignment.
- At most one `Shrink` and at most one `Grow` statement per `id` is allowed per epoch, and never both: a flush knows whether it is growing or shrinking.
- Multiple `Ref`, `Zero`, and `Inline` statements for pairwise disjoint ranges within the same allocation are allowed.
  They don't conflict in regard to the size of the allocation because they all merely bound the size from below.
- However, a `Ref`, `Zero`, or `Inline` statement conflicts with a `Shrink(n)` statement in the same `epoch` if its upper bound `offset + size` is larger than `n`.
  A `Grow` never conflicts with anything, since it claims no content and only bounds the size from below.

### Conflict resolution across epochs

Two statements with different `epoch` are allowed to contradict each other.
The newer statement takes precedence, but only as far as it contradicts the older one; any part of the older statement that doesn't contradict it survives.
This is resolved at the granularity of the allocation's existence, its size, and the value of each of its content bytes.

**Existence.**
Allocation `id` exists if the statement with highest `epoch` that mentions `id` is `Ref`, `Zero`, `Shrink`, `Grow`, or `Inline`.
It does not exist if that statement is `Tombstone(id)`, or if no statement mentions `id`.

**Size.**
Let `anchor_epoch` be the largest `epoch` of any `Shrink(id, n)` **or** `Tombstone(id)` statement (or `anchor_epoch = -1` if the id has neither), and call the statement at that epoch the *anchor*, whose `n` is `0` when it is a `Tombstone`.
The size of allocation `id` is the maximum over:

- the anchor's `n`, if the id has an anchor;
- the `n` field of every `Grow(id, n)` statement with `epoch > anchor_epoch`; and
- all values `offset + size` of all `Ref(id, offset, size, ...)`, `Zero(id, offset, size)` and `Inline(id, offset, size, ...)` statements with `epoch > anchor_epoch`.

The maximum is over a non-empty set whenever the allocation exists, since existence requires at least one statement mentioning `id` and forbids the newest of them from being a `Tombstone`.
A **non-existent** allocation falls out of the same rule with size `0` and needs no separate case: its newest statement, if any, is a `Tombstone`, which is therefore its anchor with nothing above it; and if no statement mentions the `id` at all, the maximum is over the empty set, which is `0` by convention.
(Existent zero-sized allocations are still distinguished from non-existent ones — by the **Existence** rule above, not by this one.)

Only `Shrink` and `Tombstone` anchor the epoch; a `Grow` contributes a lower bound and nothing else, which is why dropping one can never readmit older extents.
A `Tombstone` anchors at `n = 0` and denies every probe, so a re-allocated id is described entirely by the statements above its tombstone.

Note that, under the no-conflicts rule, the predicates `epoch > anchor_epoch` above could be relaxed to `epoch >= anchor_epoch` without changing semantics.
The stricter formulation is chosen to be more robust to a buggy writer that violates that rule: the strict form then yields a bounded allocation with an illegal statement out of range, where the relaxed form would yield a probe with two same-epoch matchers.

**Content at a given offset `probe`.**
Defined by the statement with highest `epoch` that matches any of these patterns:

- `Tombstone(id)`;
- `Shrink(id, n)` where `n <= probe`; or
- `Ref(id, offset, size, ...)`, `Zero(id, offset, size)`, or `Inline(id, offset, size, ...)` where `offset <= probe < offset + size`.

A `Grow` statement never matches, which is the whole of the difference between the two halves of a resize.
There is at most one winner, because at most one matching statement per epoch is allowed.
Given the winning statement:

- no matching statement, or a winner of type `Tombstone`, `Shrink`, or `Zero`: the content is zero;
- `Inline(id, offset, size, payload)`: the content is `payload[probe - offset]`;
- `Ref(id, offset, size, address)`: the content is `file[address + (probe - offset)]`.

## Physical format

There is only a single `kind` for address table pages: `kind == AddressTable`.
The header page is always an `AddressTable` page.
Every `AddressTable` page contains zero or more references to other `AddressTable` pages followed by zero or more statements.
References between `AddressTable` pages form a tree rooted at the header page.

```
address_table_page := num_children:varint child_ref{num_children}
                      num_statements:varint statements:statement{num_statements}
child_ref          := page_number_delta:varint  ; see "Delta encoding" below
statement          := id_delta (tagged_ref | tagged_zero | tagged_shrink
                                | tagged_tombstone | tagged_grow | inline)
id_delta           := varint                    ; see "Delta encoding" below
tagged_ref         := 0:byte offset_delta:varint size:varint address:varint
tagged_zero        := 1:byte offset_delta:varint size:varint
tagged_shrink      := 2:byte offset_delta:varint
tagged_grow        := 3:byte offset_delta:varint
tagged_tombstone   := 4:byte
offset_delta       := varint                    ; see "Delta encoding" below

; Inline is optimized for lots of small payloads. Max size is 255 - 4 = 251.
; size >= 1: zero-sized Inline payloads are deliberately unrepresentable.
; size_tag = size + 4 >= 5, so it can't clash with any of the above tagged_*.
inline             := size_tag:byte offset_delta:varint payload:byte{size_tag - 4}
```

### Delta encoding

Child refs within a page are sorted by their page number, and statements within a page are sorted lexicographically by `(id, offset)`.
That is well defined even for `Tombstone` statements, which carry no `offset`, because no other statement mentioning the same `id` can coexist with a `Tombstone(id)` in one epoch.

To save encoding space by exploiting the compactness of varints, page numbers, ids, and offsets are delta-encoded.
When starting to decode an address table page, a reader initializes a `page_cursor`, an `id_cursor`, and an `offset_cursor` to `0`, then decodes the page in reading order.

- For each `child_ref`, increment `page_cursor` by `page_number_delta`; the resulting `page_cursor` is the number of the referenced page.
- For each `statement`, perform these steps in this order:
	1. Increment `id_cursor` by `id_delta`.
	2. Read off the statement's `id` from `id_cursor`.
	3. If `id_delta != 0`: set `offset_cursor = 0`.
	4. If the statement isn't a tombstone: increment `offset_cursor` by `offset_delta`.
	5. If the statement isn't a tombstone: read off `offset` (or `n` for `Shrink` or `Grow`) from `offset_cursor`.
	6. If the statement is a `Ref`, `Zero`, or `Inline`: increment `offset_cursor` by `size` (where `size = size_tag - 4` for `Inline`).

Since `offset_delta` is a varint and therefore non-negative, `offset_cursor` must never need to move backwards.
This is a **writer invariant** the format depends on for decodability.
For `Ref`, `Zero` and `Inline` it follows from the no-conflicts rule: their ranges within one epoch are disjoint, so sorting by `offset` already leaves the cursor at or below the next statement's offset.
For `Shrink(id, n)` it follows too, since a content statement in the same epoch may not reach past `n`.
For `Grow(id, n)` it does **not** follow, because a `Grow` conflicts with nothing: a writer must simply never put a `Grow(id, n)` in a page whose content for `id` reaches past `n`.
That costs nothing, since such a `Grow` would be dead on arrival anyway — content reaching past `n` means the size already exceeds `n`.

**Delta encoding buys more than compactness**.
It makes non-compliant order of statements *unrepresentable*.
The decoder of the reference implementation exploits the fact that statements within each page are sorted lexicographically by `(id, offset)`, which allows it to efficiently merge statements across pages and iterate over them by increasing offset without having to copy and sort them first.
Since a wrong order is *unrepresentable* rather than just forbidden by the spec, the decoder does not have to verify correct order.

### Notes on the physical format

- The root `AddressTable` page *is* the current [header slot](file-format.md#header-pages), so before the payload described by this grammar it carries the file-header fields, all covered by the header CRC.
- A `child_ref` deliberately stores only a delta-encoded page number and no epoch.
  The child's own framing carries its epoch, and [invariant I1](durability.md#the-two-invariants) guarantees that a page referenced by a valid header is present and intact, so a per-child expected-epoch field would only duplicate an assertion the child already makes.
- Nothing in the format constrains *which* page a statement lives in or how the tree is shaped; those are writer policies.
- The `child_ref`s must form a **tree**, and a reader should verify this, to avoid getting trapped in an infinite loop on cyclic refs or parsing the same page twice — which can happen even in a DAG that is not a tree.

## Bounds

An implementation must support these, and must fail cleanly rather than silently wrap when an application exceeds them.

- **Allocation IDs** are 32 bit.
  This is visible to data type implementations, which may serialize a 32-bit pointer into one allocation to point at another.
  Varint encoding of ids therefore takes up to `⌈32/7⌉ = 5` bytes.
  Once `Segment`s land, each will likely have its own id space, so a file may then contain more than `2^32` allocations.
- **Page sizes** are uniform throughout a file and are given by the header's `log2(page size)`, currently 4 KiB with 8, 16, 32, and 64 KiB reserved.
  Therefore, offsets into a page that don't point at the exact end of the page always fit into 16 bit.
- **Payload sizes** of `Ref` is bounded by `MAX_PAGE_CONTENT`, the page size minus the 15 bytes of [page framing](file-format.md#page-framing), thus at most `2^12 - 15` bytes in the current 4 KiB page size setup, and at most `2^16 - 15` if 64 KiB pages become a reality.
  Their varint encoding thus takes at most `⌈16/7⌉ = 3` bytes.
- **Page numbers** are 32 bit.
  Therefore **addresses** fit into `32 + 16 = 48` bit (49 bit to point at EOF), their varint encoding takes at most `⌈49/7⌉ = 7` bytes, and the maximum **file size** is `2^(32+12) ≈ 17.6 TB` with 4 KiB pages and `2^(32+14) ≈ 70.4 TB` with 16 KiB pages.
- **Allocation sizes** are bounded by `2^32 - 1` bytes ≈ 4.3 GB.
  Note the `-1`, i.e., allocations of size 4 GiB are (just) *not* supported.
  A varint-encoded offset into an allocation therefore uses up to `⌈32/7⌉ = 5` bytes.
- The **number of statements** in a file must not exceed `2^32 - 1`, and an implementation may limit it further, so that statements can be tracked with 32-bit indices that leave one value free as a niche for optional references, and counted — per id, say — in 32-bit counters.
  Even at an optimistic 3 bytes per statement that still allows 13 GB of address-table pages alone.
- The **framing** of a statement — its encoded length excluding an `Inline` payload — is at most **21 bytes**: a `Ref` with 5 bytes of `id_delta`, 1 tag byte, 5 bytes of `offset_delta`, 3 bytes of `size`, and 7 bytes of `address`.
  Most statements are far shorter.
- The **framings of the live statements naming any one `id`** must sum to at most `2^32 - 1` bytes, so that a reader can track an allocation's description cost in a 32-bit counter.
  That allows over four gigabytes of address table describing a single allocation — at the 21-byte maximum, more than 200 million live statements for one id — so no writer approaches it without already being pathological.
  An allocation's *fragments* need no bound of their own: they are non-empty and disjoint within `[0, size)`, so there are never more of them than the allocation has bytes.
- The `size` fields of the **live `Ref` statements pointing into any one `Data` page** must sum to at most `2^32 - 1`, so that a reader can track a page's live bytes in a 32-bit counter.
  The [[#statement types|writer invariant]] holds that sum below one page's worth.
  This bound binds only files that let several `Ref` statements claim the same byte, which is legal, which a reader must therefore survive, and which without a bound would let the sum reach `2^47`.

## Design directions

**The byte-referenced-once invariant.**
Not required for read correctness; keeping it makes data-page coverage a plain counter and consolidation locally decidable.
Relaxing it would enable copy-on-write clones of allocations and deduplication, at the price of refcounted coverage and clone-aware consolidation.
Since it is a writer invariant invisible to readers, choosing the strict form now costs nothing later.

**Decision: copy-on-write clones are not pursued.**
They add complexity and open a zip-bomb-like attack vector, and most of the use case is covered by a [`Move` operation](../drafts/move-op.md) instead, which transfers rather than shares and so keeps the invariant intact.
If the need comes up later, it may be worth implementing at the data-type level rather than the allocation level — allocations reference disjoint regions of the file, and a data type on top resolves multiple indices to the same allocation, as one would with ordinary in-memory allocations.

**Per-page epochs versus per-statement epochs.**
Per page is cheaper and imposes exactly one discipline — rewrites emit resolved truth — which consolidation requires anyway.
Per-statement epochs would spend bytes on every statement to relax a constraint the design already satisfies.

**Sub-allocation statements versus whole-allocation entries.**
This design keys statements by *byte offset*, which is stable under overwrites and resizes — the operations that dominate.
The price is middle insertion: a splice that shifts content shifts the offsets of everything behind it, so all later statements of that allocation must be restated, `O(fragments behind the splice point)` in address-table bytes.
If splice-heavy containers ever matter, the open path is a per-allocation indirection (a rope of ranges) layered *on top of* offset-keyed statements for exactly the allocations that need it; the format does not block it.

**The `Inline` threshold.**
The encoding fixes the ceiling at 251 bytes; the policy threshold belongs far below it, and is an implementation choice rather than a format one.
See [the implementation notes](../impl/flush.md#the-inline-threshold).
