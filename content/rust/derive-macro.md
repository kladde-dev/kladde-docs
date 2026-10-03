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

An enum is a discriminant followed by the selected variant's fields, each variant laid out like a struct in its own right, based past the discriminant.
So, like a struct, an enum owns no allocation, and its `INLINE_SIZE` is the discriminant plus the largest variant's field sum.
Positional fields are named by position — `"0"`, `"1"` — which keeps the schema model uniform.

**The discriminant is as wide as its values need, unless an integer `#[repr]` fixes its width.**
`#[repr(u8)]`, `u16`, `u32` or `u64` sets the width; without one, it is the smallest of 1, 2, 4 and 8 bytes that holds the largest discriminant value, the widths the [Enum descriptor](../spec/schema/type-descriptors.md#enum) allows.
A signed or pointer-sized `repr`, and a negative discriminant, do not compile, since descriptors store discriminants unsigned and `usize` has no fixed width.
Without a `repr`, the width is a layout cliff: a 257th variant, or a discriminant of 256 or more, widens every value of the type and moves every field behind it in the structs that contain it.
A type whose layout has to stay put pins the width with a `repr`.

**The guard's `parts()` hands out the guards of the current variant's fields**, as a generated enum `{Enum}Parts` with the same variants, so a field is mutated in place by matching:

```rust
match guard.parts() {
    ShapeParts::Circle(mut radius) => radius.set(2)?,
    ShapeParts::Rectangle { mut width, .. } => width.set(3)?,
    ShapeParts::Origin => {}
}
```

Each field guard is based at the field's offset past the discriminant and writes inside its field only, so the discriminant cannot change underneath it.
And `parts()` borrows the guard, so `set` cannot switch the variant while a field guard is alive.
`set` is how the variant changes: it stores the new value, then frees the old one.
An enum without any fields gets no `parts()`, since it has nothing to mutate in place.

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

## Testing compile failures

The macro's *compile errors* are part of its contract — a `String` field must fail to compile, and that is the mechanism preventing silent data loss.
These are tested with rustdoc `compile_fail` doctests rather than a separate harness, which keeps the expected-failure cases next to the documentation that explains them.
