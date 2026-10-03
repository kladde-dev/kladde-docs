---
title: Schema
---

How a kladde file describes the types it contains.

This is the most fully specified part of kladde: it is pinned down precisely enough that any conforming implementation, in any language, produces **byte-identical** descriptor serializations and **bit-identical** fingerprints.

## Why a file carries its schema

Two reasons, one defensive and one enabling.

**Defensive.** Without a schema, opening a file written by a different version of an application is undefined behaviour dressed as a successful read: offsets shift, a field is interpreted as the field next to it, and the corruption is silent.
An embedded schema turns that into a detectable error.

**Enabling.** A schema that describes *layout*, not merely identity, carries enough information to actually bridge a difference rather than just report one.
A reader whose struct gained a field can determine, from the writer's descriptors, where the fields it does share actually live, and read them correctly.
That is what makes evolution possible at all.

The cost model is what makes this affordable.
The schema is stored **once per file**, not per value — unlike a tagged format such as JSON or Protobuf, where every instance pays.
For a file holding millions of instances of a type, the schema blob is negligible; for a small settings file it may be a noticeable fraction, which is the honest trade.

## The documents

| document | contents |
| --- | --- |
| [Type descriptors](type-descriptors.md) | the model: what kinds of type exist and what each carries |
| [Canonical encoding](canonical-encoding.md) | the byte serialization of a descriptor table |
| [Fingerprints](fingerprints.md) | the reproducible 128-bit hash identifying a type's representation |
| [Evolution](evolution.md) | how a reader reconciles its own types against a writer's — **largely TBD** |

## The guiding principle

> **A descriptor describes a representation, not an identity.**

A descriptor says how bytes are laid out and interpreted.
It does not say which source-language type produced them.
Two types with byte-identical representations have identical descriptors and identical fingerprints, and this is intentional.

It is also harmless, because a value is always reconstructed **top-down from a known root type**.
A reader never asks "what type is this anonymous blob?" — the parent field's declared type already determines which loader to call.
The descriptor exists only to answer "does the writer's layout for this field match what my loader expects, and if not, how do I bridge it?"

This principle resolves several questions that otherwise look hard.
A type with a hand-written implementation that happens to write exactly the bytes of a plain struct *has* that struct's descriptor — the presence of non-persisted fields in the author's source type is invisible and irrelevant.
A type is [Opaque](type-descriptors.md#opaque) not because its implementation is hand-written but because its representation is genuinely not decomposable.
And a string and a packed vector of `char`s, which lay out the same UTF-8 bytes behind the same kind of pointer, share a descriptor however differently they keep their content in memory.

What a descriptor therefore cannot say is what a library makes of a structure — that a string is kept normalized, say — and only an Opaque descriptor names the library that defines its type.
A way to annotate any descriptor with that library is a [draft](../../drafts/library-annotations.md).

## Scope

These documents define how descriptors are **modeled, serialized, and fingerprinted**.

Where the descriptor table physically sits in a file is a [container-layer](../file-format.md#header-pages) question.
How a reader *resolves* a mismatch — the compatibility policy — is [Evolution](evolution.md), and is the least settled part of the specification.
