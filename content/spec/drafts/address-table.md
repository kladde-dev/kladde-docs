# Address Table

## On-disk representation

### Logical level

On disk, the address table is a list of *statements*, written in one or more pages, where each statement implicitly inherits its page's `epoch`.
Statements from all live pages are merged, resolving conflicts by recency, see [[#conflict resolution across epochs]].

#### Statement types

| Statement                           | Existence of `id` | Size of `id`                                                     | Content of `id`                                                                    | Usage note                                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------- | ----------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Ref(id, offset, size, address)`    | exists            | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` equals `file[address, address + size)`. | `[address, address + size)` must be within a single `Data` page. As a *writer invariant* (readers never depend on it), every byte in a `Data` page is referenced by at most one live `Ref`; this is not needed for correctness but keeps data-page coverage a plain counter instead of a refcount. Relaxing it later would enable `O(1)` allocation clones and is a format-compatible policy change, see [[#Design directions and trade-offs]].                                                                                                                                                                  |
| `Undefined(id, offset, size)`       | exists            | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` is `Undefined`.                         | Kladde is allowed to hand out arbitrary data for `Undefined` regions, and consolidation may rewrite `Undefined` regions with `Ref` or `Inline` with arbitrary data (useful for data types that have long sequences of repeated small `[data] [undefined] [data] [undefined] ...` patches, which could happen for arrays of enums with slightly unbalanced payload sizes per variant). |
| `Size(id, size)`                    | exists            | `size`                                                           | Anything beyond `size` is `Undefined`.                                             | Only has an effect if an older live page states a different size or some non-`Undefined` content beyond `size` that is not overwritten by a newer live statement.                                                                                                                                                                                                                     |
| `Tombstone(id)`                     | does not exist    | `0`                                                              | All bytes are `Undefined`.                                                         | Only has an effect if an older live page states existence of `id` and no newer live page states existence of `id` and fully overwrites any size and content remaining from the old incarnation.                                                                                                                                                                                       |
| `Inline(id, offset, size, payload)` | exists            | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` equals `payload`.                       | Small-size optimization. Requires `1 <= size  <= 252`.                                                                                                                                                                                                                                                                                                                                |

#### No conflicts within each epoch

Statements in the same `epoch` must not conflict with each other:

- no two statements in the same `epoch` may make contradicting statements about the existence of an `id`;
- no two statements in the same `epoch` may name any byte of content twice, not even if the two statements assign the same value to that byte.
  This includes `Undefined`: an assignment of `Undefined` to a given byte — whether by an `Undefined`, `Size`, or `Tombstone` statement — conflicts with any other assignment to the same byte, including a second `Undefined` assignment.
- At most one `Size` statement per `id` is allowed per epoch.
- Multiple `Ref`, `Undefined`, and `Inline` statements for pairwise disjoint ranges within the same allocation are allowed.
  They don't conflict in regards of the size of the allocation because they all merely bound the size of the allocation from below.
- However, a `Ref`, `Undefined`, or `Inline` statement conflicts with a `Size(size)` statement in the same `epoch` if its lower bound `offset + size` is larger than `size`, and this is not allowed in a single `epoch`.

#### Conflict resolution across epochs

Two statements with different `epoch` are allowed to contradict each other.
In this case, the newer statement (the one with the higher `epoch`) takes precedence, but only as far as it contradicts the older statement.
Any part of the older statement that doesn't contradict the newer statement survives, and this is resolved at the granularity of the allocations existence, its size, and the value of each of its content bytes:

- **Existence:** allocation `id` exists if the statement with highest `epoch` that mentions `id` is `Ref`, `Undefined`, `Size`, or `Inline`.
  It does not exist if the statement with highest `epoch` that mentions `id` is `Tombstone(id)` or if no statement mentions `id`.
- **Size:** `0` if the allocation does not exist (note: this is only to simplify the rules; we still distinguish *existent* zero-sized allocations from *non-existent* allocations).
  Otherwise, let `tombstone_epoch` be the largest `epoch` of any `Tombstone(id)` statement (or `tombstone_epoch = -1` if no `Tombstone(id)` exists), and let `size_epoch` be the largest `epoch > tombstone_epoch` of any `Size(id, ...)` statement (or `size_epoch = tombstone_epoch` if no `Size(id, ...)` statement exists).
  The size of allocation `id` is then the maximum over:
	- the `size` field of the `Size(id, size)` statement at `size_epoch`, if this statement exists; and
	- all values `offset + size` of all `Ref(id, offset, size, ...)`, `Undefined(id, offset, size)` and `Inline(id, offset, size, ...)` statements with `epoch > size_epoch`.
  At least one matching `Size`, `Ref`, `Undefined`, or `Inline` statement must exist because otherwise the allocation does not exist.
- **Content at a given offset `probe`:** defined by the latest statement that sets the value of this content, i.e., the statement with highest `epoch` that matches any of the following patterns:
	- `Tombstone(id)`;
	- `Size(id, size)` where `size <= probe`; or
	- `Ref(id, offset, size, ...)`, `Undefined(id, offset, size)`, or `Inline(id, offset, size, ...)` where `offset <= probe < offset + size`.
  There must be at most one winner because at most one statement matching the above criteria is allowed per `epoch` (see [[#no conflicts within each epoch]]).
  Given the winning statement, the content of allocation `id` at offset `probe` is defined as follows:
	- If there is no matching statement or if the winning statement is of type `Tombstone`, `Size`, or `Undefined`: the content of allocation `id` at offset `probe` is `Undefined`.
	- if the winning statement is `Inline(id, offset, size, payload)`: the content of allocation `id` at offset `probe` is `payload[probe - offset]`.
	- If the winning statement is `Ref(id, offset, size, address)`: the content of allocation `id` at offset `probe` is `file[address + (probe - offset)]`.

### Physical level

There is only a single `kind` for address table pages: `kind == AddressTable`.
The header page is always an `AddressTable` page.
Every `AddressTable` page contains zero or more references to other `AddressTable` pages followed by zero or more statements.
References between `AddressTable` pages form a tree rooted at the header page.

```
address_table_page := num_children:varint child_ref{num_children} num_statements:varint statements:statement{num_statements}
child_ref          := page_number:varint  ; the page number (not the start address of the page)
statement          := id_delta (tagged_ref | tagged_undefined | tagged_size | tagged_tombstone | inline)
id_delta           := varint              ; increment from id of last statement (for first statement in page: id)
tagged_ref         := 0:byte offset:varint size:varint address:varint
tagged_undefined   := 1:byte offset:varint size:varint
tagged_size        := 2:byte size:varint
tagged_tombstone   := 3:byte              ; tombstones have no payload apart from the id

; inline is optimized for lots of small payloads. size_tag = size + 3 >= 4 can't clash.
inline             := size_tag:byte offset:varint payload:byte{size_tag - 3}
```

Notes on the physical format:

- Statements within a page are sorted by `(id, offset)`, so `id_delta` is never negative; a further statement about the same id has `id_delta == 0`.
- The root `AddressTable` page *is* the current header slot (pages 0/1, alternating per the CoW design), so before the payload described by this grammar it carries the file-header fields — magic, format version, page size, epoch, journal-segment pointer — all covered by the header CRC.
- A `child_ref` deliberately stores only a page number.
  The child's own framing carries its epoch, and invariant I1 of the CoW design guarantees that a page referenced by a valid header is present and intact, so a per-child expected-epoch field would only duplicate the end-to-end assertion that the child's own `epoch` field already provides.
- Nothing in the format constrains *which* page a statement lives in or how the tree is shaped; those are writer policies, specified in [[#Maintenance]].

## In-memory representation

The in-memory state after a bulk read is the page images themselves plus three indexes over them.
Content is never copied out of the page images: `Ref` and `Inline` content both have a file address (an `Inline` payload lives inside an address-table page, but it is bytes at a known address all the same), so every resolved fragment points into the in-memory copy of the file, and `Undefined` needs no bytes at all.

**1. The fragment map**, which everything else hangs off: a B-tree `(id, offset) → Fragment`, where each entry describes the resolved content from `offset` up to the next key with the same `id`:

```rust
struct Fragment {
    source: Source,          // address into a Data or AddressTable page, or Undefined
    statement: StatementRef, // the live statement this fragment survives from
}

struct StatementRecord {     // one per live statement, arena-allocated
    page: PageNumber,        // where its encoding lives
    encoded_len: u16,        // its size in that page
    live_fragments: u16,     // how many fragments still resolve to it
}
```

This is the draft's `(id, offset) → Option<Address>` B-tree and its `(id, offset) → Statement` B-tree collapsed into one structure: the fragment map *is* the resolved view of [[#conflict resolution across epochs]], computed once during the bulk read and maintained incrementally afterwards, so epochs never need to be consulted again at run time.
A partially shadowed statement stays alive — its record keeps a nonzero `live_fragments`, because its surviving fragments still depend on its encoded bytes — and only when `live_fragments` reaches zero do its `encoded_len` bytes stop counting toward its page's live bytes.
Content queries resolve `(id, offset)` in one lookup, `O(log F)` for `F` live fragments; sequential reads iterate from there.

**2. The allocation map**: a hash map `id → AllocationMeta { size, fragment_count, statement_bytes, mentions }`.
`size` answers size queries in `O(1)`.
`fragment_count` and `statement_bytes` (total encoded size of the allocation's live statements) measure the allocation's *description overhead* relative to `size`, which drives defragmentation in [[#Maintenance]]; `mentions` counts live statements naming the id, which decides tombstone dropping and id recycling.

**3. Page tables**: for every page its kind, epoch, and live-byte counter (`Data`: referenced bytes; `AddressTable`: encoded bytes of statements with `live_fragments > 0`).
Pages are bucketed by live fraction (a handful of buckets suffices) together with an age mark, making victim selection `O(1)` rather than a priority queue's `O(log P)`, with `O(1)` bucket moves as counters change.
A free list tracks reusable pages, under the two-generation quarantine of the CoW design (a page freed by commit `E` becomes writable in flush `E + 2`).

Two derived structures complete the picture, both rebuilt at open and never persisted: the **id allocator** (next fresh id, plus recyclable ids — those with `mentions == 0` that do not exist, the same condition that lets their tombstones drop), and the **eviction clock** over the statements currently buffered in the header page ([[#Maintenance]]), which records how many flushes each has gone untouched.

Maintenance of all of the above costs `O(log F)` per fragment created or destroyed, and a flush creates or destroys at most a small multiple of the statements it writes.

## Maintenance

### Goals, and the one currency behind them

Everything below optimizes a single currency: **pages written now versus pages occupied — and rewritten again — later.**
The individual goals — few live address-table pages, few `Data` pages written per flush, few live `Data` pages — are all instances of it, and so is their apparent tension, which dissolves once statements can shadow *parts* of older statements: writing one large `Ref` for a bulk write and overwriting only the bytes a later flush actually changes are not alternatives.
The small overwrite simply shadows a slice of the large `Ref`, which stays live for everything else.

What accumulates instead is **description debt**: after many small overwrites, an allocation is described by one big `Ref` plus dozens of patches, which costs address-table bytes, fragment-map memory, and read assembly.
Paying that debt down — rewriting the patched region as one fresh `Ref` into fresh `Data` pages — is just another way of spending the same write budget.

So the design has exactly one knob, the per-flush **maintenance budget** in pages beyond the flush's own dirty content, and one queue of candidates competing for it, ranked by `bytes reclaimed / bytes written`:

1. **Data-page consolidation** — relocate the live bytes of low-live-fraction `Data` pages into the fill of pages being written anyway; reclaims dead data bytes.
2. **Address-table consolidation** — rewrite the live statements of low-live-fraction `AddressTable` pages, gathered by id range and emitted sorted and dense; reclaims dead statement bytes and restores delta-encoding density.
3. **Allocation defragmentation** — rewrite a heavily patched region of one allocation as a single fresh `Ref` (for `[data] [undefined] [data]` stipple, as a single `Ref` with arbitrary bytes in the don't-care gaps, as the `Undefined` usage note permits); reclaims statement bytes *and* the partially dead data behind the patches, and is the only candidate type that also shrinks the in-memory fragment map.

All three emit the same kind of output — fresh pages plus fresh statements — so they compose with the flush's ordinary work.
Ranking needs no cleverness: type-1 and type-2 candidates come from the live-fraction buckets in `O(1)`; type-3 candidates from an analogous bucket structure over `statement_bytes / size` in `AllocationMeta`.
The budget itself is the policy: spend at least enough to keep the reusable-page supply ahead of consumption (the pacing obligation of the CoW design), at most a constant number of pages per flush, highest ratio first.

On the requested complexity bound: strictly logarithmic *per flush* is unattainable, because a flush must at least record what it changed.
What the design achieves is the attainable form: `O((k + s) · log F)` in-memory work for a flush dirtying `k` ranges and rewriting `s` statements under the budget, `O(1)` victim selection per candidate, and page writes bounded by `dirty data pages + c + depth` for budget `c` — with the common small flush writing **no** address-table page beyond the header, as follows.

### The header as write buffer

The root of the address table is the header page, and the header page is rewritten by every flush no matter what.
That page is free real estate, so the design uses it as the address table's **write buffer**: every statement a flush produces lands in the header first, at zero additional page writes.
A flush that touches three small allocations spends a few bytes in a page it was writing anyway; that is the entire address-table cost of a small flush, and — via `Inline` — the entire cost of a small flush over small allocations, period.

Because the header is rewritten each flush, its statements are re-stamped with the current epoch every time.
That is sound under one discipline: **the header states resolved truth**.
Carried-over statements are merged with the flush's new ones first, and only surviving fragments are re-emitted, narrowed to their surviving ranges; a statement whose every fragment has been shadowed is dropped rather than carried.
Re-stamping resolved truth at a higher epoch is idempotent under [[#conflict resolution across epochs]], and the rules of [[#no conflicts within each epoch]] hold by construction, because resolved truth cannot contradict itself.

When the header overflows, the flush **evicts** statements into one fresh leaf page: the coldest statements — untouched for the most flushes, per the eviction clock — sorted by id and delta-encoded.
Hot statements stay in the header, so allocations under active rewrite generate no leaf garbage at all; a statement reaches a leaf only once it has stopped changing, which is exactly when writing it to a durable resting place is cheap.
This is generational: the header is the young generation, the leaves are the old one, and the assumption doing the work — most garbage is young — is the same one behind generational collectors and LSM memtables.

### The shape of the tree

The tree above the leaves exists only so that the root can *find* them, and that changes what kind of tree it should be.
It is a **directory, not a search structure**: every lookup is served by the in-memory fragment map, nothing ever descends the tree by key, so there is no ordering invariant to maintain across pages — and a B-tree's ~25 % expected slack is precisely the price of maintaining that invariant under inserts, so dropping the invariant drops the slack.
Interior pages are packed full, in arrival order; at each level only the newest interior page is partially filled.

- While everything fits in the header: zero extra pages — the draft's case (i), and the steady state of every small file.
- Overflow spills to leaves referenced directly from the header, with statements continuing to share the header alongside the `child_ref`s — case (ii).
  At ~3 bytes per `child_ref`, a header can reference on the order of a thousand leaves, i.e. a few MiB of statements, before this stops sufficing.
- Beyond that, one layer of interior pages (header → interiors → leaves) reaches roughly a million leaves; deeper is not going to be needed.

Structural updates are copy-on-write path copies, but the paths are short and the root is free: adding or removing a leaf rewrites at most `depth − 1` pages besides the leaf itself, the root being rewritten anyway.
Because interior pages are plain arrays of page numbers, removing a leaf's entry compacts its interior page for free during that rewrite — the opportunistic consolidation the draft asked for falls out of CoW by itself.
A depth change is a single-page event: when the header's child list outgrows its budget, move the whole list into one fresh interior page and reference that instead.

One soft property is worth cultivating without depending on it: eviction and consolidation both emit leaves sorted by id, so leaves *tend* to cover coherent id ranges, which keeps type-2 consolidation windows aligned with page boundaries and keeps delta encoding dense.
Nothing breaks as this degrades; consolidation restores it.

### Tombstones and id recycling

A `Tombstone(id)` is needed exactly as long as some other live statement mentions `id` — otherwise the freed allocation would resurrect — and it can be dropped by any rewrite of the page holding it once that stops being true.
An id becomes recyclable at the same moment its tombstone becomes droppable: `mentions == 0` in `AllocationMeta`, maintained in `O(1)` per statement change and re-derived at open from the same bulk read that builds the fragment map.
Neither condition is persisted; both are properties of the live statement set.

## Design directions and trade-offs

**Sub-allocation statements versus whole-allocation entries.**
The predecessor draft ([[cow]]) used one statement per allocation, arguing that per-chunk statements lacked stable keys.
This draft keys statements by *byte offset*, which is stable under overwrites and resizes — the operations that dominate — and the predecessor's spill machinery disappears outright: a large allocation's statements split across pages naturally, because no single statement ever needs to span one.
The price is middle insertion: a splice that shifts content shifts the offsets of everything behind it, so all later statements of that allocation must be restated, `O(fragments behind the splice point)` in address-table bytes.
The current non-CoW design physically moves the tail *bytes* for the same operation, so this is no regression against the status quo — but it is a genuine regression against the predecessor's chunk-sequence idea, which spliced in `O(1)` statements at the cost of that spill machinery.
If splice-heavy containers ever matter, the open path is a per-allocation indirection (a rope of ranges) layered *on top of* offset-keyed statements for exactly the allocations that need it; the format does not block it.

**Unordered directory versus on-disk search tree.**
Unordered is justified entirely by the bulk-read premise: nothing ever locates an id by descending pages.
This is the decision to revisit if `Section`s later require loading only part of a file; the cheap retrofit is optional id-range fences on `child_ref`s, which make the directory coarsely searchable without changing its maintenance, and full B-tree ordering would buy nothing beyond that.

**Epoch per page versus per statement.**
Per page is cheaper and imposes exactly one discipline — rewrites emit resolved truth — which consolidation requires anyway.
Per-statement epochs would spend bytes on every statement to relax a constraint the design already satisfies.

**The byte-referenced-once invariant** (the open question in the `Ref` row).
Not required for read correctness; keeping it makes data-page coverage a plain counter and consolidation locally decidable.
Relaxing it enables `O(1)` copy-on-write clones of allocations and deduplication, at the price of refcounted coverage and clone-aware consolidation.
Since it is a writer invariant invisible to readers, choosing the strict form now costs nothing later.

**Inline threshold.**
The encoding fixes the ceiling at 252 bytes (one-byte tag arithmetic); the policy threshold belongs far below it.
Below roughly the encoded size of a `Ref` (~10 bytes), inlining is strictly better — the pointer would be as large as the data.
Between there and the ceiling it is a trade: inline payloads ride the header for free while hot but inflate the address table when cold, whereas `Ref` plus data bytes are written once but occupy a data-page slot and keep a page partially live.
A starting policy: inline everything up to ~64 bytes, plus anything whose eviction would leave a `Data` page holding only scraps; then measure.
This is what makes small allocations first-class: below the threshold, an allocation lives its entire life — creation, every update, deletion — as a few bytes of statements inside pages the flush was writing anyway, with no page-granularity amplification anywhere, which is the cost profile that a dynamically typed guest language, allocating by the thousands, needs.

**The single ranked queue.**
One budget with ratio-greedy ranking is the simplest policy that is not obviously wrong, and it inherits the known failure mode of ratio-greedy cleaners: costs that current ratios cannot see get starved — e.g., description debt on a cold allocation that will be read forever but never written again never looks urgent.
If that bites, the established fix is to fold the invisible cost into the ranking (LFS's cost-benefit policy does the analogous thing with segment age); the queue structure stays.

## Related work

| System | Shared ideas | Where kladde diverges, and why |
| --- | --- | --- |
| **LSM trees** (LevelDB, RocksDB) | The header-as-write-buffer is a memtable; eviction is an L0 flush; ratio-driven consolidation is compaction; cold/hot separation at eviction. | LSMs keep runs sorted and leveled so point lookups work without full residency; kladde is fully resident after the bulk read, so it needs no sort order, no levels, no bloom filters — one tier of dense, unordered leaves. |
| **LFS and SSD FTLs** | Never overwrite; live-fraction victim selection; cost-benefit (sparse *and* old) cleaning. | Their cleaning unit is a large fixed segment; kladde's is a page, and its leaves can be re-cut freely because the directory keeps no order. |
| **LMDB** | CoW pages, alternating root, page-reuse quarantine by transaction age. | LMDB's B-tree is sorted because readers descend it in the mapped file; kladde never descends, so it trades away the ordering along with its ~25 % slack and rebalancing writes. |
| **btrfs, ZFS** | CoW everything, checksums everywhere; btrfs *inline extents* and ZFS *bonus buffers* store small data directly in metadata — the precedent for `Inline` as a first-class citizen. | Both maintain on-disk search structures for partial access to data far larger than memory; kladde's bulk-read premise removes the requirement those structures exist to serve. |
| **ext4, XFS** | Extent-shaped `Ref`s; small-file inlining (`inline_data`); description debt fought by defragmentation. | In-place journaling and fixed metadata locations rather than CoW shadowing. |
| **MVCC stores** (PostgreSQL) | Epoch shadowing is tuple versioning; tombstone lifetime is dead-tuple visibility; consolidation is `VACUUM`. | MVCC retains versions for concurrent readers; kladde retains them only across the commit boundary, so its vacuum needs no visibility horizon beyond the two-header rule. |
