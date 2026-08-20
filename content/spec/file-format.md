---
title: File format
---

The container layer: how a kladde file is laid out at the coarsest level, how an implementation identifies one, and how versions are negotiated.

**Status: draft.** The overall shape is settled; byte offsets and field widths are TBD.

## Overall shape

A kladde file is a byte sequence containing three things:

1. a fixed-size **header** at offset zero;
2. the **snapshot** — a heap of allocations holding a recent, compact, but not necessarily current state of the stored value graph;
3. the **journal** — an append-only sequence of records describing mutations that have not yet been folded into the snapshot.

The snapshot is the bulk of the file and is described by the [allocation model](allocations.md).
The journal is described in [Journal](journal.md).

A reader that opens the file reconstructs the current state as *snapshot, with the journal replayed on top*.
A file whose journal is empty is fully compacted in the logical sense: the snapshot alone is current.

## Header

The header is a small, fixed-size region at offset zero that the allocator never hands out.
Its size is frozen for the life of the format, which is why it holds only what must be at a known offset; anything that could grow lives in an allocation instead.

The header carries:

| field | purpose |
| --- | --- |
| magic | identifies the file as kladde and catches accidental opens of unrelated files |
| format version | the specification version this file was written against |
| minimum reader version | the oldest specification version that can still read this file |
| root allocation id | the [allocation](allocations.md) holding the root value |
| schema table id | the allocation holding the [descriptor table](schema/canonical-encoding.md#table-encoding) |
| root fingerprint | the [schema fingerprint](schema/fingerprints.md) of the root type |
| journal start | where the journal region begins |
| commit marker | the atomically-updated pointer that publishes a completed fold |

Exact widths, ordering, and checksumming: **TBD**.

The `root fingerprint` is deliberately in the header rather than only in the schema table.
It makes the common case — an application opening a file it wrote itself, with an unchanged schema — a single 16-byte comparison, with no need to parse the descriptor table at all.
See [Fingerprints](schema/fingerprints.md#as-a-fast-path).

## Versioning

Two version numbers, because "can I read this?" and "was this written by something I know?" are different questions.

- **format version** — what the writer used.
  Informational for a reader; useful for diagnostics and for deciding whether to rewrite the file in a newer form.
- **minimum reader version** — the writer's declaration of the oldest reader that can still make sense of the file.
  A reader whose own version is below this **must** refuse to open the file rather than attempt a partial interpretation.

A writer raises the minimum reader version only when it uses a feature that older readers would silently misinterpret.
Adding a new [primitive code](schema/type-descriptors.md#primitive), for instance, does raise it, because an older reader would not know the width of the new primitive and would compute every subsequent offset wrongly.
Reordering the descriptor table does not, because references are table-local and any conforming reader follows them.

This is the same slot that a per-file *application* semantic-versioning scheme would use.
Whether kladde exposes application versioning through the same mechanism or a separate one is **TBD**; it should be one mechanism, not two parallel ones.

## Crash consistency at the container level

The file must be safe against a crash at any instant.
Two separate mechanisms cover the two things that can be in flight.

**A crash while appending to the journal** is handled by the journal's own framing: every record carries a length prefix and a checksum, so a torn record at the tail is detectable and truncatable, and nothing before it is affected.
See [Journal](journal.md#recovery).

**A crash during a fold** — while the journal is being applied to the snapshot — is handled by copy-on-write.
The fold never overwrites live snapshot data in place; it writes new and moved allocations to space that is not currently live.
The old snapshot stays byte-for-byte valid for the entire duration.
The fold commits with a single small, `fsync`'d write of the header's commit marker, which is only durable after the data it points at is durable.
A crash before that write leaves the old state intact and the half-written new data inert; a crash after it leaves the new state, with the old space merely unreferenced.

The cost is transient: old and new coexist until the commit, so a fold temporarily needs extra space, and the superseded space is reclaimed afterwards.
Reclamation is not atomic and does not need to be — a crash during reclamation leaks space that a later fold recovers, and never corrupts anything.

An implementation **may** use a different crash-consistency mechanism, provided a reader that crashes at any point still finds a valid file.
Copy-on-write is the specified default because it is the simplest member of the family and needs no undo information.

## Concurrency

The format currently assumes **a single process with exclusive write access**.
Multiple concurrent writers, and readers in other processes observing a file being written, are out of scope.

This is a scoping decision, not a structural one.
The single-owner pointer discipline and the copy-on-write fold are both compatible with an MVCC-style snapshot-isolation scheme, which is the direction a concurrent version would take.
**TBD.**
