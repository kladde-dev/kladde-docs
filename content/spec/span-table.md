---
title: Span table
---

The span table describes the file's content as chains of spans: each span shows a run of bytes and names the span that follows it.
It is the only structure in a kladde file that is interpreted rather than merely pointed at, and it is what makes a flush write a small delta rather than rewriting the world.

## The idea

**The file's content is three byte strings, each kept as a chain of spans: a span shows a run of bytes — bytes in a data page, bytes in its entry itself, or zeros — and names the span after it.**
The [header](file-format.md#header-pages) names the first span of each chain: the [sequence](sequence.md), which is the root value's encoding, the schema, and the implementation's private state.
An entry in a table page describes one span, and the entries of all live table pages together are the span table.

This is a *piece table*, the structure in which text editors have kept documents under editing since Xerox's Bravo: a document is a list of pieces, each a run of some buffer, and an edit changes the list but never a buffer.
Here the buffers are data pages and entries, the pieces are spans, and the list is kept in the file, as the spans' successor links.

**A span is known by an id, not by a position, so an edit restates only the spans at the places where it cuts the chain.**
A span's id stays the same when bytes before it come or go, and the order of the content is recorded nowhere but in the links, so an edit changes what the spans at its cuts show and where they link, and nothing behind them.
A splice states one to three entries, counting the span of its new bytes, and a move three to six, wherever it lands and however long the content around it is.

*Why links rather than positions.*
Keying each piece by its position, as an extent tree keys extents by their offset in a file, would make an insertion change the key of every piece behind it, and each of those would have to be written again.
Links cost a reader nothing it does not pay anyway: kladde [reads the file whole](../impl/index.md#the-shape-of-an-implementation), so it walks each chain once, at open, and keeps a tree over the spans in memory.
A tree of spans on file would let a reader find a position without reading what lies before it, which kladde never does, and keeping the lengths in its interior nodes current would restate a path to the root at every edit.

## Logical level

### Entries

**One kind of entry, in three forms:**

| form | shows |
| --- | --- |
| `Ref(id, length, address, next)` | `length` bytes of the file at `address`, inside the `content` of one `Data` page |
| `Inline(id, payload, next)` | its payload, 1 to 126 bytes |
| `Zero(id, length, next)` | `length` zeros |

`id` is the span's id, and `next` the id of the span after it in its chain, or none at the end of the chain.
No span is empty.
Writing a span's entry is called *stating* the span, and writing it again *restating* it.

*Why `Inline`.*
Below the encoded size of a `Ref`, a few bytes, the address would be as large as the data, and a short span that a page rewrite moves would cost a data page's bytes as well as an entry.
Inline spans ride in the table pages, which the [header](../impl/flush.md#the-header-as-write-buffer) rewrites every flush anyway; how short a span must be to be kept inline is a writer's choice, below the format's ceiling.

*Why `Zero`.*
Encodings without padding hold few zeros, but `Fixed` values and large preallocated arrays have runs of them, and a `Zero` states any length of them in a few bytes.

### Resolution

1. **A span id's entry in the table page of highest epoch is its description.**
   An epoch holds at most one entry per id, so the winner is unique.
   Only pages the header reaches through child references are consulted.
2. **Each chain runs from the span its header field names, following `next`, and holds its spans' content in that order.**
   A header field of `0` names an empty chain.
3. **A file is invalid if a chain names an id that no entry describes, reaches a span twice, or shares a span with another chain.**
   A reader must check that no span is reached twice, which would otherwise trap the walk in an endless loop at open.
4. **A file is invalid if a reached `Ref` shows bytes outside the `content` of a `Data` page**, or in a page whose epoch exceeds the governing header's ([the epoch assertion](file-format.md#epochs)).

A reader refuses an invalid file rather than return content from it.

**Liveness follows: an entry is live if it is the newest of a span that some chain reaches.**
There are no fragments, no sizes stated apart from content, no tombstones and no counts of mentions, and dropping any other entry can never change what the file holds.
A page rewrite states each live span in it again, under the span's own id, and no entry elsewhere changes its meaning.

### Rules for writers

- **At most one entry per span id and epoch.**
- **The reach rule: a span that a chain reaches at the end of an epoch, but that no chain reached at the end of the epoch before, is stated in that epoch.**
- **No two reached spans show the same byte of a data page.**
  This is a writer invariant that readers do not depend on; it keeps a data page's live bytes a plain sum, see [design directions](#design-directions).

*Why the reach rule.*
Entries of older epochs stay in their pages until those pages are rewritten, so without it a writer could link in a span id whose newest entry a page rewrite had dropped as dead, and an older entry of the same id, still in a page nobody rewrote, would show its stale bytes.
With it, a span id can be handed out again as soon as its span is no longer reached, since the new span's entry is newer than any old one.
Readers cannot check the rule, since checking it would need the chains of the epoch before.

## Physical format

Span-table pages follow the [[file-format#Page framing|general page framing]] with `kind == Table` (`0x01`), and encode their payload in the `content` field.
The header page is always a table page.
Every table page contains zero or more references to other table pages followed by zero or more entries.
References between table pages form a tree rooted at the header page: every page reachable from the header other than the header itself is named by exactly one `child_ref` among the pages reachable from the header, and no `child_ref` names a header slot, page 0 or page 1.

```
; `content` field of a table page:
table_page := child_ref* 0:byte entry*
child_ref  := page_number_delta:varint
entry      := id_delta:varint tag:byte body next?
; bit 0 of tag: 1 if the next span is id + 1, and no `next` field follows;
;               0 if a `next` field follows the body
; tag >> 1 = 0:        Ref     body := length:varint address_delta:varint
; tag >> 1 = 1:        Zero    body := length:varint
; tag >> 1 = 2..127:   Inline  body := payload:byte{(tag >> 1) - 1}
next       := varint   ; 0: the chain ends here; else zigzag(next - id)
```

`zigzag` maps a signed integer to an unsigned one, `0, −1, 1, −2, …` to `0, 1, 2, 3, …`, as Protocol Buffers' `sint` types do: `zigzag(v) = (v << 1) ^ (v >> 63)` on 64-bit two's complement.

### Delta encoding

A reader decodes a page in order, with a `page_cursor`, an `id_cursor` and an `end_cursor`, all starting at 0.

- For each `child_ref`, `page_cursor` grows by `page_number_delta`, and is then the referenced page's number.
  Child references end at the delimiter byte `0`, which cannot be a `page_number_delta`: the first child's number is at least 2, since no `child_ref` names a header slot, and each later one exceeds its predecessor, since child references are sorted and the tree names each page once.
- For each entry, `id_cursor` grows by `id_delta`, and is then the entry's id.
  Entries are sorted by id, so `id_delta` is at least 1; a delta of 0 makes the page invalid.
- A `Ref`'s address is `end_cursor + unzigzag(address_delta)`, after which `end_cursor` is that address plus the `Ref`'s length, where that `Ref` ended.
  `Zero` and `Inline` entries leave `end_cursor` alone.
- Entries continue to the end of `content`, which `content_size` gives.

`next` never names the span itself, which would be a cycle, so `0` is free to end the chain.
A `next` that would name the span itself, id 0, or an id outside the bounds makes the page invalid.

**Delta encoding makes a wrong order unrepresentable**: entries out of id order would need a negative `id_delta`, which a varint cannot hold.
A span written together with its neighbours — by a merge, by evacuation, or when its content was new — gets the next id, and its data follows theirs in the page, so its entry takes about five bytes: one each for the id delta, the tag and the address delta, and two for the length.

### A worked example

A drawing whose sequence was one span, `#1`, showing 28 bytes at address 8203 (page 2, right after its framing), is renamed by a splice of 9 bytes over its first 3.
Afterwards, `#2` holds the 9 new bytes inline and links to `#1`, and `#1` is restated to show the last 25 bytes, at address 8206; the header's `sequence_head` becomes 2.
If both entries go in the header, its `content` is:

```
00                                  no child references
01 00 19 9C 80 01 00                #1: id 0 + 1; Ref, next follows; length 25;
                                        address 0 + unzigzag(16412) = 8206; next: end of chain
01 14 57 6F 72 6C 64 20 6D 61 70 01 #2: id 1 + 1; Inline of 20 >> 1 − 1 = 9 bytes, next follows;
                                        "World map"; next = 2 + unzigzag(1) = 1
```

Had the splice landed in the middle of a sequence of a million spans, the same two entries, and one more for the span its start cut in two, would have described it: every span behind it keeps its entry although its bytes moved.

### Notes on the physical format

- The root table page *is* the current [header slot](file-format.md#header-pages), so before its `content` it carries the file-header fields, all covered by the header CRC.
- A `child_ref` stores only a page number and no epoch.
  The child's own framing carries its epoch, and [invariant I1](durability.md#the-two-invariants) guarantees that a page referenced by a valid header is present and intact.
- Nothing in the format constrains *which* page an entry lives in or how the tree is shaped; those are writer policies.
  The tree is a *directory*, not a search structure: nothing descends it by key.
- A reader should verify that the `child_ref`s form a **tree**, so that cyclic references, or a page named twice in a graph that is not a tree, cannot trap it in a loop or have it decode a page twice.

## Bounds

An implementation must support these, and must fail cleanly rather than silently wrap when an application exceeds them.

- **Span ids** are 32-bit, and range from 1 to `2^32 − 2`: 0 means "none" in the header and in `next`, and the largest id is left out so that `id + 1`, which an implicit successor names, is always a valid id.
  So a file holds at most `2^32 − 2` spans per moment, and a varint-encoded id takes at most 5 bytes.
- **Page sizes** are uniform throughout a file and given by the header's `log2_page_size`, currently 4 KiB with 8, 16, 32, and 64 KiB reserved, so an offset within a page fits in 16 bits.
- **A `Ref`'s length** is at most `MAX_PAGE_CONTENT`, the page size minus the 15 bytes of [page framing](file-format.md#page-framing): at most `2^12 − 15` bytes with 4 KiB pages, and `2^16 − 15` with 64 KiB ones; its varint takes at most 3 bytes.
- **An `Inline` payload** is 1 to 126 bytes, and **a `Zero`'s length** 1 to `2^32 − 1`.
- **Page numbers** are 32-bit, so **addresses** fit in 48 bits (49 to point at the end of the file), an `address_delta` takes at most 7 bytes, and the largest **file** is `2^(32+12)` bytes, about 17.6 TB, with 4 KiB pages.
- **Each chain's content** is shorter than `2^48` bytes, so a position in the sequence takes at most 7 bytes as a varint.
- **An entry's framing** — its encoded length without an `Inline` payload — is at most **21 bytes**: a `Ref` with 5 bytes of `id_delta`, a tag byte, 3 bytes of length, 7 of `address_delta` and 5 of `next`.
- **The live `Ref`s showing bytes of one `Data` page** have lengths summing to at most `2^32 − 1`, so that a reader can track a page's live bytes in a 32-bit counter.
  The writer invariant holds that sum below one page's worth; the bound binds only files that let several reached spans show the same byte, which is legal and which a reader must therefore survive.
- **An encoded [transaction](journal.md#framing)**, without framing, takes at most `2^32 − 1` bytes, so that its length prefix fits in 32 bits.
  Trying to encode a larger transaction must fail rather than silently cut it in parts.

## Design directions

**The byte-referenced-once invariant.**
Not required for read correctness; keeping it makes data-page coverage a plain counter and consolidation locally decidable.
Relaxing it would enable copy-on-write clones and deduplication, at the price of refcounted coverage and clone-aware consolidation.
Since it is a writer invariant invisible to readers, choosing the strict form now costs nothing later.
**Copy-on-write clones are not pursued**: they add complexity and open a zip-bomb-like attack vector, and duplicating a value writes its encoding instead.

**Per-page epochs versus per-entry epochs.**
Per page is cheaper and imposes exactly one discipline — a rewrite states the newest entries — which consolidation requires anyway.

**Overwrites inside a span: split or rewrite, not shadow.**
An overwrite of bytes inside a span either splits the span in three around the written bytes, or writes the whole span again with the new bytes; which one is a writer's choice ([the flush](../impl/flush.md#overwrites-split-or-rewrite)).
A third form, an entry that overwrites part of a span by offset, would cost one entry and write no other byte, but it would bring back per-byte resolution, fragments inside spans, and liveness that counts them: most of what one entry per span removes.
It is the option to revisit if overwrites inside long, cold spans turn out to dominate a workload.
A smaller first step would be entries that only shrink or relink a span stated by an `Inline` entry of an earlier epoch, sparing a restatement of its payload of up to 126 bytes.
They would be restricted to spans that a chain reached at the end of the epoch before, by the reach rule; they would keep the `Inline` entry alive with a dead part of its payload; "newest wins" would then apply separately to an inline span's payload, size and successor, so a span could have two live entries; and a page rewrite that writes the `Inline` entry again would have to state the span's current size and successor with it.
Neither is adopted until measurements motivate it.

**The `Inline` threshold.**
The encoding fixes the ceiling at 126 bytes; the policy threshold belongs below it, and is an implementation choice rather than a format one.
See [the implementation notes](../impl/flush.md#the-inline-threshold).
