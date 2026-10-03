---
title: The derive macro
---

What `#[derive(Persistable)]` generates, and why.

## What it generates

For a struct or enum, four things:

1. a `Persistable` implementation — both encodings (`encoded_size`, `encode`, `decode`), `prepare`, `free`, the constants `SLOTTED_SIZE` and `PACKED_SIZE`, the `RootEncoding`, and the schema descriptor;
2. a `Slottable` implementation, unless the type is marked `#[kladde(packed_only)]`;
3. a **guard type**, `MyTypeGuard<'s, B, E = Slotted>`, with a `_mut()` accessor per field, a `parts()` handing out every field's guard at once, and a whole-value `set`;
4. a `Deref` implementation on the guard, so read-only methods stay available — and no `DerefMut`, which would hand out a `&mut` whose changes no journal record describes.

```rust
#[derive(Persistable)]
struct Point { x: i32, y: PersistableString }
```

generates, in essence:

```rust
impl<'s, B: WriteBackend, E: Encoding> PointGuard<'s, B, E> {
    fn x_mut(&mut self) -> I32Guard<'_, B, E> {
        let place = self.place.field::<E, 3>(&self.fields, 0, 0);
        self.inner.x.guard(self.backend, place)
    }
    fn y_mut(&mut self) -> PersistableStringGuard<'_, B, E> {
        let place = self.place.field::<E, 3>(&self.fields, 1, 4);
        self.inner.y.guard(self.backend, place)
    }
}
```

Each accessor reborrows the **same** backend and hands the field a place.
In a slotted `Point` at a fixed location, that is the location advanced by the field's static offset — the sum of every earlier field's slot — so `y` sits at 4 regardless of how long its string currently is, because a string's fixed encoding is its pointer.
Anywhere else, the place links to the guard's `fields`, a `FieldOffsets` the guard fills from the in-memory value when it is made: in a packed `Point`, `x` is a zigzag varint of one to five bytes, and `y` starts wherever `x` ends.

## The aggregate records nothing of its own

A struct owns no storage beyond its fields, so its guard has nothing to write but whole values.
Every accessor reborrows into the field's own guard, recursively, and `free` and `prepare` recurse into every field.
What the guard does keep, in a packed place, is its fields' offsets, so that each field's guard finds itself after a sibling grows, and so that a field's change of size becomes the struct's own, reported to the struct's parent.
This is why the macro stays small: it computes offsets and forwards; the actual work is in the leaf types and in `kladde-persist`'s `Place` and `FieldOffsets`.

## Primitives are `Persistable` too

