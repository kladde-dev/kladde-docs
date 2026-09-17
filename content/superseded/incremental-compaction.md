---
title: Incremental compaction
---

**Superseded.** Replaced by [consolidation](../impl/consolidation.md).

How the [relocatable heap](relocatable-heap.md) closed its own gaps, one bounded step at a time, choosing each step in logarithmic time.
This was the most developed part of the old implementation: built, benchmarked across several orders of magnitude, and tuned.

It is gone because there is no address ordering left to descend: content lives in pages, nothing is adjacent to anything, and "moving a byte downward" is not a meaningful operation.
What replaced it is the log-structured live-fraction rule, which is a much simpler policy over a much simpler geometry.

Three things in it are worth keeping, and are called out below: the **no-stop-the-world requirement**, the **adversarial-workload stance**, and the **measurement methodology**.

## The constraints

- **No stop-the-world.** Every step bounded and independently valid.
- **Finding the next step must be `O(log n)`**, not just executing it.
  A scan over allocations to pick the best move would make each step linear, and a full compaction quadratic.
- **Steps must compose** into an actual compaction — repeated application must converge, without cycling or stalling.

The no-stop-the-world requirement was a hard one rather than a nicety: kladde is meant to sit under interactive applications, and a multi-second pause while a large heap is rearranged is not acceptable.
It also forced a genuinely useful property — because every step is independently valid, the heap is always consistent, so a crash mid-compaction costs at most one step.

**This requirement survives**, and the page design satisfies it more easily: a flush's consolidation work is bounded by a budget, and a crashed flush costs nothing at all.

## The potential

The key move was to find a single number that every useful step decreases.

$$\Phi = \sum_{\text{live bytes } b} \operatorname{address}(b)$$

The sum of every live byte's address.
Equivalently, per allocation, $\sum_a \operatorname{size}(a)\cdot(\operatorname{address}(a) + (\operatorname{size}(a)-1)/2)$.

With $L$ total live bytes, $\Phi \geq L(L-1)/2$, with equality for **every** gapless layout regardless of the order allocations happen to sit in.
The excess counts exactly the (free byte, live byte) pairs where the free byte sits *below* the live byte — the heap's inversions.
So **minimal $\Phi$ ⟺ compact**, and the excess doubled as a fragmentation-debt metric.

A tempting simpler variant — summing allocation *start* addresses rather than every byte's — is subtly wrong: its minimum depends on allocation order, insisting that small allocations come first, and the bias this induces in the greedy policy is actively harmful.

Relocating an allocation of size $s$ downward by distance $d$ copies $s$ bytes and makes $\Delta\Phi = s\cdot d$ progress, so **the gain per byte copied is $d$** — the travel distance.
That single lens priced every candidate move, which is what made them comparable.

$\Phi$ was the *progress* measure and `end` the *payoff*, and the payoff lags: `end` only shrinks when a step clears the topmost live byte.
So the real objective added a term:

$$\Phi' = \Phi + K \cdot \mathrm{end}$$

where $K$ is a tuning knob in **addresses per byte of file size**.
$K = 0$ recovers pure $\Phi$-descent.

## The candidate shapes

Four kinds of move, best-scoring one taken.

**Frontier slide** — take the run of allocations immediately above the lowest gap and slide it down into that gap.
The workhorse, and the choice of *lowest* gap rather than *widest* gap is what made compaction linear rather than quadratic: sliding bottom-up telescopes, because every slide extends the already-final prefix, so each byte is copied once.

The measured difference was dramatic — on the endgame benchmark, switching from widest-gap to frontier slides took total copying from 157 MB to 573 KB, and steps from 81,280 to 2,135.
Bytes copied per live byte went from scaling with heap size to a flat 1.03 at every scale.

**End slide** — a slide of the topmost run, chosen because it retires `end` rather than because it travels far.
Offered only when the untruncated run fits the budget, since a truncated end slide moves bytes without retiring the length that justified it.

**End evacuation** — move the topmost allocation into the lowest gap that fits it.

**Classic evacuation** — move any allocation into the lowest gap that fits, chosen for travel distance.
A classic evacuation never needed an `end` credit: whenever it would earn one it coincides with the end evacuation, which is scored anyway — verified byte-identical across 66,907 recorded steps.

**Why it terminated.** Every step strictly decreases $\Phi'$, a non-negative integer bounded below, so the process cannot cycle or run forever.
It also could not stall: if any gap exists below any live byte, a frontier slide with positive gain exists, so quiescence is exactly compactness.

## The evacuation index

The data structure that answered "what is the best move?" in `O(log n)` — an augmented B+ tree holding allocations **and** gaps in one ordered structure, keyed by a triple:

```
( (size << 2) | (is_gap << 1) | is_fixed,  score,  address )
```

The crucial property is that **the key order is the validity relation**: for a gap `G` and an allocation `A`,

$$\operatorname{key}(G) > \operatorname{key}(A) \iff G.\text{width} \geq A.\text{size}$$

So "does this gap fit this allocation?" is a key comparison, "the set of gaps that fit `A`" is a suffix of the tree, and a maximum-over-*pairs* query becomes a range query.
The bit layout made the ordering within one size *resizable allocation, fixed allocation, gap*, so a gap sorts above every allocation it can hold, including one of exactly its own size.

Each node carried a subtree summary with `min_gap_pos`, `max_gap_pos`, and per-class best-mover and best-pairing fields.
`min_gap_pos` at the root *was* the compaction frontier.

### The lesson worth keeping

Adding a class cost **seven** scalar slots rather than the three one might expect — the node grew from 72 to 112 bytes — and the reason generalises.

