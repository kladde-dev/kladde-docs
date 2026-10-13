---
title: Related work
---

Where kladde's design comes from, and where it deliberately parts company.

**No system known to these documents combines what kladde does, but each of its layers has a close precedent**: kladde is an editor's piece table kept on disk, linked rather than keyed by position, made crash-safe by shadow paging with an intent log, kept compact by a log-structured cleaner, and holding a serialization much like Avro's.
None of the load-bearing pieces is novel, which is the point: their failure modes are understood and bounded, which is exactly what the [bespoke machinery they replaced](../superseded/in-place-flush.md) could not claim.

What *is* different from all of them is the premise: **kladde reads in bulk and looks nothing up on disk.**
Almost every divergence below is a consequence of that one fact, and every one of them cuts toward simplicity.

| layer | closest precedents | shared | different |
| --- | --- | --- | --- |
| the encoding | Avro's binary encoding | the schema in the file, no tags, no padding, varints, counts | edited in place; text ends in a terminator; `Fixed` |
| the sequence on file | Word's fast save; FAT's chains | an on-disk piece table; successors in a table | links instead of positions; byte-granular spans |
| the span table | LFS's inode map; Bitcask | a table of locations, newest wins, held in memory | nothing is searched on disk |
| the header as write buffer | LSM trees' memtables | hot entries buffered, cold ones evicted to runs | one tier of unordered leaves, no levels |
| commit and recovery | shadow paging; LMDB; WAFL and ZFS | a root written last after an `fsync`; a log replayed over it | no tree on disk; one `fsync` per commit |
| reclamation | Sprite LFS's cleaner; SSD flash translation layers | victims by utilization and age | pages instead of segments |
| state and durability | system prevalence; Redis | all state in memory, every change logged | byte records instead of commands; incremental checkpoints |
| moving values | Cap'n Proto's orphans | detach, adopt elsewhere, no copy | journaled, within a transaction |
| positions in memory | ropes and piece trees | a counted tree over pieces | — |
| order by links | Yjs | a linked list of items with ids, merged into runs | one writer: no merging, tombstones or history |

## The encoding: Avro's, nearly

