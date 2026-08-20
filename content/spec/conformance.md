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
- **Stable ids.** Preserve every allocation's id across folds and across compaction.
- **Ownership.** Maintain exactly one owning pointer per allocation.
- **Journal framing.** Frame every record with a length prefix and a checksum, and recover a torn tail by truncation.
- **Prefix validity.** Order the records of a single mutation so that any prefix replays to a valid state.
- **Descriptor encoding.** Produce byte-identical output to the [canonical encoding](schema/canonical-encoding.md) for the same type graph.
- **Fingerprints.** Produce bit-identical output to [the fingerprint computation](schema/fingerprints.md), including on recursive types.
- **Crash consistency.** Ensure that a crash at any instant leaves a file that opens.

## May

- Choose any placement policy.
- Compact, or not compact, by any strategy.
- Fold at any time, and optimize the fold arbitrarily, so long as the result is indistinguishable from an in-order replay given that [unwritten bytes are unspecified](allocations.md#content-semantics).
- Assign, reuse, and recycle ids however it likes.
- Expose any API shape at all.
  Nothing above the storage layer is constrained.

## Must not

- Change an allocation's id, size, or content during compaction.
- Assume that unwritten bytes hold any particular value.
- Depend on the descriptor table's index assignment for any semantic purpose.
- Emit a fingerprint that depends on table layout, traversal order, or any runtime-incidental state.

## The suite

The conformance suite should have three parts.

**Vector tests** — pure functions, checkable without any file I/O.

- A set of type graphs with their expected canonical encodings, byte for byte.
- The same graphs with their expected fingerprints, including recursive and mutually recursive cases, and cases that differ only by table permutation (which must produce equal fingerprints) or only by variant declaration order (likewise).
- Descriptor decoding, including rejection of malformed input.

**File tests** — a corpus of files, each with a manifest describing its expected [level-2 and level-3](tooling.md) interpretation.
Every implementation must reproduce the manifest from the file.
Files written by each implementation are added to the corpus, so that the suite grows to cover the cross product.

**Crash tests** — for each of a set of mutation sequences, truncate the resulting file at every byte offset, open it, and assert that it opens successfully and yields a state that is a valid prefix of the intended one.
This is the only mechanical check on the [ordering discipline](journal.md#ordering), which is otherwise a matter of implementer care.

## Open questions

- Should conformance be **versioned** — an implementation claiming "conforms to spec version N" — and can a level of partial conformance (say, read-only) be meaningful?
- How are reserved features handled?
  An implementation that encounters a reserved [primitive code](schema/type-descriptors.md#reserved-primitive-codes) or the Array kind must reject it rather than guess, but should that be a conformance requirement or a recommendation?
- Should the suite mandate a minimum level of tooling support, so that every implementation ships something that can dump a file?
