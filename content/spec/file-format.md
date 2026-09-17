---
title: File format
---

The page layer: how a kladde file is divided, what every page carries, how a file is identified, and how versions are negotiated.

**Status: draft.** The shape is settled; exact field widths and offsets are TBD.

## Pages

A kladde file is a sequence of fixed-size **pages**.
The page size is recorded in the file header and is one of 4, 8, 16, 32, or 64 KiB, uniform throughout a file.

*Why fixed pages, and why 4 KiB by default.*
The operating system rewrites a whole page even when the application modifies one byte of it, so the page is the honest unit of I/O; accounting in anything smaller measures a cost the file system does not charge.
4 KiB matches the write-back granularity of common file systems.
16 KiB may pay on platforms with 16 KiB native pages, which is a measurement question rather than a design one — nothing in this specification depends on the value.

Outside of journal appends, an implementation **writes whole pages only**, and only to pages that are reusable under the [reuse rule](durability.md#the-reuse-rule).

## Page framing

Every page except the two header pages concatenates these fields, in order, without delimiters:

| field | width | meaning |
| --- | --- | --- |
| `kind` | 2 bits | one of `Data`, `AddressTable`, `Journal` |
| `content_size` | 14 bits | the size of `content` in bytes |
| `epoch` | 64 bits | the flush counter at the time the page was written |
| `content` | `content_size` bytes | at most `MAX_PAGE_CONTENT` |
| `crc` | 32 bits | checksum over all preceding bytes of the page |
| `padding` | to the page boundary | arbitrary, excluded from the CRC, may be absent on the file's last page |

`MAX_PAGE_CONTENT` is the page size minus 14 bytes, i.e. 4082 bytes for 4 KiB pages.

A page whose CRC does not validate **must be ignored** during loading.
This is safe because the durability protocol guarantees that no committed state ever references a page whose write did not complete, so an invalid CRC can only belong to garbage that nothing references.

There is deliberately **no `Free` kind and no free marker**.
Liveness is not recorded in a page; it is defined by reachability from a committed header.

### How much of the framing is load-bearing

Less than it looks, and the distinction matters to anyone reasoning about the crash argument.

Only two checksums in the file are load-bearing: the **header** CRCs, because a header is written without a covering `fsync` before it matters, so a torn or missing header write must be detectable; and the **journal**'s per-transaction CRC chain, because journal appends are likewise never fsynced before a power cut can hit them.
The framing on `Data` and `AddressTable` pages is *not* load-bearing: [invariant I1](durability.md#the-two-invariants) guarantees that any page a valid header can reach was fsynced before that header was written, so recovery never meets a referenced page whose write did not complete.

The framing is required on every page anyway, as defence in depth.
The `crc` turns *later*, silent damage — bit rot, a misdirected write by other software, a bug that writes to a live page — into a detected failure instead of quietly wrong data.
The `epoch` is an end-to-end assertion about the `fsync` contract.
And `kind` with `content_size` keeps every page self-describing, which keeps a last-resort scavenger possible and keeps the page writer uniform.
The price is 14 bytes in 4096, or 0.34 %.

## Header pages

Pages 0 and 1 are the two **header pages**, used alternately: the flush with epoch `E` writes header slot `E mod 2`.
Each holds a fixed-size structure:

| field | purpose |
| --- | --- |
| magic | identifies the file as kladde and catches accidental opens of unrelated files |
| format version | the specification version this file was written against |
| minimum reader version | the oldest specification version that can still read this file |
| `page_size` | 4, 8, 16, 32, or 64 KiB |
| `epoch` | this header's flush counter |
| root allocation id | the [allocation](allocations.md) holding the root value |
| schema table id | the allocation holding the [descriptor table](schema/canonical-encoding.md#table-encoding) |
| root fingerprint | the [schema fingerprint](schema/fingerprints.md) of the root type |
| next journal segment | the start page of the segment that will collect transactions after this flush |
| `crc` | over all of the above |

The header page **is** the root [address-table](address-table.md) page: the fields above are followed by the address-table page payload, all covered by the same CRC.
So the address table is reached without indirection, and the commit that publishes a new header publishes a new root table page in the same write.

Exact widths, ordering, and the reservation of space for future roots are **TBD**.

On open, both header pages are read; the CRC-valid one with the **higher epoch** governs.
A torn header write can only damage the slot being written, which held the older of the two headers, so the previous state remains reachable through the other slot.

*Why two alternating slots.*
This is the only fixed-location, overwritten-in-place structure in the file, and it is the minimum needed to publish a new state atomically without an allocation protocol for the root itself.
It is LMDB's meta-page scheme.
Both slots corrupting simultaneously is unrecoverable without a scan, but they are single-sector writes at opposite ends of a two-page span and are never written in the same flush, so that requires two independent failures.

The **root fingerprint** is in the header rather than only in the schema table so that the common case — an application opening a file it wrote itself, with an unchanged schema — is a single 16-byte comparison with no need to parse the descriptor table at all.
See [Fingerprints](schema/fingerprints.md#as-a-fast-path).

## Epochs

The **epoch** is a monotone counter, incremented by one per flush, stored in every page and in each header.

It serves three purposes:

1. It **orders contradicting statements** between address-table pages, which is what makes shadowing work without a rewrite ([Address table](address-table.md#conflict-resolution-across-epochs)).
2. It **salts the journal's CRC chain**, so stale bytes in a reused page can never impersonate valid journal content.
3. It is an **end-to-end assertion**: a page reachable from the header of epoch `E` must carry an epoch `≤ E`.
   A violation reveals that the platform broke the `fsync` contract, and the file must be treated as damaged rather than silently misread.

Epochs are 64-bit.
At one flush per millisecond that lasts half a billion years, so **wrap-around is not a concern** and no implementation may assume it must handle one.

*Why not 32 bits.*
A 32-bit counter would require actively rewriting laggard pages before the counter catches up to them — exactly PostgreSQL's transaction-id wraparound "freezing", an operational burden it carries only because its format predates the lesson.
A new format should pay the four extra bytes per page; they are 0.1 % of a 4 KiB page, and modern systems (ZFS transaction groups, LMDB transaction ids) are all 64-bit.

Epochs are **replay-stable**: the epoch of a flush is the committed header's epoch plus one, so a flush re-run during recovery reproduces the same epoch.

## Versioning

Two version numbers, because "can I read this?" and "was this written by something I know?" are different questions.

- **format version** — what the writer used.
  Informational for a reader; useful for diagnostics and for deciding whether to rewrite the file in a newer form.
- **minimum reader version** — the writer's declaration of the oldest reader that can still make sense of the file.
  A reader whose own version is below this **must** refuse to open the file rather than attempt a partial interpretation.

A writer raises the minimum reader version only when it uses a feature that older readers would silently misinterpret.
Adding a new [primitive code](schema/type-descriptors.md#primitive), for instance, does raise it, because an older reader would not know the width of the new primitive and would compute every subsequent offset wrongly.
Reordering the descriptor table does not, because references are table-local and any conforming reader follows them.

This is the same slot a per-file *application* semantic-versioning scheme would use.
Whether kladde exposes application versioning through the same mechanism or a separate one is **TBD**; it should be one mechanism, not two parallel ones.

## Concurrency

The format assumes **a single process with exclusive write access**.
Multiple concurrent writers, and readers in other processes observing a file being written, are out of scope.

This is a scoping decision, not a structural one.
The single-owner pointer discipline and the copy-on-write commit are both compatible with an MVCC-style snapshot-isolation scheme, which is the direction a concurrent version would take.
**TBD.**
