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
		    _ => (size, Fragment::ZeroByDefault),
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

**A tombstone carries no offset, and nonetheless sorts exactly where its match starts.**
[The delta encoding](../spec/address-table.md#delta-encoding) leaves `offset_cursor` untouched for a `Tombstone`, which looks at first like a statement whose position says nothing about its match range of `[0, size)`.
It is not, and the reason is the no-conflicts rule: a tombstone denies its id's existence, every other statement kind asserts it, and a page is a single epoch — so **a tombstone is the only statement naming its id anywhere in its page**.
Its `id_delta` is therefore nonzero, which resets `offset_cursor` to 0, and nothing increments it again.
A tombstone therefore decodes at `offset_cursor == 0`, which *is* its match start.
So the merge needs no special case, and `resolve_id` needs no pre-pass: every statement's sort key is its match start, by three different routes — `offset` for content, `n` for a `Shrink`, and a forced cursor reset for a `Tombstone`.

### Resolving one id

One pass over the buffered group, and no sorting.
The size is not computed separately and then fed to the sweep: it comes *out* of the sweep, along with the fragments, the anchor, and everything else `AllocationMeta` holds.

Each statement matches an interval, and nothing is clipped:

| statement               | match range                                  |
| ----------------------- | -------------------------------------------- |
| `Ref`, `Inline`, `Zero` | `[offset, offset + stmt_size)`               |
| `Shrink(n)`             | `[n, u32::MAX)` — unbounded                  |
| `Tombstone`             | `[0, u32::MAX)` — unbounded                  |
| `Grow(n)`               | `[n, n)` — empty, so it never wins anything  |

`Grow` is the odd one, and giving it an empty interval *at its bound* is what lets it take part at all: it is rejected by `if s.end() > pos`, so it can never shadow anything, while [the delta encoding](../spec/address-table.md#delta-encoding) already sorts it at `n`, which is exactly where the sweep needs to meet it.

The fragment covering a stretch is the matching statement with the highest epoch, so what the sweep computes is the **upper envelope by epoch** of these intervals.

```rust
fn resolve_id(state: &mut State, id: Id, stmts: &mut Peekable<Merge>,
              emitted_fragments: &mut Vec<Fragment>) {
    // `stmts` yields this id's statements in match-start order, tombstones included,
    // and then the next id's; see the merge, above.
    // `open` is a max-heap by epoch. Entries are (epoch, end, statement). By fact 3 it holds
    // at most one entry per distinct epoch at any point in the iteration below, so its size
    // is bounded by the number of live epochs — in practice a handful, never more than P.
    let mut open = MaxHeap::new();
    let mut newest = None;             // highest epoch seen; decides existence
    let mut grow = None;               // largest admitted `Grow`; see below
    let mut mentions = 0;              // physically present statements naming this id
    let mut pos = 0;
    let mut current: Option<Decoded> = None;    // winner of the run starting at `run_start`
    let mut run_start = 0;

    loop {
        while open.peek().is_some_and(|e| e.end <= pos) { open.pop(); }   // lazy expiry
        while stmts.peek().is_some_and(|s| s.id == id && s.start() <= pos) {
            let s = stmts.next().unwrap();
            mentions += 1;             // one per statement consumed, and none is consumed twice
            newest = newest_of(newest, s);
            if s.end() > pos {
                open.push(s.epoch, s.end(), s);
            } else if s.is_grow() && beats(s, open.peek()) {
                grow = Some(s);        // `Grow`s arrive in increasing bound order, so
            }                          // the last admitted one is the largest
        }                              // expired statements fall out here too

        let winner = open.peek();      // None => this stretch resolves to zero by default
        if winner != current {
            if pos > run_start { emit(state, id, run_start..pos, current, emitted_fragments); }
            (current, run_start) = (winner.copied(), pos);
        }
        // Stop once nothing is left to open and the winner can no longer change. An
        // unbounded winner is a `Shrink` or `Tombstone`, and it is then the anchor.
        let next_start = stmts.peek().filter(|s| s.id == id).map(|s| s.start());
        if next_start.is_none() && winner.map_or(true, |w| w.is_unbounded()) { break; }
        // The winner can only change where a statement starts or where it ends.
        // Statements that end while shadowed change nothing and are skipped over.
        pos = min(next_start.unwrap_or(u32::MAX),
                  winner.map_or(u32::MAX, |w| w.end));
    }

    let size = max(run_start, grow.map_or(0, |g| g.n()));
    if run_start < size {              // the tail an admitted `Grow` opened up
        emit(state, id, run_start..size, current, emitted_fragments);
    }
    finish(state, id, mentions, size, newest, current, grow);
}
```

### What the sweep leaves behind

- **`size = max(run_start, the admitted Grow's bound)`.**
  Nothing accumulates content claims; the next subsection is why.
- **The anchor is the winner at termination.**
  The unbounded statements are exactly the `Shrink`s and `Tombstone`s, and the heap orders by epoch, so whichever is on top when the sweep stops is the newest of them — the anchor, by definition, tracked for free.
- **Existence is the one fact the envelope cannot supply.**
  `newest` is therefore maintained in the consumption loop, at one comparison per statement and no extra pass, and `exists` is `!newest.is_tombstone()`.
  The shortcut "the winner at termination is a tombstone and the size is zero" looks equivalent and is not: `Tombstone`@3 followed by `Grow(0)`@5 re-allocates the id at size zero and satisfies it.
- **A non-existent id takes the other exit.**
  No allocation-map entry; a recyclable-id entry carrying `mentions` and the tombstone; and exactly one slab slot, for the tombstone, holding its `A` pin.
  Every other statement of the group was outranked at every probe and gets none.
- **`mentions` is counted in the consumption loop**, one per statement, since every statement the sweep consumes under this id names it and the id's run in the merge is exactly its physically present statements.
  Counting there rather than measuring a buffer afterwards is what lets the group stay unbuffered.
- **The grow witness is the admitted `Grow` exactly when it *determined* the size**, that is when its bound exceeds `run_start`.
  Where the size came from `run_start` instead, it is witnessed by a content statement or by the anchor, both of which are pinned already.

### Why `run_start` is the size

At termination the winner is unbounded or absent, and `run_start` is where that final run began.
**That position is the spec's `max(anchor.n, max{claim : epoch > anchor_epoch})` restricted to statements that match something — and the heap has done the epoch filtering for free:**

- a content statement *older* than the anchor cannot win at or above `n`, because the anchor outranks it there, so it can never push the takeover point past `n`;
- a content statement *newer* than the anchor either wins up to its own end, or is shadowed there by something newer whose claim is at least as large.

With no anchor at all, the winner at termination is `None` and `run_start` is where the envelope ended — the unfiltered maximum, which is correct, because with no anchor nothing is filtered.

Two things follow.
No accumulator over content claims is needed, which is what makes the single pass cheaper than computing the size separately rather than dearer.
And nothing has to be clipped, because a bounded statement can never win above `size`: newer than the anchor, and `size` would already cover its claim; older, and the anchor outranks it there.

### Why `Grow` needs its own rule

The above algorithm has a dedicated branch `else if s.is_grow() && beats(s, open.peek())` because `Grow` would otherwise not be observable: it is never the `winner` because it is (correctly) never even pushed on the heap (`open.push` is gated by `s.start() <= pos` and `s.end() > pos`, which can't both be true for a `Grow` with `start == end`).
Therefore, a `Grow` that is buried inside a sequence of statements and not the `newest` is unobservable after exiting the loop.
For example, consider the following two sequences of statements, which only differ in the bold epoch number:

- `Shrink(10)`@7, `Ref(20, 5)`@11, `Grow(50)`@**3**, `Ref(100, 10)`@5 (correct size: 25) and
- `Shrink(10)`@7, `Ref(20, 5)`@11, `Grow(50)`@**9**, `Ref(100, 10)`@5 (correct size: 50)

Both exit the loop with the same state because `newest` is the same for both (`Ref`@11), the last read statement is the same (`Ref`@5) and thus `pos` and `run_start` exit the loop with the same value, and the only statement that differs between the two sequences (`Grow`@3 vs `Grow`@9) never came up as `winner`.
Thus, a sweep that doesn't explicitly keep track of the leading candidate for the grow witness could not distinguish these two sequences, and it would incorrectly assign the same size to both.

The pair rules out the two lesser repairs as well, which is worth noticing because each looks like it might avoid the branch.
Admitting every `Grow` without the `beats` test would take `Grow(50)`@3 in the first sequence and report 50 where the truth is 25 — the `Shrink`@7 above it is exactly what denies it.
Taking the sweep's final `pos` instead of `run_start` would report 100 for both, since `pos` is dragged out to the last statement's start whatever its epoch.
Only the epoch comparison against what is open at the `Grow`'s own position separates the three answers.

Since the root of the issue is that `Grow` never comes up as `winner` due to its empty range, it might be tempting to assign `Grow` an unbounded range `[n, u32::MAX)` (like `Shrink`) instead.
This would indeed make `Grow` observable in the sweep but wrongly assign fragments to it:
consider `Ref(0, 500)`@3, `Shrink(10)`@5, `Grow(100)`@9, `Ref(150, 50)`@11, which, with an unbounded range for `Grow` would result in a split `Zero` fragment: `[10, 100)` assigned correctly to `Shrink`@5 and `[100, 150)` assigned incorrectly to `Grow`@9.
This would be a shortcoming of the algorithm, not of the spec: the invariant that `Grow` never owns a fragment, which allows it to be consolidated earlier than `Shrink`, is the whole reason why the format distinguishes between `Grow` and `Shrink` instead of defining only a single `Size` statement.

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
  Over `Ref(0, 1000)`@3, `Shrink(500)`@5 and `Ref(0, 600)`@7, the `Shrink` is opened at 500 and shadowed there; only when `Ref`@7 expires at 600 does it surface, which is also where the size turns out to be.

**Nothing denies a truncated statement except the sweep itself**, which is worth stating because the instinct is to look for a clipping rule.
Over `Ref(0, 1000)`@3, `Shrink(10)`@5 and `Ref(100, 20)`@7 the old `Ref`'s interval is left running to 1000; the `Shrink` simply outranks it from 10 upward, so the sweep emits `[0, 10)` to `Ref`@3, `[10, 100)` to the `Shrink`, and `[100, 120)` to `Ref`@7, and stops with the size at 120.
Clipping intervals at the anchor instead would be a bug, because `[0, n)` is precisely where an older statement still wins.

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
  A statement gets a `StatementRef` the first time it wins a stretch, and a statement that never wins one — and is neither the anchor nor the grow witness — is **never given a slot at all**.
  That is what makes the slab hold exactly the live statements, and it is why dead statements cost memory only as the bytes they occupy in their page.
- **Pins are counted, not computed.**
  Each emitted fragment adds one `F` pin to its owner; the anchor and the grow witness each add their `A` pin once, at the end of the group.
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
Two `ZeroByDefault` ranges merge; a `Shrink`-owned zero range and a `ZeroByDefault` one must not.
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
    for &key in run {                                  // Pending → Bytes or ZeroExplicitly
        fragments[key] = fragments[key].stated_by(s, page);
        pin(s);
        if stmt.is_inline() { coverage[page] += length_of(key); }   // payload, per byte
    }
    match stmt {
        Grow { n }                => set_grow_witness(id, s, n),
        Shrink { .. } | Tombstone => set_anchor(id, s),
        _                         => {}
    }
}
```

Everything else a statement implies has already happened by then: the take released the old owners, and the size changes happened when the flush [made them](#mutation-at-flush-time).
What `bind` adds is ownership, framing, and the anchor — the three things that need the statement to exist.

### `grow_size_to(id, n)` and `shrink_size_to(id, n)`

```rust
fn grow_size_to(id: Id, n: u32) {
    let meta = &mut allocations[id];
    if n <= meta.size { return; }
    let exposed = meta.size .. n;
    meta.size = n;
    // The exposed range is owned by the anchor if there is one, else by nobody.
    let f = match meta.anchor {
        Some(a) => { pin(a); ZeroExplicitly { statement: a } }
        None    => ZeroByDefault,
    };
    // Extends the last fragment if it already resolves that way; else one entry.
    insert_or_extend(id, exposed, f);
    retire_dead_grows(id);                             // size > n kills them
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

`grow_size_to` is where the anchor earns its `F` pins, and `retire_dead_grows` is the three `O(1)` death tests from [Liveness](liveness.md#grow-is-locally-decidable-and-shrink-is-not).

### `set_anchor(id, new)`

```rust
fn set_anchor(id: Id, new: StatementRef) {
    if let Some(old) = allocations[id].anchor { unpin(old); }  // may die here
    allocations[id].anchor = Some(new);
    pin(new);
    retire_dead_grows(id);                              // now below anchor_epoch
}
```

## Mutation, at flush time

What the flush calls for each id its fold touched, once per flush and with the fold's net result: each call takes what it changes, applies the size change at once, and leaves the id a record for the cut.

### `write_bytes(id, offset, len, origin)`

```rust
fn write_bytes(id: Id, offset: AllocationOffset, len: u32, origin: Origin, dirty: &mut Dirty) {
    touch(id, dirty);                                 // records the id; sets `last_written`
    grow_size_to(id, offset + len);                   // a write past the end
    take(id, offset..offset + len, Pending::bytes(origin), dirty);
}
```

No page is chosen here: `pack` places the bytes later — unless they are short enough to be stated [`Inline`](flush.md#the-inline-threshold) — and the cut states them.
A write past the current end produces **two** fragments — the gap `[size, offset)` and the written range — which is the only way one write adds two.

### `resize(id, n)`, `allocate(id, n)`, and `free(id)`

```rust
fn resize(id: Id, n: u32, dirty: &mut Dirty) {
    touch(id, dirty);                                 // records the size before the change
    if n > size(id) { grow_size_to(id, n); } else { shrink_size_to(id, n); }
}

fn allocate(id: Id, n: u32, dirty: &mut Dirty) {
    touch(id, dirty);                                 // records that it did not exist
    allocations.insert(id, AllocationMeta::empty());
    grow_size_to(id, n);
}

fn free(id: Id, dirty: &mut Dirty) {
    touch(id, dirty);
    shrink_size_to(id, 0);                            // every fragment goes, and with it `F`
    let meta = allocations.remove(id);
    release_anchor_and_witness(&meta);               // the cut states the tombstone
    ids.recyclable.insert(id, RecyclableId { mentions: meta.mentions, tombstone: None });
}
```

None of them states anything: `touch` records, the first time the flush meets an id, the size and existence it had, and the cut reads that record back.
The allocation-map entry goes away at the free; only `mentions` and, once the cut binds it, the tombstone reference survive, in the recyclable set.

### What the cut states for a touched id

The fragment map says what an id's bytes are, but not how its size and existence changed, so these statements come from the id's record:

| how the id changed during the flush | the cut states |
| --- | --- |
| it existed and was freed | `Tombstone(id)` |
| it existed, was freed, and was allocated again | no tombstone, which would contradict the new incarnation in the same epoch; the fold takes the whole new extent instead, so that `Zero` covers what it did not write, plus `Shrink(id, size)` if the new allocation is smaller than the old one |
| it shrank | `Shrink(id, size)` — always, for the reason [Liveness](liveness.md#emission-when-a-resize-must-write-a-statement) gives |
| it grew, or is new | `Grow(id, size)` unless a statement the cut states for the id reaches `size` — so an allocation of size 0, which nothing reaches, always gets its `Grow(id, 0)`, the only evidence it exists |
| a page this flush retires held its anchor or grow witness | [`replacement_anchor(id)`](#replacement_anchorid--the-one-correctness-obligation), or a fresh `Grow`, unless a row above already states one |

At most one of these per id, so they cannot contradict one another in one epoch, and because the cut evaluates them last, "a statement the cut states reaches `size`" is exact.

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
            unpin(t);                                     // releases A; may die
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
            if is_anchor(stmt) || is_grow_witness(stmt) { dirty.record(stmt.id()).replace = true; }
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
        // `ZeroByDefault` has nothing to state, and a pending fragment is taken already.
        let Some(s) = fragment.statement() else { continue };
        touched.insert(slab.page_of(s));
        take(key.id, key.offset..end_of(key), Pending::in_place(key), dirty);
    }
    // Anchors are invisible in the fragment map, so their replacement is recorded by
    // hand — but only where it pays, since a fresh anchor costs bytes.
    for id in ids_in(lo..hi) {
        if anchor(id).is_some_and(|a| touched.contains(slab.page_of(a))) {
            dirty.record(id).replace = true;
        }
    }
}
```

**No statement is dropped physically here**, and that is deliberate: the old statements stay in their pages, `mentions` does not move, and what changes is that they lose their fragments and therefore their pins.
`unpin` releases their framing from their pages' coverage at the `1 → 0` transition, which is how pages that were never touched become reusable.

`ZeroByDefault` fragments are left as they are, because they are the *absence* of a statement rather than a statement about zero — taking them would make the cut state them as `Zero`, correct on content and wrong on cost, turning free gaps into bytes.

### `owned_fragments(stmt)`

The consolidator finds a statement's fragments by range-scanning the fragment map and keeping those whose owner matches:

| statement kind | scan range |
| --- | --- |
| `Ref`, `Inline`, `Zero` | `(id, offset) .. (id, offset + size)` |
| `Shrink(id, n)`, `Tombstone` | `(id, n) .. (id, size)` — the only probes it can win |
| `Grow` | none; it wins nothing |

It can stop early, because `pins` already says how many to expect: for a content statement `F == pins` exactly, and for a `Shrink` or `Tombstone`, `F == pins − 1` when it is the id's anchor and `== pins` otherwise.

This is not an implementation tax — it *is* the consolidation algorithm.
A page is rewritten as resolved truth, and one cannot emit resolved truth without first determining which parts of it this page is responsible for.
Its cost is predictable in advance from `fragment_count`, which lets the ranking price a candidate before committing to it.

A page rewrite decodes exactly **one** page: its victim.
It never needs an id's physically present statements across the table, because it only rewrites what the victim holds — and a [key-range rewrite](#rewrite_key_rangelo-hi-dirty) decodes none at all, since it reads the fragment map instead.

### `replacement_anchor(id)` — the one correctness obligation

A fragment-map-driven consolidator will faithfully re-emit the content a statement owns and **silently corrupt the file** if that statement was also the anchor, because being the anchor is not a fragment and is therefore invisible in the fragment map.

Concretely: with `Ref(7,0,1000,P1)`@3 in another page and `Shrink(7,10)`@5 in the victim, re-emitting only `Zero(7,10,40)` drops the sole `Shrink`, `anchor_epoch` falls back to `-1`, and the size becomes `max(1000, 50, 60) = 1000` — re-exposing stale bytes across `[10, 1000)`.

> **A page holding a statement with an `A` pin cannot be rewritten without transferring the anchor.**

The replacement depends on what the anchor is doing, and the cut decides it for every id whose record asks for one:

```rust
/// Evaluated at the cut, after every drop this flush makes — so the anchor being
/// replaced is no longer among the id's `mentions`.
fn replacement_anchor(id: Id) -> Option<Statement> {
    match anchor_kind(id) {
        Shrink                        => Some(Shrink { id, n: size(id) }),
        Tombstone if exists(id)       => Some(Shrink { id, n: size(id) }),  // re-allocated
        Tombstone if mentions(id) > 0 => Some(Tombstone { id }),            // still needed
        Tombstone                     => None,                              // nothing left to deny
    }
}
```

A `Shrink` there would resurrect a non-existent id, and stating nothing while some older statement still names the id would let that statement decide existence again.

A `Grow` carries no such obligation, since it anchors nothing — but the grow witness must still be stated again if the victim held it and it is still the sole witness of the size.

### The header

**The header is taken whole at the start of every flush and stated again at the cut**, which is what makes it the address table's [write buffer](flush.md#the-header-as-write-buffer): everything it holds is rewritten every flush at no extra page write, and the cut decides afresh which statements stay in it.

```rust
fn take_header(dirty: &mut Dirty) {
    for stmt in header.statements() {
        for (key, len) in owned_fragments(stmt) {
            take(stmt.id(), key..key + len, Pending::in_place(key).with_heat(clock[key]), dirty);
        }
        if is_anchor(stmt) || is_grow_witness(stmt) { dirty.record(stmt.id()).replace = true; }
        drop_physically(stmt);        // the new header replaces it in the governing world
    }
}
```

It is `rewrite_page` for the one page every flush retires, with one addition: the [eviction clock](flush.md#the-header-as-write-buffer)'s entries ride along as the pending fragments' heat, so that the cut can keep the hottest statements in the header and send the rest to leaves.

Because the header is rewritten unconditionally, a `Shrink` or `Grow` carried in it becomes a *new* statement record at the new epoch each flush, so the anchor re-points and the `A` pin moves every flush.
That is routine rather than churn — header pages are exempt from coverage-driven victim selection — but it is why the anchor must be a movable pin rather than a flag baked into the statement.

### Consolidating a data page

Unlike an address-table page, a data page is opaque bytes with no ids in it, so decoding it tells nothing about who references it.
This is an **unresolved gap**: the design specifies data-page victim selection but not how a flush gets from a victim page number to the `Ref` statements it must re-point.
See [Consolidation](consolidation.md#finding-the-referrers-of-a-data-page) for the candidate answers.

## Worked trace

One allocation through its life, to make the rules concrete.
Pages: `H` is the header, `L1`/`L2` leaves, `P1`/`PB` data pages.

| flush | operation | resulting state |
| --- | --- | --- |
| 3 | allocate id 7, write 1000 bytes | `Ref(7,0,1000,P1)`@3, evicted to `L1`. Fragment `(7,0) → P1+0`. `size` 1000, `anchor` none, `mentions` 1 |
| 5 | truncate to 10 | `Shrink(7,10)`@5 in `L2`. `Ref@3`'s fragment narrows to `[0,10)`; the `Shrink` owns nothing yet (`[10,10)` is empty) and holds `A` alone. `size` 10, `coverage[P1]` −990 |
| 9 | grow to 60, write `[50,60)` | `Ref(7,50,10,PB)`@9 in `H`; the grow emits nothing, since that `Ref` reaches 60. `[10,50)` comes into existence and resolves *through* `Shrink@5`, which therefore **gains** a fragment pin: pins 1 → 2 (`A` + `F`). `size` 60, `fragment_count` 3 |
| 10 | resize to 55 | The header carries `Ref(7,50,10,PB)` forward, but at epoch 10 its extent would exceed the new size, so resolved truth narrows it to `Ref(7,50,5,PB)`@10. The shrink **always** emits, so `Shrink(7,55)`@10 is physically present and becomes the anchor; `Shrink@5` loses `A`, keeps `F` over `[10,50)`, drops to pins 1. `coverage[PB]` −5 |

Two things this trace is chosen to show.

**`Shrink(7,55)` is semantically unnecessary and emitted anyway.**
Without it, `anchor_epoch` stays 5 and the size is `max(10, 50+5) = 55` — the target, reached with no new statement, because the narrowing forced on the tail created a statement ending exactly at the new size.
Semantic necessity and physical presence are different questions, and a state table must answer the second.

**The superfluous statement then has a short life**: it holds `A` alone and dies at the next shrink, which takes `A` from it.

If instead flush 10 had resized to **30**, `Ref(7,50,10,PB)`@9 would fall entirely outside the new size and be dropped rather than narrowed, leaving nothing above epoch 5 and a size of `max(10) = 10`.
There the `Shrink(7,30)` is genuinely required — and the two cases are indistinguishable from inside the flush without the bit that [Liveness](liveness.md#emission-when-a-resize-must-write-a-statement) declines to maintain.

### Re-allocation

A separate sequence, since id 7 above is never re-allocated.
`Ref(12,0,100,P1)`@3, then `Tombstone(12)`@5, then id 12 recycled at flush 7 to 100 bytes with only `[0,10)` written — as `Ref(12,0,10,PX)`@7 **and** `Grow(12,100)`@7, the grow being required since the flush's own output reaches 10, not 100 — then a truncation to 50 at flush 9.

| statement | after flush 7 | after flush 9 |
| --- | --- | --- |
| `Ref(12,0,100,P1)`@3 | pins **0** — every probe it matches is won by the tombstone or by `Ref@7` | pins **0** |
| `Tombstone(12)`@5 | pins **2** = `A` + `F`: still the newest `Shrink`-or-`Tombstone`, and it wins `[10,100)` | loses `A` to `Shrink@9`; keeps `F` over `[10,50)` → pins **1** |
| `Ref(12,0,10,PX)`@7 | pins **1** | pins **1** |
| `Grow(12,100)`@7 | pins **1** = grow witness | **dead** |
| `Shrink(12,50)`@9 | — | pins **1** = `A` |

The tombstone's `F` pin is doing real work: drop it and `[10,50)` would resolve through `Ref(12,0,100,P1)`@3, serving the *first* incarnation's bytes as the second incarnation's content.

`Grow(12,100)`@7 dies by the **third** of its death tests, which nothing else exercises: `size > n` is false (50 < 100) and `n <= anchor.n` is false (100 > 50); what kills it is `Shrink(12,50)`@9 anchoring above its epoch.
