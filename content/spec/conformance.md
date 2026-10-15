---
title: Conformance
---

What an implementation must do to claim it implements kladde, and how that claim is meant to be checked.

**Status: intent only.**
No conformance suite exists.
This document describes what one should contain.

## What conformance means

An implementation is conforming if:

1. every file it writes can be opened by every other conforming implementation, and
2. it can open every file that any other conforming implementation writes,

subject to the [compatibility contract](index.md#the-compatibility-contract) — the opening application must implement equivalent data structures for the types the file contains.

Note what this does *not* require.
It does not require that two implementations produce the same file for the same sequence of operations.
Placement, compaction, and fold timing are all free, so two implementations will routinely produce byte-different files with identical contents, and that is correct.

## Must

- **Header.** Write and validate the magic, format version, and minimum reader version.
  Refuse to open a file whose minimum reader version exceeds the implementation's own.
- **Fail closed.** Never interpret a file whose [root fingerprint](schema/fingerprints.md) differs from the expected one unless resolution actually succeeded.
  Silent misinterpretation is a conformance failure, not a quality-of-implementation issue.
- **Content.** Keep the sequence the [canonical encoding](sequence.md#what-a-valid-sequence-is) of one root value at every transaction boundary, and refuse a file whose sequence is not.
- **The reuse rule.** Write only to [reusable](durability.md#page-states) pages, or extend the file ([I2](durability.md#the-two-invariants)).
- **One `fsync` before the header.** Issue a header only after an `fsync` that covered everything it references and the previous header ([I1](durability.md#the-two-invariants)); treat a failed `fsync` as fatal for the session.
- **Page framing.** Write the `kind`, `content_size`, `epoch` and `crc` on every non-header page, and ignore any page whose CRC does not validate.
- **Journal encoding.** Encode records, transactions, and journal pages [as specified](journal.md#encoding), and recover a torn tail by keeping the longest valid prefix — without skipping a hole.
- **The start of a session.** Fold a non-empty recovered journal before appending to it, and write the journal page that a session's first flush names ([the start of a session](journal.md#the-start-of-a-session)).
- **Transaction atomicity.** Replay every transaction completely or not at all.
- **Valid transaction boundaries.** Group records so that every transaction takes a sequence that decodes to one that decodes ([ordering](journal.md#ordering)).
- **Cuts.** Paste only cuts of the same journal, each at most once, and refuse a journal that does otherwise ([cuts](journal.md#cuts)).
- **Bounds.** Support the [stated bounds](span-table.md#bounds), and fail cleanly rather than wrap when an application exceeds them.
- **Entries.** Keep [the rules for writers](span-table.md#rules-for-writers) — one entry per span id and epoch, and the reach rule — and refuse a file that [resolution](span-table.md#resolution) finds invalid: a chain that names an id no entry describes, reaches a span twice, or shares a span with another chain, or a reached `Ref` outside a data page's content.
- **Descriptor encoding.** Produce byte-identical output to the [canonical encoding](schema/canonical-encoding.md) for the same type graph.
- **Fingerprints.** Produce bit-identical output to [the fingerprint computation](schema/fingerprints.md), including on recursive types.
- **Crash consistency.** Ensure that a crash at any instant leaves a file that opens.

## May

- Choose any placement policy — which reusable page a flush writes to, how content is cut across pages, and how it is cut into spans.
- Consolidate, or not consolidate, by any strategy.
- Group application operations into transactions however it likes, including not at all.
- Fold at any time, and optimize the fold arbitrarily, so long as the resulting sequence is the one an in-order replay produces.
- Assign, reuse, and recycle span ids however it likes, within the reach rule.
- Expose any API shape at all.
  Nothing above the storage layer is constrained.
- Keep the sequence in any in-memory form, or decode none of it: a tool that only compacts reads no content at all, so what a file keeps resident must follow from the file, never from which types happened to look.
- Keep state of its own in the [private chain](file-format.md#the-private-chain), and keep, replace, or clear a state it finds there.
- Give a reusable page's blocks back to the file system ([deallocating reusable pages](durability.md#deallocating-reusable-pages)).

## Must not

- Change the content of any chain during consolidation.
- Overwrite or deallocate a page that is not [reusable](durability.md#page-states), or truncate the file below one or to anything but a page boundary ([the truncation rule](durability.md#the-truncation-rule)).
  This rule is relaxed for the last journal page, which may be appended to, and if it is empty (i.e., if its prefix of CRC-valid transactions is empty) then the file may be truncated below it.
- Continue a session after a failed `fsync`.
- Depend on the descriptor table's index assignment for any semantic purpose.
- Make a page's residency, or any storage-layer decision, depend on whether a data type read from it.
- Emit a fingerprint that depends on table layout, traversal order, or any runtime-incidental state.
- Let the private chain affect the sequence or the schema, or rely on a state found there without first checking that it is its own and still current ([the private chain](file-format.md#the-private-chain)).

## The suite

The conformance suite should have four parts.

**Vector tests** — pure functions, checkable without any file I/O.

- A set of type graphs with their expected canonical encodings, byte for byte.
- The same graphs with their expected fingerprints, including recursive and mutually recursive cases, and cases that differ only by table permutation (which must produce equal fingerprints) or only by variant declaration order (likewise).
- Descriptor decoding, including rejection of malformed input.

**File tests** — a corpus of files, each with a manifest describing its expected [level-2 and level-3](tooling.md) interpretation.
Every implementation must reproduce the manifest from the file.
Files written by each implementation are added to the corpus, so that the suite grows to cover the cross product.

**Crash tests** — two kinds, checking different things.

- *Journal prefix.* For each of a set of mutation sequences, truncate the resulting journal at every byte offset, open it, and assert that it opens successfully and that its sequence decodes to the state after some prefix of the transactions.
  This is the mechanical check on [the ordering rule](journal.md#ordering): that every container groups the records of one edit into one transaction.
- *Payload pages.* For each of a set of mutation sequences with [external bytes](journal.md#external-bytes), drop every subset of the payload page writes since the last `fsync`, and assert that the file opens on the state after some prefix of the transactions, losing none that every write of its payload pages reached.
- *Flush interruption.* For each of a set of flushes, simulate a power cut after every individual page write — including reordering the writes, since write-back is not ordered — and assert that the file opens and lands on either the previous committed state or the new one, never in between.
  This is the mechanical check on [I1 and I2](durability.md#the-two-invariants).

**Differential tests** — an implementation checks itself against a model.

- *The store against a byte vector.* Any series of `Write`, `Splice`, `Move` and `Paste`, flushes, reopens and simulated power cuts must leave the sequence holding the vector's bytes.
- *The values against their encoding.* After every operation, the sequence must equal the canonical encoding of the in-memory root value, byte for byte.

No leak detector is needed: nothing in a file can be unreachable from the header.

## Open questions

- Should conformance be **versioned** — an implementation claiming "conforms to spec version N" — and can a level of partial conformance (say, read-only) be meaningful?
- How are reserved features handled?
  An implementation that encounters a reserved [primitive code](schema/type-descriptors.md#reserved-primitive-codes), an unassigned kind tag, or an undefined [escape](journal.md#records) in the journal must reject it rather than guess, but should that be a conformance requirement or a recommendation?
- Should the suite mandate a minimum level of tooling support, so that every implementation ships something that can dump a file?
