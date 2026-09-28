---
title: kladde
---

**Durable data structures: mutate in memory, and it's on disk.**
A cross-language file format with implementations.

Founded and currently maintained by [Robert Bamler](https://robamler.github.io/).
Source on [GitHub](https://github.com/kladde-dev); all of these pages also as [one PDF](https://kladde-dev.github.io/kladde.pdf).

**Kladde** is a system for *backed data structures*: containers and user-defined types that behave like their ordinary in-memory counterparts, but whose every mutation is durably recorded to a file as it happens.
There is no save step, no serialization pass, and no object-relational layer.
You open a value from a file, you mutate it the way you would mutate any other value, and it is on disk.

The idea is known as *orthogonal persistence*: a program works with persistent data exactly as it works with transient data, and data persists by being reachable from a persistent root.
Kladde brings it to existing languages as libraries, as far as each language allows.

Reads never touch the file.
A backed data structure keeps a full, natively-typed in-memory representation, so reading a field or iterating a map costs what it costs in the host language, not what it costs in a database.
Writes go two places at once: into that in-memory representation, and into an append-only on-disk journal that is durable by the time the mutating call returns.
The bulk on-disk form lags behind, and is brought up to date periodically when the journal is folded into it.

The name is German: a *Kladde* is a merchant's rough day-book, where transactions are scribbled down in order as they happen and later transcribed into the clean *Hauptbuch*.
The file format has exactly that shape.

## The ambition

Kladde is meant to be a **cross-language system**, not a library for one language.

At the centre sits a language-independent specification of the file format and the schema description — everything all implementations must agree on.
Around it sit implementations for individual languages: [kladde-rs](rust/) first, with `kladde-cpp`, `kladde-python`, and `kladde-java` intended to follow.

The contract between them is deliberately narrow and deliberately strict:

> A file written by any conforming implementation can be opened by any other conforming implementation, with no conversion step and no special treatment, as long as the opening application implements equivalent data structures.

Everything the specification does not pin down is free to vary, and implementations are expected to differ *substantially* above the storage layer.
Each language should get an API that is idiomatic for that language rather than a transliteration of the Rust one.
Rust needs an explicit `Guard` type because it has no way to intercept a field assignment; Python does not, and a Python implementation should use property hooks so that `doc.title = "..."` simply persists.

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

## Status

Early and moving.
The Rust implementation is the only one that exists, it is not feature-complete, and the file format has not been frozen.
These documents describe the system as it is *intended* to be; decisions not yet made are marked **TBD**, and designs since abandoned live in [Superseded](superseded/) rather than being deleted.
Nothing here should be treated as a stable interface until the specification carries a version number and a conformance suite.

## License

Everything here — text, figures, and data — is available under your choice of CC BY 4.0, MIT, Apache 2.0, or the Boost Software License 1.0; the [repository](https://github.com/kladde-dev/kladde-docs#license) has the details.

**Patent pledge.** I, Robert Bamler, will not assert any patent I own or control against any implementation of the kladde specification.
