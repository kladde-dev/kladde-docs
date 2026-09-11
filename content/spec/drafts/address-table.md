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

struct StatementRecord {     // one per statement that still has a reason to live
    page: PageNumber,        // where its encoding lives
    framing_len: u16,        // encoded size, excluding any Inline payload
    pins: u16,               // reasons this statement must stay; see below
}
```

Content bytes are deliberately *not* counted here; [[#What counts as a live byte]] charges them to whichever page physically holds them, which is what makes `Inline` payloads accounted per byte without any per-statement counter.

`pins` is a single refcount over heterogeneous holders, and a statement is live exactly while `pins > 0`.
Three things take a pin, and [[#What counts as a live byte]] argues that one counter is right even though `Size` statements can hold all three at once:

1. each **fragment** resolving to the statement;
2. each physically-present **older statement it denies** (suppressors only);
3. being the id's **size authority** (the newest `Size` for that id).

The name is deliberate: this is a refcount whose holders are of different kinds, so `liveness` would read as a boolean or an enum, and `refcount` would say nothing about what is doing the referring.
"Pin", in the buffer-manager sense of *something prevents this from being reclaimed*, is exactly the relationship, and it stays accurate as reason (2) and (3) join reason (1).

This is the draft's `(id, offset) → Option<Address>` B-tree and its `(id, offset) → Statement` B-tree collapsed into one structure: the fragment map *is* the resolved view of [[#conflict resolution across epochs]], computed once during the bulk read and maintained incrementally afterwards, so epochs never need to be consulted again at run time.
Content queries resolve `(id, offset)` in one lookup, `O(log F)` for `F` live fragments; sequential reads iterate from there.

**2. The allocation map**: a hash map `id → AllocationMeta { size, fragment_count, statement_bytes, mentions, size_statement, suppressors }`.
`size` answers size queries in `O(1)`.
`fragment_count` and `statement_bytes` measure the allocation's *description overhead* relative to `size`, which drives defragmentation in [[#Maintenance]].
`mentions` counts the statements naming this id that are **physically present** in live address-table pages — *not* the ones that are live in the resolution sense.
That distinction is the whole point: a statement shadowed into irrelevance still sits in its page, and still resurrects if the thing shadowing it disappears, so it must keep being counted until a page rewrite actually drops it.
`mentions` is incremented when a statement naming the id is written or read at open, and decremented when one is dropped during consolidation or when its page is reclaimed; it initializes suppressors' denial pins ([[#What counts as a live byte]]) and decides id recycling.
`size_statement` holds the id's size authority so it can be pinned and re-pinned as it changes hands, and `suppressors` lists the id's live `Tombstone` and `Size` statements — a list rather than a single pointer because dropping one statement may release pins on several of them.

**3. Page tables**: for every page its kind, epoch, and live-byte counter (`Data`: referenced bytes; `AddressTable`: per [[#What counts as a live byte]] below).
Pages are bucketed by live fraction (a handful of buckets suffices) together with an age mark, making victim selection `O(1)` rather than a priority queue's `O(log P)`, with `O(1)` bucket moves as counters change.
A free list tracks reusable pages, under the two-generation quarantine of the CoW design (a page freed by commit `E` becomes writable in flush `E + 2`).

Two derived structures complete the picture, both rebuilt at open and never persisted: the **id allocator** (next fresh id, plus recyclable ids — those with `mentions == 0` that do not exist, the same condition that lets their tombstones drop), and the **eviction clock** over the statements currently buffered in the header page ([[#Maintenance]]), which records how many flushes each has gone untouched.

Maintenance of all of the above costs `O(log F)` per fragment created or destroyed, and a flush creates or destroys at most a small multiple of the statements it writes.

### What counts as a live byte

A page's live-byte counter — its `coverage`, in the vocabulary of [[cow#Coverage accounting]] — is what makes it a consolidation victim, so it has to fall as the page's contents become useless.
`AddressTable` pages need no separate mechanism for this: **one rule covers both page kinds**, and it is the rule data pages already follow.

**Content bytes are charged to the page that physically holds them.**
Every fragment names a `source` address, so when a fragment is destroyed, decrement the coverage of the page that address falls in, by the fragment's length.
A `Ref`'s fragments point into a `Data` page; an `Inline`'s fragments point into the `AddressTable` page carrying the payload; an `Undefined` fragment points nowhere and costs nothing.
An inline payload is therefore just content that happens to live in a table page, and it gets per-byte accounting for free — no per-statement payload counters, no special case, the same line of code.
Splitting a fragment changes nothing, since both halves still cover the same bytes.

**Framing bytes are charged to the statement's own page, and released in one step** when the statement loses its last reason to live.
The two charges cover disjoint byte ranges of the encoding, so they cannot double-count.

Per-byte accounting for inline payloads is what the design actually needs, and it is worth saying why, because statement-granularity liveness looks adequate until it isn't.
For a `Ref`, partial shadowing strands only ~10 bytes of framing in the address-table page, while the content bytes it no longer reaches are tracked exactly in the data page's counter, which duly falls.
For an `Inline` there is no data page: the payload *is* the content, so charging it all-or-nothing would let shadowing half of a 200-byte inline strand 100 bytes that no counter in the system ever notices, and a page holding sixty mostly-shadowed inlines would report itself nearly full and never be cleaned.
Routing the charge through `fragment.source` avoids that without treating inlines specially at all.

**Framing is released when the statement's `pins` reach zero**, and the three kinds of pin are what the rest of this section is about.

| Statement kind | Can be pinned by | Content bytes, and where charged |
| --- | --- | --- |
| `Ref` | fragments | a `Data` page; released per fragment |
| `Inline` | fragments | its own `AddressTable` page; released per fragment |
| `Undefined` | fragments | none |
| `Size` | fragments, denials, **and** size authority | none |
| `Tombstone` | denials | none |

**Content statements need only fragment pins**, because a content statement's denial is exactly co-extensive with what it defines.
If a `Ref` covering `[a, b)` has lost every fragment, then every byte of `[a, b)` is covered by something strictly newer, and that newer thing also outranks everything the `Ref` was denying — so dropping it can resurrect nothing.
The size formula is safe too: covering `[a, b)` requires some newer statement to reach offset `b`, so the id's size cannot shrink when the `Ref` goes.

**Suppressors take denial pins.**
A `Tombstone` or `Size` exists to stop *older* statements from showing through, so it is pinned once per physically-present older statement naming that id — initialized when it is written, decremented whenever one of those is dropped by a page rewrite.
Note this counts *physically present* statements, not resolution-live ones: a statement with zero pins that has not yet been swept out of its page will still resurrect if its suppressor vanishes first.

**A `Size` statement can hold all three kinds of pin simultaneously**, which is the interesting case and the reason a single counter is the right shape rather than two fields.
Three claims, each with a witness:

- *It can own fragments.*
  With `Ref(id, 0, 1000)` at epoch 3, `Size(id, 10)` at epoch 5, and `Ref(id, 50, 10)` at epoch 9, the id's size is 60, and the range `[10, 50)` resolves through the `Size` statement to `Undefined` — a live fragment whose owner is the `Size`.
  (So the earlier claim in this document that suppressors "produce no fragments at all" was wrong for `Size`; it holds only for `Tombstone`.)
- *It can simultaneously owe denials.*
  In that same example, dropping the `Size` would move `size_epoch` back past epoch 3, so the old `Ref`'s extent of 1000 would set the size again and resurrect content in `[60, 1000)`.
  It must therefore stay pinned by that older `Ref` as long as the `Ref` is physically present.
- *And neither of those covers being the size authority.*
  With `Size(id, 100)` at epoch 5 and `Ref(id, 0, 10)` at epoch 9 and nothing older, `[10, 100)` resolves to `Undefined` by *default* rather than through the `Size` (a `Size(id, n)` only matches probes `>= n`), so the statement owns no fragment and denies nothing — yet dropping it would shrink the id from 100 to 10.

So the question "is there a case needing both counters?" has the answer *yes, `Size` is that case* — and that is precisely the argument for collapsing them.
Nothing in the design ever asks **which kind** of pin a statement holds; every consumer asks only whether any remain.
Two fields would therefore encode a distinction that is never read, at two bytes per live statement, while inviting the bug where a predicate checks one field and forgets the other.

**Multiple suppressors can pin the same statement**, so `AllocationMeta.suppressors` is a small list rather than a single newest pointer: with `Ref`(3), `Size(id, 10)`(5) and `Size(id, 5)`(8) all present, both `Size` statements are pinned by the `Ref`, and dropping it must decrement both.
Dropping a statement therefore decrements every suppressor for that id with a higher epoch — `O(k)` for a `k` that consolidation keeps at zero or one, since resolved truth contains at most one `Size` per id and no redundant tombstones.

This produces a cascade that clears itself without any special pass.
Statements shadowed by a tombstone contribute no content bytes, so their pages sink toward zero live fraction and become victims; consolidating those pages drops the statements, which decrements the tombstone's pins; when they reach zero the tombstone goes dead, sinking *its* page's live fraction, which eventually recycles it too.
Nothing has to reason about epochs to make this happen — only about counters.

Two limits worth stating rather than hiding.
A `Size` statement that still holds denial pins is counted live conservatively: deciding exactly whether it still denies anything reachable would need per-offset reasoning about older extents.
The over-count is bounded and tiny — a `Size` is about three bytes, and an allocation accumulates one per resize — and such statements die by rewrite anyway, since consolidation emits resolved truth.
And the counters are policy, not correctness: an over-count only delays cleaning, an under-count only cleans a page earlier than ideal, and neither can make a resolved read wrong.

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

This discipline also means header-resident `Inline` statements never fragment: a partial overwrite of one is merged into a single re-emitted `Inline` covering the union, so a small allocation under active rewrite keeps exactly one statement no matter how often it is touched.
Fragmented inlines can therefore only arise in *evicted* statements — a leaf-resident inline partially shadowed by a later flush — which is precisely the case the per-byte payload accounting of [[#What counts as a live byte]] exists to expose.
A worthwhile refinement, at the writer's discretion: when a flush partially shadows an evicted inline, pull the survivor back into the header as one merged `Inline` rather than leaving a patch behind.
That costs at most the payload's length in header bytes, kills the leaf statement outright instead of stranding part of it, and turns a partial shadow into a full one — cheap here precisely because the payload is small by construction.

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

A `Tombstone(id)` is needed exactly as long as some *physically present* statement in a live address-table page still names `id` — otherwise the freed allocation would resurrect — and it can be dropped by any rewrite of the page holding it once that stops being true.
That is its `pins` reaching zero ([[#What counts as a live byte]]) — a tombstone can only ever hold denial pins — equivalently `mentions == 1` (the tombstone alone).
An id becomes recyclable at the same moment, and by the same counter.
Neither condition is persisted; both are properties of the physically present statement set, re-derived at open from the same bulk read that builds the fragment map, and maintained in `O(1)` per statement written or dropped thereafter.

Re-allocating a tombstoned id does **not** by itself retire the tombstone, which is worth stating because the opposite is the intuitive guess.
Under [[#conflict resolution across epochs]] a tombstone matches every probe, so it goes on denying any offset that the new incarnation's statements do not cover — precisely the floor that keeps a smaller re-allocation from exposing the tail of the old one.
It retires only once nothing older is left to deny, which its denial pins already express; the `mentions > 1` test is therefore conservative in exactly one direction, keeping a tombstone that a fully covering re-allocation would have made redundant, and that is the safe direction.

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
Note the accounting asymmetry that argues for keeping the threshold low: an inline's payload is charged to the address-table page that holds it and is only reclaimed by rewriting that page, whereas a `Ref`'s content is charged to a data page that consolidation can clean independently of the statement describing it.
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
