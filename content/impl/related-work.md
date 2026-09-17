---
title: Related work
---

Where kladde's storage design comes from, and where it deliberately parts company.

None of the load-bearing pieces is novel, which is the point: never-overwrite plus cleaning is Rosenblum's log-structured file system, the alternating header commit is LMDB's meta pages, copy-on-write with checksums is ZFS, WAFL and btrfs, and extents-with-escalation is ext4, XFS and NTFS.
Their failure modes are understood and bounded, which is exactly what the [bespoke machinery they replaced](../superseded/in-place-flush.md) could not claim.

What *is* different from all of them is the premise: **kladde reads in bulk and looks nothing up on disk.**
Almost every divergence below is a consequence of that one fact, and every one of them cuts toward simplicity.

| system | shared ideas | where kladde diverges, and why |
| --- | --- | --- |
| **LSM trees** (LevelDB, RocksDB) | The [header-as-write-buffer](flush.md#the-header-as-write-buffer) is a memtable; eviction is an L0 flush; ratio-driven [consolidation](consolidation.md) is compaction; cold/hot separation at eviction. | LSMs keep runs sorted and levelled so point lookups work without full residency; kladde is fully resident after the bulk read, so it needs no sort order, no levels, no bloom filters — one tier of dense, unordered leaves. |
| **LFS and SSD FTLs** | Never overwrite; live-fraction victim selection; cost-benefit (sparse *and* old) cleaning. | Their cleaning unit is a large fixed segment; kladde's is a page, and its leaves can be re-cut freely because the directory keeps no order. |
| **LMDB** | Copy-on-write pages, alternating root, page-reuse quarantine by transaction age. | LMDB's B-tree is sorted because readers descend it in the mapped file; kladde never descends, so it trades away the ordering along with its ~25 % slack and its rebalancing writes. It also needs two `fsync`s per commit where kladde needs [one](../spec/durability.md#why-one-fsync-suffices), because kladde can let the header lag one write behind and lean on journal replay. |
| **btrfs, ZFS** | Copy-on-write everything, checksums everywhere; btrfs *inline extents* and ZFS *bonus buffers* store small data directly in metadata — the precedent for [`Inline`](../spec/address-table.md#statement-types) as a first-class citizen. | Both maintain on-disk search structures for partial access to data far larger than memory; kladde's bulk-read premise removes the requirement those structures exist to serve. |
| **ext4, XFS** | Extent-shaped `Ref`s; small-file inlining (`inline_data`); description debt fought by defragmentation. | In-place journaling and fixed metadata locations rather than copy-on-write shadowing. |
| **MVCC stores** (PostgreSQL) | Epoch shadowing is tuple versioning; [tombstone lifetime](liveness.md#tombstones) is dead-tuple visibility; consolidation is `VACUUM`. | MVCC retains versions for concurrent readers; kladde retains them only across the commit boundary, so its vacuum needs no visibility horizon beyond the two-header rule. |

One lesson taken from the same literature rather than from a system: PostgreSQL's transaction-id wraparound "freezing" is why [epochs are 64-bit](../spec/file-format.md#epochs).
A 32-bit counter would require actively rewriting laggard pages before the counter caught up with them, which is an operational burden PostgreSQL carries only because its format predates the lesson.
