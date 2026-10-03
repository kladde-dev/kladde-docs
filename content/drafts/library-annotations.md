---
title: Library annotations
---

**Status: an idea, not a design.**

**A structural descriptor says how a type's bytes are laid out, not which library defines what they mean, and a later revision could let any descriptor say that too.**
A string and a packed vector of `char`s get one descriptor and one fingerprint, as the schema's [guiding principle](../spec/schema/index.md#the-guiding-principle) intends for byte-identical representations, although one keeps text in memory and the other a vector of four-byte `char`s.
And two libraries could lay out text alike and mean different things by it, one keeping it in a Unicode normalization form and the other not, or one comparing it without regard to case; a hash map's liveness flag, likewise, means what its library says it means.

Only an [opaque](../spec/schema/type-descriptors.md#opaque) descriptor carries the name and version of the library that defines its type, so the built-in containers, which [describe their structure](../spec/tooling.md#containers), carry none.
A generic way to annotate any descriptor with the name and version of the library that defines it — fingerprinted by the version's compatibility component, as an opaque descriptor's is — would give structural types that identity back, and would tell a [tool](../spec/tooling.md) what it cannot learn from a layout: that a string is kept normalized, or that a slot whose flag is clear holds no entry.

**Open:** whether an annotation should change a type's fingerprint at all, given that the guiding principle wants byte-identical representations to share one; whether it annotates a descriptor or a reference to one; and how a reader that does not know the library treats it.
