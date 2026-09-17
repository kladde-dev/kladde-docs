---
title: Freeing
---

Recursive reclamation of backed memory.

**Status: designed, not implemented.**
Today, replacing or dropping an owning value orphans its allocation.
This is harmless against the current in-memory backend and will not be once there is a real file.

## The problem

An allocation must be released when the value that owns it goes away — dropped, overwritten, or removed from a container.

The obvious mechanism is unavailable.
An owned handle **cannot free itself** from its own destructor, because it holds no reference to the backend.
It deliberately holds none: a handle is meant to be small, and embedding a backend reference in every one would inflate every container and every derived struct.

So freeing has to be driven by something that *does* have the backend in hand.

## The mechanism

A type-driven free hook — call it `free_owned` — invoked by guards at the points where ownership ends.

- **Derive-generated** for structs and enums: recurse into each field.
- **Hand-written** for the containers: free the content allocation, after recursing into the elements.
- **Invoked by guards** at removal, overwrite, and teardown.

The walk follows **owning pointers only**, which is trivial here because the type knows which of its fields own.
That is the decisive advantage of putting the hook on the type rather than deriving it from the schema.

### Ordering

The free walk must respect the [prefix-replay discipline](../spec/journal.md#ordering): **publish, then free.**

Overwriting a map entry must write the new value's pointer *before* freeing the old value's allocation.
A crash in between then leaks — which is recoverable — rather than dangling, which is not.

## Ownership transfer

Freeing is not the only way ownership ends.

Taking a value *out* of a container moves its handle to the returned value without freeing it.
At every instant exactly one live value owns the allocation, so the transfer can neither double-free nor leak.

This means containers need two flavours of removal, and which one a given method should offer is a real design question:

- **transfer** — return the owning value; the caller re-homes or drops it;
- **delete** — free eagerly; return a detached snapshot, or nothing.

Likely per-container and driven by ergonomics.
**Open.**

## Partial initialization

A container that has allocated slots but not filled them all has a genuine problem: a free walk must not recurse into a slot that was never initialized.

The recommendation is to keep this **entirely inside the Rust containers** — track slot liveness the way the hash map already does with its tag byte — and keep the format unaware of it.

The alternative, modelling partial initialization in the schema so that a language-independent tool could do the walk, is deferred.
When it is built, arrays should be modelled as length-bounded and optional slots as `Option`-like enums, so that no dedicated "maybe uninitialized" schema kind is ever needed.

## The schema-level alternative

A tool that does not link the application cannot use a type-driven hook, so a language-independent garbage collector or leak checker would need the schema to say which fields are owning pointers.

That is what the reserved [`Pointer` descriptor kind](../spec/schema/type-descriptors.md#pointer-reserved) is for.

**Deferred** until such a tool actually exists.
It also forces the owning/non-owning distinction into the format, which is a decision worth making deliberately rather than as a side effect.

## Open questions

- Should `free_owned` take `&mut self` and null out pointers as it goes — making a double free a safe no-op — or consume the value?
- Per-method transfer-versus-delete policy, as above.
- Interaction with a future [non-owning reference](pointers.md#non-owning-references) type.
  The free walk must follow owning pointers only, which is trivial for a type-driven hook and forces the distinction into the schema for the alternative.

## Testing

The natural check is a **leak detector**: walk every allocation reachable from the root via the type structure, compare against the heap's live set, and assert they match.

This is worth building at the same time as the hook itself.
It is cheap, it validates the trusted-not-checked convention that every implementation is expected to uphold, and it would immediately surface exactly the drop-and-replace leaks that exist today.
