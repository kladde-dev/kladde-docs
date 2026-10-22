---
title: Related work
---

What applications use in kladde's place, where kladde's design comes from, and where it deliberately parts company.

## Systems that address related problems

This section discusses systems that are related to kladde *from the perspective of a user*, addressing questions like "how is kladde different from SQLite, or from whole-file serialization"?

**Applications have kept their documents safe from crashes in six ways, and each of the five in use today gives up at least one of three things that kladde combines: one model, the application's own values in memory, which are what the file holds; every change durable against an application crash when its call returns; and writes in proportion to the change, rather than to the document or to the value the change lands in.**
Saving the whole document gives up the second and the third.
A journal beside a checkpoint of the whole state keeps both between checkpoints, and gives up the third at each checkpoint.
An embedded database such as SQLite gives up the first, and the third below a page, or below a whole value whose size changes.
An object store keeps the first, but writes only at a commit that the application issues, and often each changed object whole.
A CRDT library keeps the third, but replaces the application's types with a JSON-like model and a history meant for merging.
Durable containers in persistent memory kept all three, and needed hardware that is no longer made.
Each approach is still the better choice where an application needs what it offers instead — a format that other programs read, queries, data larger than memory, several processes on one file, or collaboration — none of which kladde offers.

| approach | in industry | gives up | does better than kladde |
| --- | --- | --- | --- |
| [saving the whole document](#saving-the-whole-document) | most document applications; macOS's autosave; Word's AutoRecover; Android's DataStore | each change's durability; writes in proportion to the change | a format other programs read; any type; the simplest code |
| [a journal beside a checkpoint](#a-journal-beside-a-checkpoint-of-the-whole-state) | Figma's servers; Chromium's sessions; Prevayler; LMAX; Tencent's MMKV | writes in proportion to the change, at each checkpoint | a log of the application's own commands |
| [an embedded database](#embedded-databases-sqlite-and-its-kin) | SQLite, alone or under Core Data, SwiftData or Room; LMDB, RocksDB | one model; writes below a page, or below a value whose size changes | partial loading, queries, several processes, maturity |
| [an object store](#object-stores) | ZODB; EclipseStore; Realm | durability without a commit; often writes below an object | partial loading, data larger than memory |
| [a CRDT library](#collaborative-documents-crdts) | Automerge; Yjs | the application's types; a file without history | collaboration and sync |
| [durable containers](#durable-containers-in-persistent-memory) | Intel's PMDK; Corundum | ordinary hardware | no file to load at open |

### Saving the whole document

**Most applications still write their whole model at each save, made safe by writing a new file and moving it into place; a crash loses everything since the last save, and each save costs time and writes in proportion to the document.**
macOS's `NSDocument` can [autosave in place](https://developer.apple.com/library/archive/documentation/DataManagement/Conceptual/DocBasedAppProgrammingGuideForOSX/StandardBehaviors/StandardBehaviors.html) since 10.7 (2011), by writing a new file and moving it over the document; Apple's guide notes that "the system does not attempt to save every change immediately", and warns that a slow save "could cause periodic blocking of your user interface".
Microsoft Word keeps an AutoRecover copy of each open document, written every ten minutes by default and removed when the user saves.
Android's [Jetpack DataStore](https://developer.android.com/topic/libraries/architecture/datastore) persists one typed object, in Protocol Buffers or JSON, by an atomic read-modify-write of the whole, and calls itself "ideal for small datasets"; it "does not support partial updates", and for anything larger it points to Room, an object mapping over SQLite.
Firefox writes its whole session every 15 seconds, which users [measured](https://www.servethehome.com/firefox-is-eating-your-ssd-here-is-how-to-fix-it/) in 2016 at 10 GB a day and more.
On the drawings and texts that kladde is measured on, saving the SVG every 100 edits would write 590 to 10,699 bytes per edit, and saving the text every 1000 keystrokes 241 and 700 bytes per keystroke, where kladde writes 112 to 389 and 53 to 58 ([measured](evaluation/one-sequence.md#writes)).

What a whole-document save keeps in exchange is real: the file is in a format that other programs read, whatever the serializer handles can be saved, and no type has to change.

### A journal beside a checkpoint of the whole state

**Systems that cannot afford to lose a minute of work hold their state in memory, append each change to a journal, and now and then write a checkpoint of the whole state that replaces the journal: kladde's own shape, built anew for each application, and with checkpoints that cost as much as the state.**
[Figma's multiplayer servers](https://www.figma.com/blog/making-multiplayer-more-reliable/) hold each open file in memory and checkpoint the whole file every 30 to 60 seconds; since 2022 they also append the changes to a journal about every half second, and recover by replaying it over the newest checkpoint, so that "95% of edits to a Figma file get saved within 600ms".
Chromium's session service appended commands to its session file and [recreated the file every 250 commands](https://chromium.googlesource.com/git/chromium/+/trunk/chrome/browser/sessions/session_service.cc).
[Prevayler](https://prevayler.org/apidocs/2.6/org/prevayler/PrevaylerFactory.html) (2001) does this for any Java object graph: every change is a `Transaction` object, journaled and synced before it runs, and deterministic, so that replaying it reproduces it — it may not even read the clock.
The LMAX exchange runs its business logic in memory on the same principle, journaling its input events and recovering from snapshots ([Fowler, 2011](https://www.martinfowler.com/articles/lmax.html)).
Tencent's [MMKV](https://github.com/Tencent/MMKV/wiki/design), the key-value store of WeChat, holds its map in memory and appends each write, encoded as a protobuf, to a memory-mapped file, so that the write survives an application crash at once; when the file is full, MMKV rewrites it with only the newest value of each key, and doubles it if that is not enough.

**Their journals hold what only the application can replay, and their checkpoints write everything.**
Figma's changes, Chromium's commands and Prevayler's transactions mean something only to the application's code, which recovery has to run, and run deterministically.
kladde's journal holds byte effects on the encoding of typed values, which [recovery](spec/durability.md) replays without the application or its schema.
A checkpoint of the whole state takes as long as the state, however little changed since the last one, where kladde's flush writes what changed.
And MMKV promises nothing under a power cut.

### Embedded databases: SQLite and its kin

**SQLite is the strongest alternative, and [argues it for itself](https://www.sqlite.org/appfileformat.html): an application file in SQLite is written only where it changed, as the changes occur, and safely — but the application has to map its model to tables and keep the two in step.**
SQLite's page on application file formats states that "only those parts of the file that actually change are written out to disk", that "changes can be written back to the disk as they occur" instead of at *File/Save*, and that an [undo stack kept by triggers](https://www.sqlite.org/undoredo.html) in the file lets "undo/redo … occur across session boundaries".
In WAL mode with [`synchronous=NORMAL`](https://www.sqlite.org/pragma.html#pragma_synchronous), it offers the same two tiers as kladde's journal and flush: "Transactions are durable across application crashes", while a power cut may roll back the transactions since the last checkpoint but cannot corrupt the file.
Its [session extension](https://www.sqlite.org/sessionintro.html) records what transactions changed as a *changeset*, which can be inverted to undo them, concatenated with others, and applied to another copy of the file.
It reads only what a query asks for, so a document need not fit in memory, and "the application can do queries to extract only the information needed for the initial screen"; several processes can read and write one file; its format has stayed compatible since 2004; the US Library of Congress recommends it for preservation; and it is "robust against maliciously malformed database files".

**SQLite writes in units of pages, and within a row in units of whole values whose size changes.**
Each commit in WAL mode appends every page it changed to the log, at least one page of 4 KiB by default, where a kladde transaction for an edit of a drawing appends 31 to 43 bytes to the journal ([measured](evaluation/one-sequence.md#writes)).
An `UPDATE` overwrites a row in place only if its size [stays the same](https://www.sqlite.org/src/info/5887d8beb502ad62) (since 2018), and otherwise writes the row again, overflow pages included; [incremental BLOB I/O](https://www.sqlite.org/c3ref/blob_write.html) overwrites bytes in place, but "it is not possible to increase the size of a BLOB".
So an application that keeps a long text in one row writes all of it at each typed character, and avoids that only by splitting the text into rows of paragraphs or lines, which it then has to keep in order itself.
[Apple Notes](https://simonwillison.net/2021/Dec/9/notes-on-notesapp/) keeps each note's body as a gzipped protobuf in one column of its SQLite file: SQLite holds the notes, and each note is still written whole.

**The mapping is the cost that kladde avoids.**
In tables, a tree becomes rows with parent keys, ordered children need a position column that an insertion renumbers, and variants become separate tables or nullable columns.
An application that shows its whole document keeps a model in memory anyway, and has to issue, for each change to it, the SQL that mirrors the change; a statement it forgets lets the file drift from the model unnoticed.
Object mappers take part of this on, at the price of a model that fits tables: Apple's Core Data, and [SwiftData](https://developer.apple.com/documentation/swiftdata/modelcontext/autosaveenabled), which saves a context's changes at `save()` or at an autosave whose moment Apple leaves unspecified; and Android's Room.
Embedded key-value stores such as LMDB, RocksDB and LevelDB sit one level lower: atomic transactions over byte strings, with serialization and the design of keys left to the application.

### Object stores

**Object stores persist the application's objects themselves, as kladde does, but write a changed object only at a commit that the application issues, often whole, and load objects lazily, so that a document need not fit in memory.**
[ZODB](https://zodb.org/en/latest/guide/writing-persistent-objects.html), the object database under Zope and Plone, notices assignments to attributes of `Persistent` objects, but not changes inside a plain `list` or `dict`, for which it offers `PersistentList`, `PersistentMapping` and B-trees: kladde's need for containers of its own, met the same way.
Each persistent object is "stored in its own database record", written whole at `transaction.commit()`, and loaded when first accessed.
[EclipseStore](https://docs.eclipsestore.io/manual/storage/storing-data/lazy-eager-full.html), formerly MicroStream, persists plain Java object graphs: the application calls `store` on each object it modified, and since storing skips objects already stored, a field changed in place goes unwritten unless its own object is stored too.
Its storage is log-structured like kladde's: each store is [appended](https://docs.eclipsestore.io/manual/storage/housekeeping.html) to the newest file, a background housekeeping rewrites files whose live share falls below 75 %, and a garbage collection removes entities that the root no longer reaches.
[Realm](https://academy.realm.io/posts/threading-deep-dive), for mobile applications in Swift, Kotlin, .NET, JavaScript and Dart, reads objects through accessors into its memory-mapped file and writes them in write transactions, one at a time per file; MongoDB [deprecated](https://www.mongodb.com/community/forums/t/atlas-device-sync-end-of-life-and-deprecation/296687) Realm's SDKs and its sync service in September 2024, and the on-device database lives on as open source.
Persistence goes furthest in [MUMPS](https://en.wikipedia.org/wiki/MUMPS), the language of Epic's health records: any variable whose name begins with `^` is a *global*, a sparse tree of strings and numbers that lives on disk and is shared by every process.

What sets kladde apart from these stores is that a mutation needs no commit to be durable, and that a change inside a long string or list writes the bytes that changed rather than the object that holds them.
What sets them apart from kladde is that they read only what is accessed.

### Collaborative documents: CRDTs

**CRDT libraries record each change as it happens and save incrementally, as kladde does, for a model built for many writers: a JSON-like document that keeps the history it needs to merge, stored wherever and whenever the application chooses.**
[Automerge](https://automerge.org/docs/reference/under-the-hood/storage/) changes a document in a callback; `saveIncremental` returns the changes since its last call, to be appended to a file, and `save` writes the whole document compactly; automerge-repo writes each change to its storage as it happens, and now and then compacts the changes into a snapshot.
Yjs works the same way, and Apple Notes keeps a CRDT inside each note to merge edits from several devices.
[MobX-State-Tree](https://mobx-state-tree.js.org/concepts/patches), a state container for JavaScript applications, intercepts mutations as kladde does, but with no file behind them: it emits a JSON patch for each mutation and a snapshot at the end of each action, and leaves storing either to the application.
An application that needs collaboration or sync is better served by a CRDT, since kladde merges nothing; one that does not would pay for a history it never uses, and give up typed values.

### Durable containers in persistent memory

**The API closest to kladde's was built for byte-addressable persistent memory — containers whose updates inside a transaction are durable, with no file format between them and the program — and it went with the hardware.**
Intel's PMDK offered, in [`libpmemobj-cpp`](https://github.com/pmem/libpmemobj-cpp), a persistent `vector`, `string` and `concurrent_hash_map`, updated in transactions on Optane memory.
[Corundum](https://github.com/nvsl/corundum) (ASPLOS 2021) brought the model to Rust, and used its type system to rule out, at compile time, pointers from persistent into volatile memory and updates that bypass the log, the second of which kladde-rs's `Mut` handles prevent as well.
Intel [wound down](https://pmem.io/blog/2022/11/update-on-pmdk-and-our-long-term-support-strategy/) PMDK along with Optane in 2022, and archived `libpmemobj-cpp` in March 2023.
kladde offers the same programming model on ordinary files, where durability comes from `fsync` and copy-on-write rather than from flushing cache lines.

### What they offer that kladde does not

**Five features of these systems have no counterpart in kladde yet.**

- **Holding back what others see until it is durable.**
  Cloudflare's Durable Objects let a write return at once, into an in-memory cache, and [hold back every outgoing message](https://blog.cloudflare.com/durable-objects-easy-fast-correct-choose-three/) until the write is durable; should the write fail, the messages turn into errors and the object restarts.
  A kladde mutation, too, returns before it is safe from a power cut, and an application that tells someone else about a change — a server, another process, the user through a "saved" indicator — has to remember to call `flush()` first.
- **Changesets.**
  SQLite's session extension turns transactions into changesets that can be inverted, concatenated and applied to another copy, which is what undo, redo and synchronization between copies are built from; kladde's journal holds the same information, at the level of bytes, until the next flush folds it.
- **Partial loading.**
  SQLite, ZODB, EclipseStore and Realm read what is accessed; kladde reads the whole file at open, and [segments](spec/sequence.md#partial-loading-later) are planned for this.
- **Readers in other processes.**
  SQLite, Realm and MMKV let other processes read a file while one writes; kladde's [scope](spec/file-format.md#concurrency) excludes them.
- **A stance on hostile files.**
  SQLite promises that a malformed file cannot provoke a memory error in its reader; kladde's checksums and [fingerprints](spec/schema/fingerprints.md) guard against accident, not against an adversary, and its specification says nothing about files built to attack a reader — though a document file is exactly what gets sent by email.

## Systems with related internal parts

This section discusses systems that overlap with kladde in terms of specific internal parts — often, these are systems from which kladde borrows ideas to solve some common subproblem in a different context.

**Measured against what applications use in its place, kladde combines three things, and every approach in use today gives up at least one of them: one model, the application's own values in memory, which are what the file holds; every change durable against an application crash when its call returns; and writes in proportion to the change.**
[What applications use instead](#systems-that-address-related-problems) compares kladde with whole-document saves, journals beside checkpoints, embedded databases such as SQLite, object stores, CRDT libraries and durable containers in persistent memory; the sections before it compare kladde's parts with the systems they come from.

**No system known to these documents combines what kladde does, but each of its layers has a close precedent**: kladde is an editor's piece table kept on disk, linked rather than keyed by position, made crash-safe by shadow paging with an intent log, kept compact by a log-structured cleaner, and holding a serialization much like Avro's.
None of the load-bearing pieces is novel, which is the point: their failure modes are understood and bounded, which is exactly what the [bespoke machinery they replaced](superseded/in-place-flush.md) could not claim.

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

### The encoding: Avro's, nearly

**[Avro](https://avro.apache.org/docs/1.11.1/specification/)'s binary encoding is the closest precedent of the sequence's encoding, and its object container files carry their schema as kladde files do.**
An Avro record is its fields' encodings concatenated, with no tags and no padding; integers are zigzag varints, a union starts with its branch's index as a varint, and an array is its elements in blocks, each preceded by its count.
kladde's [evolution](spec/schema/evolution.md#prior-art) takes Avro's model of reconciling a writer's schema with a reader's as well.
The differences follow from mutation: Avro data is written once and read in sequence, while kladde edits values in place, so it keeps counts in one place rather than in blocks, ends text in a terminator so that an edit of text needs one splice, and offers `Fixed` for values that change often.
*Protocol Buffers* share the varints and drop padding too, but tag every field with its number, which makes evolution possible without a stored schema at the price of bytes per field.

### The sequence on file: a piece table, as Word's fast save kept one

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
The other classic scheme is *extents* keyed by their offset in the file, as ext4's and XFS's extent trees keep them; inserting a range there, as Linux's `FALLOC_FL_INSERT_RANGE` does, shifts every extent behind it — the cost that keying spans by id avoids, and that kladde's [previous design](superseded/allocations.md) paid per allocation.
The name *extent* would mislead for that reason, and *span* is used instead; the fold's transient runs keep the literature's name, *pieces*, and a span is a piece made persistent: it has an id, links to its successor, and outlives the flush.

### Storage: a log-structured file system for one file

**The pages, the span table, the cleaner and the two header slots are those of a log-structured file system, with pages for segments and one file in it.**
Sprite LFS (Rosenblum and Ousterhout, "The Design and Implementation of a Log-Structured File System", SOSP 1991, and TOCS 1992) writes everything to fresh segments, finds each file's inode through an *inode map* that is itself written to the log and held in memory, tracks each segment's live bytes in a *segment usage table*, cleans by moving live blocks out of segments chosen by utilization and age, and alternates between two checkpoint regions.
The span table is that inode map, coverage the segment usage table, cost-benefit scoring its cleaning policy, and the header slots the checkpoint regions.
Its cleaning unit is a large fixed segment; kladde's is a page, and its spans can be re-cut freely because the directory keeps no order.
*Bitcask* (Basho, 2010) holds every key in memory, rebuilt at open from its log, lets the newest write of a key win, and merges files by writing their live values again: the span table, read at open, is a Bitcask keyed by span id.
*LSM trees* (LevelDB, RocksDB) resolve a key's versions across runs by recency, as entries are resolved across epochs, and the [header as write buffer](impl/flush.md#the-header-as-write-buffer) is a memtable whose eviction is an L0 flush; but LSMs keep runs sorted and levelled so point lookups work without full residency, while kladde is fully resident after the bulk read and needs no sort order, no levels and no bloom filters.

**The commit is shadow paging with an intent log.**
System R's shadow pages (Lorie, "Physical Integrity in a Large Segmented Database", TODS 1977), LMDB's two meta pages, WAFL's consistency points with their log of requests in NVRAM (Hitz, Lau and Malcolm, USENIX Winter 1994), and ZFS's transaction groups with their uberblocks and intent log all write a new root last, after an `fsync`, over pages that no older root reaches, and replay a log over the last root after a crash.
kladde's flush is such a consistency point, its journal such an intent log, and its two header slots LMDB's meta pages; it has no tree on disk to copy paths in, since nothing is searched on disk.
LMDB's B-tree is sorted because readers descend it in the mapped file; kladde never descends, so it trades away the ordering along with its ~25 % slack and its rebalancing writes.
LMDB also needs two `fsync`s per commit where kladde needs [one](spec/durability.md#why-one-fsync-suffices), because kladde can let the header lag one write behind and lean on journal replay.
btrfs's *inline extents* and ZFS's *bonus buffers* store small data directly in metadata — the precedent for [`Inline`](spec/span-table.md#entries) entries.

### State and durability: a memory image with incremental checkpoints

**Keeping all state in memory, logging every change before it returns, and folding the log into a checkpoint is *system prevalence*, as Prevayler implements it and Martin Fowler describes it as a *memory image*; Redis does the same with its append-only file and its snapshots.**
kladde differs in two ways.
Its log holds byte effects rather than commands, so replaying it needs no application code and no schema; and its checkpoints are incremental, since a flush writes what changed, where those systems write whole snapshots or rewrite their whole log.

### Moving values: Cap'n Proto's orphans

**[Cut and paste](impl/values.md#moving-a-value-between-containers) is Cap'n Proto's *orphans*: an object disowned by its parent and adopted by another in the same message, without copying, move-only, and zeroed if nothing adopts it.**
Here the cut lives in the journal's transaction rather than in a message's arena, and the flush turns it into a few links.

### Order by links: sequence CRDTs

**[Yjs](https://github.com/yjs/yjs) keeps a shared text as a linked list of items, each with an id and links to its neighbours, merges consecutive items of one writer into runs, and persists updates that it later merges into one, which is close to linked spans with ids.**
*Automerge* keeps the history of operations, in a compressed, columnar format.
Both merge concurrent edits from many writers, and so keep tombstones and history and compact by encoding the whole document again; a single writer needs none of that.

### Where the redesigns have led

**Each redesign moved kladde further from object stores and closer to document storage.**

1. **The first design** was a [relocatable heap](superseded/relocatable-heap.md): allocations contiguous in the file, pointers between them, and compaction that moved whole allocations — a persistent heap, as PMDK's `libpmemobj` or Boost.Interprocess's mapped files keep one, and an object store, as ZODB's `FileStorage` or Realm are.
2. **The second** kept [allocations and pointers](superseded/allocations.md), but stored them as a log-structured file system stores files: pages written copy-on-write, an address table of extents keyed by offset, an intent log, and a cleaner.
3. **The present design** keeps the file system and drops the objects: one file whose extents are linked, a piece table, holding a serialization as Avro would write it.

So kladde has converged on known parts, put together in a way not found elsewhere: a typed document held whole in memory, durable at every change through a byte-level journal, and saved as deltas into a linked piece table on copy-on-write pages.
The nearest whole is Word's fast save, without its rewrites of the piece table, its leaked deletions, and its lack of crash safety.

One lesson taken from the same literature rather than from a system: PostgreSQL's transaction-id wraparound "freezing" is why [epochs are 64-bit](spec/file-format.md#epochs).
