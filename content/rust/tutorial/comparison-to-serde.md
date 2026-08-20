---
title: Compared to serde
---

The most common question about kladde is how it relates to serde.
The short answer: they solve different problems and compose badly, and if you are choosing between them you are probably choosing between "exchange" and "storage."

## The core difference

**Serde serializes a value.**
You call `to_vec(&value)`, you get bytes, you write them somewhere.
The cost is proportional to the size of the whole value, every time.

**Kladde stores a structure and records changes to it.**
You mutate it, and the cost is proportional to the size of the change.

For a 100 MB structure with a one-field edit:

| | serde | kladde |
| --- | --- | --- |
| cost of one small edit | rewrite 100 MB | append tens of bytes |
| cost of a read | free (already in memory) | free (already in memory) |
| cost of opening | parse 100 MB | read the snapshot, replay the journal |
| crash mid-edit | lose everything since the last write | lose nothing acknowledged |

The interesting column is the first one, and it is the whole reason kladde exists.

## What serde does better

**Interchange.**
Serde targets many formats — JSON, MessagePack, CBOR, YAML — and other systems can read them.
A kladde file is a heap with an embedded schema.
Another kladde implementation can read it; nothing else can.

**Type coverage.**
Serde works on essentially any Rust type, including `String`, `Vec<T>`, `HashMap`, tuples, and third-party types with derives.
Kladde needs its own container types and does not yet support generics in its derive.

**Maturity.**
Serde is one of the most-used crates in the ecosystem.
Kladde is early.

**Zero setup.**
`#[derive(Serialize)]` and you are done.
Kladde asks you to restructure your types around backed containers.

## What kladde does better

**Incremental durability.**
The one thing serde structurally cannot do.
There is no serde-based design where a small edit to a large structure costs less than rewriting the structure, short of building an incremental format yourself — which is what kladde is.

**Crash safety with no explicit save.**
With serde you choose when to write, and everything since the last write is at risk.
With kladde, a mutation that has returned is durable.

**In-place mutation of stored data.**
A kladde file is randomly addressable and mutable.
A serde document is a byte stream you rewrite.

**Language-independent tooling.**
A kladde file carries its own [schema](../../spec/schema/), so a generic tool can inspect and even compact it.
A postcard blob is opaque without the writing program.

## They are not exclusive

`PersistableBlob<T>` wraps any serde type as an opaque payload inside a kladde structure.

This is the right escape hatch for a foreign type you cannot change, and the wrong default for anything else: a blob is rewritten in full on every change, so it forfeits exactly the property you came for.
Use it at the leaves, for things that change rarely.

## Choosing

Use **serde** when the data leaves your process: config files people edit, API payloads, anything another program reads.

Use **kladde** when the data *is* your application's state: it lives across runs, it is large relative to each change, and losing it to a crash is unacceptable.

Use **both** when your state is mostly kladde-shaped but contains a few foreign leaves.

## An honest caveat

Serde is a safe default and kladde is not yet.
If your structure is small enough that rewriting it is cheap — under a few megabytes, say, edited a few times a second — serde is simpler, better tested, and fast enough.
Kladde starts winning when rewriting starts hurting.
