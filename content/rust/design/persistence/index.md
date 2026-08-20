---
title: Persistence
---

Where types meet bytes: pointers, `Persistable`, guards, the containers, and the derive macro.

**This is the Rust-specific layer.**
Everything below it — the [heap](../heap/), the [journal](../journal/), the [schema model](../../../spec/schema/) — is shaped by the file format and ports more or less directly.
Everything here is shaped by Rust, and a port should expect to redesign it.

## The documents

| document | contents |
| --- | --- |
| [Pointers](pointers.md) | the four handle types, sizedness, and the ownership discipline |
| [Persistable and guards](persistable-and-guards.md) | the central traits, and why mutation needs a guard at all |
| [Containers](containers.md) | how the built-in containers are laid out and why |
| [The derive macro](derive-macro.md) | what it generates, and why per-type guards |
| [Freeing](freeing.md) | recursive reclamation — designed, not built |

## The central problem

Rust has no way to intercept a field assignment.

Every comparable system relies on exactly such a hook: Python has `__setattr__`, Smalltalk has message dispatch, Swift has property wrappers.
Those systems make persistence syntactically invisible — `self.foo = bar` just works — because the language hands them a place to stand.

Rust hands them nothing.
So kladde-rust makes the recording point **explicit and unforgeable** instead: the only mutating API is the one that records, and it is reached through a `Guard`.

Everything idiosyncratic about this layer follows from that one constraint.

## The seam, for porters

If you are porting kladde, the boundary is here, and it is sharp.

**Keep:** the heap, the journal semantics, the fold, the schema model, the pointer *encoding*, the ownership discipline, the prefix-replay ordering rule.
These are format-level facts.

**Redesign:** the trait shape, the guard mechanism, the way offsets are computed, and the container APIs.
A Python implementation should use property hooks and have no guards.
A C++ one might use proxy objects with `operator=`.
A Java one might use bytecode enhancement, which is what db4o did.

**Watch out for:** `INLINE_SIZE` as a compile-time constant.
It is what makes field offsets free in Rust, and a language without compile-time constant folding will need to compute offsets some other way — probably once per type at startup, which is fine, but it changes the shape of the code.
