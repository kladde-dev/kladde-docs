---
title: Evaluation
---

Measurements of [kladde-rust](../rust/) under realistic workloads, and what they say about the design in [Implementation](../impl/).

Each page names the commit it measured and the commands that reproduce it.
Its figures are drawn by `tools/plot-evaluation.py` from the tables in `data/`, which are kept, gzipped, so that a figure can be redrawn without rerunning the benchmark.

The numbers come from one machine.
Ratios, such as the file's size over its live size or the bytes written per byte the application wrote, carry over to other machines far better than times do.

## The evaluations

| page | what it measures |
| --- | --- |
| [Consolidation under load](consolidation.md) | the design in [Consolidation](../impl/consolidation.md), on six workloads: space and write amplification, compaction after a mass free, the growth of the description, flush times, and variants of the churn floor, the target fill, and defragmentation's share |
