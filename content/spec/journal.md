---
title: Journal
---

What a mutation records, how records are grouped into transactions, how transactions are laid out in pages, and how a torn tail is recovered.

**Status: draft.** The record set, its byte encoding, and the page layout are specified; none of it is frozen.

## What the journal is

An append-only sequence of **transactions**, each a sequence of **records** describing byte-level effects on the [sequence](sequence.md).
Every transaction is appended before the call that completes it returns, so an application crash loses no transaction that has been reported as complete.
Appends are not fsynced; what a power cut can take is stated in [Durability](durability.md#what-is-guaranteed).

Each epoch has a journal of its own.
The journal of epoch `E` collects the transactions issued between flush `E − 1` and flush `E`, in a chain of journal pages whose first page the header of epoch `E − 1` names.
Flush `E` folds it into table and data pages, and the header it commits names the first page of the journal of epoch `E + 1`.

A *record* is what the journal holds; an *operation* is what an application's call does, which may take several records, as a push onto a vector takes two, the element and the count.

The journal is deliberately **type-agnostic**.
It records effects on bytes of the sequence, not application-level operations: there is no "push element", no "insert key", only overwrites, splices, and moves of bytes.

*Why.* Replay never dispatches on an application type, never needs a type registry, and never calls back into a container implementation.
That is exactly what makes a file readable by an implementation that has never heard of the container that wrote it.
What it gives up is the ability to collapse operations that cancel only at the semantic level: a hundred edits that cancel out still record a hundred edits' worth of byte changes, because nothing in the log knows they were edits.

## Record kinds

**Four records, on positions in the sequence:**

| record | effect |
| --- | --- |
| `Write(position, bytes)` | overwrites `[position, position + bytes.len())` |
| `Splice(position, removed, bytes)` | replaces the `removed` bytes at `position` with `bytes`, shifting what follows |
| `Move(from, length, to)` | cuts `[from, from + length)` and inserts it at `to`, a position in the sequence without the cut bytes |
| `Paste(position, removed, cut)` | replaces the `removed` bytes at `position` with the bytes of a cut that an earlier record of the same journal made |

Records take effect in the order they were recorded, and a record's positions refer to the sequence as the records before it left it.
To move `bc` between `d` and `e` in `abcde`, giving `adbce`, `from` is 1 and `length` 2, which leaves `ade`, and `to` is 2, between `d` and `e` there: `Move(1, 2, 2)`.

**A record whose range does not lie inside the sequence makes the file invalid**, as a transaction whose checksum matches but whose records do not decode does: it is a writer's bug, never a torn write, and a reader refuses the file.
With one sequence, there is nothing for a record to bring into existence, so no record needs a meaning outside it.

**A move of `B` behind `C` in `A B C D` is also a move of `C` in front of `B`, and which one a writer records changes nothing on file**: the resulting sequence is the same, cut at the same three places.
A container records the move of the element it was asked to move.

There is no record to allocate, free, resize or copy.
Duplicating a value writes its encoding: the flush would have to write a copy's bytes again anyway, to keep every data byte shown by one span, so a copy record would save journal bytes only.

### Cuts

**Every record that removes bytes — a `Write`, and a `Splice` that removes some — makes a *cut* of them, which a later `Paste` of the same journal may insert elsewhere, once, and which is gone when the journal is folded.**

- A `Paste` names its cut by how many cuts of the journal were made after it: 0 for the latest.
  With `removed` equal to the cut's length it overwrites, and with 0 it inserts.
- A cut is pasted at most once, so that no data byte is shown twice.
  A reader refuses a `Paste` that names a cut already pasted, or one its journal did not make.
- A cut that nothing pastes is removed bytes, as after any splice: nothing frees it, and nothing can leak.
- A `Write` is a `Splice` that removes as many bytes as it inserts, a byte shorter in the journal, so a writer records a `Write` wherever the lengths agree; that its overwritten bytes make a cut too keeps that choice from deciding whether the old value can be pasted elsewhere.
- A `Write` of no bytes makes no cut, and neither does a `Splice` that removes nothing.
- A `Move` makes no cut either: the bytes it cuts it inserts again at once, so nothing is left for a `Paste` to name, and counting it would only make a `Paste`'s distance depend on the moves between it and its cut.
- A cut and its paste may lie in different transactions, as long as both lie in one journal; recovery replays a journal from its start, so every cut that a recovered `Paste` names is among the transactions it replays.
- `Move(from, length, to)` is `Splice(from, length, [])` followed by `Paste(to, 0, 0)`, and stays as the shorter way to write it.

*Why cuts.*
Every value is its bytes, so moving a value from one container to another would otherwise mean writing its encoding again, and the journal and the flush would carry all of its bytes.
A cut and its paste carry none: the flush resolves them as a move, which states three to six spans however large the value is.
Naming cuts by distance rather than popping them off a stack lets a writer paste its cuts in any order, so two values that swap containers need no care about which is pasted first.

*Why as long as the journal, and not longer.*
Within one journal every cut is still at hand: its bytes lie in the committed sequence or in the journal itself, which the fold and recovery both read, so a cut can outlive its transaction at no cost to the file.
That serves an application that removes a value in one transaction and inserts it again in a later one — undoing a deletion is the common case — which would otherwise write the value's encoding again.
A cut that outlived its journal would need its bytes kept reachable from the header, as content of their own, after the flush that folds their removal; deleted content would then stay in the file, which is not what an application deleting it expects.
So a cut is a *weak* promise: [an implementation](../impl/values.md#moving-a-value-between-containers) pastes it while its journal lasts, and encodes the value again otherwise.
The price is that a reader keeps the length of every cut of the journal, to check the positions after it, which the journal's length bounds, and an implementation keeps what it needs to resolve the journal's cuts until the journal is folded.

## Transactions

> **Transactions are the unit of atomicity.**
> If the application crashes while recording a transaction, or while the journal is being appended to, the next open replays that transaction either completely or not at all.

This is the one durability guarantee application authors build on directly, which is why transactions are specified here while the mechanisms an implementation uses to *group* operations into them — batching, nesting, buffering — are not.
Those are [implementation concerns](../impl/transactions-and-batches.md).

How operations are grouped is free, but every record belongs to exactly one transaction.
A record that occurs outside any transaction the application opened is recorded as a transaction of its own, so a lone operation can never be torn.

Atomicity hides intermediate states; it does not erase order.
Records take effect in sequence within a transaction just as across transactions.

## Ordering

> **Every transaction takes a sequence that decodes to one that decodes.**

Replay stops only at transaction boundaries, so the states inside a transaction are never observed and may be anything, while the state at every boundary is one that recovery can land on, and must be a [valid sequence](sequence.md#what-a-valid-sequence-is).
A container whose edit changes two places — the element and its count, for a push — makes both in one transaction.

That is the whole discipline.
Nothing in the sequence points at anything else, so nothing can dangle, and nothing can become unreachable, so nothing can leak.
[The conformance suite](conformance.md#the-suite) checks it mechanically: truncate the journal at every byte, replay, and decode.

## Encoding

Integers are unsigned LEB128 varints and fixed-width fields are little-endian, as everywhere in the file ([integer encodings](file-format.md#integer-encodings-and-crcs)).

### Records

**Each position is written as its distance from where the record before it left off, and the record's kind rides in the low two bits of the same varint:**

```
record  := head:varint (write | splice | move | paste)
; x := unzigzag(head), a signed integer
; kind := x & 3, and delta := x >> 2, shifting arithmetically
write   := bytes                          ; kind 0; position := cursor + delta
splice  := removed:varint bytes           ; kind 1; position := cursor + delta
move    := length:varint to_delta:varint  ; kind 2; from := cursor + delta,
                                          ;   to := from + unzigzag(to_delta)
paste   := removed:varint cut:varint      ; kind 3; position := cursor + delta
bytes   := length:varint byte{length}
```

So a writer computes `head = zigzag((delta << 2) | kind)`, with `zigzag` as in [the span table](span-table.md#physical-format).

**The cursor is where the record before left off**: the end of what a `Write`, `Splice` or `Paste` put in place — `position` plus the bytes it inserted — and the end of the moved run at its destination, `to + length`, for a `Move`.
It starts at 0 at the start of each epoch's journal, which is always replayed from its start, and carries over from one transaction to the next.

**A `Write` of no bytes is the escape to further kinds of record**, since it changes nothing: its head is followed by `escape:varint`, which says what record follows.

```
escaped := 0x00 0x00 escape:varint (seal) ; the head of a Write with delta 0,
                                          ;   and its length 0
seal    := page:varint                    ; escape 0
```

An escaped record must have delta 0, and leaves the cursor where it was.

The only escape defined is 0, the [seal](#seals); a reader refuses any other until a revision defines it.

Every position, `removed` count, `length` and `to` is below `2^48` ([bounds](span-table.md#bounds)), and a record's range must lie inside the sequence.
A `Paste`'s `cut` must name a cut of its own journal that is not pasted yet; its length is the cut's.

*Why relative positions.*
Edits mostly land near the one before, so a delta from −16 to 15 fits in the head's one byte with the kind, where an absolute position takes three bytes in a sequence of 50 KB and four in one of 100 MB, besides a byte for the kind.
A jump across most of the sequence costs at most one byte more than an absolute position would, since zigzag adds one bit.
A keystroke is then a `Splice` of four bytes — the head, nothing removed, a length of one, and the character — and a transaction of nine with its length and checksum.
A `Move`'s `to` counts from its `from`, since restacking moves bytes a short way.

### Seals

**A seal ends its epoch's journal and names the page where the journal of the next epoch begins**, so that a flush can fold one journal while the application appends to the next.

- A seal is a transaction of its own, holding the seal record and nothing else, and nothing follows it in its journal.
- Its `page` is the first page of the next epoch's journal, chosen as any other journal page is: a reusable page, or a new page at the end of the file, never a page of the journal it ends.
- From the moment the seal is appended, the next journal belongs to the world of every header whose journal leads to the seal, and [the reuse rule](durability.md#the-reuse-rule) protects its pages as it protects the journal itself.
- The next journal is an ordinary journal of epoch `E + 1`: its CRC chain is salted with `E + 1`, its cursor starts at 0, and its cuts are its own.
  The header that the flush folding journal `E` writes names the same page as its `journal_pointer`.

A writer need not seal: a flush that folds its journal without one is as valid as ever, and the header it writes names the next journal, as before.

*Why a seal.*
Without one, the next journal exists only once the flush's header names it, so a transaction appended while a flush is in progress would belong to a journal that recovery cannot reach if the application crashes before that header is written, although the call that recorded it had returned.
With a seal, recovery reaches the next journal through the journal it folds, whichever of the two headers governs.
A writer can then let the application append while it folds, writes and syncs, on the caller's thread or another; whether it does is [its own choice](../impl/transactions-and-batches.md#a-flush-off-the-callers-thread), and the format is the same either way.
The seal reuses the escape that a `Write` of no bytes was reserved for, so it costs no kind of its own.

### Framing

```
transaction := length:varint record* crc:byte{4}
```

`length` counts the bytes of the records, and `crc` is the value of the journal's [CRC chain](#the-crc-chain) just after them.
A transaction is complete — committed — once its `crc` is in the journal; a transaction whose `crc` is missing or does not match never happened.
`length` is at most `2^32 − 1` ([bounds](span-table.md#bounds)).

*Why a length prefix.*
It tells a reader where the `crc` is before the reader decodes a single record, so nothing unvalidated is ever parsed.
It also means a writer must know a transaction's size before appending its first byte, and so encodes the transaction whole — which costs nothing, since an unfinished transaction [cannot be folded](#checkpoint-versus-commit) and is held in memory until it ends anyway.

A transaction whose `crc` matches but whose records do not decode to exactly `length` bytes, break a bound, reach outside the sequence, or paste a cut they may not, is not a torn tail — a torn write cannot produce a matching CRC — but a writer's bug.
A reader must refuse the file, as it would refuse any other damage, rather than stop replay there and silently drop every transaction after it.

### Pages

**An epoch's journal is a singly linked chain of journal pages**, starting at the page the header's [`journal_pointer`](file-format.md#the-journal-pointer) names.
Journal pages carry neither the [page framing](file-format.md#page-framing) nor a header of their own; each page reserves only its last 8 bytes, for a **trailer**:

```
journal_page := stream:byte{page_size − 8} next:byte{4} crc:byte{4}
```

The journal's transactions form one byte **stream**, laid out across the `stream` areas of its pages in chain order.
Where a page's `stream` area ends, the stream continues at the start of the next page — in the middle of a transaction, a record, or a field if it must, which is what lets a record carry a payload larger than a page.
A transaction's `crc` is no exception: it splits like any other field, and its two parts are checked together once both have been read.

The trailer is written when, and only when, the stream continues past its page: `next` is the number of the page the stream continues on, and `crc` is the value of the CRC chain just after `next`.
So the writer chooses each next page only once it needs one, and the last page of a journal has no valid trailer, only stale bytes where a trailer would go.
`next` names a reusable page, or a new page added at the end of the file, as [the reuse rule](durability.md#the-reuse-rule) allows, and never a page already in the journal.

A reader follows `next` only once the trailer's `crc` has matched, so it never trusts a pointer that no checksum covers.
The trailer also means that every byte a page holds is covered by a checksum as soon as the stream has moved past the page, including the first part of a transaction that continues on the next one.

### The CRC chain

**Every checksum in an epoch's journal is a value of one running CRC-32C over the journal's chain input**, using the CRC-32C [specified for the file](file-format.md#integer-encodings-and-crcs).
The chain input is the journal's epoch as 8 little-endian bytes — one more than the epoch of the header that names the journal — followed by every stream byte and every `next` field in order, but not the checksum fields themselves.
Each checksum field stores the CRC-32C of the chain input up to that field: a transaction's `crc` covers everything up to its last record, and a trailer's `crc` everything up to its `next`.

- *Chained*, so that recovery keeps the longest valid **prefix** rather than the longest valid set: a hole cannot be skipped, even if the bytes past it are intact.
  This matters because operating-system write-back is not ordered, so a later journal page can reach disk while an earlier one does not.
- *Epoch-salted*, so that stale bytes left in a reused page do not validate as journal content.
  The epoch is not stored in the journal, because the header that names the journal implies it.
  This is what lets a flush hand the journal of epoch `E + 1` a reusable page without writing anything to it first — with [one exception](#the-start-of-a-session).
- *Blind to its own checksums*, because CRC-32C, fed a message followed by that message's own CRC, always ends in the same state.
  Feeding each stored checksum back in would restart the chain from a constant after every transaction, and a stale transaction from any earlier journal that happened to start where a valid one ended would validate.

## Recovery

1. Read and validate the [header](file-format.md#header-pages), taking the CRC-valid slot with the highest epoch, and bulk-load its world.
2. Walk the journal from the header's `journal_pointer`, feeding the chain as it goes: check each transaction's `crc` once its records have been read, and each trailer's `crc` before following its `next`.
3. At a complete transaction that is a [seal](#seals), continue the walk at the page it names, as the journal of the next epoch: the chain restarts, salted with that epoch, and so do the cursor and the cuts.
4. Stop at the first check that fails, or at a `journal_pointer`, `next` or seal that points past the end of the file.
   The complete transactions before that point are the journal; whatever follows them is the torn tail.
5. Replay the complete transactions in order.

Replay is purely mechanical and involves no application code.

## The start of a session

Two rules keep a session from damaging, or misreading, a journal that an earlier session left behind.
Each costs at most one flush or one page write per session.

**A session appends only to a recovered journal that is empty; a non-empty one it folds with a flush first.**
Appending would rewrite the page holding the recovered journal's end, and a power cut during that write can corrupt the transactions already on it.
Those can be durable: when a crash loses header `E`, recovery lands on header `E − 1` and replays the journal of epoch `E`, which flush `E`'s `fsync` made durable — so damaging it would lose transactions that a completed flush had covered, which [Durability](durability.md#what-is-guaranteed) promises cannot happen.

**A session's first flush writes the first page of the journal it names**, for which zeros suffice, unless that page lies past the end of the file; a flush that seals writes it before appending the seal.
The epoch salt tells every journal apart except in one situation.
When a crash loses header `E` after transactions were appended to the journal it named, recovery lands on header `E − 1`, and the recovering session's first flush issues epoch `E` again.
If the page it named still held the lost journal's first transactions, they would validate — same epoch, same starting offset — although the session committing the header knows nothing of them; its later headers would describe states without them, and after a second lost header, recovery could replay stale transactions on top of a state they were never appended to.
Only a session's first flush can re-issue an epoch, because writing header `E` required the `fsync` that made header `E − 1` durable, so a lost header `E` never leaves recovery further back than `E − 1`.
And only the first page of a journal is exposed, since every later page validates only as the continuation of the exact bytes before it.

## Folding

Periodically the journal is **folded**: its records are applied, the on-file state is brought up to date, and a fresh journal is started.
The protocol that commits this is in [Durability](durability.md#the-flush-protocol).

**A flush folds its whole journal, which holds complete transactions only**, so every header describes the state at a transaction boundary.

An implementation has wide latitude in *what* it writes.
It may apply records naively in order, or it may optimize — dropping a write that a later write fully supersedes, resolving a cut and its paste as a move, stating bytes a splice shifted without writing them again.
It may reorder freely, so long as the resulting sequence is the one an in-order replay produces: every byte of the sequence has one right answer, so a fold's freedom is to reach that answer by any route, and to cut it into spans however it likes.

### Checkpoint versus commit

**Decided: every checkpoint lands on a commit.**
A *commit* is the end of a transaction, marked by its `crc`; a *checkpoint* is a flush, which folds complete transactions only, so every header describes a state some commit produced.
[Durability](durability.md#what-is-guaranteed) requires exactly this, since a header describing half a transaction would let recovery land inside it — and it means the format needs no commit record beyond the transaction's `crc`.

A checkpoint that folded part of an unfinished transaction, to bound the memory the transaction holds, would need a way to undo that part should the transaction never end, and the format has none.
So an unfinished transaction stays in memory until it ends, and keeping that small is the application's side of the bargain: [short transactions, and batches for long sequences](../impl/transactions-and-batches.md#recommendations).
