---
title: Payload pages
---

**Status: a proposal.**
It changes the journal's record encoding, what a header's world holds, and recovery; the API is untouched.

**A transaction whose bytes are large writes them once, straight into framed `Data` pages of their own, *payload pages*, and its journal records name them by address; the flush that folds the journal adopts the payload pages as data pages and states spans over them, writing none of their bytes again.**
Today every byte a transaction inserts is written twice, once into the journal and once by the flush into data pages, and the journal pages it took become holes once they are folded.
For small transactions that is the cheap way, and it stays the way; for a transaction of hundreds of kilobytes, an image pasted into a drawing or a file's first content, it doubles the file and leaves a hole that only compaction drains.
Creating a file is such a transaction, so creation needs no path of its own.

## The problem

**A drawing of 4.26 MB, created in the Inkscape demo and edited for five flushes, was a file of 8.00 MB, where a consolidated copy takes 4.20 MB.**
kladde-inspect on that file, `map7.kladde`, which holds three embedded raster images of 4.2 MB and 17 KB of everything else, shows where it went:

| pages | what |
| --- | --- |
| 1070 | live data pages, all but five of them full |
| 960 | unreached pages 109–1068, chained by journal trailers: the journal that created the file |
| 17 | headers, span table, the current journal, and pages held for the older header |

