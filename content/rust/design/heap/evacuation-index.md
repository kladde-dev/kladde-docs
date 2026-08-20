---
title: The evacuation index
---

The data structure that answers "what is the best compaction move?" in `O(log n)`.

**Status:** implemented.

## Why an ordinary map is not enough

The compaction step must be found in logarithmic time, not merely executed in it.
A scan over allocations to pick the best candidate would make each step linear in the number of allocations, and a full compaction quadratic — which is exactly the wall this design exists to avoid.

So the index has to answer, without scanning:

- *what is the lowest gap?* — the compaction frontier;
- *what is the lowest gap that fits `n` bytes?* — for placement and evacuation;
- *what is the highest gap?* — for end-shaped moves;
- *which allocation-into-gap pairing has the best gain?* — the evacuation candidate.

The last one is the demanding query, because it is a maximum over *pairs*.

## One tree, both kinds

The index holds allocations **and** gaps in a single ordered structure — an augmented B+ tree, currently over the `sweep-bptree` crate.

The key is a triple:

```
( (size << 2) | (is_gap << 1) | is_fixed,  score,  address )
```

The crucial property is that **the key order is the validity relation**.
For a gap `G` and an allocation `A`:

$$\operatorname{key}(G) > \operatorname{key}(A) \iff G.\text{width} \geq A.\text{size}$$

So "does this gap fit this allocation?" is answered by a key comparison, and "the set of gaps that fit `A`" is a suffix of the tree.
That is what turns a pair query into a range query.

The bit layout is chosen so the ordering within one size is *resizable allocation, fixed allocation, gap* — which makes a gap sort above every allocation it can hold, including one of exactly its own size.
The size field is shifted by two, so addresses are capped at $2^{62}$.

## The aggregate

Each node carries a summary of its subtree:

```rust
struct Aggregate {
    min_gap_pos: u64,
    max_gap_pos: u64,
    max_alloc_score: [u64; CLASSES],
    max_alloc_addr:  [u64; CLASSES],
    max_alloc_size:  [u32; CLASSES],
    best:      [u64; CLASSES],
    best_from: [u64; CLASSES],
    best_to:   [u64; CLASSES],
    best_len:  [u32; CLASSES],
}
```

`min_gap_pos` at the root *is* the compaction frontier: everything below it is at its final address.

The `best*` fields carry the best allocation-into-gap pairing found so far, which the merge extends leftward — an allocation in a left subtree can pair with any fitting gap in a right subtree, because fitting gaps are exactly the keys above it.

## Why seven slots per class, not three

The aggregate is per-class, and adding a class costs seven scalar slots rather than the three one might expect.
Measured, the node grew from 72 to 112 bytes.

The obvious three are `max_alloc_score`, `max_alloc_addr`, and `max_alloc_size` — the running best mover in that class.

The other four exist because the class weight **cannot enter the merge**.
The tree's aggregation function is a pure fold over subtree summaries; it has no access to a runtime parameter.
So the merge must keep each class's best *unweighted*, and the weighting is applied afterwards, at the root, by a separate scoring step:

```
fn scored_step(self, γ) -> Option<(gain, Step)>:
    # Saturate *before* scaling: an upward pair must collapse to 0 first,
    # or γ turns a wrapped difference into a large positive.
    return argmax over classes c with best[c] > 0 of
        ( γ[c].saturating_mul(self.best[c]),
          Step { from: best_from[c], to: best_to[c], len: best_len[c] } )
```

So roughly half the aggregate's growth exists purely to defer the weighting.

This is the general lesson for augmenting a tree with a tunable: a **multiplicative** weight that takes finitely many values can be expressed by *partitioning* the aggregate — one slot per value — rather than by linearising it into the merge.
That works precisely because the class count is small and fixed.

## The queries

| query | visitor |
| --- | --- |
| lowest gap | `min_gap_pos` at the root, `O(1)` |
| highest gap | `max_gap_pos` at the root, `O(1)` |
| lowest gap fitting `n` | descend on the key boundary for `n` |
| highest gap fitting `n` | mirror of the above |
| best evacuation | read the root aggregate, apply γ |
| best evacuation within a budget | a descending visitor that prunes on `len` |
| largest allocation of class `c` fitting width `w` above address `f` | a rightmost-above visitor |

The descending visitors use the tree's `DescendVisit` protocol, returning `GoDown`, `Cancel`, or `Complete` at each node — so a query that can be answered from an aggregate never touches the leaves.

## Maintenance cost

Every allocation move re-keys the moved allocations and adjusts the neighbouring gaps.
That is roughly sixteen index operations per allocation moved, of which two are irreducible.

This is the dominant cost of a compaction step in CPU terms, and it is why steps are expressed as **runs** of contiguous allocations rather than single ones: the per-operation overhead is amortized across the run.

An `α == 0` short-circuit skips the neighbour-tracking work entirely when the fragmentation term is disabled, which is the common configuration.

## Dependency note

`sweep-bptree` appears unmaintained.
The augmentation API it provides — `Argument`, `from_leaf`, `from_inner`, `descend_visit` — is small, and the expectation is to vendor it or replace it eventually.
Nothing in the design depends on that particular crate beyond the shape of its augmentation hook.
