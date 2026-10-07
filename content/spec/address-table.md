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
| `Ref(id, offset, size, address)` | exists | — | Content in range `[offset, offset + size)` equals `file[address, address + size)`. | `[address, address + size)` must be within a single `Data` page. As a *writer invariant* (readers never depend on it), every byte in a `Data` page is referenced by at most one live `Ref`; this is not needed for correctness but keeps data-page coverage a plain counter instead of a refcount. See [Design directions](#design-directions). |
| `Zero(id, offset, size)` | exists | — | Content in range `[offset, offset + size)` is zero. | Costs no data bytes, so it is how a writer states zeros: the bytes a growth exposes, and a range that held data and is set back to zero. Consolidation may rewrite a `[data] [zeros] [data] …` stipple as one `Ref` or `Inline` over a range it fills with actual zeros — useful for data types with unbalanced variant payloads. |
| `Inline(id, offset, size, payload)` | exists | — | Content in range `[offset, offset + size)` equals `payload`. | Small-size optimization. Requires `1 <= size <= 125`, since the payload length and the sizing bit share the statement's tag byte. Zero-sized payloads are unrepresentable, and nothing needs them. |
| `Ref*`, `Zero*`, `Inline*`, with the fields of their plain variants | exists | `offset + size` | As their plain variants. | The **sizing** variants: each states, besides its content, that the allocation ends where that content ends — whether this grows the allocation, shrinks it, or leaves its size as it was. |
| `Size(id, n)` | exists | `n` | **Nothing.** Matches no probe. | States a size where no content statement ends at it: a shrink that writes nothing at the new end, a zero-sized allocation, or a size stated again. |
| `Tombstone(id)` | does not exist | `0` | **Nothing.** Matches no probe. | If a later epoch asserts existence again, the new incarnation states its size and [every byte of its content](#the-coverage-rule), so nothing of the old one carries over. |

`Ref`, `Zero`, and `Inline`, in either variant, are the **content statements**.
`Size`, `Tombstone`, and the sizing variants are the statements that **state a size**.

*Why the newest size wins.*
A writer knows the size of every allocation it writes, so it states the size whenever it changes, and a reader takes the [newest statement of it](#conflict-resolution-across-epochs).
The size then depends on exactly one statement per id: a reader tracks it with one comparison per statement, an implementation keeps it alive with one reference per allocation, and dropping any other statement can never change a size.
The sizing variants make stating the size free wherever a resize writes content that ends at the new size, which covers the common cases: an append ends at the new end, and so does the tail a deleting splice restates.

*Why zeros are stated rather than implied.*
The size has no memory of the sizes before it, so a byte below the size resolves through whatever statement matches it, however old.
A byte that a shrink cut off and a later growth brought back would resolve through the statement that held it before the shrink; so a writer states every byte an allocation gains, zeros included, by [the coverage rule](#the-coverage-rule).
In return, every byte below the size is stated: two conforming implementations return identical bytes for the same file without agreeing on any default, deleted data cannot reappear, and a byte that no statement matches is a detectable error rather than a silent zero.
Stating zeros costs a `Zero` of a few bytes for each range the application leaves unwritten, however long the range.

### No conflicts within each epoch

Statements in the same `epoch` must not conflict with each other:

- no two statements in the same `epoch` may make contradicting statements about the existence of an `id`;
- no two content statements in the same `epoch` may name any byte of content twice, not even if the two statements assign the same value to that byte;
- at most one statement per `id` and `epoch` states the size: a `Size`, a sizing variant, or the `Tombstone` — even two that agree conflict;
- multiple content statements for pairwise disjoint ranges within the same allocation are allowed;
- a content statement conflicts with a statement in the same `epoch` that states the size `n` of its `id` if its upper bound `offset + size` is larger than `n`.

### The coverage rule

> **In the epoch in which a byte becomes part of an allocation, a content statement of that epoch states it.**

A byte becomes part of an allocation when the allocation comes into existence with a size above the byte's offset, or grows past it.
An id freed and allocated again within one epoch comes into existence in that epoch.

This is a rule for writers, and readers cannot check it: checking it would need the size each epoch started from.
It is what keeps truncated content out.
With `Ref*(7, 0, 1000, a)`@3 and `Size(7, 10)`@5, the allocation holds 10 bytes; if epoch 9 grows it to 50 and writes bytes 10 to 30, it states `Ref(7, 10, 20, b)` and `Zero*(7, 30, 20)`, and without the `Zero*` the bytes 30 to 50 would resolve through `Ref*`@3 again.

### Conflict resolution across epochs

Two statements with different `epoch` are allowed to contradict each other.
The newer statement takes precedence, but only as far as it contradicts the older one; any part of the older statement that doesn't contradict it survives.
This is resolved at the granularity of the allocation's existence, its size, and the value of each of its content bytes.

**Existence.**
Allocation `id` exists if the statement with highest `epoch` that mentions `id` is anything but a `Tombstone`.
It does not exist if that statement is `Tombstone(id)`, or if no statement mentions `id`.

**Size.**
The size of allocation `id` is the size stated by its newest statement that states one: `n` for `Size(id, n)`, `offset + size` for a sizing variant, and `0` for `Tombstone(id)`.
If no statement states its size, the size is `0`.
The newest such statement is unique, since an epoch holds at most one per id.

A non-existent allocation has size `0` by the same rule, since its newest statement is a `Tombstone`.
(Existent zero-sized allocations are still distinguished from non-existent ones — by the **Existence** rule above, not by this one.)

**Content at a given offset `probe < size`.**
Defined by the content statement with highest `epoch` whose range `[offset, offset + size)` contains `probe`, of either variant.
`Size` and `Tombstone` statements match no probe.
There is at most one winner, because at most one matching statement per epoch is allowed.
Given the winning statement:

- `Zero`: the content is zero;
- `Inline(id, offset, size, payload)`: the content is `payload[probe - offset]`;
- `Ref(id, offset, size, address)`: the content is `file[address + (probe - offset)]`.

**A probe below the size that no content statement matches makes the file invalid**, and a reader must fail cleanly on it rather than return a value.
A writer that keeps [the coverage rule](#the-coverage-rule) never produces one.

## Physical format

Address table pages follow the [[file-format#Page framing|general page framing]] and encode their payload in the `content` field.
There is only a single `kind` for address table pages: `kind == AddressTable`.
The header page is always an `AddressTable` page.
Every `AddressTable` page contains zero or more references to other `AddressTable` pages followed by zero or more statements.
References between `AddressTable` pages form a tree rooted at the header page: every page reachable from the header other than the header itself is named by exactly one `child_ref` among the pages reachable from the header, and no `child_ref` names a header slot, page 0 or page 1.

```
; `content` field of an address table page:
address_table_page := child_ref* 0:byte statements:statement*
child_ref          := page_number_delta:varint  ; see "Delta encoding" below
statement          := id_delta (tagged_ref | tagged_zero | tagged_tombstone
                                | tagged_size | inline)
id_delta           := varint                    ; see "Delta encoding" below
tagged_ref         := (0 | 1):byte offset_delta:varint size:varint address:varint  ; 1: Ref*
tagged_zero        := (2 | 3):byte offset_delta:varint size:varint                 ; 3: Zero*
tagged_tombstone   := 4:byte
tagged_size        := 5:byte offset_delta:varint                                  ; n
offset_delta       := varint                    ; see "Delta encoding" below

; Inline is optimized for lots of small payloads. Max size is 125.
; tag = 4 + 2 * size + sizing, where sizing is 1 for Inline* and 0 for Inline.
; size >= 1: zero-sized Inline payloads are deliberately unrepresentable,
; so tag >= 6, and it can't clash with any of the above tagged_*.
inline             := tag:byte offset_delta:varint payload:byte{(tag - 4) >> 1}
```

For every content statement, bit 0 of the tag is the sizing bit.

### Delta encoding

Child refs within a page are sorted by their page number, and statements within a page are sorted lexicographically by `(id, offset)`, where the `offset` of a `Size(id, n)` is `n`.
This order is well defined even for `Tombstone` statements, which carry no `offset`, because no other statement mentioning the same `id` can coexist with a `Tombstone(id)` in one epoch.

To save encoding space by exploiting the compactness of varints, page numbers, ids, and offsets are delta-encoded.
When starting to decode an address table page, a reader initializes a `page_cursor`, an `id_cursor`, and an `offset_cursor` to `0`, then decodes the page in reading order.

- For each `child_ref`, increment `page_cursor` by `page_number_delta`; the resulting `page_cursor` is the number of the referenced page.
  Decode `child_ref`s until encountering the delimiter byte `0` (which is also a varint-encoded zero).
  The delimiter cannot be confused with a `child_ref`, whose `page_number_delta` is never zero: the first child's page number is at least 2, since no `child_ref` names a header slot, and each later one exceeds its predecessor, since the tree names each page at most once.
- Decode `statements` until the end of the page's `content` field is reached, which can be detected by the `content_size` field of the [[file-format#Page framing|page framing]].
  For each `statement`, perform these steps in this order:
	1. Increment `id_cursor` by `id_delta`.
	2. Read off the statement's `id` from `id_cursor`.
	3. If `id_delta != 0`: set `offset_cursor = 0`.
	4. If the statement isn't a tombstone: increment `offset_cursor` by `offset_delta`.
	5. If the statement isn't a tombstone: read off `offset` (or `n` for `Size`) from `offset_cursor`.
	6. If the statement is a content statement: increment `offset_cursor` by `size` (where `size = (tag - 4) >> 1` for `Inline`).

Since `offset_delta` is a varint and therefore non-negative, `offset_cursor` must never need to move backwards.
This is a **writer invariant** the format depends on for decodability, and it follows from the no-conflicts rule.
The ranges of content statements within one epoch are disjoint, so sorting by `offset` already leaves the cursor at or below the next statement's offset; and a content statement in the same epoch as a `Size(id, n)` ends at or below `n`, so the `Size` comes after all of them.

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
- **Page sizes** are uniform throughout a file and are given by the header's `log2_page_size`, currently 4 KiB with 8, 16, 32, and 64 KiB reserved.
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
- The size of an encoded [transaction](journal.md#framing) (without framing) must not exceed `2^32 - 1` bytes, so that its length prefix fits in 32 bits.
  Trying to encode a larger transaction must fail rather than silently cutting the transaction in parts.

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
The encoding fixes the ceiling at 125 bytes; the policy threshold belongs far below it, and is an implementation choice rather than a format one.
See [the implementation notes](../impl/flush.md#the-inline-threshold).
