---
title: Address-table operations
---

Pseudocode for every operation on the [in-memory state](in-memory-state.md).

Two levels are in play and it is worth separating them before reading any of this.
The **application level** mutates a value's native in-memory representation and appends a record to the journal; it does not touch the fragment map at all.
The **storage level** — everything below — describes what the *file* currently says, and changes only when a flush writes statements or a page is rewritten.
So `write_bytes` below is not what `vec.push()` calls; it is what a flush calls once, after folding a whole journal.

Notation: Rust-like, with `?` for "may fail", and with the obvious map operations left undefined.
`F` is the number of live fragments, `S` the number of live statements.

## Reading

### `resolve(id, probe) -> Fragment`

```rust
fn resolve(id: Id, probe: AllocationOffset) -> Option<(Fragment, Range)> {
    let meta = allocations.get(id)?;
    if probe >= meta.size { return None; }              // outside the allocation

    // Predecessor search: greatest key <= (id, probe).
    let (key, fragment) = fragments.range(..=(id, probe)).next_back()?;
    debug_assert!(key.id == id);                        // the partition invariant

    let end = match fragments.range((id, probe+1)..).next() { // `probe+1` can't overflow because `probe < size`
        // If a B-tree implementation supports `fragment.next` in O(1) time then
        // that should be preferred over the above second `range` query.
        Some((next, _)) if next.id == id => next.offset,
        _                                => meta.size,  // last fragment of this id
    };
    Some((fragment, key.offset..end))
}
```

`O(log F)`.
The `end` computation is where "a fragment stores no length" is paid for, and it is one extra step on the same cursor.

### `read(id, offset, len) -> bytes`

```rust
enum Chunk<'a> {
	Bytes(&'a [u8]),   // borrowed from the in-memory page mirror
	Zero { len: u32 },
}

fn read(id: Id, offset: AllocationOffset) -> Result<impl Iterator<Item = Chunk> + Read + Seek> {
    // Below is rust-like pseudocode using a hypothetical `yield` statement for generator
    // shorthand. The real rust implementation manually implements the state machine for
    // the iterator and also trivially implements `Read` and `Seek` on top of the iterator
    // (so that callers can easily read integers using `byteorder::ReadBytesExt`).

    let size = allocations.get(id).ok_or(InvalidPointerDereferenced)?.size;
    if offset >= size { return Ok(empty()); }   // also covers every zero-sized id

    let (key, mut fragment) = fragments.range(..=(id, offset)).next_back().unwrap();
    debug_assert!(key.id == id);                // the partition invariant, given offset < size
    let mut fragment_offset = key.offset;
    let mut skip = offset - fragment_offset;

    // Iterate over the fragments that follow `fragment`. In rust, this currently requires
    // a second B-tree descent due to a limitation of rust's B-tree implementation.
    // `offset + 1` cannot overflow, since we verified `offset < size <= u32::MAX` above.
    let mut iterator = fragments.range((id, offset + 1)..);

    while fragment_offset < size {
	    let (next_fragment_offset, next_fragment) = match iterator.next() {
		    Some((key, fgmt)) if key.id == id => (key.offset, fgmt),
		    // `next_fragment` can be any `Fragment`; the loop exits before reading it.
		    _ => (size, fragment),
	    };
	    // Assumes a constructor `Chunk::from_fragment(fragment, start, end)`
	    // where `start` and `end` are relative to `fragment`.
	    let chunk = Chunk::from_fragment(fragment, skip, next_fragment_offset - fragment_offset);
	    fragment = next_fragment;
	    fragment_offset = next_fragment_offset;
	    skip = 0; // Only the first fragment can have some skipped head bytes.
	    yield chunk;
    }
}
```

`O(log F + k)` for `k` fragments spanned: one predecessor search, then a walk of amortized `O(1)` steps.
The cursor is what buys that bound, and it is easy to lose.
Calling `resolve` once per fragment instead might read more naturally but would cost `O(k · log F)`, because every call would be a fresh search.

There is no `len` parameter, because the iterator is the caller's to stop: it runs to the end of the allocation, and a caller that does not want to read to the end simply drops the iterator when it's done reading.
The guard on `offset >= size` is what keeps the debug_assert honest — without it, reading at or past the end of an allocation, and reading any offset of a zero-sized one, lands the predecessor search on some other id’s last fragment.


### `size(id)` and `exists(id)`

```rust
fn size(id: Id)   -> Option<u32> { allocations.get(id).map(|m| m.size) }
fn exists(id: Id) -> bool { allocations.contains(id) }
```

`O(1)` both, because `size` is maintained incrementally rather than recomputed.

## Opening a file

Loading is the one place where the whole address table is processed at once, and the only place where epochs are consulted.
It is worth doing properly, because it is `Õ(file)` while everything else is `Õ(change)`.

### Three facts about the physical format that decide the algorithm

