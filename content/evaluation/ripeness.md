---
title: Cleaning by ripeness
---

**Cleaning by ripeness, as [the draft](../drafts/ripeness.md) proposes, keeps a file under skewed overwrites 9 to 11 % smaller than the current design for the same writes, or writes 4 to 20 % less for the same file size; under uniform overwrites the two are level, as the draft predicts.**
At the default settings of each, the 8 and 64 MiB skewed files come out 6 to 7 % smaller for 10 % fewer writes.
It gains where the draft says it should: under skew, [the current design](consolidation.md)'s data pages lose fill for most of a run as pages freeze above the churn floor, and ripeness's hold at 0.76.

It took one change to the draft to work: the controller that sets the price of space must measure the fill of the data pages and leaves, not of the whole file, or the price swings over decades.
Three costs remain.
Recording every page's estimate in the consolidator state takes 2 to 7 % of the writes.
Under churn the file comes out 18 % larger.
And after a mass free the price takes a dozen flushes to climb back from where calm phases left it, so the file peaks higher.

## What was measured

**The [same benchmark](consolidation.md#what-was-measured) ran on the `ripeness` branch of kladde-rust, and, back to back with it, on main, so that their times compare.**
Main is at commit `cb34576`, as on [the consolidation page](consolidation.md); the branch is at `f7d4eab` and adds [the draft](../drafts/ripeness.md) on top of it.
The benchmark is deterministic but for its times: main's run here reproduced every other number of the consolidation page exactly.
A third run of the branch, at `d87d3ad`, left the consolidator state out, to measure what it costs.
The experiments this page cites besides, on the controller and on churn, ran on the branch changed for the purpose, and are not part of the committed code.

The branch fills in what the draft leaves open with a forgetting rate `β = 0.1` per flush, a floor `R_MIN = 10⁻⁴` and a starting price `κ = 0.01` (the draft's own examples), a cursor that gives up pages older than `W = 8` flushes, and a budget of 256 pages per flush, which is now a fixed cap.
Its controller aims the data pages and leaves at a fill `τ = 0.75` by default, where main aims the whole file at 0.8 and never gets there.
`implementation-notes.md` in kladde-rust lists every departure from the draft.

Where main's page compares policies, it uses **steady states**: the file size averaged over the second half of each run, and the bytes written during that half per byte the application wrote.
Both controllers take a live size or more of writes to settle, which end-of-run values would mix in.

## The trade-off

**Under skewed overwrites, ripeness's curve lies below main's everywhere they overlap; under uniform ones, the two curves cross, within 3 % of each other.**

![File size against bytes written in the steady state of the 8 MiB overwrite runs: main's churn floors and targets, and ripeness's targets.](figures/ripeness/tradeoff.svg)

| 8 MiB, steady state | main | ripeness, same writes | ripeness, same file size |
| --- | --- | --- | --- |
| skewed, `λ = 2` | 1.83 times the live size, 2.34 bytes per byte | 1.66 (−9 %) | 2.25 bytes per byte (−4 %) |
| skewed, `λ = 1` | 1.65, 2.74 | 1.47 (−11 %) | 2.35 (−14 %) |
| skewed, `λ = 0.5` | 1.51, 3.22 | below 1.43 | 2.59 (−20 %) |
| uniform, `λ = 1` | 1.69, 3.13 | 1.74 (+3 %) | 3.33 (+7 %) |
| uniform, `λ = 0.5` | 1.63, 3.95 | 1.61 (−2 %) | 3.72 (−6 %) |

The ripeness columns interpolate between its runs at targets 0.65, 0.7, 0.75, 0.8, and 0.85.

**Skew is where ripeness gains, and for the reason the draft gives.**
The churn floor cleans a page only once it is half empty; a page whose hot content has died and whose cold content stays freezes above that, and keeps its garbage.
In main's 64 MiB skewed run, data pages lose fill for most of the run, down to 0.70; in ripeness's, they hold at 0.76, since a frozen page, draining at the floor rate, is ripe at a fill a draining page never is.

![The live fraction of data pages and of address-table pages, in the 64 MiB files.](figures/ripeness/live-fraction.svg)

**Under uniform overwrites every page drains alike, and ripeness reduces to a single fill threshold, like the churn floor.**
What remains between the curves is noise: a page's estimate comes from its own sporadic losses, so pages that drain alike get different estimates, and a threshold that differs page by page costs a little at light cleaning.

## The controller

**Measuring the whole file, the price controller swung by two to three decades; measuring the data pages and leaves, it held within a few percent.**
The draft's controller moves `κ` by `exp(gain · (τ − fill))` after every flush, as main's moves its budget.
Its first version measured the whole file, like main's.
But cleaning does not shrink the file at once: the pages it frees stay free until new writes reuse them, so the whole file's fill answers a change of price only many flushes later, and an integral controller on a lagging measure cycles.
At 8 MiB and a target of 0.5, the price swung between `2·10⁻⁴` and 0.09, a swing per one to two live sizes of writes, and at targets of 0.5 to 0.6 the files ended up 8 to 20 % larger than a fixed price gave for the same writes.
The data pages and leaves, which cleaning acts on, answer within the flush.
Measured there, the price held within a few percent in every overwrite run, gains of 0.5 and 2 gave the same results, and the steady states fell on the curve that fixed prices traced; the holes the measure leaves out are compaction mode's to return anyway.
[The consolidation page](consolidation.md#what-to-change) suggests the same change for main's budget.

![The price of space: the default runs (left), and the uniform 8 MiB runs by target (right).](figures/ripeness/kappa.svg)

**The price also has to hold where moving it can change nothing.**
A file starts full, so without that the first flushes drove the price to its floor, and it took one to two live sizes of writes to climb back.
The branch holds the price after a flush that cleaned nothing while the fill is above target, and after one that spent its whole budget while the fill is below.

**That is not enough after a mass free.**
Between the append workload's rotations, the live pages sit above target, and the price drifts down to `3.5·10⁻⁵` even so, since small moves by free filling count as cleaning.
When the logs rotate, the price needs a dozen flushes to climb three decades, and the file peaks at 4.5, 3.3 and 2.9 times its live size, where main's peaks at 2.9, 2.3 and 2.3.

![Statements per KiB of live data, and file size over live size, under appends and churn.](figures/ripeness/description.svg)

## At the default settings

**Ripeness at its default target writes less than main at its default under skewed overwrites and appends, and more under uniform overwrites, where its target lies further along the same curve.**

| steady state, file size / live size and bytes written per byte | main | ripeness | change |
| --- | --- | --- | --- |
| uniform, 8 MiB | 1.69, 3.13 | 1.66, 3.45 | −2 %, +10 % |
| uniform, 64 MiB | 1.53, 3.26 | 1.48, 4.02 | −3 %, +24 % |
| skewed, 8 MiB | 1.65, 2.74 | 1.56, 2.47 | −6 %, −10 % |
| skewed, 64 MiB | 1.53, 3.15 | 1.43, 2.85 | −7 %, −10 % |
| append, 16 MiB | 1.38, 3.14 | 1.42, 2.99 | +3 %, −5 % |
| churn, 16 MiB | 1.41, 2.18 | 1.67, 2.12 | +18 %, −2 % |
| typed, 16 MiB | 1.53, 3.04 | 1.46, 3.88 | −5 %, +28 % |

![File size over live size as the overwrites accumulate.](figures/ripeness/space-amplification.svg)

The 1 MiB files are dominated by the quarantine, as on [the consolidation page](consolidation.md#space), and their 33 flushes are too few for either controller to settle; the skewed one comes out 11 % larger with ripeness.

**Under churn, ripeness's file is 18 % larger, and neither a higher target nor more generous free filling closes the gap.**
Main's churn run keeps data pages 91 % full, taking half of its victims for free in the room of pages the flush writes anyway, and moves 14 MB of survivors against ripeness's 4 MB.
At a target of 0.85, ripeness's data pages reach 0.88, but its file still keeps a third of the live size in free pages, against main's quarter, and ends 9 % larger for about the same writes.
In experiments, letting free filling take any ripe victim that fits, or any victim at all, changed nothing, or made things worse; why churn leaves ripeness with more free pages remains open.

**After three quarters of a file are freed, compaction mode returns the space as fast as on main, but stops earlier.**
The file reaches 1.29 times its live size within 14 flushes, where main's reaches 1.14 within 16.
Both stop once holes fall below a quarter of the file; ripeness's run crossed that line with 20 % holes left, main's overshot to 11 % in its last step.

![File size over live size after three quarters of the file were freed.](figures/ripeness/shrink.svg)

## What it costs

**Keeping every page's estimate in the consolidator state costs 2 to 7 % of the bytes written, and buys nothing within one session.**
The draft has the state record the estimate of every page the fold drains, which at 1000 operations per flush means an entry for about every page those operations touched, and a snapshot of every page below `1 − θ` whenever the records outgrow the last one.
Without the state, the 64 MiB runs write 7 % less under uniform overwrites and 6 % less under skewed ones, at the same file sizes; append writes 5 % less, the 8 MiB runs 2 to 3 %.
The estimates matter only across a reopen, which these runs do not exercise; a denser encoding, or recording only pages whose estimate moved by a margin, would cut the cost.

![Where the bytes written to the file went, per byte the application wrote, over each whole run. On ripeness, "other" is mostly the consolidator state.](figures/ripeness/write-breakdown.svg)

**A flush takes time in proportion to the pages it writes, on either branch.**
Per page written, ripeness's flushes took between 4 % less and 16 % more time than main's in the same session, within the spread identical runs showed before.
Where ripeness writes fewer pages, as under skew, its flushes are faster: at the median over the second half of each run, 9.7 ms against 11.6 for the 8 MiB skewed file, 16.5 against 17.7 at 64 MiB.
Where it writes more, as under uniform overwrites at its default target, they are slower: 34.9 ms against 27.0 at 64 MiB.

![Flush times, including the fsync: each run's median, and its 99th percentile.](figures/ripeness/flush-latency.svg)

**In memory, each page's estimate adds 12 bytes to the page table, and each ranked page takes an entry in one of two ordered sets and one in a map from page to entry.**
The ranking takes `O(log P)` per page that changed and per victim, where main's bucket queues take `O(1)`; the flush times do not show it.

## What to change in the draft

- **Measure the fill the price acts on**: the data pages and leaves, not the whole file, whose fill lags a change of price by many flushes.
- **Hold the price where moving it can change nothing**, and keep it from sinking in calm phases, where every page is fuller than the target: a floor well above `10⁻⁶`, or holding the price whenever the budget took no ripe page, would spare the file the peaks after a mass free.
- **Record estimates more cheaply.**
  A drain record could leave out the estimate's epoch, which is the record's own, store the rate in a byte on a logarithmic scale, and skip pages whose estimate moved by less than a margin.
- **Say what a gap in the records means for content consolidation moved.**
  The records are written right after the fold, so content moved out of a page later in the same flush looks, at a reopen, like a natural loss in the gap, and inflates that page's estimate.
- **Look into churn**, the one workload where ripeness loses: replaced values free whole allocations every flush, and ripeness leaves more of the file free than main does.
- **Expect noise under uniform overwrites.**
  Estimates from sporadic losses differ between pages that drain alike; a larger `β` or two rates per page, as the draft's open questions consider, trades that noise against how soon a frozen page is recognised.

## Reproducing

The two branches ran back to back from one kladde-rust checkout, main in a worktree of its own, into one directory each:

```sh
git worktree add ../kladde-main main
cargo build --release -p kladde-bench --manifest-path ../kladde-main/Cargo.toml
git checkout ripeness
cargo build --release -p kladde-bench
../kladde-main/target/release/kladde-bench /tmp/bench-main
./target/release/kladde-bench /tmp/bench-ripeness
KLADDE_BENCH_NO_STATE=1 ./target/release/kladde-bench /tmp/bench-ripeness-no-state uniform skewed append churn
```

In kladde-docs, keep the tables gzipped under `content/evaluation/data/ripeness/`, in `main/`, `ripeness/`, and `ripeness-no-state/`, and draw the figures from the first two:

```sh
tools/plot-evaluation.py --summary --out content/evaluation/figures/ripeness \
    main=content/evaluation/data/ripeness/main \
    ripeness=content/evaluation/data/ripeness/ripeness
```
