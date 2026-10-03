---
title: Freeing
---

Recursive reclamation of backed memory: a type-driven hook, and the points where ownership ends.

## The problem

An allocation must be released when the value that owns it goes away — overwritten, or removed from a container and not kept.

The obvious mechanism is unavailable.
An owned handle **cannot free itself** from its own destructor, because it holds no reference to the backend.
It deliberately holds none: a handle is meant to be small, and embedding a backend reference in every one would inflate every container and every derived struct.
So freeing has to be driven by something that *does* have the backend in hand, which is a guard.

## The mechanism

`Persistable::free(&mut self, backend)` releases everything a value owns, and guards call it where ownership ends.

- **Derive-generated** for structs and enums: recurse into each field, or into the current variant's.
- **Hand-written** for the containers: free every element, then the content allocation.
- **A no-op** for scalars, which own nothing.

The walk follows **owning pointers only**, which is trivial here because the type knows which of its fields own.
That is the decisive advantage of putting the hook on the type rather than deriving it from the schema.

### Ordering

The free walk must respect the [ordering discipline](../spec/journal.md#ordering): **publish, then free.**
A `set` on any guard stores the new value — writing the pointer that publishes it — and only then frees the old one.
A crash in between leaks, which is recoverable, rather than dangling, which is not.

## Where ownership ends

**Overwriting** a value with `set` frees the old one.

**Removing** from a container comes in two flavours, because taking a value *out* is not the same as discarding it:

- **transfer** — `remove` and `pop` return the value with its allocations intact, so it can be stored elsewhere without copying a byte;
- **delete** — `delete` and `clear` free what they remove.

A transferred value that is then dropped without being stored anywhere leaks its allocations in the file, exactly as a `Box` leaked with `std::mem::forget` leaks memory.
Nothing can prevent that without a destructor that knows the backend, which is the thing handles deliberately lack.

## Partial initialization

No container has slots it has not written: a vector's length is its allocation's size, so every slot below it was written, and a hash map records each slot's liveness in a tag byte.
So a free walk never recurses into uninitialized bytes, and the format needs no notion of them.

## The schema-level alternative

A tool that does not link the application cannot use a type-driven hook, so a language-independent garbage collector or leak checker would need the schema to say which fields are owning pointers.
The [`Pointer` descriptor kind](../spec/schema/type-descriptors.md#pointer) makes such a tool possible for every type but an opaque one, and none exists yet.
