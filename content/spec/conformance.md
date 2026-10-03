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
- **Stable ids.** Preserve every allocation's id across flushes and across consolidation.
- **Ownership.** Maintain exactly one owning pointer per allocation.
- **The reuse rule.** Write only to [reusable](durability.md#page-states) pages, or extend the file ([I2](durability.md#the-two-invariants)).
- **One `fsync` before the header.** Issue a header only after an `fsync` that covered everything it references and the previous header ([I1](durability.md#the-two-invariants)); treat a failed `fsync` as fatal for the session.
- **Page framing.** Write the `kind`, `content_size`, `epoch` and `crc` on every non-header page, and ignore any page whose CRC does not validate.
- **Journal encoding.** Encode records, transactions, and journal pages [as specified](journal.md#encoding), and recover a torn tail by keeping the longest valid prefix — without skipping a hole.
- **The start of a session.** Fold a non-empty recovered journal before appending to it, and write the journal page that a session's first flush names ([the start of a session](journal.md#the-start-of-a-session)).
- **Transaction atomicity.** Replay every transaction completely or not at all.
- **Valid transaction boundaries.** Order records so that every transaction takes a valid state to a valid state ([ordering](journal.md#ordering)).
- **Bounds.** Support the [stated bounds](address-table.md#bounds), and fail cleanly rather than wrap when an application exceeds them.
- **Descriptor encoding.** Produce byte-identical output to the [canonical encoding](schema/canonical-encoding.md) for the same type graph.
- **Fingerprints.** Produce bit-identical output to [the fingerprint computation](schema/fingerprints.md), including on recursive types.
- **Crash consistency.** Ensure that a crash at any instant leaves a file that opens.

## May

- Choose any placement policy — which reusable page a flush writes to, and how content is cut across pages.
- Consolidate, or not consolidate, by any strategy.
- Group application operations into transactions however it likes, including not at all.
- Fold at any time, and optimize the fold arbitrarily, so long as the result is indistinguishable from an in-order replay.
- Assign, reuse, and recycle ids however it likes.
- Expose any API shape at all.
  Nothing above the storage layer is constrained.
- Read none of an allocation's bytes while loading a value from it.
  A data type may leave any part of its content untouched, and a tool that only compacts or only edits one subtree reads no content at all — so what a file keeps resident must follow from the file, never from which types happened to look.
- Keep state of its own in the [consolidator state](file-format.md#the-consolidator-state), and keep, replace, or clear a state it finds there.
- Give a reusable page's blocks back to the file system ([deallocating reusable pages](durability.md#deallocating-reusable-pages)).

## Must not

- Change an allocation's id, size, or content during consolidation.
- Overwrite or deallocate a page that is not [reusable](durability.md#page-states), or truncate the file below one or to anything but a page boundary ([the truncation rule](durability.md#the-truncation-rule)).
  This rule is relaxed for the last journal page, which may be appended to, and if it is empty (i.e., if its prefix of CRC-valid transactions is empty) then the file may be truncated below it.
- Continue a session after a failed `fsync`.
- Return anything but zero for a byte that has not been explicitly written.
- Depend on the descriptor table's index assignment for any semantic purpose.
- Make a page's residency, or any storage-layer decision, depend on whether a data type read from it.
- Emit a fingerprint that depends on table layout, traversal order, or any runtime-incidental state.
- Let the consolidator state affect what any allocation contains, or rely on one without first checking that it is its own and still current ([the consolidator state](file-format.md#the-consolidator-state)).

## The suite

The conformance suite should have three parts.

**Vector tests** — pure functions, checkable without any file I/O.

- A set of type graphs with their expected canonical encodings, byte for byte.
- The same graphs with their expected fingerprints, including recursive and mutually recursive cases, and cases that differ only by table permutation (which must produce equal fingerprints) or only by variant declaration order (likewise).
- Descriptor decoding, including rejection of malformed input.

**File tests** — a corpus of files, each with a manifest describing its expected [level-2 and level-3](tooling.md) interpretation.
Every implementation must reproduce the manifest from the file.
Files written by each implementation are added to the corpus, so that the suite grows to cover the cross product.

**Crash tests** — two kinds, checking different things.

- *Journal prefix.* For each of a set of mutation sequences, truncate the resulting journal at every byte offset, open it, and assert that it opens successfully and yields a state that is a valid prefix of the intended one.
  This is the only mechanical check on the [ordering discipline](journal.md#ordering), which is otherwise a matter of implementer care.
- *Flush interruption.* For each of a set of flushes, simulate a power cut after every individual page write — including reordering the writes, since write-back is not ordered — and assert that the file opens and lands on either the previous committed state or the new one, never in between.
  This is the mechanical check on [I1 and I2](durability.md#the-two-invariants).

A complementary check is a **leak detector**: walk every allocation reachable from the root via the type structure, add the allocations the header names directly, compare against the set the address table says is live, and assert they match.
That checks the "clean up" half of the ordering discipline, which the prefix test does not.
Since every owning pointer outside an opaque type is a [`Pointer` descriptor](schema/type-descriptors.md#pointer), the walk needs only the file's own descriptors, and no application's types, as long as no opaque type holds a pointer.

## Open questions

- Should conformance be **versioned** — an implementation claiming "conforms to spec version N" — and can a level of partial conformance (say, read-only) be meaningful?
- How are reserved features handled?
  An implementation that encounters a reserved [primitive code](schema/type-descriptors.md#reserved-primitive-codes) or the Array kind must reject it rather than guess, but should that be a conformance requirement or a recommendation?
- Should the suite mandate a minimum level of tooling support, so that every implementation ships something that can dump a file?
