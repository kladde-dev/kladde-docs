---
title: Packed and small layouts
---

**Packing kladde-svg's lists of enums makes its drawings 18 to 38 % smaller on file, and a drawing editor's edits write 33 to 44 % fewer bytes.**
Small values then take most allocations and nearly every address-table page out of a stored file, and a third of the memory.
Under edits they cost writes again where they put large content inline: a splice in a long list shifts more, and although kladde-rs states the shifted bytes where they lie rather than writing them again, each statement it restates is a few bytes in a table page, and the statements multiply as splices cut the lists into fragments.

The benchmarks first ran as they were, on the branch of kladde-rs that implements [packed places](../spec/schema/type-descriptors.md#places).
The drawings and the 1,803 files of the corpus hold the same bytes as on main, but for 216 bytes less schema each.
Edits write about half of what they write on main: the journal, which on the branch records a stored value as one record, is half as large, and the branch restates what a splice shifts, where main writes it again.

The four layouts after that add to each other, one step at a time:

- **Packed lists of enums.**
  With children, transforms, attributes and path data packed, the drawings' live bytes fall by 41 % on the tiger and the coat of arms, and by 18 % on the world map.
  The world map's path data is 92 % cubic Béziers, the largest variant, which packs to the 26 bytes of its slot.
  An open drawing takes 4 to 9 % more memory, since a packed vector keeps where its elements start, four bytes for each element after the first, and takes 8 bytes more in its value.
- **Small ids.**
  Small strings for element ids take one allocation and one statement per id out of the file, and 6 to 18 % of the memory, and change the writes by 1 % at most.
- **Small attribute lists and strings.**
  These take out half the allocations that remain, settle the files 4 to 8 KiB smaller, and take 20 to 24 % less memory than the slotted layout.
  The edits write 5 % less than with packed lists on the tiger, and 7 % and 26 % more on the coat of arms and the world map.
- **Small child lists, transforms and path data.**
  These leave 99 to 630 allocations per drawing, next to no table pages in a stored file, and a third less memory than the slotted layout.
  But on the world map the edits write twice what packed lists do, 395 bytes against 187, two thirds of them in table pages.

**Splices in the small layout's long lists shift 461 bytes per edit on the world map, and kladde-rs restates them all**: it writes 63 bytes of data pages per edit, close to the packed layout's 57, but 265 of table pages, four times as many.
Restating keeps every fragment a shifted tail lies in, and every splice cuts one more: the world map's statements grow fivefold over the run in the small layout, to 14,066, and three- to fourfold in the others.

**At the sizes kladde is meant to serve, the files keep far more than their content.**
Stored afresh and settled, a packed drawing takes 0.85 to 1.1 times its canonical SVG; edited and closed, 1.5 to 4.6 times, since closing returns only the free pages at the file's end, and the address table, 12 to 61 % full during the edits, keeps statements of every edit spread over its pages.
Reopening the edited world map takes 2.3 to 4.4 times as long as opening it fresh.
Nearly every file of the corpus, a few kilobytes of drawing, settles at four pages, 16 KiB, in every layout.

## What was measured

**kladde-svg, a crate of kladde-rs, stores SVG drawings in a model of `#[derive(Persistable)]` types, and edits them as a drawing editor would.**
An `Element` holds five things:

- its tag, with the tag's core geometry, in an enum `ElementKind`;
- its transform list;
- its other attributes, as a list of an enum `Attr`, which has a variant for each typed attribute and one, `Other`, that keeps an attribute verbatim;
- its inline style declarations, as a second list of `Attr`;
- its children, as a list of an enum `Node`.

A path's data is a list of an enum `PathSegment`, whose variants take from 2 bytes, a `ClosePath`, to 26, a cubic Bézier, with coordinates as `f32`s.
Anything the model does not type it keeps as a string, so every SVG fits, and its canonical form survives the trip through kladde byte for byte.

**Three drawings from Wikimedia Commons, and resvg's test suite at v0.48.1, are the workloads**, pinned by the crate's `corpus/fetch.sh`:

| drawing | canonical SVG bytes | elements to edit | paths | path segments | of them cubic | live bytes, slotted |
| --- | --- | --- | --- | --- | --- | --- |
| the Ghostscript tiger | 66,850 | 481 | 240 | 2,510 | 52 % | 102,775 |
| the coat of arms of Spain, from Inkscape | 163,768 | 606 | 494 | 5,612 | 53 % | 202,815 |
| a blank world map | 1,005,908 | 2,528 | 2,213 | 29,666 | 92 % | 971,531 |

The canonical SVG is what kladde-svg writes: no whitespace between tags outside text, single spaces between attributes, and path data in the grammar's two shortcuts, a command letter left out where the previous segment implies it and no separator before a minus sign.
It is smaller than every original, by 3 % for the tiger, which its authors had written compactly, and by 9 to 17 % for the two drawings that Inkscape and others had indented; so it, rather than the originals, is what the kladde files compare with.

The test suite contributes 1,800 files of a few kilobytes that cover SVG's features; a 1,801st, not UTF-8, is skipped.

**`svg-roundtrip` stores every file, closes it, reopens it, and checks that it writes the same canonical SVG.**
All 1,803 round trips were exact in every layout.
It records the live bytes, allocations and statements, and the file's size *settled*, once two more sessions have opened and closed it, since a new file holds free pages that only the following flushes return.

**`svg-bench` edits a drawing with a seeded mix of what a drawing editor does**, recording a row in [kladde-bench's](consolidation.md#what-was-measured) format every 1000 edits:

| share | edit | what it does to the model |
| --- | --- | --- |
| 30 % | drag a shape | set the translation that leads its transform list, or insert one; nine drags in ten go to a tenth of the shapes |
| 20 % | move a path node | set two coordinates of a segment |
| 8 % / 7 % | insert / delete a path node | insert or remove a segment |
| 15 % | recolor | set a fill's colour, in the attributes or the style, or add a fill attribute where there is none |
| 8 % / 7 % | duplicate / delete a shape | insert a deep copy right above it, or remove and free it |
| 5 % | restack | move a shape elsewhere among its siblings with the child list's `move_element`, which takes its bytes from where they lie |

The mix gives text edits 5 % of their own, which these drawings, having no text, turn into recolors.
Each run makes a fixed number of edits, the same edits in every layout: 31,613 on the tiger, 41,130 on the coat of arms, and 177,831 on the world map, which is where the edits of the slotted layout have stored eight times its live size.
The tables end at the last full thousand.
After the last edit, the file is closed, and its size recorded, and the time to reopen it, as the median of 21 opens.

**The store flushes whenever the journal outgrows four pages**, `Options::journal_budget_pages = 4`, and at every row: every 243 to 477 edits, depending on the drawing and the layout.
The default budget, 256 pages, would let the journal grow to a megabyte between flushes, more than any of these drawings.

`--only` runs of 20,000 edits of one kind each measured what each kind costs, on the coat of arms and the world map.
A run of duplications alone keeps the drawing within a fifth of its size by deleting every other time, and so does a run of deletions, so those two kinds measure the same half-and-half mix.

**The layouts are type aliases in the model, which cargo features select:**

| layout | feature | what it changes |
| --- | --- | --- |
| slotted | none | every list is a `PersistableVec`, whose elements take fixed slots, and every string a `PersistableString` |
| packed | `packed` | children, transforms, attributes and path data are `PackedPersistableVec`s, and everything inside their elements is packed: element kinds, lengths, pointers |
| small ids | `small-ids` | as packed, and an element's `id` is a `SmallPersistableString`, inline in its attribute while short |
| small attributes | `small-attrs` | as small ids, and attribute lists are `SmallPersistableVec`s and every string a `SmallPersistableString`; the document is a packed root |
| small | `small` | as small attributes, and transforms, path data, points and child lists are `SmallPersistableVec`s too |

Lists of points and of lengths, whose elements all have one size, stay slotted until the last step, where they become small.
[Containers](../rust/containers.md) describes each container.

**The flush restates the bytes an allocation shifts within itself.**
A splice, or a move within one allocation, leaves the bytes behind it where they lie, and the flush states each fragment they lie in at its new offset, as [the flush's design](../impl/flush.md#per-record-rules) has it; only bytes copied within an allocation, bytes from another allocation, and pieces whose statements would take more than their bytes are written again.
That counts as *restated* in what follows, and the tails kladde-rs still writes again come to at most 4 bytes per edit in any run.

The runs measured kladde-rs's `variable-size` branch at `f3f7eeb`, and main at `113cfc3` for the baseline, with its default journal budget set to four pages.
Both were built with `--release` by Rust 1.99.0, with default options otherwise, on an Intel Core i7-1165G7 with 16 GB of memory under Linux 7.0, in a development container.
Memory is the heap that opening a drawing allocates, counted by a global allocator in a separate program.
The times to open are medians of 21 opens; other times come from single runs and are noisy.
The bytes the flush still writes again behind splices were counted by a counter added to kladde-store's fold for that purpose and not committed; the restated bytes are in the store's statistics.

## The baseline: the benchmarks as they were

**The branch stores every drawing's content in the same allocations and bytes as main, apart from a schema 216 bytes smaller.**
That holds for all 1,803 files: every file's live bytes are exactly 216 fewer, and its allocations the same.
Seven files, the three drawings among them, state the same allocations in 1 to 246 fewer statements.

| live bytes | main | branch, slotted |
| --- | --- | --- |
| tiger | 102,991 | 102,775 |
| coat of arms | 203,031 | 202,815 |
| world map | 971,747 | 971,531 |
| corpus, 1,803 files | 6,662,168 | 6,272,720 |

**The closed files differ by a few pages, in both directions, a matter of where pages fall rather than of what they hold.**
The drawings' data and table pages agree to within two pages.
Settled, the tiger takes 116 KiB against main's 128, the coat of arms 216 against 228, and the world map 1,020 against 1,012.
In the corpus, 1,525 files settle at the same size and 273 settle one page larger, at 16 KiB, where 1,473 others settle on both branches.
On main, those 273 had a larger creation journal, which pushed them to 20 KiB when first closed, with a free page that let the next sessions bring them down to 12 KiB.

**The edits write 48 to 53 % less: a journal half as large, and 69 to 84 % fewer data pages.**
The branch writes a stored value as one record, where main wrote one per scalar, so the journal shrinks by 42 to 50 % and fills its budget less often.
And the slotted layout splices too, inserting and deleting path nodes, duplicating, deleting and restacking shapes, whose shifted tails main writes again and the branch restates; on the world map, that costs the branch 84 bytes per edit more of table pages and headers and saves it 392 of data pages.

| bytes written per edit | main | slotted | journal: main | slotted | data pages: main | slotted |
| --- | --- | --- | --- | --- | --- | --- |
| tiger | 297 | 154 | 76 | 44 | 150 | 46 |
| coat of arms | 460 | 226 | 110 | 59 | 255 | 80 |
| world map | 698 | 325 | 128 | 64 | 469 | 77 |

## The layouts at a glance

**On file, packing makes the drawings smaller, and small values then trade allocations and table pages for data pages.**

| | slotted | packed | small ids | small attributes | small |
| --- | --- | --- | --- | --- | --- |
| **tiger** live bytes | 102,775 | 60,988 | 60,509 | 59,988 | 59,746 |
| allocations | 1,453 | 1,453 | 971 | 485 | 99 |
| data + table pages | 20 + 7 | 13 + 3 | 13 + 3 | 14 + 1 | 15 + 0 |
| settled file, KiB | 116 | 72 | 72 | 68 | 68 |
| **coat of arms** live bytes | 202,815 | 119,350 | 118,744 | 117,951 | 117,806 |
| allocations | 2,007 | 2,007 | 1,398 | 638 | 239 |
| data + table pages | 44 + 8 | 26 + 6 | 26 + 5 | 28 + 2 | 30 + 0 |
| settled file, KiB | 216 | 136 | 132 | 128 | 128 |
| **world map** live bytes | 971,531 | 800,986 | 798,461 | 795,189 | 793,689 |
| allocations | 8,542 | 8,542 | 6,014 | 2,775 | 630 |
| data + table pages | 218 + 34 | 169 + 37 | 169 + 35 | 177 + 23 | 196 + 1 |
| settled file, KiB | 1,020 | 836 | 828 | 820 | 800 |

Each drawing's statements number its allocations plus 2 to 81.
Settled, a packed drawing takes 1.1 times its canonical SVG for the tiger, and 0.85 times for the coat of arms and the world map.

**Under the edits, packing cuts the writes by a third or more; the small layouts give back two fifths of that saving on the coat of arms, and half again all of it on the world map.**

| bytes written per edit | slotted | packed | small ids | small attributes | small |
| --- | --- | --- | --- | --- | --- |
| tiger | 154 | 86 | 85 | 82 | 82 |
| coat of arms | 226 | 151 | 152 | 162 | 180 |
| world map | 325 | 187 | 186 | 235 | 395 |
| **of which data pages** | | | | | |
| tiger | 46 | 19 | 20 | 20 | 21 |
| coat of arms | 80 | 54 | 57 | 60 | 60 |
| world map | 77 | 57 | 58 | 62 | 63 |
| **of which table pages** | | | | | |
| tiger | 52 | 26 | 25 | 23 | 21 |
| coat of arms | 70 | 43 | 42 | 49 | 64 |
| world map | 168 | 65 | 65 | 109 | 265 |
| **of which journal** | | | | | |
| tiger | 44 | 32 | 31 | 31 | 32 |
| coat of arms | 59 | 42 | 41 | 40 | 43 |
| world map | 64 | 51 | 50 | 50 | 53 |

The rest is headers, 8 to 17 bytes per edit, a page per flush.

**The live pages shrink at nearly every step, but the files much less: every file keeps two to four flushes' worth of written pages free**, those the last commit dropped, which stay fallback until the next commit, and the reusable ones that the next flush will fill.
That pool takes 12 to 54 % of the file, the more the smaller the drawing, and closing returns only the part of it that lies at the file's end.

| over the second half of each run, KiB | slotted | packed | small ids | small attributes | small |
| --- | --- | --- | --- | --- | --- |
| tiger: data and table pages | 129 | 73 | 74 | 87 | 116 |
| free pages | 134 | 97 | 88 | 76 | 108 |
| file | 274 | 181 | 172 | 174 | 236 |
| coat of arms: data and table pages | 477 | 260 | 246 | 237 | 234 |
| free pages | 170 | 122 | 122 | 135 | 134 |
| file | 660 | 393 | 378 | 383 | 380 |
| world map: data and table pages | 2,333 | 1,525 | 1,453 | 1,397 | 1,293 |
| free pages | 321 | 211 | 233 | 283 | 335 |
| file | 2,667 | 1,748 | 1,698 | 1,691 | 1,639 |

![The three drawings in each layout under the same edits: the file's size, its data and table pages, and the bytes written per edit so far.](figures/variable-size/svg-layouts.svg)

**The data pages stay 68 to 86 % full, but the address table only 12 to 61 %**, and the statements grow with the edits: the slotted world map ends with 32,099 statements, on 287 table pages, for the 10,393 it had after its first thousand edits.

**Closed after the edits, a file takes 1.8 to 7.4 times its live size**, and 1.4 to 6.2 times the canonical SVG of the edited drawing:

| closed after the edits, KiB | slotted | packed | small ids | small attributes | small | canonical SVG |
| --- | --- | --- | --- | --- | --- | --- |
| tiger | 248 | 184 | 172 | 152 | 216 | 40 |
| coat of arms | 652 | 400 | 432 | 332 | 404 | 207 |
| world map | 2,648 | 1,716 | 1,720 | 1,672 | 1,640 | 1,163 |

Flushes took 2.8 to 6.3 ms at the median, but for the small world map's 14.4 ms, which restates the most statements per flush.

## Packed lists of enums

**Packing saves what the slots padded, so it saves most where variants differ most.**
A path segment's slot takes 26 bytes, the size of a cubic Bézier, while a line takes 10 and a close 2.
On the tiger, the path data packs to 75 % of its slotted size, on the coat of arms to 68 %, and on the world map, 92 % of whose segments are cubic, to 95 %.
The child lists save more: a `Node`'s slot takes 38 bytes, where a packed node, an element whose lists are pointers, takes 9 or 10, as the bytes a restack stores show.
Attributes shrink in the same way: an attribute's slot takes 17 bytes, a fill colour packed 6.

| live bytes saved by packing | slotted | packed | saved |
| --- | --- | --- | --- |
| tiger | 102,775 | 60,988 | 41 % |
| coat of arms | 202,815 | 119,350 | 41 % |
| world map | 971,531 | 800,986 | 18 % |

The world map's coordinates are what remains, four bytes each, which only a narrower number would shrink, as [floats as decimals](../drafts/decimal-floats.md) measures.

**Every kind of edit writes less with packed lists.**
A packed value is smaller, so its records are: the journal shrinks by 20 to 29 % in the mix.
A restack stores 9.5 bytes instead of 38, and an inserted path node about 10 instead of 26.
A splice shifts fewer bytes, since a packed list is shorter, and so has fewer fragments to restate: inserting a path node on the world map restates 166 bytes per edit, against 206 in the slotted layout, and a restack 40, against 237.
And smaller records fill the journal's budget more slowly, so the packed layouts flush a sixth to a quarter less often.

![What one kind of edit writes per edit, in each layout, from runs of 20,000 edits of that kind. Duplicate and Delete both measure the same half-and-half mix of the two.](figures/variable-size/svg-edit-kinds.svg)

On the world map, a drag that adds the first translation to a shape's transform list creates the list's allocation.
The shape's pointer grows from the one byte of a null pointer to the two of an id, and the shape's encoding grows with it, a splice in its parent's child list that restates 16 bytes per drag; a slotted pointer slot takes the id in place, and yet the slotted layout's drags write 91 bytes against 81.

## Small ids

**Small ids take one allocation and one statement per id out of the file, and with them 6 to 18 % of the memory; the bytes and the writes stay as they were.**
An id in a packed place is a varint pointer, one or two bytes, to an allocation of a few bytes.
Small, it is a one-byte tag and the same bytes, inline in its attribute: the tiger loses 482 allocations, the coat of arms 609, and the world map 2,528.
Those allocations were stored [inline in the address table](../impl/flush.md#the-inline-threshold), so up to two table pages go, and the data pages stay.
Writes change by 1 % at most.

## Small attribute lists and strings

**Small attribute lists halve the remaining allocations again, and shrink the table pages, but put each attribute list inside its element.**
The tiger falls to 485 allocations, the coat of arms to 638, the world map to 2,775.
Table pages fall by 2 to 12 and data pages grow by 1 to 8, since content that the table held inline now lies in its element's list, and the settled files take 4 to 8 KiB less.

An element now changes size whenever its attribute list does, and splices its parent's child list.
The world map colours its shapes through CSS classes, so its first recolor of a shape adds a fill attribute: recolors restate 22 bytes per edit, against none with packed lists alone, and the mix 133, against 63.
So the edits write 26 % more than with packed lists on the world map, and 7 % more on the coat of arms, most of it in table pages; on the tiger, 5 % less.

## Small child lists, transforms and path data

**With every list small, a drawing is a few hundred allocations, the address table nearly empty, and its memory a third smaller than slotted; but whole subtrees lie inside their parents' child lists.**
The tiger keeps 99 allocations, the coat of arms 239, the world map 630: the root's child list, the child lists of groups, and path data too long to stay inline.

A list that changes size now shifts its siblings' whole content, and the flush restates what it shifts.
On the world map, whose root holds 234 children and whose largest group 286, splices shift 461 bytes per edit in the mix, against 63 with packed lists:

| world map, bytes per edit | slotted | packed | small attributes | small |
| --- | --- | --- | --- | --- |
| written, the mix | 325 | 187 | 235 | 395 |
| of which table pages | 168 | 65 | 109 | 265 |
| restated, the mix | 194 | 63 | 133 | 461 |
| restated: drag | 0 | 16 | 33 | 134 |
| restated: recolor | 0 | 0 | 22 | 86 |
| restated: restack | 237 | 40 | 99 | 689 |
| restated: insert a path node | 206 | 166 | 166 | 418 |
| restated: delete a path node | 164 | 147 | 147 | 319 |

A drag that inserts a translation grows a transform list inline, and with it the shape; a restack moves a whole shape's content, 72 bytes on average instead of 9.5, and everything between its two places; a path node inserted into inline path data grows the path element.
Each splices a child list, and the flush restates the list's tail.
On the coat of arms, the mix restates 96 bytes per edit against 23 with packed lists, and on the tiger, 24 against 11.

**The data pages see none of it, and the table pages all of it.**
The small layout writes 63 bytes of data pages per edit on the world map, against 57 with packed lists, and its consolidation moves 22 bytes per edit, against 19; but it writes 265 bytes of table pages, against 65.
That is about half a byte of table pages for every byte restated, far more than a statement per fragment would cost if the fragments were as long as when the drawing was stored: restating keeps every fragment a tail lies in, and every splice cuts one more, so the description fragments as the edits go on.
The world map's statements grow from 2,697 after its first thousand edits to 14,066 at the end in the small layout, more than fivefold, and its table holds 75 pages when it closes, against one when it was stored.

## The corpus: drawings of a few kilobytes

**The corpus's files are dominated by the schema, which every layout carries: an empty drawing takes 1,930 to 1,968 bytes, nearly all of it schema.**
Beyond that, packing shrinks the content by 40 %, and small values save allocations but not bytes: in a file this small a pointer takes one byte, as a tag does, and the content takes the same bytes inline as in an allocation of its own.

| corpus, 1,803 files | slotted | packed | small ids | small attributes | small |
| --- | --- | --- | --- | --- | --- |
| live bytes | 6,272,720 | 5,162,539 | 5,164,297 | 5,096,806 | 5,130,927 |
| beyond an empty drawing each | 2,724,416 | 1,625,053 | 1,621,402 | 1,617,016 | 1,616,880 |
| allocations | 59,061 | 59,061 | 47,077 | 14,787 | 6,470 |
| statements | 60,962 | 60,947 | 48,963 | 16,679 | 8,390 |
| settled files, MB | 30.78 | 30.56 | 30.55 | 30.55 | 30.53 |

The small layout's schema is 19 bytes larger than the small-attribute layout's, which outweighs what its small lists save in most of these files.
Of the 1,800 files of the test suite, 1,775 hold their content in one page in the slotted layout and 1,777 in every other.

**1,769 to 1,799 of them, depending on the layout, settle at 16 KiB, four pages, for a median of 2.2 to 2.7 KB of live bytes**: the two header slots, a journal page, and one data page; the rest settle at 12 KiB, but for two larger ones.
Documents of a few kilobytes are what kladde is meant to serve as well as larger ones, so the three pages a file takes besides its content are a cost to reduce, not a floor to accept.

## Memory and opening

**Packed lists cost a little memory and small values save much of it, a third once every list is small.**

| heap after opening, KiB | slotted | packed | small ids | small attributes | small |
| --- | --- | --- | --- | --- | --- |
| tiger | 407 | 422 | 397 | 327 | 265 |
| coat of arms | 680 | 725 | 610 | 520 | 470 |
| world map | 2,929 | 3,183 | 2,617 | 2,252 | 1,943 |

A packed vector keeps where each element after the first starts, a `u32` each, in a `ThinVec` beside its content's pointer and end, 16 bytes in the value; it takes 40 bytes in memory, a slotted one 32.
So an `Element`, with four lists and a fifth in a path's kind, grows from 168 bytes in memory to 200, whether its lists hold anything or not.
Each allocation the small layouts remove saves 50 to 230 bytes, by the differences between the layouts.

| opening the stored drawing, median of 21, ms | slotted | packed | small ids | small attributes | small |
| --- | --- | --- | --- | --- | --- |
| tiger | 1.5 | 1.8 | 1.5 | 1.3 | 1.1 |
| coat of arms | 2.3 | 2.6 | 2.2 | 2.0 | 1.7 |
| world map | 8.0 | 9.0 | 7.8 | 6.5 | 5.5 |

Opening follows memory: packed lists decode their elements one by one and build their offsets, and fewer allocations mean fewer statements to load.
Opening the world map takes less than parsing its SVG into the model in every layout, 5.5 to 9.0 ms against 9.5 to 10.3; on the smaller drawings, the two are within a millisecond of each other.

**Reopening a drawing closed after the edits takes up to four times as long as opening it fresh, the longer the more its address table has grown.**

| reopening after the edits, median of 21, ms | slotted | packed | small ids | small attributes | small |
| --- | --- | --- | --- | --- | --- |
| tiger | 1.0 | 2.3 | 1.1 | 1.5 | 1.4 |
| coat of arms | 3.6 | 4.8 | 3.7 | 3.4 | 2.8 |
| world map | 35.1 | 25.6 | 19.6 | 16.0 | 12.5 |

An open reads every table page and every statement, and the edits leave both far more numerous than a fresh store: the slotted world map closes with 287 table pages and 32,099 statements, the small one with 75 and 14,066.
On the tiger, the packed layout reopens slowest, with 21 table pages and 2,711 statements; the small one, with as many pages and 1,162 statements, takes 1.4 ms.
Reopening the edited world map takes 4.4 times as long as opening the drawing fresh in the slotted layout, and 2.3 times in the small one.

## What to change

- **Defragment the descriptions that splices fragment.**
  Restating keeps every fragment a shifted tail lies in, and every splice adds one, so a list that is spliced again and again is described by ever more, ever shorter statements, each of which the next splice restates.
  On the world map, the small layout writes four times the table pages of packed lists, and the edits multiply the statements three- to fivefold, which also makes the edited file slower to reopen.
  The cost estimate that chooses between restating a piece and writing it again looks at one flush; writing a tail again also merges its fragments, which pays at every later splice, so a tail whose fragments have grown short is better written again, or left to description defragmentation aimed at the allocations that are spliced most.
- **Keep small values to ids and strings, and to attribute lists where memory counts for more than writes**; keep child lists and path data in packed vectors while splices fragment what they shift.
  Packed vectors for every list of enums whose variants differ in size pay on all three drawings, in space and in writes, for 4 to 9 % more memory, which small ids more than win back.
- **Consolidate the address table harder at these sizes.**
  It runs 12 to 61 % full during the edits, and its pages are what makes an edited file slow to reopen.
- **Return the free pages when a file closes**, not only those at its end: a closed file keeps the two to four flushes' worth of pages that were free when it closed.
- **Reconsider the default journal budget, 256 pages**, which lets a megabyte of journal build up between flushes, more than these drawings take; at four pages, a flush comes every 243 to 477 edits.
- **Shrink what a file takes besides its content**: two header slots and a journal page, three of the four pages a small drawing's file takes.

## Reproducing

The corpus comes from `crates/kladde-svg/corpus/fetch.sh` in kladde-rs, and each layout is one build of kladde-svg's two programs, from `f3f7eeb` on the `variable-size` branch:

```sh
C=crates/kladde-svg/corpus
B="--journal-budget 4"
for layout in base packed small-ids small-attrs small; do
    features=$([ $layout = base ] || echo "--features $layout")
    cargo build --release -p kladde-svg $features
    ./target/release/svg-roundtrip $B --csv rt-$layout.csv $C/drawings $C/resvg
    ./target/release/svg-bench $C/drawings/tiger.svg mix-$layout --edits 31613 $B
    ./target/release/svg-bench $C/drawings/coat-of-arms-spain.svg mix-$layout --edits 41130 $B
    ./target/release/svg-bench $C/drawings/world-map.svg mix-$layout --edits 177831 $B
    for kind in Drag MoveNode InsertNode DeleteNode Recolor Duplicate Delete Restack; do
        for d in coat-of-arms-spain world-map; do
            ./target/release/svg-bench $C/drawings/$d.svg only-$layout-$kind --edits 20000 --only $kind $B
        done
    done
done
```

Main's runs, at `113cfc3` with `journal_budget_pages: 4` as the default in `kladde-store/src/options.rs`, are `svg-roundtrip` on the same paths and `svg-bench` on each drawing without `--edits`.

The tables are kept gzipped under `content/evaluation/data/variable-size/`:

- the mix in `<layout>/`, with `base` for the slotted layout, and main's in `main/`, each run's summary beside its table in `<drawing>.log`;
- the round trips in `roundtrip/<layout>.csv.gz`;
- the runs of one kind in `kinds/<layout>-<kind>/`, summed up per edit in `kinds/edit-kinds.tsv`;
- memory and the time to open in `memory.txt`, measured with `memory.rs` built against each layout, and an empty drawing's live bytes in `empty.txt`;
- the bytes still written again behind splices, and those restated, in `hoisted.txt`, counted with `hoist-counter.patch` applied to kladde-rs.

The figures are drawn from them:

```sh
D=content/evaluation/data/variable-size
tools/plot-evaluation.py --only svg-layouts --out content/evaluation/figures/variable-size \
    base=$D/base packed=$D/packed small-ids=$D/small-ids small-attrs=$D/small-attrs small=$D/small
tools/plot-evaluation.py --only svg-edit-kinds --out content/evaluation/figures/variable-size kinds=$D/kinds
```
