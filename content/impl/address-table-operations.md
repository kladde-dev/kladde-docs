---
title: Address-table operations
---

Pseudocode for every operation on the [in-memory state](in-memory-state.md).

Two levels are in play and it is worth separating them before reading any of this.
The **application level** mutates a value's native in-memory representation and appends a record to the journal; it does not touch the fragment map at all.
The **storage level** — everything below — describes what the *file* currently says, and changes only when a flush writes statements or a page is rewritten.
So `write_bytes` below is not what `vec.push()` calls; it is what a flush calls once, after folding a whole journal segment.

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

    let end = match fragments.range((id, probe+1)..).next() {
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
fn read(id: Id, offset: AllocationOffset, len: u32) -> Result<Bytes> {
    let mut out = Bytes::with_capacity(len);
    let mut at = offset;
    while at < offset + len {
        let (fragment, range) = resolve(id, at).ok_or(OutOfBounds)?;
        let take = min(range.end, offset + len) - at;
        match fragment {
            Bytes { page, offset: po, .. } => {
                let skip = at - range.start;
                out.extend(page_image(page)[po + skip ..][.. take]);
            }
            UndefinedExplicitly { .. } | UndefinedByDefault => out.extend_undefined(take),
        }
        at += take;
    }
    Ok(out)
}
```

`O(log F + k)` for `k` fragments spanned.
The first `resolve` is the only search; afterwards the cursor walks.

### `size(id)` and `exists(id)`

```rust
fn size(id: Id)   -> u32  { allocations.get(id).map_or(0, |m| m.size) }
fn exists(id: Id) -> bool { allocations.contains(id) }
```

`O(1)` both, because `size` is maintained incrementally rather than recomputed.

## Opening a file

```rust
fn open(file) -> State {
    let header = pick_header(file)?;          // CRC-valid, highest epoch
    let mut state = State::empty();

    // 1. Walk the address-table tree, collecting every statement.
    //    The tree is a directory, never searched; a plain traversal suffices.
    let mut seen_pages = Set::new();
    let mut stmts = Vec::new();
    let mut queue = vec![header.page];
    while let Some(p) = queue.pop() {
        if !seen_pages.insert(p) { return Err(NotATree); }
        let page = decode(file.page(p))?;     // validates CRC and framing
        if page.epoch > header.epoch { return Err(FsyncContractViolated); }
        state.pages.insert(p, page.kind, page.epoch);
        queue.extend(page.children);
        for s in page.statements { stmts.push((s, p, page.epoch)); }
    }

    // 2. Resolve. Group by id; within an id, apply the rules of the spec.
    for (id, group) in stmts.group_by_id() {
        state.allocations.insert(id, resolve_allocation(id, group));
    }

    // 3. Count coverage from the winners, and initialise pins.
    //    Both fall out of step 2 and cost nothing extra.

    // 4. Rebuild the id allocator and the eviction clock.
    state.ids = IdAllocator::from(&state.allocations, &tombstones);

    // 5. Replay the journal segment the header names, stopping at the
    //    first transaction whose chained CRC fails.
    for txn in file.journal(header.next_segment).valid_prefix() { replay(txn); }
    state
}
```

`O(n)` in live bytes.
Step 2 is where epochs are consulted, and it is the only place they ever are.

### `resolve_allocation(id, statements) -> AllocationMeta`

This is the spec's resolution rules, made operational.

```rust
fn resolve_allocation(id: Id, stmts: Vec<(Statement, Page, Epoch)>) -> AllocationMeta {
    // Existence and the anchor.
    let newest = stmts.max_by_key(|s| s.epoch);
    if newest.is_tombstone() && no_statement_above(newest) { return NonExistent; }

    let anchor = stmts.filter(|s| s.is_shrink() || s.is_tombstone())
                      .max_by_key(|s| s.epoch);
    let anchor_epoch = anchor.map_or(-1, |a| a.epoch);

    // Size: the maximum over the anchor's n and everything strictly above it.
    let mut size = anchor.map_or(0, |a| a.n());          // Tombstone contributes 0
    for s in stmts.filter(|s| s.epoch > anchor_epoch) {
        size = max(size, s.size_claim());                // Grow: n; content: offset+size
    }

    // Content: for each probe, the highest-epoch matcher. Done as a sweep
    // rather than per probe: sort the matchers and walk the boundaries.
    let fragments = sweep_winners(stmts, anchor_epoch, size);

    // Pins fall out: one F per fragment owned, plus A for the anchor and the
    // grow witness. Everything with zero pins is discarded, never recorded.
    AllocationMeta { size, anchor, grow_witness: witness(stmts, size), .. }
}
```

The content sweep is the only non-obvious part, and it is an ordinary interval problem: collect each statement's matching range (`[offset, offset+size)` for content, `[n, size)` for a `Shrink`, `[0, size)` for a `Tombstone`), sort the boundaries, and walk them keeping the highest-epoch matcher currently open.
`O(m log m)` for `m` statements naming the id.

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
        other => other,                              // Undefined carries no offset
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
        if let Bytes { page, .. } = old { coverage[page] -= length_of(key); }
    }
    if let Some(s) = new.statement() { pin(s); }
    fragments.insert((id, range.start), new);
    allocations[id].fragment_count += 1;
    coalesce_around(id, range);
}
```

The only non-trivial primitive: split at both boundaries, drop what is strictly inside, insert the new one.

### `coalesce_around(id, range)`

Merge each new boundary with its neighbour when both fragments resolve identically **and** name the same statement.
Two `UndefinedByDefault` ranges merge; a `Shrink`-owned `Undefined` and an `UndefinedByDefault` must not.
Two `Bytes` fragments merge only when they are contiguous in the same page *and* owned by the same statement.

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

## Applying a statement

What a flush does for each statement it writes, and what a page rewrite does for each statement it re-emits.

```rust
fn apply(stmt: Statement, page: PageNumber, framing: u8) {
    let id = stmt.id();
    let s = slab.alloc(page, framing);                 // pins = 0 for now
    allocations[id].mentions += 1;
    allocations[id].statement_bytes += framing;
    coverage[page] += framing;

    match stmt {
        Ref { offset, size, address } => {
            grow_size_to(id, offset + size);
            overwrite(id, offset..offset+size,
                      Bytes { page: address.page, offset: address.offset, statement: s });
        }
        Inline { offset, size, .. } => {                // payload lives in `page`
            grow_size_to(id, offset + size);
            overwrite(id, offset..offset+size,
                      Bytes { page, offset: payload_offset(stmt), statement: s });
            coverage[page] += size;                     // payload charged per byte
        }
        Undefined { offset, size } => {
            grow_size_to(id, offset + size);
            overwrite(id, offset..offset+size, UndefinedExplicitly { statement: s });
        }
        Grow { n }   => { grow_size_to(id, n); set_grow_witness(id, s, n); }
        Shrink { n } => { set_anchor(id, s); shrink_size_to(id, n); }
        Tombstone    => { set_anchor(id, s); tombstone(id, s); }
    }
}
```

### `grow_size_to(id, n)` and `shrink_size_to(id, n)`

```rust
fn grow_size_to(id: Id, n: u32) {
    let meta = &mut allocations[id];
    if n <= meta.size { return; }
    let exposed = meta.size .. n;
    meta.size = n;
    // The exposed range is owned by the anchor if there is one, else by nobody.
    let f = match meta.anchor {
        Some(a) => { pin(a); UndefinedExplicitly { statement: a } }
        None    => UndefinedByDefault,
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

### `write_bytes(id, offset, bytes)`

The flush has already decided which data page the bytes go to; this records the effect.

```rust
fn write_bytes(id: Id, offset: AllocationOffset, len: u32, dest: Address) {
    emit(Ref { id, offset, size: len, address: dest });
    // `apply` above does the rest: grow_size_to covers a write past the end,
    // overwrite re-owns the range and releases whatever held it before.
}
```

A write past the current end produces **two** fragments — the gap `[size, offset)` and the written range — which is the only way one statement adds two.

### `resize(id, n)`

```rust
fn resize(id: Id, n: u32) {
    let old = size(id);
    if n > old {
        // Emit only if nothing this flush writes reaches n. Exact and O(1).
        if !this_flush_reaches(id, n) { emit(Grow { id, n }); }
        else                          { grow_size_to(id, n); }
    } else if n < old {
        emit(Shrink { id, n });        // always; see Liveness, "Emission"
    }
}
```

### `allocate(id, n)`

```rust
fn allocate(id: Id, n: u32) {
    allocations.insert(id, AllocationMeta::empty());
    if n == 0 { emit(Grow { id, n: 0 }); }   // the only evidence it exists
    else      { resize(id, n); }             // a grow from 0
}
```

### `free(id)`

```rust
fn free(id: Id) {
    emit(Tombstone { id });
    // apply() sets the anchor, which releases the previous anchor's pin;
    // shrink_size_to(0) then destroys every fragment, so every content
    // statement for the id drops to zero pins in the same step.
    let meta = allocations.remove(id);
    ids.recyclable.insert(id, RecyclableId {
        mentions: meta.mentions,             // after this flush's own drops
        tombstone: Some(the_new_tombstone),
    });
}
```

The allocation-map entry goes away at the free; only `mentions` and the tombstone reference survive, in the recyclable set.

**Free and re-allocate in one flush** is the one case that emits no tombstone, since a tombstone and the new incarnation would make contradicting existence claims in one epoch.
Emit statements that fully cover the new extent instead — `Undefined(id, 0, n)`, plus `Shrink(id, n)` if the new allocation is smaller than the old one.

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

Both eviction and consolidation are the same operation — **rewrite a page as resolved truth** — differing only in which page and why.

### `rewrite_page(victim) -> new_page`

```rust
fn rewrite_page(victim: PageNumber) -> PageNumber {
    let target = allocate_reusable_page();
    let mut out = Vec::new();

    for stmt in decode(victim).statements {
        let id = stmt.id();
        match keep(stmt) {
            Keep::Drop        => drop_physically(stmt),
            Keep::AsResolved  => {
                for f in owned_fragments(stmt) { out.push(restate(f, target)); }
                if is_anchor(stmt) { out.push(replacement_anchor(id)); }
                drop_physically(stmt);
            }
        }
    }

    out.sort_by_key(|s| (s.id(), s.offset()));   // required by the delta encoding
    write_page(target, out, epoch: current);
    for s in out { apply(s, target, framing_of(s)); }
    target
}
```

### `owned_fragments(stmt)`

The consolidator finds a statement's fragments by range-scanning the fragment map and keeping those whose owner matches:

| statement kind | scan range |
| --- | --- |
| `Ref`, `Inline`, `Undefined` | `(id, offset) .. (id, offset + size)` |
| `Shrink(id, n)`, `Tombstone` | `(id, n) .. (id, size)` — the only probes it can win |
| `Grow` | none; it wins nothing |

It can stop early, because `pins` already says how many to expect: for a content statement `F == pins` exactly, and for a `Shrink` or `Tombstone`, `F == pins − 1` when it is the id's anchor and `== pins` otherwise.

This is not an implementation tax — it *is* the consolidation algorithm.
A page is rewritten as resolved truth, and one cannot emit resolved truth without first determining which parts of it this page is responsible for.
Its cost is predictable in advance from `fragment_count`, which lets the ranking price a candidate before committing to it.

Consolidation decodes exactly **one** page: its victim.
It never needs an id's physically present statements across the table, because it only rewrites what the victim holds.

### `replacement_anchor(id)` — the one correctness obligation

A fragment-map-driven consolidator will faithfully re-emit the content a statement owns and **silently corrupt the file** if that statement was also the anchor, because being the anchor is not a fragment and is therefore invisible in the fragment map.

Concretely: with `Ref(7,0,1000,P1)`@3 in another page and `Shrink(7,10)`@5 in the victim, re-emitting only `Undefined(7,10,40)` drops the sole `Shrink`, `anchor_epoch` falls back to `-1`, and the size becomes `max(1000, 50, 60) = 1000` — re-exposing stale bytes across `[10, 1000)`.

> **A page holding a statement with an `A` pin cannot be rewritten without transferring the anchor.**

The replacement depends on what the anchor is doing:

```rust
fn replacement_anchor(id: Id) -> Statement {
    match anchor_kind(id) {
        Shrink                      => Shrink { id, n: size(id) },
        Tombstone if exists(id)     => Shrink { id, n: size(id) },  // re-allocated
        Tombstone if mentions(id) > 1 => Tombstone { id },          // still needed
        Tombstone                   => Nothing,                     // last statement
    }
}
```

A `Shrink` there would resurrect a non-existent id, and emitting nothing when `mentions > 1` would let an older statement decide existence again.

A `Grow` carries no such obligation, since it anchors nothing — but the grow witness must still be re-emitted if the victim held it and it is still the sole witness of the size.

### Eviction from the header

The header is rewritten every flush and states resolved truth, so it is a **write buffer**: every statement a flush produces lands there first, at zero additional page writes.
When it overflows, the coldest statements are evicted into a fresh leaf.

```rust
fn evict(n_bytes: usize) -> PageNumber {
    let batch = eviction_clock.coldest(n_bytes);   // flushes gone untouched
    let target = allocate_reusable_page();
    // Eviction re-stamps: a statement moving into a page of epoch E becomes a
    // statement at epoch E. Since the header always held resolved truth, the
    // evicted statements are already narrowed and need no re-resolution.
    write_page(target, batch, epoch: current);
    for s in batch { reassign_page(s, target); }
    target
}
```

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
