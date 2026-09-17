---
title: Segments
---

**Status: a direction, not yet a design.**

A **segment** would be an independently loadable part of a kladde file: its own id space, its own address table, loadable without reading the rest of the file.

Nothing about this is worked out.
It is recorded because several settled decisions were made *so that* it stays possible, and those decisions would otherwise look arbitrary.

## Why it is reachable at all

The copy-on-write redesign made the file **rooted**: every live page is reachable from a committed header, rather than discovered by scanning.

That is the precondition.
Under the [superseded heap](../superseded/relocatable-heap.md), opening a file meant scanning every live allocation to rebuild the id-to-address table, so partial loading was not expressible: you could not know what was live without reading everything.
With a root, "load only this subtree" becomes a question about which pages to follow.

## What has already been decided with segments in mind

- **Allocation ids are 32 bit**, and the [bounds](../spec/address-table.md#bounds) note that each segment will likely get its own id space, so a file may eventually hold more than `2^32` allocations without widening the id.
- **The header reserves space for future roots**, or should — the exact field list is explicitly [left open](../spec/file-format.md#header-pages) partly for this.
- **Whether address-table child references should carry each page's id range** is an open question: it costs a few bytes per reference now and buys partial loading later.
  This is the one decision that would be expensive to retrofit, since it changes bytes that files already contain.

## What is not decided

Essentially everything else:

- How a segment is named, and whether one segment may point into another.
- Whether the ownership tree may cross a segment boundary, and what that means for [freeing](../rust/freeing.md).
- Whether segments share a journal or each carry their own, and what a transaction spanning two segments means for atomicity.
- Whether a segment can be loaded read-only while another is being written.
- How the [schema table](../spec/schema/) is shared or duplicated.

The last two are where this stops being a storage question and becomes a concurrency one, which is [out of scope](../spec/file-format.md#concurrency) for now.