`i32`, `bool`, `char` and friends have implementations, specifically so the macro can treat **every** field uniformly rather than special-casing leaves.
Those implementations must live in the crate that *defines* `Persistable`, because `impl Persistable for i32` is `impl ForeignTrait for ForeignType` from anywhere else, which the orphan rules forbid; the same reasoning forces the scalar guard types into that crate.
Their packed encodings are those of the [specification](../spec/schema/type-descriptors.md#primitive): varints for the integers wider than a byte, UTF-8 for `char`.

## Enums

An enum is a discriminant followed by the selected variant's fields, each variant laid out like a struct in its own right, based past the discriminant.
So, like a struct, an enum owns no allocation.
Its fixed encoding pads every value to the largest variant, and its packed encoding writes the discriminant as a varint and the variant's fields, with no padding.
Positional fields are named by position — `"0"`, `"1"` — which keeps the schema model uniform.

**The discriminant is as wide as its values need, unless an integer `#[repr]` fixes its width.**
`#[repr(u8)]`, `u16`, `u32` or `u64` sets the width; without one, it is the smallest of 1, 2, 4 and 8 bytes that holds the largest discriminant value, the widths the [Enum descriptor](../spec/schema/type-descriptors.md#enum) allows.
A signed or pointer-sized `repr`, and a negative discriminant, do not compile, since descriptors store discriminants unsigned and `usize` has no fixed width.
Without a `repr`, the width is a layout cliff for slotted values: a 257th variant, or a discriminant of 256 or more, widens every value of the type and moves every field behind it in the structs that contain it.
A type whose layout has to stay put pins the width with a `repr`.

**The guard's `parts()` hands out the guards of the current variant's fields**, as a generated enum `{Enum}Parts` with the same variants, so a field is mutated in place by matching:

```rust
match guard.parts() {
    ShapeParts::Circle(mut radius) => radius.set(2)?,
    ShapeParts::Rectangle { mut width, .. } => width.set(3)?,
    ShapeParts::Origin => {}
}
```

Each field guard writes inside its field only, so the discriminant cannot change underneath it.
And `parts()` borrows the guard, so `set` cannot switch the variant while a field guard is alive.
`set` is how the variant changes: it stores the new value, then frees the old one; in a packed place, a variant of another size makes it a `Splice`.
An enum without any fields gets no `parts()`, since it has nothing to mutate in place.

## Slotted fields and packed-only types

**A field marked `#[kladde(slotted)]` keeps its fixed encoding inside a packed value**, so that it never changes size: for a counter, say, whose varint would grow at every power of 128, or an enum whose variant switches often.
It is the one declaration of a place a derived type needs, since a field is packed only by inheriting it.
Its descriptor is a [`Slotted`](../spec/schema/type-descriptors.md#slotted) wrapper around its type's, for every field marked, fixed-size or not, and its type must be `Slottable`, which the derive requires in the `Persistable` impl's `where` clause.

**A type that holds a field without a fixed encoding, such as a `SmallPersistableString`, is marked `#[kladde(packed_only)]`**, gets no `Slottable` implementation, and stands only in packed places: inside a `PackedPersistableVec` or a `SmallPersistableVec`, or as a packed root.
Without the mark, it does not compile, with a message that names the attribute.

The mark is needed because a derive macro sees a field's type only as written, and cannot tell whether it has a fixed encoding.
It could not simply require `Slottable` conditionally either: Rust rejects an `impl` whose `where` clause names a concrete type that lacks the trait, even where nothing uses the `impl`.

**The derive requires `Slottable` of the type's parameters, as `#[derive(Debug)]` requires `Debug`, and checks the other fields with a `const` assertion.**
Requiring it of every field's type would be circular for any type that holds itself through a container: `struct Node { children: PersistableVec<Node> }` would require `PersistableVec<Node>: Slottable`, which holds only if `Node: Slottable` does, and the compiler gives up on the cycle rather than assuming it holds.
The assertion compares a field type's `SLOTTED_SIZE` with `None`, which needs no trait, and panics at compile time with the message that names `#[kladde(packed_only)]`.
What it costs is precision for generic types: a parameter used only inside a packed container, which holds any type, is still required to be slottable for the derived type to be.

`RootEncoding` is `Slotted` joined with every field's, so a packed-only type's is `Packed`, and so is a generic type's whose parameter is packed-only.

## Generics and transparency

Type parameters are supported: each gets a `Persistable` bound in the `Persistable` impl and a `Slottable` bound in the `Slottable` impl.
Lifetime and const parameters are rejected.
A generic type that holds a `PersistableVec<T>` says `where T: Slottable` itself, since the vector requires it and the derive cannot know.

`#[kladde(transparent)]` on a single-field struct persists it exactly as its field — same bytes in either encoding, same descriptor, same fingerprint — and gives its guard one `get_mut()` returning the field's guard, at the wrapper's own place, as `#[serde(transparent)]` does.

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

**More field attributes.**
There is no `#[kladde(skip)]` for a non-persisted field, no `#[kladde(id = ...)]` for rename-robust identity, and no way to declare a default for a field added later.
The last two become necessary when [evolution](../spec/schema/evolution.md) lands.

## Testing compile failures

The macro's *compile errors* are part of its contract — a `String` field must fail to compile, and that is the mechanism preventing silent data loss; so must a packed-only field in a type not marked so, and a slotted field without a fixed encoding.
These are tested with rustdoc `compile_fail` doctests rather than a separate harness, which keeps the expected-failure cases next to the documentation that explains them.
