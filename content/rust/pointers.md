---
title: Pointers
---

The handle types, what each is for, and the ownership discipline they enforce.

## Two types, one idea

| type | copyable? | owns? |
| --- | --- | --- |
| `Pointer` | yes | no |
| `UniquePointer` | no | yes |

**`Pointer`** is the serialized, at-rest form: an allocation's id and nothing else.
It is `Copy`, it is what a `Location` anchors to, and it is what gets written into a file.
Its field is a `NonZeroU32`, so `Option<Pointer>` gets the null niche in memory for free, and the [on-file null encoding](../spec/allocations.md#pointer-encoding) — all-zero bytes — is sound rather than merely conventional.

**`UniquePointer`** is the owned handle: single-owner, not `Copy`.
Holding one *is* the claim to the allocation, and it is what `resize`, `splice`, and `free` take, so that only the owner can reshape or release an allocation.
It is untyped: an allocation holds bytes, and the type that owns the handle knows what they mean.

## Why exactly one copyable type

If a pointer can be freely copied, a moved allocation may have arbitrarily many live references to it, which requires a registry just to find them all.
With exactly one owned handle per allocation, ownership is local: nothing needs finding, because the serialized value is a stable id and the in-memory owner is unique by construction.

`Pointer` is copyable because it is *not* a claim — it is an id-like value, useful for anchoring a `Location` and for serialization, and it confers no rights.

## Ownership

> **Exactly one owning handle per allocation, at all times.**

This is a format-level invariant, not a Rust convention: [the value graph is a tree of ownership](../spec/allocations.md#ownership), and a tool may rely on it.

Ownership **transfers** rather than duplicating.
Storing a value that already owns allocations writes only its handles, so moving a value from one container to another moves its content without copying a byte.

Application code never constructs or holds a bare owned handle.
Only library code does — the containers and generated code — so this is an invariant of a handful of types rather than something every mutating method must remember.

### The one hole

`Persistable::load` reconstructs an owned handle from the `Pointer` it reads, which means it **can mint a second owner**.
It is needed, since loading a value has to recover the handle from the serialized id, and there is no way to do that without constructing one.
It is a load-time operation only, and the single-owner property is therefore conventional at that one point rather than enforced.

## Width

The format [fixes ids at 32 bit](../spec/address-table.md#bounds), and so does `Pointer`.
`Pointer` is nevertheless generic over a `Word`, defaulted to `u32`, and `Persistable` is generic over the pointer type, so that a per-file width, should one ever arrive, is a type argument rather than a rewrite.
