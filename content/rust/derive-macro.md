---
title: The derive macro
---

What `#[derive(Persistable)]` generates, and why.

## What it generates

For a struct or enum, three things:

1. a `Persistable` implementation — `INLINE_SIZE`, `store`, `load`, `free`, and the schema descriptor;
2. a **guard type**, `MyTypeGuard`, with a `_mut()` accessor per field and a whole-value `set`;
3. a `Deref` implementation on the guard, so read-only methods stay available — and no `DerefMut`, which would hand out a `&mut` whose changes no journal record describes.

```rust
#[derive(Persistable)]
struct Point { x: i32, y: PersistableString }
```

generates, in essence:

```rust
impl<'s, B: WriteBackend> PointGuard<'s, B> {
    fn x_mut(&mut self) -> I32Guard<'_, B> {
        self.inner.x.guard(self.backend, self.location)
    }
    fn y_mut(&mut self) -> PersistableStringGuard<'_, B> {
        self.inner.y.guard(self.backend, self.location + 4)
    }
}
```

Each accessor reborrows the **same** backend and extends the location by that field's static offset — the sum of every earlier field's `INLINE_SIZE`.
`y`'s offset is 4 regardless of how long its string currently is, because a string's inline representation is a fixed header.

## The aggregate records nothing of its own

A struct owns no storage beyond its fields, so its guard has nothing to write.
Every accessor just reborrows into the field's own guard, recursively, and `free` recurses into every field.
This is why the macro is small: it computes offsets and forwards; all the actual work is in the leaf types.

## Primitives are `Persistable` too

`i32`, `bool`, `char` and friends have implementations, specifically so the macro can treat **every** field uniformly rather than special-casing leaves.
Those implementations must live in the crate that *defines* `Persistable`, because `impl Persistable for i32` is `impl ForeignTrait for ForeignType` from anywhere else, which the orphan rules forbid; the same reasoning forces the scalar guard types into that crate.

## Enums

An enum is a four-byte discriminant followed by the selected variant's fields, each variant laid out like a struct in its own right, based past the discriminant.
So, like a struct, an enum owns no allocation, and its `INLINE_SIZE` is the discriminant plus the largest variant's field sum.
Positional fields are named by position — `"0"`, `"1"` — which keeps the schema model uniform.

**Only whole-value replacement is supported**: `set` stores the new value, then frees the old one.
Mutating a field within the current variant in place, and matching directly on a generated guard, are both intended and unbuilt.

## Generics and transparency

Type parameters are supported: each gets a `Persistable` bound, the heuristic `#[derive(Debug)]` uses.
Lifetime and const parameters are rejected.

`#[kladde(transparent)]` on a single-field struct persists it exactly as its field — same bytes, same descriptor, same fingerprint — and gives its guard one `get_mut()` returning the field's guard, as `#[serde(transparent)]` does.

## Path resolution

Generated code names items by absolute path, because a macro cannot know what the user called their imports.
It roots them at **`::kladde`**, and `kladde` re-exports every item generated code or a hand-written impl names, so an application depends on `kladde` and nothing below it.
That matters for more than convenience: if the facade did *not* re-export them, an author would add `kladde-persist` themselves, and a version that disagreed with the facade's would produce two distinct copies of the `Persistable` trait and an error that prints the expected and found types identically.

A library built directly on `kladde-persist` has no reason to pull the facade in, and redirects the macro instead, as `serde`'s `#[serde(crate = "...")]` does:

```rust
#[derive(Persistable)]
#[kladde(crate = "kladde_persist")]
struct Node { /* ... */ }
```

## What is missing

**Field attributes.**
There is no `#[kladde(skip)]` for a non-persisted field, no `#[kladde(id = ...)]` for rename-robust identity, and no way to declare a default for a field added later.
The last two become necessary when [evolution](../spec/schema/evolution.md) lands.

**Fine-grained enum mutation**, as above.

## Testing compile failures

The macro's *compile errors* are part of its contract — a `String` field must fail to compile, and that is the mechanism preventing silent data loss.
These are tested with rustdoc `compile_fail` doctests rather than a separate harness, which keeps the expected-failure cases next to the documentation that explains them.
