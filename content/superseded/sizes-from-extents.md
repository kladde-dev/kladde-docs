---
title: Sizes from extents
---

**Superseded.** Replaced by [the newest size wins](../spec/address-table.md#conflict-resolution-across-epochs) and [the coverage rule](../spec/address-table.md#the-coverage-rule).

An earlier address table let a reader infer an allocation's size from its statements instead of reading it from one: every content statement's extent counted toward the size, and a resize was stated by one of two statements, one that only bounded the size from below and one that also fenced off everything older beyond its bound.
Bytes that no statement matched read as zero by default.

## Why it was abandoned

**The reader's inference bought a flexibility that no writer ever needed, and the machinery that kept it correct hid failure modes until tests ran into them.**
A writer always knows the size it wants, yet the size depended on what was physically present, including statements in leaves that no in-memory structure reaches.
Keeping it right took a per-allocation record of the fencing statement, which every page rewrite had to transfer; a second kind of pin for statements that only bounded the size; a fencing statement at every shrink, since whether one was needed could not be decided; and a rule against both resize statements in one epoch, which led kladde-rs to refuse nearly empty leaves as consolidation fillers, so that they [piled up](../drafts/consolidation-from-statements.md) on a realistic workload.
Six gaps in this machinery surfaced only once kladde-rs implemented and tested it.

Its replacement has the writer state the size whenever it changes, mostly at the cost of one bit on a content statement that ends there, and state the zeros a growth exposes, which kladde's own data types wrote anyway.