The class weight **cannot enter the merge**: the tree's aggregation function is a pure fold over subtree summaries and has no access to a runtime parameter.
So the merge had to keep each class's best *unweighted*, and the weighting was applied afterwards, at the root, by a separate scoring step.
Roughly half the aggregate's growth existed purely to defer the weighting.

> A **multiplicative** weight that takes finitely many values can be expressed by *partitioning* the aggregate — one slot per value — rather than by linearising it into the merge.

That works precisely because the class count is small and fixed, and it is the kind of thing worth remembering the next time a tunable has to live inside an augmented tree.

A related saturation subtlety: saturate *before* scaling, or a weight turns a wrapped difference into a large positive.

The implementation used the `sweep-bptree` crate, which appears unmaintained.
The augmentation API it provided — `Argument`, `from_leaf`, `from_inner`, `descend_visit` — is small, and the expectation was to vendor or replace it eventually; nothing in the design depended on that crate beyond the shape of its augmentation hook.

**Maintenance cost** was the dominant CPU cost of a step: every move re-keys the moved allocations and adjusts the neighbouring gaps, roughly sixteen index operations per allocation moved, of which two are irreducible.
That is why steps were expressed as **runs** rather than single allocations.

## Pathologies

### The stance

Kladde is meant to be used by applications outside the authors' control, so the workload is treated as **adversarial** in the oblivious sense: not that an attacker is choosing it, but that no assumption may be made about it beyond what the API allows.

This rules out a category of reasoning that would otherwise be tempting.
"That does not happen in our benchmarks" is not an argument, because our benchmarks are not the workload.
The design accepts a small average-case cost in exchange for a bounded worst case.

**This stance survives** and applies to every policy decision in the current design.

### The stress test

> A resizable allocation sits at a **low address** and is repeatedly **shrunk** by a small amount.

Each shrink opens a small gap just above it, at a very low address, so the frontier is always at that gap, and a frontier slide dutifully slides *everything above it* down.
The next shrink opens the gap again, and the whole heap slides again.

Measured: 255,360 bytes copied for 16 shrinks over 15,960 live bytes — and, crucially, **independent of the shrink size**, because the cost is driven by how much lives above the gap, not by how big the gap is.

The greedy policy was not wrong here; each individual slide really was the steepest descent available.
The problem is that the *sequence* of locally optimal moves is globally terrible, because the churning allocation keeps re-creating the same gap.

### Remedy 1: weighting by class

Give fixed-size and resizable allocations different weights $\gamma$ in the potential, on the grounds that a fixed-size allocation, once placed well, stays placed.
Expressible in the index precisely because $\gamma$ takes finitely many values, which is what cost the four extra slots per class.

**What it does not fix:** in the stress test the heap has only *one* resizable allocation, so $\gamma$ scales every competing mover uniformly and has no effect at all.
It addressed a different failure mode and was implemented first because it is the cheaper half.

### Remedy 2: the lift

The mechanism that actually addressed the stress test.

**Diagnosis:** the problem is not any single move; it is that a *specific allocation* keeps re-opening a gap at a low address.
So detect that allocation and move it out of the way — *up*, against the direction compaction normally pushes.

**Mechanism:** a sparse `HashMap<Id, u8>` counts how many times each allocation has shrunk; resetting the counter is removal, so the map holds only currently-churning allocations and stays small.
When a count crosses a threshold the allocation is **lifted** to a gap that fits it, or to the end, and a **replacement** allocation is moved down into the space it vacated.

**The trigger** compares the work the churn is causing against the cost of getting rid of it:

```
count × L ≥ T · (s + r_len)     and     count > 1
```

where `L` is the distance the lift would travel, `s` the churning allocation's size, `r_len` the replacement's, and `T` a tuning knob.
The left side is the copying the churn has already caused; the right side is what fixing it costs.

**Choosing the replacement** is largest-fit rather than best-gain, because the goal is to fill the space rather than to make progress — a partly filled hole would just become the next frontier.
The search used a predicate that is *sound but incomplete*: `max_alloc_addr[class] > floor` proves a candidate might exist and never claims one exists when none does.
An incomplete predicate was acceptable because failing to find a replacement merely means the lift falls back to a plain slide.

**Measured:** the lift reduced total copying about fourfold — from 1,995 bytes per byte reclaimed to 505 — and left the churning allocation topmost in every enabled configuration.

Both mechanisms defaulted to off ($\gamma = 1$, $T = 0$), and that path was bit-identical to the unweighted policy.

## Measurement methodology

Worth keeping, because it applies to anything measured later.

Three workloads — growing, shrinking, and quiescing heaps — recorded per-step across several orders of magnitude of heap size.
Headline results: frontier slides make total copying linear in live bytes at all scales; the endgame went from 7.51 s to 63 ms; adding the `end` term with a plausible $K$ improved file-length payoff without measurably hurting $\Phi$ descent.

> **Build-to-build timing drift on the development host exceeded 30 %, while within-burst variance was ±3 %.**
> Several early conclusions about the cost of small code changes turned out to be measuring the drift.
> Compare alternating builds, not sequential ones.

## What was still open when it was retired

- **A generation or age counter**, weighting allocations by how long they had sat still, to generalise the shrink counter from "has shrunk repeatedly" to "is churning at all". It needed compaction to be able to *swap* runs rather than only slide them.
- **The reshape conservatism**: several places treated "this allocation was resized" as "it may have relocated", because placement is not known until it runs. Safe but pessimistic — and the same conservatism reappears in the current design's [hoist condition](../impl/flush.md#why-the-graph-may-be-unnecessary).
- **Other pathologies not yet characterized.** The stress test was found by reasoning about the policy, not by fuzzing. A systematic adversarial search over operation sequences, maximizing bytes copied per byte reclaimed, would be a good way to find the next one, and did not exist.

That last item is the one most worth carrying forward: the current consolidation policy has had no equivalent adversarial search either.
