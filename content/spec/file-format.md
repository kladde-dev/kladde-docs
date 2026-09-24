---
title: File format
---

The page layer: how a kladde file is divided, what every page carries, how a file is identified, and how versions are negotiated.

**Status: draft.** The shape is settled; exact field widths and offsets are TBD.

## Integer encodings and CRCs

All integer fields in kladde files are unsigned.
For every integer field, the file format specification states either a fixed width (in bytes) or a `varint` encoding.
Fixed width integers are encoded in little-endian byte order.
Varints are encoded as unsigned [LEB128](https://en.wikipedia.org/wiki/LEB128).

CRC calculations use **CRC-32C** (Castagnoli), specified precisely as: reflected polynomial `0x82F63B78`, initial value `0xFFFFFFFF`, input and output both reflected, and a final XOR with `0xFFFFFFFF`.
This is the variant Btrfs, ext4 metadata, iSCSI and SCTP use, and the one both x86-64 (since SSE4.2) and ARMv8 compute with a single instruction.
As a check value, the CRC-32C of the nine ASCII bytes `123456789` is `0xE3069283`, which a kladde file stores — little-endian, like every other fixed-width field — as the four bytes `83 92 06 E3`.

## Pages

A kladde file is a sequence of fixed-size **pages**.
The page size is recorded in the file header as a binary logarithm and is uniform throughout a file.
Currently only 4 KiB is allowed; 8, 16, 32, and 64 KiB are reserved, and the bounds elsewhere are derived from a 64 KiB ceiling so that nothing has to be revisited if a larger page is ever wanted.

*Why fixed pages, and why 4 KiB by default.*
The operating system rewrites a whole page even when the application modifies one byte of it, so the page is the honest unit of I/O; accounting in anything smaller measures a cost the file system does not charge.
4 KiB matches the write-back granularity of common file systems.
16 KiB may pay on platforms with 16 KiB native pages, which is a measurement question rather than a design one — nothing in this specification depends on the value.

Outside of journal appends, an implementation **writes whole pages only**, and only to pages that are reusable under the [reuse rule](durability.md#the-reuse-rule).

## Page framing

Every page except the pages that make up the current journal concatenates the following fields, in order, without delimiters:

| field          | width                        | meaning                                                                                             |
| -------------- | ---------------------------- | --------------------------------------------------------------------------------------------------- |
| `header`       | 0 unless it is a header page | only present in [[#header pages]]                                                                   |
| `kind`         | 1 byte                       | one of `AddressTable` (`0x01`) or `Data` (`0x02`). For header pages: always `AddressTable` (`0x01`) |
| `epoch`        | 8 bytes                      | the flush counter at the time the page was written                                                  |
| `content_size` | 2 bytes                      | the size of `content` in bytes                                                                      |
| `content`      | `content_size` bytes         | the payload of the page, encoded depending on `kind`                                                |
| `crc`          | 4 bytes                      | checksum over all preceding bytes of the page except `content_size` (but including `header`)        |
| `padding`      | to the page boundary         | arbitrary, excluded from the CRC, may be absent on the file's last page                             |

A page whose CRC does not validate **must be ignored** during loading.
This is safe because the durability protocol guarantees that no committed state ever references a page whose write did not complete, so an invalid CRC can only belong to garbage that nothing references.
The CRC calculation excludes `content_size` so that a writer may encode a page and calculate its CRC before knowing the page size if this turns out to be easier in a given situation.
This is safe because `content_size` tells the reader where to stop decoding `content` and expect the `crc`, so if the CRC at that position matches then `content_size` is correct.
A reader must nevertheless **bounds-check `content_size` before using it**, since a corrupted value can point the CRC read past the end of the page; a value that does not leave room for the `crc` within the page is itself a failed validation.

`MAX_PAGE_CONTENT` — the largest `content` a non-header page can hold — is the page size minus the 15 bytes of framing above, i.e. **4081 bytes** for 4 KiB pages.
A header page holds correspondingly less, by the width of its `header` field.

There is deliberately **no `Free` kind and no free marker**.
Liveness is not recorded in a page; it is defined by reachability from a committed header.

### How much of the framing is load-bearing

Less than it looks, and the distinction matters to anyone reasoning about the crash argument.

Only two checksums in the file are load-bearing: the **header** CRCs, because a header is written without a covering `fsync` before it matters, so a torn or missing header write must be detectable; and the **journal**'s per-transaction CRC chain, because journal appends are likewise never fsynced before a power cut can hit them.
The framing on `Data` and `AddressTable` pages is *not* load-bearing: [invariant I1](durability.md#the-two-invariants) guarantees that any page a valid header can reach was fsynced before that header was written, so recovery never meets a referenced page whose write did not complete.

The framing is required on every page anyway, as defence in depth.
The `crc` turns *later*, silent damage — bit rot, a misdirected write by other software, a bug that writes to a live page — into a detected failure instead of quietly wrong data.
The `epoch` is an end-to-end assertion about the `fsync` contract.
And `kind` with `content_size` keeps every framed page self-describing, which keeps a last-resort scavenger possible and keeps the page writer uniform.
Journal pages carry no framing, so a scavenger cannot identify them — which costs nothing, since a scavenger's job is to recover committed state and the journal is by definition what has not been committed.
The price is 15 bytes in 4096, or 0.37 %.

## Header pages

Page 0 must always be present and it is always a **header page**, and page 1 must also be a header page if it is present (i.e., if the file is larger than 1 page).
Page 0 must always have an even `epoch` field, and if page 1 is present then it must have an odd `epoch` field.

Each header page holds a fixed-size `header` field in the [[#page framing]], which concatenates the following fields with no delimiters:

| field                  | width    | purpose                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| magic                  | 11 bytes | identifies the file type as kladde and catches accidental opens of unrelated files; present even in page 1 to simplify size calculations. See [[#the magic\|below]]                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| log2(page size)        | 1 byte   | binary logarithm of the page size; currently, the only allowed value is `12`, indicating a 4 KiB page size, but the spec is designed not to prevent `13`, `14`, `15`, or `16` (for powers of 2 from 8 KiB through 64 KiB) in the future if measurements deem them useful.                                                                                                                                                                                                                                                                                                                                                                                 |
| format version         | 2 bytes  | the specification version this file was written against; currently only version `0` is allowed, indicating "pre-stable".                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| minimum reader version | 2 bytes  | the oldest specification version that can still read this file; currently only version `0` is allowed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| root allocation id     | 4 bytes  | the [allocation](allocations.md) holding the root value                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| schema table id        | 4 bytes  | the allocation holding the [descriptor table](schema/canonical-encoding.md#table-encoding)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| root fingerprint       | 16 bytes | the [schema fingerprint](schema/fingerprints.md) of the root type                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| first journal page     | 4 bytes  | the page holding the beginning of the journal that will be used after the flush that created this header page (must point to either an existing page `>= 2` that is not part of nor reachable from the address table, or to `max(2, num_pages_in_file)` to indicate that the journal page should be appended when the first op is journaled, if necessary after extending the last page to its page boundary and appending a slot for an unused header page 1). Here, `num_pages_in_file` is the *logical* number of pages, i.e., for a [[spec/durability#Niche optimization\|niche optimized]] file it is 1 plus the number of physically present pages. |

The header page **is** the root [address-table](address-table.md) page, so the address table is reached without indirection, and the commit that publishes a new header publishes a new root table page in the same write.

The two header pages are written alternatingly: the flush with epoch `E` writes header slot `E mod 2`.
On open, a reader reads both header pages (if both are present), picks the CRC-valid one with higher epoch, and ignores the other header page.
A torn header write can only damage the slot being written, which held the older of the two headers, so the previous state remains reachable through the other slot.

**Why two alternating slots.**
This is the only fixed-location, overwritten-in-place structure in the file, and it is the minimum needed to publish a new state atomically without an allocation protocol for the root itself.
It is LMDB's meta-page scheme.
Both slots corrupting simultaneously is unrecoverable without a scan, but they are single-sector writes at opposite ends of a two-page span and are never written in the same flush, so that requires two independent failures.

The **root fingerprint** is in the header rather than only in the schema table so that the common case — an application opening a file it wrote itself, with an unchanged schema — is a single 16-byte comparison with no need to parse the descriptor table at all.
See [Fingerprints](schema/fingerprints.md#as-a-fast-path).

### The magic

```
8B 4B 4C 41 44 44 45 0D 0A 1A 0A       \x8B K L A D D E \r \n \x1A \n
```

Eleven bytes, following the pattern PNG established and HDF5 copied, because every byte of it does a job:

| bytes               | purpose                                                                                                                                                                                                          |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `8B`                | high bit set, so any tool applying a "does this look like text?" heuristic classifies the file as binary immediately. Not `89`, which is PNG's and HDF5's, so that a partial match cannot be mistaken for either |
| `4B 4C 41 44 44 45` | `KLADDE` in ASCII, so a human running `xxd \| head` or `strings` sees what it is                                                                                                                                 |
| `0D 0A`             | catches a transfer that mangled CRLF→LF: the pair arrives as a lone `0A` and the magic fails                                                                                                                     |
| `1A`                | DOS end-of-file, so `type file` on Windows stops here instead of spewing the whole file                                                                                                                          |
| `0A`                | catches the reverse mangling, LF→CRLF: this byte arrives as `0D 0A` and the magic fails                                                                                                                          |

`file(1)` has no entry for it, so a kladde file reports as `data` — which is the desired outcome, and better than a wrong guess.
Submitting an entry to the `file` magic database is worth doing once the format is frozen, not before.

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
