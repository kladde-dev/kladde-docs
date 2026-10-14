---
title: Evaluation
---

Measurements of [kladde-rs](../rust/) under realistic workloads, and what they say about the design.

**Every page but the last measured the design that the present one replaced**, of [allocations and an address table](../superseded/allocations.md): "main" in them is kladde-rs's main branch as it stood then, which holds that design and is kept as the branch `address-table`.
Their findings about consolidation's policies — the churn floor, the budget, free filling, compaction mode — carry over to pages of spans, since the present design keeps those policies; their findings about statements, allocations and layouts do not.
The last page, [the file as one sequence, measured](one-sequence.md), compares the present design with that main.

Each page names the commit it measured and the commands that reproduce it.
Its figures are drawn by `tools/plot-evaluation.py` from the tables in `data/`, which are kept, gzipped, so that a figure can be redrawn without rerunning the benchmark.
[Benchmarks and tests](tools.md) describes the programs behind them, and the tests of kladde-rs: what each runs and records, how to run it, and how to read what it writes.

The numbers come from one machine.
Ratios, such as the file's size over its live size or the bytes written per byte the application wrote, carry over to other machines far better than times do.

## The evaluations

| page | what it measures |
| --- | --- |
| [Consolidation under load](consolidation.md) | the design in [Consolidation](../impl/consolidation.md), on six workloads: space and write amplification, compaction after a mass free, the growth of the description, flush times, and variants of the churn floor, the target fill, and defragmentation's share |
| [Cleaning by ripeness](ripeness.md) | the [draft](../drafts/ripeness.md)'s policy, implemented on a branch of kladde-rs, against the design above on the same workloads: the trade-off between space and writes, the controller that sets the price of space, what the logarithm in its threshold contributes, whether it keeps content that never changes apart from content that does, and what keeping the estimates costs |
| [Ripeness with a static share](ripeness2.md) | the draft's later estimate, a draining share over a static one fitted to each page's losses, against the single rate per page above: how often pages earn a static share, and what keeping the fit costs |
| [Bayesian ripeness](bayesian-ripeness.md) | the [Bayesian draft](../drafts/bayesian-ripeness.md)'s posterior per chunk, started from the file's empirical prior and decided by the expected gain or by the option to wait, against both estimates above: what it finds static, how it cleans, how its prior fares, and what it costs to keep and to rank |
| [Cleaning edited drawings and texts](cleaning-edits.md) | main's churn floor and the three ripeness estimates, under the newest size rule, on three edited SVG drawings in three layouts and on two typed texts, cleaning until the live pages are about nine tenths full: live pages, data pages and leaves, their fill, which policy lies lowest, the files' free pages and closed sizes, writes, free filling, and times |
| [Ripeness with main's free filling](ripeness-free-filling.md) | the three ripeness branches of the page above with main's free filling in place of the draft's: how often free filling cleans, the pages the branches open and the bytes they write for it, how far each branch's curve moves toward the churn floor's, and the live pages, fill, files and writes at each target |
| [Porting kladde to C++ and into Inkscape](cpp-port.md) | what a second implementation, kladde-cpp, and the Inkscape demo found: compatibility of the two implementations' files and types, what the specification left open, what C++ made harder than Rust, what Inkscape needed, and the bugs the port found |
| [The file as one sequence, measured](one-sequence.md) | the [present design](../spec/sequence.md), implemented on a branch of kladde-rs while it was a proposal, on the drawings and texts of the cleaning page under the churn floor, against main in its three layouts: the trade-off between space and writes, the encoding's size, live pages and their fill, the span policy's bound, files, writes, and times |
