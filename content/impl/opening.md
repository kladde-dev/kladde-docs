---
title: Opening a file
---

The steps from the two headers to a decoded root value, and what each checks.

**Opening reads every live table page once, walks each chain once, reads the data pages the chains show in page order, decodes the sequence, and replays the journal: `O(entries + n)` for a sequence of `n` bytes.**
There is no sweep resolving bytes across epochs, no clipping at sizes, and nothing to resolve per byte.

## The steps

1. **Read both headers and take the governing one**: the CRC-valid header with the highest epoch ([recovery](../spec/durability.md#recovery--loading-a-kladde-file)).
   `fsync` the file once, which makes that header durable and retires the older world at once.
2. **Read every table page the header reaches**, through its child references, checking that they form a tree, and keep, for each span id, the entry of the highest epoch in the [span table](in-memory-state.md#1-the-span-table).
   No sorting is needed: an id's entries are compared by their pages' epochs as they come.
   Each page's framing is checked, and its epoch must not exceed the header's.
3. **Walk each chain from its head**, building the [rope](in-memory-state.md#2-the-rope) for the sequence and lists for the short chains.
   Refuse the file on an id no entry describes, a span reached twice, or a span two chains share; count each reached span's entry, and its bytes, toward its pages' coverage, and enter it in its data page's [reverse list](in-memory-state.md#4-the-reverse-index).
   Entries of spans no chain reaches are dead, and their ids free.
4. **Read the data pages the reached spans show, in the order of their page numbers, into the [mirror](index.md#the-mirror).**
   Each must be a `Data` page whose framing checks and whose epoch does not exceed the header's, and each `Ref` must lie inside its content.
   Reading them in the sequence's order instead, as step 5 needs them, would jump between pages wherever the sequence was ever edited; step 3 has already found which pages are needed, so step 4 reads them in one sequential pass.
5. **Decode the sequence through a cursor that follows the rope**: the root type's decoder reads front to back, and every container [builds its index of children](values.md#positions-belong-to-containers) as it goes.
   A recursive type nests as deeply as the file says, so the decode limits its depth, or does not recurse, and refuses a file beyond the limit rather than overflow its stack.
   It refuses an encoding that is not [canonical](../spec/schema/type-descriptors.md#encodings), and bytes left after the root value.
   Then drop the mirror.
6. **Replay the journal**, applying each recovered transaction's records to the in-memory values as well as to the store's structures.

If the header's root fingerprint differs from the application's root type's, step 5 decodes by the writer's schema chain instead, [resolving](../spec/schema/evolution.md#what-is-settled) it once against the application's types, or refuses the file if resolution is not implemented.

## Replaying through a flush

**An implementation may fold the recovered journal in a flush at open, before step 5**, so that the decode reads the state after the last transaction and needs no replay of its own.
It has to fold a non-empty recovered journal before appending to it anyway ([the start of a session](../spec/journal.md#the-start-of-a-session)), and replaying records into in-memory values would need every container to accept byte-level records it did not make.
The price is that a file whose journal was not empty costs a flush to open.

## The seeds of the rotating walk and of heat

What the file does not record, open seeds:

- **the heat** of each span, from its entry's page epoch, which can only understate how long its bytes have gone unwritten — content is never younger than the page that holds its entry — or from the [consolidator state](consolidator-state.md), if one is current;
- **the rotating walk's position**, from the consolidator state, or else at a span chosen by the governing header's CRC modulo the number of spans: deterministic for a given file, so tests reproduce, and spread over the sequence across sessions.
