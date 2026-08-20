---
title: Incremental compaction
---

How the heap closes its own gaps, one bounded step at a time, choosing each step in logarithmic time.

**Status:** implemented, measured, and tuned.
This is the most developed part of kladde-rust.

## The problem

Allocation and freeing leave gaps.
Gaps waste space and, worse, push `end` — the file's length — above the number of bytes actually live.

Compaction moves live bytes downward to close gaps.
The constraints that make it interesting:

- **No stop-the-world.** Every step is bounded and independently valid.
- **Finding the next step must be `O(log n)`**, not just executing it.
  This is the hard part: a scan over allocations to pick the best move would make each step linear, and a full compaction quadratic.
- **Steps must compose** into an actual compaction — repeated application must converge, without cycling or stalling.

## Measuring progress: the potential

The key move is to find a single number that every useful step decreases.

$$\Phi = \sum_{\text{live bytes } b} \operatorname{address}(b)$$

The sum of every live byte's address.
Equivalently, per allocation, $\sum_a \operatorname{size}(a)\cdot(\operatorname{address}(a) + (\operatorname{size}(a)-1)/2)$.

With $L$ total live bytes, $\Phi \geq L(L-1)/2$, with equality for **every** gapless layout regardless of the order allocations happen to sit in.
The excess $\Phi - L(L-1)/2$ counts exactly the (free byte, live byte) pairs where the free byte sits *below* the live byte — the heap's inversions.

So **minimal $\Phi$ ⟺ compact**, and the excess doubles as a fragmentation-debt metric.

A tempting simpler variant — summing allocation *start* addresses rather than every byte's — is subtly wrong: its minimum depends on allocation order, insisting that small allocations come first, and the bias this induces in the greedy policy is actively harmful.

Note that $\Phi$ is the *progress* measure and `end` is the *payoff*, and the payoff lags: `end` only shrinks when a step clears the topmost live byte.

## Pricing a move

Relocating an allocation of size $s$ downward by distance $d$ copies $s$ bytes and makes $\Delta\Phi = s\cdot d$ progress.

Per byte copied, the **gain is $d$** — the travel distance.
That single lens prices every candidate move, which is what makes them comparable.

## The objective, extended

Pure $\Phi$-descent has a blind spot: it does not care about `end` directly, only about addresses.
A move that retires file length is worth more than its $\Phi$ gain suggests, because file length is what the user actually pays for.

So the real objective adds a term:

$$\Phi' = \Phi + K \cdot \mathrm{end}$$

where $K$ is a tuning knob measured in **addresses per byte of file size** — the price of one byte of file length, denominated in potential.
The greedy policy is steepest descent on $\Phi'$ per byte copied.

$K = 0$ recovers pure $\Phi$-descent.
Raising it makes the policy prefer moves that truncate the file over moves that merely tidy its interior.

## The candidate shapes

At any moment the policy considers four kinds of move and takes the best-scoring one.

### Frontier slide

The **compaction frontier** is the lowest gap position.
Everything below it is already at its final address.

A frontier slide takes the run of allocations immediately above the lowest gap and slides it down into that gap.

This is the workhorse, and the choice of *lowest* gap rather than *widest* gap is what makes compaction linear rather than quadratic.
Sliding bottom-up telescopes: each byte is copied once, because every slide extends the already-final prefix.
Any other order re-lengthens runs that later slides must move again.

The measured difference is dramatic — on the endgame benchmark, switching from widest-gap to frontier slides took total copying from 157 MB to 573 KB, and steps from 81,280 to 2,135.
Bytes copied per live byte went from scaling with heap size to a flat 1.03 at every scale.

### End slide

A slide of the topmost run, chosen because it retires `end` rather than because it travels far.
Scored on **`end` bytes retired per byte copied**, converted into the common currency by $K$.

Offered only when the untruncated run fits the budget — a truncated end slide would move bytes without retiring the length that justified it.

### End evacuation

Move the topmost allocation into the lowest gap that fits it.
Retires `end` by exactly that allocation's size, at the cost of copying it once.

### Classic evacuation

Move any allocation into the lowest gap that fits, chosen for travel distance.
This is the move the [evacuation index](evacuation-index.md) exists to find.

Note that a classic evacuation never needs an `end` credit: whenever it would earn one, it coincides with the end evacuation, which is scored anyway.
This was verified byte-identical across 66,907 recorded steps.

## Placement is the same move, for free

An allocation's initial placement is a compaction step with the copy already paid for.

Placing a new allocation in the lowest gap that fits it is exactly the evacuation move, except that no bytes need moving — they were going to be written anyway.
So good placement is free compaction, and it uses the same index and the same query.

See [Placement](placement.md).

## Why it terminates

Every step strictly decreases $\Phi'$, which is a non-negative integer bounded below.
So the process cannot cycle and cannot run forever.

It also cannot stall: if any gap exists below any live byte, a frontier slide with positive gain exists.
The policy returns `None` — quiescence — only when no such pair remains, which is exactly compactness.

## The budget

A step's cost is its `len`: the number of bytes copied.

The budget is a **ranking input**, not a cap.
A step exceeding the remaining budget is executed only when nothing has moved yet, so a single oversized move can never be starved forever but also cannot blow the budget on top of work already done.

Callers run bounded rounds — a flush attempts a fixed number of bytes of compaction work and stops.

## What was measured

Three workloads — growing, shrinking, and quiescing heaps — recorded per-step across several orders of magnitude of heap size.
The headline results:

- Frontier slides make total copying linear in live bytes, at all scales.
- The endgame (driving an already-mostly-compact heap to fully compact) went from 7.51 s to 63 ms.
- Adding the `end` term with a plausible $K$ improved file-length payoff without measurably hurting $\Phi$ descent.

A caveat worth recording for anyone re-measuring: build-to-build timing drift on the development host is over 30%, while within-burst variance is ±3%.
Several early conclusions about the cost of small code changes turned out to be measuring the drift.
Compare alternating builds, not sequential ones.

## Further reading

- [The evacuation index](evacuation-index.md) — how the best candidate is found in `O(log n)`.
- [Pathologies](pathologies.md) — where greedy descent misbehaves, and what bounds it.
