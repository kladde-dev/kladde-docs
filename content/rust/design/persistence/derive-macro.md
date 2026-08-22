---
title: The derive macro
---

What `#[derive(Persistable)]` generates, and why.

## What it generates

For a struct or enum, three things:

1. a `Persistable` implementation — `INLINE_SIZE`, `store`, `load`, and the schema descriptor;
2. a **guard type**, `MyTypeGuard`, with a `_mut()` accessor per field;
3. a `Deref` implementation on the guard, so read-only methods stay available.

```rust
#[derive(Persistable)]
struct Point { x: i32, y: PersistableString }
```

generates, in essence:

```rust
impl<'s, B: WriteBackend> PointGuard<'s, B> {
    fn x_mut(&mut self) -> I32Guard<'_, B> {
        self.data.x.guard(self.backend, self.location)
    }
    fn y_mut(&mut self) -> PersistableStringGuard<'_, B> {
        self.data.y.guard(self.backend, self.location + 4)
    }
}
```

Each accessor reborrows the **same** backend and extends the location by that field's static offset — the sum of every earlier field's `INLINE_SIZE`.
`y`'s offset is 4 regardless of how long its string currently is, because a string's inline representation is a fixed header.

## The aggregate records nothing of its own

A struct owns no storage beyond its fields, so its guard has nothing to write.
Every accessor just reborrows into the field's own guard, recursively.

This is why the macro is small: it computes offsets and forwards.
All the actual work is in the leaf types.

## Primitives are `Persistable` too

`i32`, `bool`, `char` and friends have blanket implementations, specifically so the macro can treat **every** field uniformly rather than special-casing leaves.

Those implementations must live in the crate that *defines* `Persistable`, because `impl Persistable for i32` is `impl ForeignTrait for ForeignType` from anywhere else, which the orphan rules forbid.
The same reasoning forces the scalar guard types into that crate.

## Enums

An enum is a discriminant followed by the selected variant's fields, each variant laid out like a struct in its own right — same per-field offset computation, based past the discriminant.

So, like a struct, an enum owns no allocation.
Its `INLINE_SIZE` is the discriminant width plus the largest variant's field sum.

Positional fields are named by position — `"0"`, `"1"` — which keeps the schema model uniform.

**Only whole-value replacement is supported.**
Mutating a field within the current variant in place, and matching directly on a generated guard, are both intended and unbuilt.
The latter is the more interesting one: it would let a caller write `match guard { ... }` and get a guard per variant's payload.

## Path resolution

Generated code names items by absolute path, because a macro cannot know what the user called their imports.
It roots them at **`::kladde`**, and `kladde` re-exports every item generated code touches — thirteen of them, all of which a hand-written impl needs anyway.

So an application depends on `kladde` and nothing below it, and an author who later hand-writes an impl finds the same items in the same place.
That matters for more than convenience: if the facade did *not* re-export them, the author would add `kladde-persist` themselves, and a version that disagreed with the facade's would produce two distinct copies of the `Persistable` trait and an error that prints the expected and found types identically.

A library built directly on `kladde-persist` has no reason to pull the facade in, and redirects the macro instead:

```rust
#[derive(Persistable)]
#[kladde(crate = "kladde_persist")]
struct Node { /* ... */ }
```

This is the same arrangement as `serde`'s `#[serde(crate = "...")]`, for the same reason.

## What is missing

**Generics and where-clauses.**
The generation pattern is worked out for plain, non-generic types.
Extending it needs bounds propagated onto the generated guard type and its implementations, and it is outstanding.

This is the most-felt gap, because a generic wrapper is exactly the kind of thing an application author writes.

**Field attributes.**
There is no `#[kladde(skip)]` for a non-persisted field, no `#[kladde(id = ...)]` for rename-robust identity, and no way to declare a default for a field added later.
The last two become necessary when [evolution](../../../spec/schema/evolution.md) lands.

**Fine-grained enum mutation**, as above.

## Testing compile failures

The macro's *compile errors* are part of its contract — a `String` field must fail to compile, and that is the mechanism preventing silent data loss.

These are tested with rustdoc `compile_fail` doctests rather than a separate harness, which keeps the expected-failure cases next to the documentation that explains them.
