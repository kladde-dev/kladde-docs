---
title: kladde
---

**Kladde** is a system for *backed data structures*: containers and user-defined types that behave like their ordinary in-memory counterparts, but whose every mutation is durably recorded to a file as it happens.
There is no save step, no serialization pass, and no object-relational layer.
You open a value from a file, you mutate it the way you would mutate any other value, and it is on disk.

Reads never touch the file.
A backed data structure keeps a full, natively-typed in-memory representation, so reading a field or iterating a map costs what it costs in the host language, not what it costs in a database.
Writes go two places at once: into that in-memory representation, and into an append-only on-disk journal that is durable by the time the mutating call returns.
The bulk on-disk form — the *snapshot* — lags behind, and is brought up to date periodically when the journal is folded into it.

The name is German: a *Kladde* is a merchant's rough day-book, where transactions are scribbled down in order as they happen and later transcribed into the clean *Hauptbuch*.
The file format has exactly that shape.

## The ambition

Kladde is meant to be a **cross-language system**, not a library for one language.

The centre of the project is a **language-independent specification** of the file format and the schema description — everything that all implementations must agree on.
Around that centre sit implementations for individual languages: [kladde-rust](rust/) first, with `kladde-cpp`, `kladde-python`, and `kladde-java` intended to follow.

The contract between them is deliberately narrow and deliberately strict:

> A file written by any conforming implementation can be opened by any other conforming implementation, with no conversion step and no special treatment, as long as the opening application implements equivalent data structures.

Everything the specification does not pin down is free to vary.
Implementations are expected to differ — in fact, they are expected to differ *substantially* — above the storage layer.
Each language should get an API that is idiomatic for that language rather than a transliteration of the Rust one.
Rust needs an explicit `Guard` type because it has no way to intercept a field assignment; Python does not, and a Python implementation should use property hooks so that `doc.title = "..."` simply persists.
Java, C++, and others will each want something different again.

Below that layer, implementations may still differ, but only where the specification allows it.
Two implementations may use entirely different allocation and compaction strategies, so long as both produce files that satisfy the format's invariants.
A small embedded implementation might never compact at all; a server-side one might compact aggressively.
Neither choice is visible in the resulting file beyond the layout it happens to produce.

## Language-independent tooling

A file format that several languages can read is also a file format that *tools* can read, without knowing anything about the application that wrote it.
Kladde is designed so that a generic tool can do useful work on any kladde file:

- **Storage-level analysis** — enumerate allocations, report their sizes and addresses, measure fragmentation, and run compaction, all without interpreting a single byte of application data.
- **Schema-level inspection** — read the file's embedded type descriptors and report what types it contains, how they are laid out, and what their fingerprints are.
- **Structural export** — walk the value graph from the root and render it in a textual format, as far as that is possible without knowing what the opaque types mean.

The third of these is inherently partial: a type that declares itself opaque is a black box by definition, and a tool can report its identity, version, and size but not its contents.
Everything structural — the containers, the derived structs and enums, the primitives — is fully walkable.

## Where to start

- **[Specification](spec/)** — the normative, language-independent description: file format, allocation model, journal records, and the schema-description model.
  Start here if you are implementing kladde for a new language, or writing a tool that reads kladde files.
- **[kladde-rust](rust/)** — the reference implementation.
  Its [tutorial](rust/tutorial/) is for people who want to *use* kladde in a Rust application; its [design documents](rust/design/) are for people who want to work on it, or to use its architecture as a blueprint for a port.

## Status

Early and moving.
The Rust implementation is the only one that exists, it is not yet feature-complete, and the file format has not been frozen.
Documents in this hub describe the system as it is *intended* to be, not as it is currently built; sections that record a decision that has not yet been made are marked **TBD**.
Nothing here should be treated as a stable interface until the specification carries a version number and a conformance suite.