**[Avro](https://avro.apache.org/docs/1.11.1/specification/)'s binary encoding is the closest precedent of the sequence's encoding, and its object container files carry their schema as kladde files do.**
An Avro record is its fields' encodings concatenated, with no tags and no padding; integers are zigzag varints, a union starts with its branch's index as a varint, and an array is its elements in blocks, each preceded by its count.
kladde's [evolution](../spec/schema/evolution.md#prior-art) takes Avro's model of reconciling a writer's schema with a reader's as well.
The differences follow from mutation: Avro data is written once and read in sequence, while kladde edits values in place, so it keeps counts in one place rather than in blocks, ends text in a terminator so that an edit of text needs one splice, and offers `Fixed` for values that change often.
*Protocol Buffers* share the varints and drop padding too, but tag every field with its number, which makes evolution possible without a stored schema at the price of bytes per field.

## The sequence on file: a piece table, as Word's fast save kept one

**A piece table on disk is old: Microsoft Word's binary `.doc` format kept one for its *fast save*, which appended the changed text to the document's stream and wrote a new piece table over it.**
Bravo, at Xerox PARC in the 1970s, introduced piece tables for editing, and Visual Studio Code's piece tree (2018) keeps one in a balanced tree in memory, as the rope does here.
Word's table differs from the span table in three ways that matter.
It keys each piece by its character position, so it is rewritten whole at every fast save, where links let kladde state only the spans at an edit.
It reclaims nothing until a full save rewrites the document, so deleted text stayed in fast-saved files, which leaked it to whoever received them.
And it gave no crash safety of its own.

**The span table's successors are *linked allocation*, as FAT keeps it, with byte-granular spans instead of clusters.**
FAT file systems, and Microsoft's compound files, keep each file or stream as a chain of fixed-size clusters, whose successors a table holds.
A chain of fixed-size clusters can take an insertion only at a cluster's boundary; a span can be split at any byte.
And linked allocation's classic weakness, reaching a byte by walking the chain to it, does not arise, since kladde walks each chain once, at open, and keeps a rope.
The other classic scheme is *extents* keyed by their offset in the file, as ext4's and XFS's extent trees keep them; inserting a range there, as Linux's `FALLOC_FL_INSERT_RANGE` does, shifts every extent behind it — the cost that keying spans by id avoids, and that kladde's [previous design](../superseded/allocations.md) paid per allocation.
The name *extent* would mislead for that reason, and *span* is used instead; the fold's transient runs keep the literature's name, *pieces*, and a span is a piece made persistent: it has an id, links to its successor, and outlives the flush.

## Storage: a log-structured file system for one file

**The pages, the span table, the cleaner and the two header slots are those of a log-structured file system, with pages for segments and one file in it.**
Sprite LFS (Rosenblum and Ousterhout, "The Design and Implementation of a Log-Structured File System", SOSP 1991, and TOCS 1992) writes everything to fresh segments, finds each file's inode through an *inode map* that is itself written to the log and held in memory, tracks each segment's live bytes in a *segment usage table*, cleans by moving live blocks out of segments chosen by utilization and age, and alternates between two checkpoint regions.
The span table is that inode map, coverage the segment usage table, cost-benefit scoring its cleaning policy, and the header slots the checkpoint regions.
Its cleaning unit is a large fixed segment; kladde's is a page, and its spans can be re-cut freely because the directory keeps no order.
*Bitcask* (Basho, 2010) holds every key in memory, rebuilt at open from its log, lets the newest write of a key win, and merges files by writing their live values again: the span table, read at open, is a Bitcask keyed by span id.
*LSM trees* (LevelDB, RocksDB) resolve a key's versions across runs by recency, as entries are resolved across epochs, and the [header as write buffer](flush.md#the-header-as-write-buffer) is a memtable whose eviction is an L0 flush; but LSMs keep runs sorted and levelled so point lookups work without full residency, while kladde is fully resident after the bulk read and needs no sort order, no levels and no bloom filters.

**The commit is shadow paging with an intent log.**
System R's shadow pages (Lorie, "Physical Integrity in a Large Segmented Database", TODS 1977), LMDB's two meta pages, WAFL's consistency points with their log of requests in NVRAM (Hitz, Lau and Malcolm, USENIX Winter 1994), and ZFS's transaction groups with their uberblocks and intent log all write a new root last, after an `fsync`, over pages that no older root reaches, and replay a log over the last root after a crash.
kladde's flush is such a consistency point, its journal such an intent log, and its two header slots LMDB's meta pages; it has no tree on disk to copy paths in, since nothing is searched on disk.
LMDB's B-tree is sorted because readers descend it in the mapped file; kladde never descends, so it trades away the ordering along with its ~25 % slack and its rebalancing writes.
LMDB also needs two `fsync`s per commit where kladde needs [one](../spec/durability.md#why-one-fsync-suffices), because kladde can let the header lag one write behind and lean on journal replay.
btrfs's *inline extents* and ZFS's *bonus buffers* store small data directly in metadata — the precedent for [`Inline`](../spec/span-table.md#entries) entries.

## State and durability: a memory image with incremental checkpoints

**Keeping all state in memory, logging every change before it returns, and folding the log into a checkpoint is *system prevalence*, as Prevayler implements it and Martin Fowler describes it as a *memory image*; Redis does the same with its append-only file and its snapshots.**
kladde differs in two ways.
Its log holds byte effects rather than commands, so replaying it needs no application code and no schema; and its checkpoints are incremental, since a flush writes what changed, where those systems write whole snapshots or rewrite their whole log.

## Moving values: Cap'n Proto's orphans

**[Cut and paste](values.md#moving-a-value-between-containers) is Cap'n Proto's *orphans*: an object disowned by its parent and adopted by another in the same message, without copying, move-only, and zeroed if nothing adopts it.**
Here the cut lives in the journal's transaction rather than in a message's arena, and the flush turns it into a few links.

## Order by links: sequence CRDTs

**[Yjs](https://github.com/yjs/yjs) keeps a shared text as a linked list of items, each with an id and links to its neighbours, merges consecutive items of one writer into runs, and persists updates that it later merges into one, which is close to linked spans with ids.**
*Automerge* keeps the history of operations, in a compressed, columnar format.
Both merge concurrent edits from many writers, and so keep tombstones and history and compact by encoding the whole document again; a single writer needs none of that.

## Where the redesigns have led

**Each redesign moved kladde further from object stores and closer to document storage.**

1. **The first design** was a [relocatable heap](../superseded/relocatable-heap.md): allocations contiguous in the file, pointers between them, and compaction that moved whole allocations — a persistent heap, as PMDK's `libpmemobj` or Boost.Interprocess's mapped files keep one, and an object store, as ZODB's `FileStorage` or Realm are.
2. **The second** kept [allocations and pointers](../superseded/allocations.md), but stored them as a log-structured file system stores files: pages written copy-on-write, an address table of extents keyed by offset, an intent log, and a cleaner.
3. **The present design** keeps the file system and drops the objects: one file whose extents are linked, a piece table, holding a serialization as Avro would write it.

So kladde has converged on known parts, put together in a way not found elsewhere: a typed document held whole in memory, durable at every change through a byte-level journal, and saved as deltas into a linked piece table on copy-on-write pages.
The nearest whole is Word's fast save, without its rewrites of the piece table, its leaked deletions, and its lack of crash safety.

One lesson taken from the same literature rather than from a system: PostgreSQL's transaction-id wraparound "freezing" is why [epochs are 64-bit](../spec/file-format.md#epochs).
