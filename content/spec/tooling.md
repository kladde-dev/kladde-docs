---
title: Tooling
---

What a generic tool may assume about a kladde file it did not write, and how far it can get without knowing the application's data structures.

**Status:** design intent.
No tools exist yet.

## The premise

A kladde file is self-describing in two independent ways.
It describes its **storage**: the [span table](span-table.md) is reachable from the header and describes every span of every chain, so the file's structure is recoverable without any application knowledge at all — and without a scan, since everything is rooted.
And it describes its **types**: the [schema chain](sequence.md#three-chains) says how every value in the sequence is encoded.

Together these mean a tool can be useful at three levels, each requiring strictly more than the last.

## Level 1 — storage analysis

Requires: the [page](file-format.md), [span-table](span-table.md) and [sequence](sequence.md) layers.
Requires **no** schema and no application knowledge.

A tool at this level can:

- list every chain, its length, and its spans with the pages they show bytes of;
- compute the live bytes, the description's share, the file's length, and therefore **garbage**;
- report the per-page live fraction, and how much of the file is superseded pages awaiting reuse;
- report the journal's length and how far the committed state lags behind it;
- **consolidate** the file into a tight new one, and truncate it.

Rewriting a file from an external tool is safe for exactly the reason it is safe internally: it changes the content of no chain, so it is invisible to any reader.
This makes a standalone `kladde compact` genuinely implementable, which is unusual for a format of this kind.

What it cannot do is tell you what any of it *means*.

## Level 2 — schema inspection

Requires: additionally the [descriptor table](schema/canonical-encoding.md).
Still requires no application knowledge.

A tool at this level can:

- list every type in the file, with its kind, fields, variants, and whether it is fixed-size;
- compute and display each type's [fingerprint](schema/fingerprints.md);
- identify the root type;
- report which types are Opaque, and their declaring library and version — which is exactly the list of things a reader would need an implementation of;
- diff two files' schemas, or a file's schema against an application's.

This is what makes "why won't my application open this file?" a diagnosable question rather than a guess.

## Level 3 — structural export

Requires: additionally decoding the sequence by the descriptors.
Requires no application knowledge.

A tool at this level can render the whole content as text by decoding the sequence from the root descriptor, front to back: primitives decode by their code, structs by their fields, enums by their discriminant, sequences by their count, text up to its terminator.
Every container is described structurally, there is no pointer graph to follow, and nothing can be unreachable, so the export is complete.

It stops only at **Opaque** types, whose content is by definition not decomposable: a tool can report their identity and version, and dump their bytes, which the encoding delimits, but not interpret them.

## A canonical textual representation

**The specification should eventually define a canonical textual representation of kladde files**, and canonical conversions to and from common human-readable formats such as JSON.
These representations would cover the schema and the sequence: not the private chain, which any writer may keep, replace or clear without changing what the file holds, and not how either is cut into spans.
Two files with the same schema and sequence would then have the same text, whatever implementation wrote them and however often they were consolidated.

## Cross-implementation testing

Tools are also the natural vehicle for testing the cross-language contract.
A conformance suite would consist of files written by each implementation, plus the expected level-2 and level-3 output for each, so that every implementation can be checked against every other's output without either needing to link the other.

See [Conformance](conformance.md).
