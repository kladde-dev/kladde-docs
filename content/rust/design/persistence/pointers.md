---
title: Pointers
---

The handle types, what each is for, and the ownership discipline they enforce.

## Four types, one idea

| type | copyable? | owns? | sized? | typed? |
| --- | --- | --- | --- | --- |
| `Pointer<W>` | yes | no | — | no |
| `UniquePointerResizable<P>` | no | yes | resizable | no |
| `UniquePointerFixedSize<P>` | no | yes | fixed | no |
| `UniquePointer<T, P>` | no | yes | fixed | yes |

**`Pointer<W>`** is the serialized, at-rest form: a stable id and nothing else.
It is `Copy`, it is what a `Location` anchors to, and it is what gets written into a file.
The width `W` is a type parameter, so 32- and 64-bit pointers are the same code.

Every `Pointer` is **nonzero by construction** — its field is the nonzero form of `W` — so `Option<Pointer>` gets the null niche in memory for free, and the on-file null encoding is sound rather than merely conventional.

**The owned handles** are single-owner and not `Copy`.
Holding one *is* the claim to the allocation.
The resizable and fixed variants differ only in which operations they permit, which is a typestate: `resize` takes `&UniquePointerResizable`, so resizing a fixed allocation is not expressible.

**`UniquePointer<T>`** adds a phantom `T` over the fixed handle, for the typed case.
The phantom is `PhantomData<*const T>` rather than `PhantomData<T>`, deliberately: it gives covariance in `T`, which is sound here because mutation only ever happens through an exclusive guard, without imposing the drop-check obligation that `PhantomData<T>` would — and a `UniquePointer` never runs `T`'s destructor.

## Sizedness lives in the id

The low bit of the raw pointer value carries the sizedness flag:

```
raw = (counter << 1) | fixed_bit
```

Two reasons.

It is the **only** per-allocation fact the heap ever needs, so a separate metadata channel would carry exactly this one bit.
And it is **placement-relevant** — see [Placement](../heap/placement.md) — so the heap has to be able to read it.

The counter pool is **shared** between the two sizednesses, so the counter alone is unique.
Ids stay dense at roughly twice the counter, which matters for any table that wants to index by id.
A high bit would have been the alternative and is much worse: it doubles the apparent id range and is fatal to density.

A consequence worth naming: because sizedness is *in* the id, a sizedness conversion cannot re-tag in place.
It must mint a **new** id, copy the content, and free the old one.

## Why exactly one copyable type

An earlier design had several copyable pointer types.
That is the wrong shape, and the reason is aliasing.

If a pointer can be freely copied, a moved allocation may have arbitrarily many live references to it, which requires a registry just to find them all.
With exactly one owned handle per allocation, moving is trivial: nothing needs finding, because the serialized value is a stable id and the in-memory owner is unique by construction.

`Pointer` is copyable because it is *not* a claim — it is an address-like value, useful for anchoring a `Location` and for serialization, and it confers no rights.

## Ownership

> **Exactly one owning handle per allocation, at all times.**

This is a format-level invariant, not a Rust convention: [the value graph is a tree of ownership](../../../spec/allocations.md#ownership), and a tool may rely on it.

Ownership **transfers** rather than duplicating.
Taking the value out of a container moves its handle to the returned value without freeing it, so at every instant exactly one live value owns the allocation, and the transfer can neither double-free nor leak.

Application code never constructs or holds a bare owned handle.
Only library code does — the containers, and eventually generated destructors — so this is an invariant of a handful of types rather than something every mutating method must remember.

### The one hole

`Backend::resolve` reconstructs an owned handle from a `Pointer`, which means it **can mint a second owner**.

It is needed: loading a value from a file has to recover the handle from the serialized id, and there is no way to do that without constructing one.
It is conventionally a load-time operation only.

But it means the single-owner property is *conventional at that one point* rather than enforced, and it is why queries must check liveness at runtime rather than trusting the typestate.

## Non-owning references

Pointers that may target an interior offset, and may alias, are **reserved and unspecified**.

They would be useful — a cursor into a rope, for instance — but they interact badly with liveness: a reference must not outlive the allocation it points into, and nothing would enforce that.
They also complicate [freeing](freeing.md), because a recursive free walk must follow owning pointers only.

**TBD**, and deliberately not urgent.

## Width

`Pointer<W>` is generic over its width, and `Persistable<P>` is generic over the pointer type, so 32- and 64-bit files use the same code with different type arguments.

The width should probably be a **per-file property** rather than a compile-time choice, since it is a format guarantee.
That is not decided.
