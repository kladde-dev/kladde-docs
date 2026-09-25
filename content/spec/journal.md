---
title: Journal
---

What a mutation records, how records are grouped into transactions, how transactions are laid out in pages, and how a torn tail is recovered.

**Status: draft.** The record set, its byte encoding, and the page layout are specified; none of it is frozen.

## What the journal is

An append-only sequence of **transactions**, each a sequence of **records** describing byte-level effects on allocations.
Every transaction is appended before the call that completes it returns, so an application crash loses no transaction that has been reported as complete.
Appends are not fsynced; what a power cut can take is stated in [Durability](durability.md#what-is-guaranteed).

The journal is kept in **segments**, one per epoch.
Segment `E` collects the transactions issued between flush `E − 1` and flush `E`, in a chain of pages whose first page the header of epoch `E − 1` names.
Flush `E` folds segment `E` into address-table and data pages, and the header it commits names the first page of segment `E + 1`.

The journal is deliberately **type-agnostic**.
It records effects on allocations, not application-level operations.
There is no "push element", no "insert key" — only resize, free, write, and three records that move content around.

*Why.* Replay never dispatches on an application type, never needs a type registry, and never calls back into a container implementation.
That is exactly what makes a file readable by an implementation that has never heard of the container that wrote it.
What it gives up is the ability to collapse operations that cancel only at the semantic level: a rope that applies a hundred edits which cancel out still records a hundred edits' worth of byte changes, because nothing in the log knows they were edits.

## Record kinds

| record | effect |
| --- | --- |
| `Free(id)` | ends `id`'s existence |
| `Resize(id, size)` | sets `id`'s size, preserving `min(old, size)` bytes |
| `Write(id, offset, bytes)` | overwrites `bytes.len()` bytes at `offset` within `id` |
| `Splice(id, offset, old_len, bytes)` | replaces `old_len` bytes at `offset` with `bytes`, shifting the tail and resizing |
| `Copy(src, src_offset, len, dst, dst_offset)` | copies a byte range between, or within, allocations |
| `Move(src, src_offset, len, dst, dst_offset)` | copies like `Copy`, then leaves the vacated source bytes zero |

Records take effect in the order they were recorded.
Every record is defined on every state, so no record needs an earlier one to be meaningful, and replay never fails on a record that decodes.

### Every record but `Free` brings its allocation into existence

**A record that writes to an allocation asserts that the allocation exists and is large enough to hold what the record writes.**
If the allocation does not exist, the record creates it with size zero before taking effect; if the written range reaches past the allocation's end, the record first grows the allocation to that end.
Bytes that growth exposes read as zero, like all [unwritten content](allocations.md#content-semantics).

The written range is `[offset, offset + bytes.len())` for `Write`, `[offset, offset + old_len)` for `Splice`, and `[dst_offset, dst_offset + len)` for `Copy` and `Move`.
`Resize` sets the size exactly, and creates the allocation just the same.
The rules hold for zero lengths too: `Write(id, n, [])` asserts that `id` exists with at least `n` bytes, and changes no content.

So there is no `Alloc` record.
`Resize(id, n)` on an id that does not exist allocates `n` zero bytes, and `Resize(id, 0)` states an existing zero-sized allocation — just as the [address table](address-table.md#statement-types) has no statement for allocating, and every statement but `Tombstone` asserts existence.

`Free(id)` ends `id`'s existence; afterwards `id` reads as size zero with no content, and on an id that does not exist `Free` has no effect.
A later record that brings `id` into existence again begins a new incarnation, whose bytes read as zero wherever that record and its successors do not write.

### Records that read

`Splice`, `Copy`, and `Move` read existing content; the others only write.
That distinction matters to an implementation's flush, and is not visible in the file.

A record reads its whole source range before it writes anything, so a `Copy` or `Move` between overlapping ranges of one allocation behaves like `memmove`.
Source bytes at or past the end of their allocation, or in an allocation that does not exist, read as zero, and reading them changes nothing about the source.

### `Move`

**`Move` is a `Copy` that then zeroes the source range, except where the destination has just overwritten it.**
The source keeps its size and its existence; only its content changes.
With `src == dst`, `Move(a, 0, 8, a, 4)` leaves `[0, 4)` zero and `[4, 12)` holding what `[0, 8)` held.

*Why a separate record.*
After a `Copy`, source and destination both hold the bytes, so a flush may not describe the destination by `Ref`s to where the bytes already lie: those data-page bytes would then be referenced twice, which the [byte-referenced-once writer invariant](address-table.md#design-directions) rules out, and the flush has to write the bytes again.
After a `Move`, the source no longer holds them, so the flush can hand the source's `Ref`s to the destination and state the vacated range as `Zero`, and [data-page coverage](../impl/liveness.md#coverage) stays a plain counter.
Rope- and tree-like containers shift ranges between neighbouring nodes constantly, and `Move` makes each such shift a statement edit rather than a byte copy; [the draft](../drafts/move-op.md) sketches how a flush states one.

## Transactions

> **Transactions are the unit of atomicity.**
> If the application crashes while recording a transaction, or while the journal is being appended to, the next open replays that transaction either completely or not at all.

This is the one durability guarantee application authors build on directly, which is why transactions are specified here while the mechanisms an implementation uses to *group* operations into them — batching, nesting, buffering — are not.
Those are [implementation concerns](../impl/transactions-and-batches.md).

How operations are grouped is free, but every record belongs to exactly one transaction.
A record that occurs outside any transaction the application opened is recorded as a transaction of its own, so a lone operation can never be torn.

Atomicity hides intermediate states; it does not erase order.
Records take effect in sequence within a transaction just as across transactions, so a `Copy` recorded after a `Write` copies what was written.

## Ordering

The journal's most important invariant is not about any single record.

> **Every transaction must take a valid state to a valid state.**

Replay stops only at transaction boundaries, so the states inside a transaction are never observed and may be invalid, while the state at every boundary is one that recovery can land on.
"Valid" permits stale, and permits slightly leaky.
It does not permit dangling: nothing may become reachable before everything it depends on is already earlier in the sequence, and nothing may be freed before whatever superseded it is already published.

In practice this means every mutation decomposes as **prepare** new, unreachable state → one **publishing** write → **clean up** what it replaced, with the steps in one transaction or spread over several.
Growing a container, removing from the middle of one, and replacing a map entry all fit this shape.
A vector push that must grow, for instance, resizes and writes the new element *before* bumping the stored length, because the length is what makes the element reachable.
A mutation that cannot be ordered this way belongs in a single transaction, which is the format's only atomic-group mechanism and the only one it needs.

This is a discipline every container implementation must follow, and **no type system enforces it**.
It is the single largest source of latent correctness risk in the design, which is why [the conformance suite](conformance.md#the-suite) makes it a mechanical check: truncate at every byte offset, replay, assert validity.

## Encoding

Integers are unsigned LEB128 varints and fixed-width fields are little-endian, as everywhere in the file ([integer encodings](file-format.md#integer-encodings-and-crcs)).

### Records

```
record  := free | resize | write | splice | copy | move
free    := 1:byte id:varint
resize  := 2:byte id:varint size:varint
write   := 3:byte id:varint offset:varint bytes
splice  := 4:byte id:varint offset:varint old_len:varint bytes
copy    := 5:byte src:varint src_offset:varint len:varint dst:varint dst_offset:varint
move    := 6:byte src:varint src_offset:varint len:varint dst:varint dst_offset:varint
bytes   := len:varint byte{len}
```

Tags `0` and `7` through `255` are reserved.
Ids and offsets are absolute rather than delta-encoded as in the address table, since records, unlike statements, are not sorted.

Every record respects the [bounds](address-table.md#bounds): ids are non-zero and fit in 32 bits, and every sum of an offset and a length that a record implies is at most `2^32 − 1`, so that no record, growth included, can make an allocation larger than the bound allows.

### Framing

```
transaction := length:varint record* crc:byte{4}
```

`length` counts the bytes of the records, and `crc` is the value of the segment's [CRC chain](#the-crc-chain) just after them.
A transaction is complete — committed — once its `crc` is in the journal; a transaction whose `crc` is missing or does not match never happened.
`length` is at most `2^32 − 1` ([bounds](address-table.md#bounds)).

*Why a length prefix.*
It tells a reader where the `crc` is before the reader decodes a single record, so nothing unvalidated is ever parsed.
It also means a writer must know a transaction's size before appending its first byte, and so encodes the transaction whole — which costs nothing, since an unfinished transaction [cannot be folded](#checkpoint-versus-commit) and is held in memory until it ends anyway.

A transaction whose `crc` matches but whose records do not decode to exactly `length` bytes, or break a bound, is not a torn tail — a torn write cannot produce a matching CRC — but a writer's bug.
A reader must refuse the file, as it would refuse any other damage, rather than stop replay there and silently drop every transaction after it.

### Pages

**A segment is a singly linked chain of pages**, starting at the page the header's [`journal_pointer`](file-format.md#the-journal-pointer) names.
Journal pages carry neither the [page framing](file-format.md#page-framing) nor a header of their own; each page reserves only its last 8 bytes, for a **trailer**:

```
journal_page := stream:byte{page_size − 8} next:byte{4} crc:byte{4}
```

The segment's transactions form one byte **stream**, laid out across the `stream` areas of its pages in chain order.
Where a page's `stream` area ends, the stream continues at the start of the next page — in the middle of a transaction, a record, or a field if it must, which is what lets a record carry a payload larger than a page.
A transaction's `crc` is no exception: it splits like any other field, and its two parts are checked together once both have been read.

The trailer is written when, and only when, the stream continues past its page: `next` is the number of the page the stream continues on, and `crc` is the value of the CRC chain just after `next`.
So the writer chooses each next page only once it needs one, and the last page of a segment has no valid trailer, only stale bytes where a trailer would go.
`next` names a reusable page, or a new page added at the end of the file, as [the reuse rule](durability.md#the-reuse-rule) allows, and never a page already in the segment.

A reader follows `next` only once the trailer's `crc` has matched, so it never trusts a pointer that no checksum covers.
The trailer also means that every byte a page holds is covered by a checksum as soon as the stream has moved past the page, including the first part of a transaction that continues on the next one.

### The CRC chain

**Every checksum in a segment is a value of one running CRC-32C over the segment's chain input**, using the CRC-32C [specified for the file](file-format.md#integer-encodings-and-crcs).
The chain input is the segment's epoch as 8 little-endian bytes — one more than the epoch of the header that names the segment — followed by every stream byte and every `next` field in order, but not the checksum fields themselves.
Each checksum field stores the CRC-32C of the chain input up to that field: a transaction's `crc` covers everything up to its last record, and a trailer's `crc` everything up to its `next`.

- *Chained*, so that recovery keeps the longest valid **prefix** rather than the longest valid set: a hole cannot be skipped, even if the bytes past it are intact.
  This matters because operating-system write-back is not ordered, so a later journal page can reach disk while an earlier one does not.
- *Epoch-salted*, so that stale bytes left in a reused page do not validate as journal content.
  The epoch is not stored in the journal, because the header that names the segment implies it.
  This is what lets a flush hand journal segment `E + 1` a reusable page without writing anything to it first — with [one exception](#the-start-of-a-session).
- *Blind to its own checksums*, because CRC-32C, fed a message followed by that message's own CRC, always ends in the same state.
  Feeding each stored checksum back in would restart the chain from a constant after every transaction, and a stale transaction from any earlier segment that happened to start where a valid one ended would validate.

## Recovery

1. Read and validate the [header](file-format.md#header-pages), taking the CRC-valid slot with the highest epoch, and bulk-load its world.
2. Walk the segment from the header's `journal_pointer`, feeding the chain as it goes: check each transaction's `crc` once its records have been read, and each trailer's `crc` before following its `next`.
3. Stop at the first check that fails, or at a `journal_pointer` or `next` that points past the end of the file.
   The complete transactions before that point are the journal; whatever follows them is the torn tail.
4. Replay the complete transactions in order.

Replay is purely mechanical and involves no application code.

## The start of a session

Two rules keep a session from damaging, or misreading, a journal that an earlier session left behind.
Each costs at most one flush or one page write per session.

**A session appends only to a recovered journal that is empty; a non-empty one it folds with a flush first.**
Appending would rewrite the page holding the recovered journal's end, and a power cut during that write can corrupt the transactions already on it.
Those can be durable: when a crash loses header `E`, recovery lands on header `E − 1` and replays journal segment `E`, which flush `E`'s `fsync` made durable — so damaging it would lose transactions that a completed flush had covered, which [Durability](durability.md#what-is-guaranteed) promises cannot happen.

**A session's first flush writes the first page of the journal segment it names**, for which zeros suffice, unless that page lies past the end of the file.
The epoch salt tells every segment apart except in one situation.
When a crash loses header `E` after transactions were appended to the segment it named, recovery lands on header `E − 1`, and the recovering session's first flush issues epoch `E` again.
If the page it named still held the lost segment's first transactions, they would validate — same epoch, same starting offset — although the session committing the header knows nothing of them; its later headers would describe states without them, and after a second lost header, recovery could replay stale transactions on top of a state they were never appended to.
Only a session's first flush can re-issue an epoch, because writing header `E` required the `fsync` that made header `E − 1` durable, so a lost header `E` never leaves recovery further back than `E − 1`.
And only the first page of a segment is exposed, since every later page validates only as the continuation of the exact bytes before it.

## Folding

Periodically the journal is **folded**: its records are applied, the on-file state is brought up to date, and a fresh journal segment is started.
The protocol that commits this is in [Durability](durability.md#the-flush-protocol).

**A flush folds its whole segment, which holds complete transactions only**, so every header describes the state at a transaction boundary.

An implementation has wide latitude in *what* it writes.
It may apply records naively in order, or it may optimize — cancelling the creation and freeing of an allocation that never escaped, dropping a write that a later write fully supersedes, eliding a grow-then-shrink that returns to the original size with nothing written in between.
It may reorder freely, so long as the result is indistinguishable from an in-order replay.
"Indistinguishable" is measured against [content semantics](allocations.md#content-semantics): every byte has one right answer, so a fold's freedom is to reach that answer by any route rather than to choose among answers.

### Checkpoint versus commit

**Decided: every checkpoint lands on a commit.**
A *commit* is the end of a transaction, marked by its `crc`; a *checkpoint* is a flush, which folds complete transactions only, so every header describes a state some commit produced.
[Durability](durability.md#what-is-guaranteed) requires exactly this, since a header describing half a transaction would let recovery land inside it — and it means the format needs no commit record beyond the transaction's `crc`.

A checkpoint that folded part of an unfinished transaction, to bound the memory the transaction holds, would need a way to undo that part should the transaction never end, and the format has none.
So an unfinished transaction stays in memory until it ends, and keeping that small is the application's side of the bargain: [short transactions, and batches for long sequences](../impl/transactions-and-batches.md#recommendations).