Creating a file is `splice(0, 0, encoding)` and a flush, in kladde-rs and kladde-cpp alike.
The journal's [budget](../impl/transactions-and-batches.md#the-journals-budget) is at most 10 pages, and a transaction too large for it is appended whole, so the one splice took 1067 journal pages from page 2 on.
The flush that folded it could not reuse them, since they were still live, so it wrote the 1070 data pages behind them.
Once folded, the journal pages were holes below live data, which [truncation](../impl/consolidation.md#truncation) cannot return and [compaction mode](../impl/consolidation.md#compaction-mode) drains at the consolidation budget's pace, a few pages per flush.
Creating the same 4.26 MB with kladde-rs and closing it at once, with no edit at all, leaves 2139 pages, 1.99 times a consolidated copy.

**Any transaction large next to the journal's budget does the same, not only the first.**
Pasting the 2.3 MB JPEG into an existing drawing would take about 600 journal pages, lowest first from the holes or at the end of the file, and the flush would write about 600 data pages beside them; the journal pages then become holes, wherever they lay.
Closing into a consolidated copy, as kladde-rs's and kladde-cpp's `close_consolidated` do, returns the space when the application closes the file, but not during the session, and not after a crash.

## The proposal in brief

- **A `Write` or a `Splice` may keep its bytes *external*: in a payload page, named by address and length, with a checksum of the bytes.**
  The record is the ordinary one behind an [escape](../spec/journal.md#encoding), so records that keep their bytes inline cost what they cost today.
- **A payload page is a framed `Data` page, of the epoch of the journal whose transactions name it, written whole before the first of them is appended, and never written again.**
  It is written while it is still reusable, so the [reuse rule](../spec/durability.md#the-reuse-rule) needs no new exception.
- **A payload page belongs to the world of every header whose journal leads to a transaction that names it**, from the moment that transaction is appended, as the next journal does from the moment its [seal](../spec/journal.md#seals) is.
- **Recovery checks each external reference against its page**: a page past the end of the file, a page whose framing fails or that is no `Data` page of the journal's epoch, a range outside its content, or bytes whose checksum does not match end the journal's valid prefix before the transaction, as a torn tail does.
- **The flush that folds the journal states spans over the payload bytes where they lie**, and the payload pages become data pages of the header it commits.
- **The store decides alone which bytes go external**, and how to cut them across records and pages; neither applications nor containers see a difference.

## Writing large bytes once: payload pages, a flush per transaction, or a creation path

Three designs would keep large bytes from being written twice; **payload pages are preferred over flushing each large transaction at once, and over a creation path of its own**, because they are the only one that keeps every mutation off the disk's `fsync` and covers every large transaction.

### Payload pages

The call that records a large transaction writes its payload pages and appends its records, without an `fsync`, as a call appends to the journal today; an application crash keeps both in the page cache, and a power cut keeps what write-back reached, which recovery checks.
No flush is needed for the transaction, and the next ordinary flush adopts the pages and writes none of their bytes.
The price is in the format: an escape, a reference with a checksum, recovery that reads the pages it names, and a world that includes them.

### A flush per large transaction

The store would not append a large transaction at all, but fold it at once in a flush that writes its bytes straight into data pages; the format would stay as it is.
But the call could not return before that flush's `fsync` and header write, since no journal record would hold the transaction until then, so every large transaction would wait for the disk on the caller's thread: milliseconds on an SSD, and much longer on a spinning disk or a network file system.
That is the stall the seal and the [flush off the caller's thread](../impl/transactions-and-batches.md#a-flush-off-the-callers-thread) exist to keep away from every mutation, so this design would give it back exactly where a pasted image makes it most visible.

### A creation path of its own

`create` would write the initial value as a consolidated file, the way closing into a consolidated copy does, and leave the journal out; nothing needs to be durable before `create` returns, so the format would stay as it is.
It would fix creation and nothing else: the pasted image would still be written twice and leave its hole.
With payload pages, creation is one large transaction among others and comes out within a page or two of a consolidated file.

## The record: an escape rather than a bit of the head

**An external record is an ordinary `Write` or `Splice` after the escape `1`, which costs three bytes per external record and nothing for any other**, where a flag in the head or in the length of `bytes` would cost every record a bit.

```
escaped  := 0x00 0x00 escape:varint (seal | external)
seal     := page:varint                     ; escape 0
external := record                          ; escape 1: a Write or Splice with external bytes
; in an external record:
bytes    := length:varint address:varint crc:byte{4}
```

- `length` is 1 to `MAX_PAGE_CONTENT`, and `[address, address + length)` lies inside the `content` of one page, as a [`Ref`](../spec/span-table.md#entries) does; `crc` is the CRC-32C of those bytes.
- The record after escape `1` is a `Write` or a `Splice`; a `Move`, a `Paste`, or another escape there makes the file invalid, as any record that does not decode does.
- The escape has delta 0 and leaves the cursor where it was, as every escape does; the record after it moves the cursor as it would with inline bytes, to `position + length`.
- An external record makes the same cut as an inline one: a `Write` its overwritten bytes, a `Splice` its removed ones.

*Why not a bit of the head.*
The head holds the delta and the kind in one zigzag varint, whose one byte fits deltas from −16 to 15; a flag would halve that to −8 to 7, for every record, so that the rare large record could save its three-byte escape.
It would also mean nothing for a `Move` or a `Paste`, which carry no bytes.
A flag in the `length` of `bytes` would leave the head alone, but cut the inline payloads that fit a one-byte length from 127 bytes to 63.
An external record names at most `MAX_PAGE_CONTENT` bytes, so its escape costs at most 0.07 % of what it names.

## Addresses: absolute rather than relative

**A reference names its page by its absolute address in the file**, rather than by its distance from where the journal's previous reference ended.
A distance would mostly be the 15 bytes of framing between the end of one full payload page's content and the start of the next, one byte against an absolute address's four in a file of 2 MB to 256 MB; three bytes per reference are again 0.07 % of what it names.
An absolute address needs no cursor carried from record to record and from transaction to transaction, and a reference reads on its own.
The span table delta-encodes addresses because a page holds hundreds of entries and every byte of them is overhead; a journal holds few external references, each beside a page of payload.

## What the checksums cover: a checksum per reference

**Each external reference carries the CRC-32C of the bytes it names, and the transaction's checksum stays a checksum of the journal's stream, as now.**
Three ways to check a payload are compared below: the reference's own checksum, the payload page's framing, and feeding the payload into the journal's CRC chain.
The reference's own checksum is preferred, because the page's framing would accept stale bytes, and the chain would have recovery parse records before it has validated them.

### A checksum in each reference

Recovery validates the transaction as it does today, by its `crc`, before it decodes a record, so [nothing unvalidated is ever parsed](../spec/journal.md#framing).
It then reads each external reference's bytes from its page, checks the page's framing, and compares their CRC-32C with the reference's `crc`; a mismatch means the page's write did not reach the disk, and the transaction never happened.
It costs four bytes per reference, at most 0.1 % of what the reference names, and changes nothing in the CRC chain.
The checksum needs no epoch salt: a page that holds exactly the bytes the reference names holds the right bytes, however it came to hold them.

### The payload page's framing alone

Recovery would accept a reference whose page carries valid framing, `Data`, and the journal's epoch, and the reference would need no checksum.
But one situation gives a stale page exactly that framing: when a crash loses header `E` after a session wrote payload pages for journal `E + 1`, recovery lands on header `E − 1`, the next session issues epoch `E` again, and its journal `E + 1` takes pages from a pool that holds the lost session's payload pages, with their valid framing of epoch `E + 1`.
If that session wrote one of them for a transaction, and a power cut kept the transaction's journal bytes but not the page, the page's stale framing would validate, and recovery would replay the lost session's bytes as the transaction's payload.
This is the same situation that makes [a session's first flush](../spec/journal.md#the-start-of-a-session) write the first page of its journal; here no zeroing could help, since the stale pages could be any of the pool's.
The page's framing stays a check, for a different reason: see below.

### The payload in the CRC chain

The journal's chain input would include, after each reference, the bytes it names, so that the transaction's `crc` would cover them.
It would cost no bytes, and a stale page would fail the chain as it fails a checksum per reference.
But recovery could check the chain only after decoding the transaction's records, to find which bytes to feed it and from where, and it would read pages that unvalidated records name; the length prefix exists so that recovery never does that.
It would also make the chain's definition depend on the record encoding, which it does not today.

### The framing is still checked

**A payload page whose framing fails, or that is no `Data` page of the journal's epoch, ends the valid prefix, even if the bytes a reference names match its checksum.**
A torn write can leave a page's new content with its old framing, and the flush adopts the page as a data page of the header it commits, which [opening](../impl/opening.md#the-steps) checks like any other; a page with failing framing would make the next header's world invalid.
Requiring the journal's epoch also keeps a reference out of the governing header's world, whose pages carry that header's epoch at most, so no rule of its own is needed for that.

## Payload pages and the world

The [durability](../spec/durability.md) layer changes in three places, all of them where it already treats the journal specially.

- **The world** of a header holds, besides the pages of the next epoch's journal, every payload page that a complete transaction of that journal names.
  A payload page written for a transaction that is never appended, because the application abandoned the transaction or the process died first, is in no world, and is reusable again.
- **Invariant I1** exempts, besides the journal the new header names, the payload pages its transactions name: they are written after the header, and validate themselves, reference by reference.
  Once a flush folds them they are data pages of the header it commits, and its `fsync` covers them, since they were written before their transactions were appended, and those before the flush began.
- **Page states** gain one transition: a reusable page becomes live when a transaction that names it as a payload page is appended, as a journal page does when the stream reaches it.

The [reuse rule](../spec/durability.md#the-reuse-rule) is untouched, because a payload page is written while it is still reusable, and never again; a writer that wanted to add later transactions' payloads to a page already named would need an exception to it like the journal's own appends, and with it the risk that tearing that page loses transactions that write-back had already kept.
The [start of a session](../spec/journal.md#the-start-of-a-session) is untouched too: a session that recovers a non-empty journal folds it before appending, which turns its payload pages into data pages that no one writes again.

## Recovery

[Recovery](../spec/journal.md#recovery) gains one check after a transaction's `crc` has matched and before its records count:

> For each external reference, read its page; if the page lies past the end of the file, its framing fails, it is no `Data` page of the journal's epoch, the referenced range does not lie inside its content, or the bytes there do not match the reference's `crc`, the transaction and everything after it are the torn tail.

A reference whose `length` is 0 or exceeds `MAX_PAGE_CONTENT`, or whose range would cross a page boundary, is a writer's bug, since a torn write cannot produce a matching transaction `crc`, and makes the file invalid.
A writer never lets two references of a journal name overlapping bytes, which would leave two spans showing one byte after the fold; this is the span table's [writer invariant](../spec/span-table.md#rules-for-writers), stated for references, and readers need not check it, as they need not for spans.

Recovery reads every payload page that its journal names, which [opening](../impl/opening.md#the-steps) would read anyway, as the pages the next flush adopts, and a [cut](../spec/journal.md#cuts) whose bytes lie in a payload page is at hand as much as one whose bytes lie in the journal.

## The store's policy

**When the store appends a transaction, it may keep the bytes of any `Write` or `Splice` of at least a threshold `T` external, cutting them across records and payload pages as it likes; nothing above the store changes.**
The threshold applies to the bytes the record carries once the [whole-value guarantee](../spec/index.md#guarantees-an-implementation-must-provide) has reduced a replaced value to its changed range, and to a `Paste` that is written as its fallback bytes because its cut has ended.

- **Cutting.**
  The bytes are cut into runs of `MAX_PAGE_CONTENT`, one payload page each; a remainder of at least `T` gets a page of its own, or shares one with other remainders of the same transaction, since the transaction's pages are all written before it is appended, and a shorter remainder stays inline in the last record.
  A `Splice(pos, removed, bytes)` becomes `Splice(pos, removed, first)`, followed by splices that remove nothing at the cursor, delta 0; a `Write` becomes the same splices, with the first removing what the write overwrote, so that it still makes one cut, and a paste of the overwritten value still names one.
- **The threshold.**
  About half a page, 2 KB, to be measured: a payload page holding `r` bytes costs a page write now and leaves `4081 − r` bytes of slack for consolidation, where inline bytes cost `r` in the journal and `r` again in a data page the flush shares with other content.
- **The pages.**
  Payload pages come from the same pool as journal pages, lowest first, and stay in the store's mirror, so the store keeps the bytes in memory as its arena does today, and a flush reads nothing back.
  A transaction abandoned after its pages were written returns them to the pool at once; a payload page that cannot be written fails the transaction, as a journal append that cannot get a page does.
- **The budget.**
  A transaction's share of the [journal's budget](../impl/transactions-and-batches.md#the-journals-budget) is what its records take in the journal, about 15 bytes per payload page with the escape, head, `removed`, length, address and checksum; payload pages do not count, since they are future data pages rather than overhead.
  Creating `map7.kladde` takes about 16 KB of records, which fits its budget of 10 pages, so no flush is forced either before or after it.
- **The fold.**
  A piece's source gains a third kind, `Payload(address)`, beside `Committed` and `Arena`; records edit those pieces as they edit others, and two of them continue each other only within one page.
- **The flush.**
  A `Payload` piece becomes a `Ref` span over the bytes where they lie, with no data written; the payload pages enter the page table as data pages, covered by the bytes their spans show, and those that no span reaches retire like the journal's pages.
  The flush may still copy a short `Payload` piece inline, or into a page it writes anyway, where that is cheaper than keeping a page alive for it.

**Nothing changes for applications or containers**: `WriteBackend` keeps its signatures, a container still hands the store its bytes as a slice, and the store copies them into payload pages instead of its arena.

## What it would do to the demo's file

Estimated from the measurements above, for the drawing of 4.26 MB:

| | today | with payload pages |
| --- | --- | --- |
| journal for the creating transaction | 1067 pages | 4 pages, about 16 KB |
| data pages the creating flush writes | 1070 | the schema's, and none of the drawing's |
| file after creation | 2139 pages | about 1075 pages, within a page or two of a consolidated copy |
| bytes written to create it | 8.7 MB | 4.4 MB |

Pasting the 2.3 MB JPEG later would write about 600 payload pages and 9 KB of records, and the next flush would write a header and a leaf or two for it.

## Deferred: encoding large values straight into payload pages

The layers above the store encode a value into memory and hand the store the encoding as one slice, which the store copies into payload pages.
An encoder that wrote a large value's encoding into payload pages as it went would save that copy, and for a value being built the buffer that holds it.
The format proposed here allows it already, since a reference names bytes wherever a payload page holds them; it would change the encoder's interface to the store, which this proposal leaves alone, and is deferred.

## Where it lands

- [Journal](../spec/journal.md): escape `1` and the external `bytes` in the record encoding, the cuts' bytes, the check in recovery, and its motivation.
- [Durability](../spec/durability.md): the world, invariant I1, the page states, and what survives an application crash.
- [Span table](../spec/span-table.md#bounds): the bounds of a reference, which are a `Ref`'s.
- [Conformance](../spec/conformance.md#the-suite): the suite drops every subset of payload page writes, as well as truncating the journal at every byte.
- [Tooling](../spec/tooling.md): level 1 reports payload pages as the journal's.
- [The fold and the flush](../impl/flush.md), [the journal's budget](../impl/transactions-and-batches.md#the-journals-budget), [opening](../impl/opening.md): the store's policy above.

## Open questions

- **The threshold**, measured on pastes of images and of large groups in the demo, and on kladde-svg.
- **Whether payload pages should count toward a flush after all**: they are no overhead once adopted, but until a flush adopts them a crash makes recovery read them, and a long session of large pastes could hold many of them unadopted.
- **Whether the flush should copy partly filled payload pages** into pages it writes anyway, rather than leave their slack to consolidation.