1. **A page is a run of constant epoch**, since every statement [inherits its page's epoch](../spec/address-table.md#the-idea).
2. **A page is sorted by `(id, offset)`**, because [`id_delta` and `offset_delta` are unsigned varints](../spec/address-table.md#delta-encoding) — the cursors only ever move forward.
3. **Within one epoch, no two statements claim the same byte**, which is [the no-conflicts rule](../spec/address-table.md#no-conflicts-within-each-epoch).

Two consequences carry the whole load:

> **Fact 2 makes loading a `k`-way merge of `P` sorted runs rather than a sort of `m` statements.**
> **Facts 1 and 3 together bound the sweep's open set by the number of distinct live epochs, not by the number of statements.**

The first replaces `O(m log m)` with `O(m log P)`, and `P` — the number of live address-table pages — is smaller than `m` by whatever the average statement density is, on the order of 600×.
More importantly it means no sorted copy of the statement array is ever materialised: the merge streams.

### The pipeline

```rust
fn open(file) -> std::io::Result<State> {
    let header = pick_header(file)?;          // CRC-valid, highest epoch
    file.fsync()?;                            // see "Why the fsync is here", below

    // Every page starts reusable and is claimed as it is discovered. After the
    // fsync above, the older header's world is retired, so "not claimed" and
    // "reusable" coincide and no page needs an epoch until something writes it.
    let mut state = State::with_pages(file.len().div_ceil(page_size));

    // 1. Walk the address-table tree, decoding every page and keeping it resident.
    //    The tree is a directory, never searched; a plain traversal suffices.
    let mut runs = Vec::new();              // one decode cursor per page
    let mut seen_addr_table_pages = Set::new();
    let mut queue = vec![header.page];
    while let Some(p) = queue.pop() {
        if !seen_addr_table_pages.insert(p) { return Err(NotATree); }
        // Decode up to first statement and validate CRC:
        let (page, stmt_decoder) = decode(file.page(p))?;
        if page.epoch > header.epoch { return Err(DurabilityContractViolated); }
        state.pages.insert(p, page.kind, page.epoch);
        queue.extend(page.children);
        runs.push(stmt_decoder);            // sorted by (id, offset); epoch = page.epoch
    }

    // 2. One merge, one group at a time. Every index is filled from the same
    //    pass: the allocation map, the slab, the fragment map, coverage, the
    //    set of data pages to mirror, and the recyclable ids.
    let mut merged = merge(&mut runs).peakable(); // iter over stmts in (id, offset) order
    let mut emitted_fragments = Vec::new();       // will be emitted in sorted order
    while let Some(id) = merged.peek().map(|s| s.id) {
        resolve_id(&mut state, id, &mut merged, &mut emitted_fragments);
    }
    state.fragments = BTree::from_sorted(emitted_fragments); // real rust: `BTreeMap::from_iter`

    // 3. The eviction clock covers only what the header holds, and is rebuilt there.
    state.clock = EvictionClock::from(header.statements());

    // 4. Replay the journal segment the header names, stopping at the first
    //    failing check. Its pages are live too, and a non-empty journal is
    //    folded by a flush before anything is appended to it.
    for txn in file.journal(header.journal_pointer).valid_prefix() { replay(txn); }
    Ok(state)
}
```

**Why the `fsync` is here.**
It is the one [durability](../spec/durability.md#recovery--loading-a-kladde-file) requires of a loader that wants to retire the older header's world immediately, and what it must precede is the first page handed to the allocator.
Putting it later would be a bug of the silent kind: every page the first flush reuses would be a page recovery might still have needed.
Putting it before loading the header would also work but would incur the cost of an unnecessary `fsync` if `pick_header` fails.

**The merge.**
`merge` holds one decode cursor per address-table page in a binary min-heap keyed by that cursor's current `(id, offset)`, pops the smallest, advances that cursor, and pushes it back.
Because each run is sorted by `(id, offset)`, the output is globally sorted by `(id, offset)`, so an id's statements arrive contiguously and `resolve_id` can consume them straight from the merge, one peek ahead, with nothing buffered.
`O(m log P)`, and the only structures alive at any moment are the `P` decode cursors and a heap bounded by the number of distinct live epochs — so the working set is `O(P)` rather than `O(m_i)` for the widest id.

**Statements that match nothing sort where they do without consequence.**
A content statement sorts at its `offset`, which is where its match range starts, so the merge hands the sweep every content statement exactly when the sweep reaches it.
A `Size(id, n)` sorts at `n`, and a `Tombstone`, which carries no offset, at `0`: by the no-conflicts rule it is the only statement naming its id in its page, so its `id_delta` is nonzero and resets `offset_cursor`.
Neither matches a probe, so the sweep only notes them as they pass, and `resolve_id` needs no pre-pass.

### Resolving one id

One pass over the group, and no sorting.
The fragments come *out* of the sweep, along with the size statement, the existence, and everything else `AllocationMeta` holds.

Only content statements match anything:

| statement                                    | match range                    |
| -------------------------------------------- | ------------------------------ |
| `Ref`, `Inline`, `Zero`, in either variant   | `[offset, offset + stmt_size)` |
| `Size`, `Tombstone`                          | none                           |

The fragment covering a stretch is the matching statement with the highest epoch, so what the sweep computes is the **upper envelope by epoch** of these intervals, which it then clips at the size.

```rust
fn resolve_id(state: &mut State, id: Id, stmts: &mut Peekable<Merge>,
              emitted_fragments: &mut Vec<Fragment>) -> Result<()> {
    // `stmts` yields this id's statements in sort order, and then the next id's;
    // see the merge, above.
    // `open` is a max-heap by epoch. Entries are (epoch, end, statement). By fact 3 it holds
    // at most one entry per distinct epoch at any point in the iteration below, so its size
    // is bounded by the number of live epochs — in practice a handful, never more than P.
    let mut open = MaxHeap::new();
    let mut newest = None;             // highest epoch seen; decides existence
    let mut sizing = None;             // newest statement that states the size
    let mut mentions = 0;              // physically present statements naming this id
    let mut runs = Vec::new();         // the envelope: (start, winner), in offset order
    let mut pos = 0;
    let mut current: Option<Decoded> = None;    // winner of the run starting at `run_start`
    let mut run_start = 0;

    loop {
        while open.peek().is_some_and(|e| e.end <= pos) { open.pop(); }   // lazy expiry
        while stmts.peek().is_some_and(|s| s.id == id && s.start() <= pos) {
            let s = stmts.next().unwrap();
            mentions += 1;             // one per statement consumed, and none is consumed twice
            newest = newest_of(newest, s);
            if s.states_size() {
                sizing = newest_sizing(sizing, s)?;  // two of one epoch: an invalid file
            }
            if s.is_content() { open.push(s.epoch, s.end(), s); }
        }                              // expired statements fall out here too

        let winner = open.peek();      // None => nothing matches this stretch
        if winner != current {
            if pos > run_start { runs.push((run_start, current)); }
            (current, run_start) = (winner.copied(), pos);
        }
        // Stop once nothing is left to open and nothing is open.
        let next_start = stmts.peek().filter(|s| s.id == id).map(|s| s.start());
        if next_start.is_none() && winner.is_none() { break; }
        // The winner can only change where a statement starts or where it ends.
        // Statements that end while shadowed change nothing and are skipped over.
        pos = min(next_start.unwrap_or(u32::MAX),
                  winner.map_or(u32::MAX, |w| w.end));
    }
    runs.push((run_start, None));      // the envelope ends here

    let size = sizing.map_or(0, |s| s.size());
    // The clip: runs at or past `size` go, and a run below it must have a winner.
    for (i, &(start, winner)) in runs.iter().enumerate() {
        if start >= size { break; }
        let end = runs.get(i + 1).map_or(size, |r| r.0).min(size);
        emit(state, id, start..end, winner.ok_or(UnmatchedProbe)?, emitted_fragments);
    }
    finish(state, id, mentions, size, newest, sizing)
}
```

### What the sweep leaves behind

- **The size is what the newest size-stating statement states**, `0` if there is none, and that statement is the size statement.
  `sizing` is maintained in the consumption loop at one comparison per statement; two statements of one epoch that both state the size violate [the rules of one epoch](../spec/address-table.md#no-conflicts-within-each-epoch), and the comparison is where that shows.
- **Existence is the other fact the envelope cannot supply.**
  `newest` is therefore maintained in the same loop, and `exists` is `!newest.is_tombstone()`.
- **A non-existent id takes the other exit.**
  Its newest statement is a tombstone, which states the size 0, so the clip leaves no fragment.
  No allocation-map entry; a recyclable-id entry carrying `mentions` and the tombstone; and one slab slot, for the tombstone, holding its `S` pin while `mentions > 1`, by [the last-tombstone rule](liveness.md#the-last-tombstone).
- **`mentions` is counted in the consumption loop**, one per statement, since every statement the sweep consumes under this id names it and the id's run in the merge is exactly its physically present statements.
  Counting there rather than measuring a buffer afterwards is what lets the group stay unbuffered.
- **A probe below the size that nothing matches is an error**, which the clip finds as a run without a winner, or as an envelope that ends short of the size.

### Why the sweep clips at the end

**The size is known only once the group is done**, because the merge hands over an id's statements in offset order, not in epoch order, and the newest size statement can sort after an older one.
With `Size(7, 10)`@5, which sorts at 10, and `Zero(7, 10, 10)`@9 and `Zero*(7, 20, 30)`@9 after it, the size is 50; a sweep that stopped at 10 on meeting the `Size` would lose `[10, 50)`.

**And the envelope runs past the size**, since a statement that a shrink truncated still matches its whole range.
Over `Ref*(7, 0, 1000, P1)`@3, `Size(7, 10)`@5, and `Zero(7, 10, 90)`@7 with `Ref*(7, 100, 20, P2)`@7, the envelope gives `[0, 10)` to `Ref*`@3, `[10, 100)` to the `Zero`, `[100, 120)` to `Ref*`@7, and `[120, 1000)` to `Ref*`@3 again; the size is 120, stated by `Ref*`@7, and the clip drops the last run.
Nothing else denies a truncated statement: its winner below the size is exactly right, since `[0, 10)` still resolves through `Ref*`@3, and above the size nothing is read.

The runs are held per group until the clip, which costs no copy, since they are emitted in order either way, and no slot is given out before it, so a statement that wins only past the size never gets one.

### Four details that carry the correctness

Each is easy to get wrong:

- **The heap, not a stack.**
  A shadowed statement re-emerges when the statement covering it ends — `Zero(0, 100)`@3 under `Ref(0, 10)`@9 wins `[10, 100)` — so the open set has to be a priority queue and entries must survive being overtaken.
- **Lazy expiry is enough.**
  Only the top matters, so entries that expired while shadowed can be discarded whenever they surface.
  Each statement is pushed once and popped once: `O(m_i log P)` for the whole sweep.
- **Emit on winner change, not on event.**
  Advancing `pos` to a start that loses, or to an end that was shadowed, must not cut a fragment — which is exactly what makes the output **minimal**, with neighbours already joined, rather than something a coalescing pass has to clean up afterwards.
- **The loop runs past the end of the group.**
  Exhausting the statements is not a stopping condition on its own, because bounded winners are still expiring and expiry is what uncovers what lies beneath them.
  Over `Ref(0, 1000)`@3 and `Ref(0, 600)`@7, `Ref`@3 is shadowed until `Ref`@7 expires at 600, and only then wins `[600, 1000)`.

### Why a sweep, and not an intermediate tree

**The sweep writes the fragment map directly, in the order the map wants, and needs no structure bigger than a heap of a handful of entries.**
Two alternatives are worth naming, because both are the natural first idea and both are worse here.

*An interval tree, or a segment tree keyed by epoch.*
Building one costs `O(m log m)` and a second allocation the size of the statement set, and it answers a question the load never asks — "who wins at this probe" — one probe at a time.
Its output also arrives in whatever order the extraction walk produces, so the fragments still have to be sorted before they can be bulk-loaded.
It would earn its keep if probes arrived interactively; at load they do not.

*Painting newest-first into a set of unclaimed gaps.*
Sort the id's statements by descending epoch, and let each claim only the parts of its range that nothing newer has taken; with a union-find "next unclaimed offset" this is `O(m α(m))` after the sort, which is great asymptotically.
But it needs the group in epoch order, which the merge does not give and which would cost a sort per id; and it produces fragments in epoch order, which is not the order the fragment map is built in, so it needs a second sort at the end.
Two sorts to avoid a heap of five entries is a bad trade.

The sweep is what fits the data the format actually hands us: already sorted by offset, already grouped by epoch.

### What falls out of emission

`emit` is where every other index is filled, and the reason the load is a single pass rather than four:

- **Slab slots are allocated on first ownership.**
  A statement gets a `StatementRef` the first time it wins a stretch below the size, and a statement that never wins one — and is not the size statement — is **never given a slot at all**.
  That is what makes the slab hold exactly the live statements, and it is why dead statements cost memory only as the bytes they occupy in their page.
- **Pins are counted, not computed.**
  Each emitted fragment adds one `F` pin to its owner; the size statement adds its `S` pin once, at the end of the group, whether it owns fragments or not.
  A sizing `Ref` that owns none points at data no fragment reads, so it keeps no data page alive, which is right, since nothing reads those bytes.
- **Coverage is accumulated twice per statement, for different reasons.**
  A live statement charges its framing to the address-table page holding it; each `Bytes` fragment charges its length to the page holding the data, which for an `Inline` is that same table page.
- **The set of data pages to mirror is the set of pages named by emitted `Bytes` fragments**, and it is also the set of data pages the page table marks live.
  Physically present `Ref`s that lost every probe name pages that nothing can read, and [those pages must not be mirrored](index.md#the-mirror) — nor kept alive.
  Their epochs are read from their framing as they are mirrored; a page nothing claims needs no epoch, because it is already reusable.
- **A non-existent id goes to the recyclable set instead of the allocation map**, carrying `mentions` and a reference to its tombstone.

**Then the fragment map is built in bulk.**
Ids are processed in ascending order and each id's fragments are emitted in ascending offset, so the emitted stream is globally sorted by `(id, offset)` — which lets the B-tree be built bottom-up in `O(F)` from the sorted sequence, instead of `F` separate `O(log F)` insertions into a tree that rebalances all the way up.
This is the single largest constant-factor win available at load, and it is free: it only requires that nothing reorders the stream between the sweep and the build.

### Cost

| step | cost |
| --- | --- |
| decode every address-table page | `O(address-table bytes)` |
| merge | `O(m log P)` |
| resolve, all ids | `O(m log P)`, and `O(m)` when few epochs overlap |
| build the fragment map | `O(F)` |
| replay the journal | `O(journal bytes)` |

No step sorts, and no step allocates a second copy of the statement set.
The remaining `O(live bytes)` term is reading the file itself, which is the bound [the specification states](../spec/index.md#guarantees-an-implementation-must-provide) and the one the design is willing to pay.

## Fragment-map primitives

Everything that changes content goes through these three.

### `split(id, at)`

Ensure a fragment boundary exists at `at`, so that a range edit has somewhere to start and stop.

```rust
fn split(id: Id, at: AllocationOffset) {
    let (fragment, range) = resolve(id, at).unwrap();
    if range.start == at { return; }                 // already a boundary
    let shifted = match fragment {
        Bytes { page, offset, statement } =>
            Bytes { page, offset: offset + (at - range.start), statement },
        Pending(p) => Pending(pending.split(p, at - range.start)),   // shifts origin and place
        other => other,                              // zero fragments carry no offset
    };
    if let Some(s) = shifted.statement() { pin(s); } // the statement now owns two
    fragments.insert((id, at), shifted);
    allocations[id].fragment_count += 1;
}
```

A split **adds a pin**, because one statement now owns two fragments.
This is the operation behind "pins can rise after creation".

### `overwrite(id, range, new: Fragment)`

```rust
fn overwrite(id: Id, range: Range, new: Fragment) {
    split(id, range.start);
    if range.end < allocations[id].size { split(id, range.end); }

    for (key, old) in fragments.drain(range) {        // strictly inside
        if let Some(s) = old.statement() { unpin(s); }
        allocations[id].fragment_count -= 1;
        if let Some(page) = old.data_page() { coverage[page] -= length_of(key); }
    }
    if let Some(s) = new.statement() { pin(s); }
    if let Some(page) = new.data_page() { coverage[page] += range.len(); }
    fragments.insert((id, range.start), new);
    allocations[id].fragment_count += 1;
    coalesce_around(id, range);
}
```

The only non-trivial primitive: split at both boundaries, drop what is strictly inside, insert the new one.
`data_page()` is the page a fragment's bytes sit in — a `Bytes` fragment's, or a pending one's whose place is `Data` — so a range taken in place is released and charged again to the same page, and a pending fragment [carries its coverage](in-memory-state.md#during-a-flush) exactly as a stated one does.

### `coalesce_around(id, range)`

Merge each new boundary with its neighbour when both fragments resolve identically **and** name the same statement.
Two `Zero` fragments merge only when the same `Zero` owns both.
Two `Bytes` fragments merge only when they are contiguous in the same page *and* owned by the same statement.
Pending fragments are left alone: they have no statement to compare yet, and the cut merges them when it derives one.

Merging releases one pin and decrements `fragment_count`.

### `pin` / `unpin`

```rust
fn pin(s: StatementRef) { slab.records[s].pins += 1; }

fn unpin(s: StatementRef) {
    let rec = &mut slab.records[s];
    rec.pins -= 1;
    if rec.pins == 0 {
        let page = rec.page_or_next;                  // read BEFORE overwriting
        coverage[page] -= slab.framing_len[s];
        allocations[id_of(s)].statement_bytes -= slab.framing_len[s];
        rec.page_or_next = slab.free_head;             // now reuse the field
        slab.free_head = s;
    }
}
```

The ordering in the `pins == 0` branch is the obligation named in [In-memory state](in-memory-state.md#2-the-statement-slab): the page number must be read before the field becomes a free-list link.

## Taking and stating

A flush changes the fragment map in two steps, and only the second creates statements: every source **takes** the fragments it changes while the flush runs, and the cut **binds** a statement to them once it knows what to state and in which page.
[Why the two are kept apart](consolidation.md#each-fragment-is-stated-once-per-epoch) is consolidation's story; the mechanics are these.

### `take(id, range, pending, dirty)`

```rust
/// Re-owns `range` for the flush in progress. Its old owners lose their pins and
/// its coverage now; the statement that will own it is the cut's to derive.
fn take(id: Id, range: Range, p: Pending, dirty: &mut Dirty) {
    overwrite(id, range.clone(), Fragment::Pending(pending.push(p)));
    dirty.ranges.insert(id, range);                  // merged with whatever it touches
}
```

Taking is how every source changes content — the fold writing, a victim's survivors moving, a description defragmentation rewrite, a page rewrite or the rotating window restating, the header being carried forward.
What differs between them is only the [`Pending` entry](in-memory-state.md#during-a-flush): whether the bytes stay where they are, need a page, go inline, or are zeros.

### `bind(stmt, page, run)`

```rust
/// Gives `stmt` its slot and its fragments, once the cut has decided which page it
/// goes in. `run` is the pending fragments the cut derived it from.
fn bind(stmt: Statement, page: PageNumber, run: &[Key]) {
    let (id, framing) = (stmt.id(), framing_of(&stmt));   // encoded length minus payload
    let s = slab.alloc(page, framing);                 // pins = 0 for now
    *mentions_of(id) += 1;                             // the recyclable set's, for a `Tombstone`
    *statement_bytes_of(id) += framing;
    coverage[page] += framing;
    for &key in run {                                  // Pending → Bytes or Zero
        fragments[key] = fragments[key].stated_by(s, page);
        pin(s);
        if stmt.is_inline() { coverage[page] += length_of(key); }   // payload, per byte
    }
    match stmt {
        Tombstone                    => set_tombstone(id, s),
        _ if stmt.states_size()      => set_size_statement(id, s),
        _                            => {}
    }
}
```

Everything else a statement implies has already happened by then: the take released the old owners, and the size changes happened when the flush [made them](#mutation-at-flush-time).
What `bind` adds is ownership, framing, and the size statement — the three things that need the statement to exist.

### `grow_size_to(id, n)` and `shrink_size_to(id, n)`

```rust
fn grow_size_to(id: Id, n: u32, dirty: &mut Dirty) {
    let meta = &mut allocations[id];
    if n <= meta.size { return; }
    let exposed = meta.size .. n;
    meta.size = n;
    // Past the old end there is nothing to release, so this take only appends:
    // one pending entry, zeros unless the flush writes over them.
    fragments.insert((id, exposed.start), Pending(pending.push(Pending::zeros())));
    meta.fragment_count += 1;
    dirty.ranges.insert(id, exposed);
}

fn shrink_size_to(id: Id, n: u32) {
    let meta = &mut allocations[id];
    for (key, old) in fragments.drain_from((id, n)) {  // everything at or past n
        if let Some(s) = old.statement() { unpin(s); }
        meta.fragment_count -= 1;
        if let Bytes { page, .. } = old { coverage[page] -= length_of(key); }
    }
    if n < meta.size { split(id, n); }                 // truncate the straddler
    meta.size = n;
}
```

`grow_size_to` is where the flush keeps [the coverage rule](../spec/address-table.md#the-coverage-rule): whatever it writes into the exposed range replaces the pending zeros, and the cut states the rest as a `Zero`.

### `set_size_statement(id, new)` and `set_tombstone(id, new)`

```rust
fn set_size_statement(id: Id, new: StatementRef) {
    pin(new);                                           // `S`
    if let Some(old) = allocations[id].size_statement.replace(new) {
        unpin(old);                                     // may die here
    }
}

fn set_tombstone(id: Id, new: StatementRef) {
    pin(new);                                           // `S`, until `mentions` falls to 1
    if let Some(old) = ids.recyclable[id].tombstone.replace(new) { unpin(old); }
}
```

For an id allocated again, the old `size_statement` is the tombstone it carried over from the recyclable set, which the new incarnation's size statement releases here.

## Mutation, at flush time

What the flush calls for each id its fold touched, once per flush and with the fold's net result: each call takes what it changes, applies the size change at once, and leaves the id a record for the cut.

### `write_bytes(id, offset, len, origin)`

```rust
fn write_bytes(id: Id, offset: AllocationOffset, len: u32, origin: Origin, dirty: &mut Dirty) {
    touch(id, dirty);                                 // records the id; sets `last_written`
    grow_size_to(id, offset + len, dirty);            // a write past the end
    take(id, offset..offset + len, Pending::bytes(origin), dirty);
}
```

No page is chosen here: `pack` places the bytes later — unless they are short enough to be stated [`Inline`](flush.md#the-inline-threshold) — and the cut states them.
A write past the current end produces **two** fragments — the gap `[size, offset)`, pending as zeros, and the written range — which is the only way one write adds two.

### `resize(id, n)`, `allocate(id, n)`, and `free(id)`

```rust
fn resize(id: Id, n: u32, dirty: &mut Dirty) {
    touch(id, dirty);                                 // records the size before the change
    if n > size(id) { grow_size_to(id, n, dirty); } else { shrink_size_to(id, n); }
}

fn allocate(id: Id, n: u32, dirty: &mut Dirty) {
    touch(id, dirty);                                 // records that it did not exist
    let tombstone = ids.recyclable.remove(id).and_then(|r| r.tombstone);
    allocations.insert(id, AllocationMeta { size_statement: tombstone, ..empty() });
    grow_size_to(id, n, dirty);                       // every byte is stated
}

fn free(id: Id, dirty: &mut Dirty) {
    touch(id, dirty);
    shrink_size_to(id, 0);                            // every fragment goes, and with it `F`
    let meta = allocations.remove(id);
    if let Some(s) = meta.size_statement { unpin(s); } // the cut states the tombstone
    ids.recyclable.insert(id, RecyclableId { mentions: meta.mentions, tombstone: None });
}
```

None of them states anything: `touch` records, the first time the flush meets an id, the size and existence it had, and the cut reads that record back.
The allocation-map entry goes away at the free; only `mentions` and, once the cut binds it, the tombstone reference survive, in the recyclable set.
An id allocated again carries its tombstone, and the tombstone's `S` pin, over as its size statement, until the cut binds the new incarnation's own.

### What the cut states for a touched id

The fragment map says what an id's bytes are, but not how its size and existence changed, so these statements come from the id's record:

| how the id changed during the flush | the cut states |
| --- | --- |
| it existed and was freed | `Tombstone(id)`, while a statement still names the id |
| it existed, was freed, and was allocated again | no tombstone, which would contradict the new incarnation in the same epoch; the fold takes the whole new extent instead, so the new incarnation is stated like a new one |
| it is new, or was resized | a size statement |
| a page this flush retires held its size statement | a size statement |
| it does not exist, and a page this flush retires held its tombstone | `Tombstone(id)`, while a statement still names the id |

**A size statement is the content statement the cut states that ends at the id's size, made sizing, or `Size(id, size)` if there is none.**
So an allocation of size 0, which no content statement reaches, gets `Size(id, 0)`, and a shrink that writes nothing at the new end gets a `Size` too; an append, a growth, whose exposed zeros the cut states, and a deleting splice, whose restated tail ends at the new size, get theirs for free.

**Every content statement the cut states that ends at its id's size is sizing**, whether a row of the table asks for a size statement or not, [since it costs nothing](liveness.md#emission-when-a-resize-writes-a-statement) — unless the cut states a `Size` for the id, which then states the size.
This is decided last, once every statement of the flush is known, the fillers' included: only one content statement of an epoch can end at the size, since their ranges are disjoint, and the sizing bit changes no statement's encoded length, so deciding it after the layout costs nothing.
A `Size` is the only statement here that takes room, and the cut adds one for an id only where no content statement it states ends at the size, which for the fillers' ids is checked again once they are known.

### `drop_physically(stmt)`

Called when a page rewrite decodes a statement and does not re-emit it.

```rust
fn drop_physically(stmt: Statement) {
    let id = stmt.id();
    let m = match allocations.get_mut(id) {
        Some(meta) => &mut meta.mentions,
        None       => &mut ids.recyclable[id].mentions,   // tombstoned id
    };
    *m -= 1;
    if *m == 1 {
        if let Some(t) = ids.recyclable[id].tombstone {
            unpin(t);                                     // releases S; may die
            ids.recyclable[id].tombstone = None;          // clear NOW, not at sweep
        }
    }
}
```

Clearing the tombstone reference at the pin release rather than at the eventual sweep is what keeps the slab free of stale references: the slot is freed at the `1 → 0` transition.

## Page rewrites

The header being carried forward and both table-side consolidation mechanisms are the same operation — **take a page's live content in place, and let the cut state it again** — differing only in which content and why.

### `rewrite_page(victim, dirty)`

```rust
fn rewrite_page(victim: PageNumber, dirty: &mut Dirty) {
    let page = decode(victim);
    for stmt in page.statements {
        if keep(stmt) == Keep::AsResolved {
            // The bytes stay where they are; only the statement stating them is new.
            for (key, len) in owned_fragments(stmt) {
                take(stmt.id(), key..key + len, Pending::in_place(key), dirty);
            }
            if holds_s_pin(stmt) { dirty.record(stmt.id()).replace = true; }
        }
        drop_physically(stmt);
    }
    adopt_children(page.children);   // into the header if it has room, else the replacement
}
```

**It writes no page of its own, and states nothing itself.**
What it takes joins the flush's [dirty set](consolidation.md#one-dirty-set-and-why-statements-are-derived-last), and the cut derives the statements, merged with whatever else the flush took nearby, into whichever page the cut chooses.
So a later source that takes the same fragments — an evacuation of their data page, say — simply changes what the cut will state, and cannot [state the same fragment twice](consolidation.md#each-fragment-is-stated-once-per-epoch).

Order does not matter either.
A statement's owned fragments interleave with its neighbours' — with `Ref(id, 0, 100)` shadowed over `[20, 25)` by `Zero(id, 20, 5)`, the `Ref` owns `[0, 20)` and `[25, 100)` and the `Zero` the gap between — so the ranges arrive in the victim's id order but not in offset order within an id, and the dirty set, which keeps its ranges merged and sorted, absorbs that.

### `rewrite_key_range(lo, hi, dirty)`

[The rotating window](consolidation.md#the-rotating-window) takes a **key range**, not a page, and it is the one rewrite that decodes nothing at all.

```rust
fn rewrite_key_range(lo: Key, hi: Key, dirty: &mut Dirty) {
    let mut touched = SmallSet::new();                   // pages it restates from
    // The fragment map is resolved truth, so it is the source.
    for (key, fragment) in fragments.range(lo..hi) {
        // A pending fragment is taken already.
        let Some(s) = fragment.statement() else { continue };
        touched.insert(slab.page_of(s));
        take(key.id, key.offset..end_of(key), Pending::in_place(key), dirty);
    }
    // Size statements are invisible in the fragment map, so their restatement is
    // recorded by hand — but only where it pays, since a fresh `Size` costs bytes.
    for id in ids_in(lo..hi) {
        if size_statement(id).is_some_and(|s| touched.contains(slab.page_of(s))) {
            dirty.record(id).replace = true;
        }
    }
}
```

**No statement is dropped physically here**, and that is deliberate: the old statements stay in their pages, `mentions` does not move, and what changes is that they lose their fragments and therefore their pins.
`unpin` releases their framing from their pages' coverage at the `1 → 0` transition, which is how pages that were never touched become reusable.

### `owned_fragments(stmt)`

The consolidator finds a statement's fragments by range-scanning the fragment map and keeping those whose owner matches:

| statement kind | scan range |
| --- | --- |
| `Ref`, `Inline`, `Zero`, in either variant | `(id, offset) .. (id, offset + size)` |
| `Size`, `Tombstone` | none; they win nothing |

It can stop early, because `pins` already says how many to expect: `F == pins` exactly, or `pins − 1` for the id's size statement.

This is not an implementation tax — it *is* the consolidation algorithm.
A page is rewritten as resolved truth, and one cannot emit resolved truth without first determining which parts of it this page is responsible for.
Its cost is predictable in advance from `fragment_count`, which lets the ranking price a candidate before committing to it.

A page rewrite decodes exactly **one** page: its victim.
It never needs an id's physically present statements across the table, because it only rewrites what the victim holds — and a [key-range rewrite](#rewrite_key_rangelo-hi-dirty) decodes none at all, since it reads the fragment map instead.

### Stating the size again — the one correctness obligation

A fragment-map-driven consolidator will faithfully re-emit the content a statement owns and **silently corrupt the file** if that statement was also the size statement, because being the size statement is not a fragment and is therefore invisible in the fragment map.

Concretely: with `Ref*(7,0,1000,P1)`@3 in another page and `Size(7,10)`@5 in the victim, dropping the victim makes `Ref*`@3 the newest statement of the size, which becomes 1000 again — re-exposing stale bytes across `[10, 1000)`.

> **A page holding a statement with an `S` pin cannot be rewritten without stating the size, or the tombstone, again.**

That is what an id's record asks the cut for, by [the rules for a touched id](#what-the-cut-states-for-a-touched-id), evaluated after every drop the flush makes, so that the statement being replaced no longer counts among the id's `mentions`:

- **an existing id gets a size statement**, which is free when the cut restates the content at the id's end, as it does whenever the victim's statement was a sizing one that still owned its tail;
- **a non-existent id gets `Tombstone(id)` while any statement still names it**, since stating nothing would let that statement decide existence again;
- **a non-existent id that nothing names any more gets nothing**: its tombstone leaves with its page, having nothing left to deny.

### The header

**The header is taken whole at the start of every flush and stated again at the cut**, which is what makes it the address table's [write buffer](flush.md#the-header-as-write-buffer): everything it holds is rewritten every flush at no extra page write, and the cut decides afresh which statements stay in it.

```rust
fn take_header(dirty: &mut Dirty) {
    for stmt in header.statements() {
        for (key, len) in owned_fragments(stmt) {
            take(stmt.id(), key..key + len, Pending::in_place(key).with_heat(clock[key]), dirty);
        }
        if holds_s_pin(stmt) { dirty.record(stmt.id()).replace = true; }
        drop_physically(stmt);        // the new header replaces it in the governing world
    }
}
```

It is `rewrite_page` for the one page every flush retires, with one addition: the [eviction clock](flush.md#the-header-as-write-buffer)'s entries ride along as the pending fragments' heat, so that the cut can keep the hottest statements in the header and send the rest to leaves.

Because the header is rewritten unconditionally, a size statement carried in it becomes a *new* statement record at the new epoch each flush, so `size_statement` re-points and the `S` pin moves every flush.
That is routine rather than churn — header pages are exempt from coverage-driven victim selection — but it is why the size statement must be a movable pin rather than a flag baked into the statement.

### Consolidating a data page

Unlike an address-table page, a data page is opaque bytes with no ids in it, so decoding it tells nothing about who references it.
This is an **unresolved gap**: the design specifies data-page victim selection but not how a flush gets from a victim page number to the `Ref` statements it must re-point.
See [Consolidation](consolidation.md#finding-the-referrers-of-a-data-page) for the candidate answers.

## Worked trace

One allocation through its life, to make the rules concrete.
Pages: `H` is the header, `L1`/`L2` leaves, `P1`/`PB` data pages.

| flush | operation | resulting state |
| --- | --- | --- |
| 3 | allocate id 7, write 1000 bytes | `Ref*(7,0,1000,P1)`@3, evicted to `L1`. Fragment `(7,0) → P1+0`. `size` 1000, `size_statement` `Ref*@3`, which holds `F` + `S`, `mentions` 1 |
| 5 | truncate to 10 | `Size(7,10)`@5 in `L2`, since nothing the flush states ends at 10; it becomes the size statement and holds `S` alone. `Ref*@3`'s fragment narrows to `[0,10)`, and it loses `S`: pins 1. `size` 10, `coverage[P1]` −990 |
| 9 | grow to 60, write `[50,60)` | The growth takes `[10,60)` as pending zeros, and the write takes `[50,60)` over them, so the cut states `Zero(7,10,40)`@9 and `Ref*(7,50,10,PB)`@9 in `H`, the `Ref*` as the size statement. `Size@5` loses `S`, owns nothing, and dies, which releases its framing from `L2`. `size` 60, `fragment_count` 3 |
| 10 | resize to 55 | The header states what it holds again as resolved truth: `Zero(7,10,40)`@10 and, narrowed to the new size, `Ref*(7,50,5,PB)`@10, which ends at 55 and so states the size. `coverage[PB]` −5 |

Two things this trace is chosen to show.

**The size costs a statement of its own only at flush 5**, the one resize that writes nothing at the new end.
At flushes 9 and 10 the content statement at the end states it, and the cut can tell from its own output alone, since the size depends on nothing else.

**No statement outlives its use.**
`Size@5` dies the moment flush 9 states the size again, and the zeros `[10,50)` are owned by a statement of the flush that exposed them.

If instead flush 10 had resized to **30**, `Ref*(7,50,10,PB)` would fall entirely outside the new size and be dropped, while the header's zeros narrow to `Zero*(7,10,20)`@10, which ends at 30 and states the size — again without a statement of its own.

### Re-allocation

A separate sequence, since id 7 above is never re-allocated.
`Ref*(12,0,100,P1)`@3, then `Tombstone(12)`@5, then id 12 recycled at flush 7 to 100 bytes with only `[0,10)` written — as `Ref(12,0,10,PX)`@7 **and** `Zero*(12,10,90)`@7, since the new incarnation states every byte below its size — then, with flush 7's statements evicted to a leaf, a truncation to 50 at flush 9, which states `Size(12,50)`@9.

| statement | after flush 7 | after flush 9 |
| --- | --- | --- |
| `Ref*(12,0,100,P1)`@3 | pins **0** — every probe below the size is won by a statement of flush 7, and the size is stated above it | pins **0** |
| `Tombstone(12)`@5 | pins **0** — it carried `S` over as the id's size statement until flush 7 bound `Zero*@7` | pins **0** |
| `Ref(12,0,10,PX)`@7 | pins **1** = `F` | pins **1** = `F` |
| `Zero*(12,10,90)`@7 | pins **2** = `F` + `S` | pins **1** = `F`, over `[10,50)` |
| `Size(12,50)`@9 | — | pins **1** = `S` |

Nothing of the first incarnation stays alive: the second states its size and every byte below it in epochs above the tombstone, so the tombstone dies with the flush that recycles its id, and the first incarnation's statements decide nothing from then on.
