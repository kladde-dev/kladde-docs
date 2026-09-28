---
title: kladde
---

**Durable data structures: mutate in memory, and it's on disk.**
A cross-language file format with implementations.

Founded and currently maintained by [Robert Bamler](https://robamler.github.io/).
Source on [GitHub](https://github.com/kladde-dev); all of these pages also as [one PDF](https://kladde-dev.github.io/kladde.pdf).

**Kladde** is becoming a system for *durable data structures*: containers (strings, vectors, hash maps, trees, ...) and user-defined types that behave like their ordinary in-memory counterparts, but whose every mutation is durably recorded to a file as it happens, in a way that is optimized for disk I/O.
Unlike serialization formats like JSON or XML, there is no save step and no serialization pass.
Unlike sqlite, there is no SQL and no object-relational layer.
You open a value from a file (typically a large and complex nested data type), you mutate some parts of it (almost) the way you would mutate any other value, and your changes are immediately and efficiently applied to the file.

The idea is known as *orthogonal persistence*: a program works with persistent data exactly as it works with transient data, and data persists by being reachable from a persistent root.
Kladde brings it to existing languages as libraries, as far as each language allows.

Reads never touch the file.
A durable data structure keeps a full, natively-typed in-memory representation, so reading a field or iterating a map costs what it costs in the host language, not what it costs in a database.
Writes go two places at once: into that in-memory representation, and into an append-only on-disk journal that is durable by the time the mutating call returns.
Whenever the journal reaches a threshold size, it is folded into the bulk part of the file, in a way that minimizes I/O and guarantees [durability under application crashes and consistency even under power outages](spec/durability.md).

The name is German: a *Kladde* is a merchant's rough day-book, where transactions are scribbled down in order as they happen and later transcribed into the clean *Hauptbuch*.
The file format has exactly that shape.

## Project status

The project is still very young and under active development.
At the current stage, the **main products** are

- a [detailed file format specification](spec/) and
- a [documentation of a reference implementation](impl/).

The reference implementation itself is *not production ready*.
Its purpose at the moment is to allow rapid experimentation on new *policies* (non-normative decisions that an implementation may make without breaking interoperability).
This is why the reference implementation is currently limited to a single language (Rust) and why it is currently fully auto-generated from the specification.

**Contributions are welcome.**
While this includes code contributions in principle, at the current stage, **the most valuable contributions are not in the form of code** but instead in the form of well-motivated opinions and proposals of what should be added or changed in the specification.
If you have a potential use case for a system like kladde and you think that something in its currently stated design would hold you back, then I want to hear your opinion.
Please feel free to describe your situation in a [new issue](https://github.com/kladde-dev/kladde-docs/issues/new).

## The ambition

Kladde is meant to be a **cross-language system**, not a library for one language.

At the centre sits a language-independent specification of the file format and the schema description — everything all implementations must agree on.
Around it sit implementations for individual languages: [kladde-rs](rust/) first, with `kladde-cpp`, `kladde-python`, `kladde-java` and others intended to follow.

The contract between them is deliberately narrow and deliberately strict:

> A file written by any conforming implementation can be opened by any other conforming implementation, with no conversion step and no special treatment, as long as the opening application implements equivalent data structures.

Everything the specification does not pin down is free to vary, and implementations are expected to differ substantially beyond the normative part (e.g., how aggressively they consolidate free space).
Each language should get an API that is idiomatic for that language rather than a transliteration of the Rust one.
For example, the Rust implementation of kladde needs an explicit `Guard` type to intercept field assignments; by contrast, a Python implementation of kladde should intercept field assignments via property hooks so that `doc.title = "..."` simply persists.

## Language-independent tooling

A file format that several languages can read is also a file format that *tools* can read, without knowing anything about the application that wrote it.
A generic tool can enumerate allocations and measure fragmentation without interpreting a byte of application data, read the file's embedded type descriptors to report what types it contains, and walk the value graph from the root to render it as text.
See [Tooling](spec/tooling.md) for how far each of those goes.

## The sections

| section | what it holds | who it is for |
| --- | --- | --- |
| **[Specification](spec/)** | the normative, language-independent description: what a kladde file is, what invariants it must uphold, and what guarantees an implementation must provide | implementers porting kladde to a new language; tool authors |
| **[Implementation](impl/)** | the reference algorithms and data structures — what must be maintained in memory to satisfy the spec, and how — stated in language-agnostic pseudocode | anyone building an implementation in any language |
| **[kladde-rs](rust/)** | everything specific to the Rust implementation: crate layout, traits, guards, macros, and the Rust-specific half of the algorithms | Rust users, and Rust contributors |
| **[Evaluation](evaluation/)** | measurements of kladde-rs on realistic workloads, and what they say about the design | anyone judging the design or choosing its constants |
| **[Superseded](superseded/)** | designs that were worked out and then rejected, kept for the reasoning | anyone tempted to re-propose one of them |
| **[Drafts](drafts/)** | half-baked ideas in progress, not yet held to the separation the other sections observe | the authors |

The three-way split between **spec**, **impl**, and **rust** is the organising idea, and the test for which one a statement belongs in is:

- Could two implementations disagree about it and still read each other's files? If no, it is **spec**.
- Would a Python or C++ implementation make substantially the same choice? If yes, it is **impl**.
- Otherwise it is **rust**.

## License

Everything here — text, figures, and data — is available under your choice of CC BY 4.0, MIT, Apache 2.0, or the Boost Software License 1.0; the [repository](https://github.com/kladde-dev/kladde-docs#license) has the details.

**Patent pledge.** I, Robert Bamler, will not assert any patent I own or control against any implementation of the kladde specification.
