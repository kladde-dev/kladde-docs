---
title: Drafts
---

Half-baked ideas in progress.

Documents here are **not** held to the separation the rest of the hub observes: they may mix normative rules, language-agnostic algorithms, and Rust-specific details freely, because working out which is which is usually the last step rather than the first.

Nothing here is adopted.
When a draft settles, it is split along the usual seams and moves into [Specification](../spec/), [Implementation](../impl/) and [kladde-rs](../rust/) — or, if it is rejected, into [Superseded](../superseded/) with the reasoning intact.

## Current drafts

| draft | status |
| --- | --- |
| [Open issues](open-issues.md) | an audit: where the other documents contradict each other, and what blocks a first prototype |
| [The `Move` operation](move-op.md) | the record is specified; how a flush states it is sketched, with the open questions listed but not answered |
| [Segments](segments.md) | a direction, not yet a design |
| [Cleaning by ripeness](ripeness.md) | a proposal: clean each page once waiting no longer pays, judged by how fast it still drains |
| [Bayesian ripeness](bayesian-ripeness.md) | a proposal on top of it: keep a posterior over each page's drain, for either variant of the draft, and decide from the posterior |

[Open issues](open-issues.md) is the odd one out: it is not an idea in progress but a standing list, and it is kept here because it is maintained by audit and goes stale the moment the documents it indexes are edited.
