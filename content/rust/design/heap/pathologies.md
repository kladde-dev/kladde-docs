---
title: Pathologies
---

Where greedy descent misbehaves, and the two mechanisms that bound it.

## The guiding principle

Kladde is meant to be used by applications outside the authors' control.
So the workload is treated as **adversarial**, in the oblivious sense: not that an attacker is choosing it, but that no assumption may be made about it beyond what the API allows.

This rules out a whole category of reasoning that would otherwise be tempting.
"That does not happen in our benchmarks" is not an argument, because our benchmarks are not the workload.
The design accepts a small average-case cost in exchange for a bounded worst case.

## The stress test

The pathology that motivated both mechanisms below:

> A resizable allocation sits at a **low address** and is repeatedly **shrunk** by a small amount.

Each shrink opens a small gap just above it, at a very low address.
The frontier is therefore always at that gap.
A frontier slide dutifully slides *everything above it* down by the shrink amount.
The next shrink opens the gap again, and the whole heap slides again.

The measured behaviour: 255,360 bytes copied for 16 shrinks over 15,960 live bytes — and, crucially, **independent of the shrink size**.
Shrinking by one byte sixteen times costs the same as shrinking by a thousand, because the cost is driven by how much lives above the gap, not by how big the gap is.

The greedy policy is not wrong here; each individual slide really is the steepest descent available.
The problem is that the *sequence* of locally optimal moves is globally terrible, because the churning allocation keeps re-creating the same gap.

## Remedy 1: weighting by class

Give fixed-size and resizable allocations different weights in the potential.

$$\Phi_\gamma = \sum_{\text{live bytes } b} \gamma_{\text{class}(b)} \cdot \operatorname{address}(b)$$

Weighting fixed-size bytes more heavily makes the policy prefer moving them, on the grounds that a fixed-size allocation, once placed well, stays placed — whereas a resizable one may have to move again on its next growth.

γ is expressible in the [evacuation index](evacuation-index.md) because it takes **finitely many values**, so the aggregate can partition by class rather than fold the weight into the merge.
That is what costs the four extra slots per class.

γ defaults to 1, which is bit-identical to the unweighted policy.

**What it does not fix:** in the stress test above, the heap has only *one* resizable allocation, so γ scales every competing mover uniformly and has no effect at all.
It addresses a different failure mode — a heap with a mix of churning and stable allocations — and was implemented first because it is the cheaper half.

## Remedy 2: the lift

The mechanism that actually addresses the stress test.

**Diagnosis.** The problem is not any single move; it is that a *specific allocation* keeps re-opening a gap at a low address.
So detect that allocation and move it out of the way — *up*, against the direction compaction normally pushes — so that its future churn happens somewhere that costs less.

**Mechanism.** A sparse `HashMap<Id, u8>` counts how many times each allocation has shrunk.
Resetting the counter is removal, so the map holds only currently-churning allocations and stays small.

When an allocation's count crosses a threshold, it is **lifted**: moved up to a gap that fits it, or to the end, and a **replacement** allocation is moved down into the space it vacated.
That is the `Relocation::Double` case in the [heap trait](relocatable-heap.md#resize-can-ask-for-two-moves) — two copies in a mandatory order, because the replacement's destination is the mover's own old address.

**The trigger** compares the work the churn is causing against the cost of getting rid of it:

```
count × L ≥ T · (s + r_len)     and     count > 1
```

where `L` is the distance the lift would travel, `s` the churning allocation's size, `r_len` the replacement's, and `T` a tuning knob.
Reading it: the left side is the copying the churn has already caused; the right side is what fixing it costs.

**Choosing the replacement** is the largest allocation that fits the vacated space, per class, with γ deciding between classes.
Largest-fit rather than best-gain, because the goal is to fill the space rather than to make progress — a partly filled hole would just become the next frontier.

**Steered descent.** The search for a replacement uses a predicate that is *sound but incomplete*: `max_alloc_addr[class] > floor` proves a candidate might exist above the floor, and never claims one exists when none does.
An incomplete predicate is acceptable here because failing to find a replacement merely means the lift falls back to a plain slide, which is correct if suboptimal.

**Measured:** the lift reduces total copying about fourfold — from 1,995 bytes per byte reclaimed to 505 — and leaves the churning allocation topmost in every enabled configuration.

Both mechanisms default to off (γ = 1, T = 0), and that path is bit-identical to the unweighted policy.

## What is still open

**A generation or age counter.**
Weighting allocations by how long they have sat still, reset on every resize, would generalize the shrink counter from "has shrunk repeatedly" to "is churning at all".
It needs compaction to be able to *swap* runs of allocations rather than only slide them, which is a larger change.

**The reshape conservatism.**
Several places treat "this allocation was resized" as "it may have relocated," because placement is not known until it runs.
That is safe but pessimistic.

**Other pathologies not yet characterized.**
The stress test above was found by reasoning about the policy, not by fuzzing.
A systematic adversarial search over operation sequences — maximizing bytes copied per byte reclaimed — would be a good way to find the next one, and does not exist.
