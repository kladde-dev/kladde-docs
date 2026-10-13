---
title: The derive macro
---

What `#[derive(Durable)]` generates, and why.

## What it generates

For a struct or enum, four things:

1. a `Durable` implementation — `FIXED_SIZE`, `encoded_len`, `encode`, `decode`, and the schema descriptor;
2. a **guard type**, `MyTypeGuard`, with a `_mut()` accessor per field, a `parts()` method handing out a guard for every field at once, and a whole-value `set`;
3. a `Deref` implementation on the guard, so read-only methods stay available — and no `DerefMut`, which would hand out a `&mut` whose changes no journal record describes;
4. with `#[kladde(bounded)]`, a `Bounded` implementation: the fixed form.

```rust
#[derive(Durable)]
struct Point { x: f32, label: DurableString, n: u32 }
```

generates, in essence:

```rust
impl Durable for Point {
    const FIXED_SIZE: Option<usize> = sum_sizes(&[f32::FIXED_SIZE, DurableString::FIXED_SIZE, u32::FIXED_SIZE]);
    fn encoded_len(&self) -> usize { self.x.encoded_len() + self.label.encoded_len() + self.n.encoded_len() }
    fn encode(&self, out: &mut Encoder<'_>) { self.x.encode(out); self.label.encode(out); self.n.encode(out) }
    fn decode(input: &mut Decoder<'_>) -> Result<Self, Error> {
        Ok(Point { x: f32::decode(input)?, label: DurableString::decode(input)?, n: u32::decode(input)? })
    }
    // ...
}

impl<'r, B: Backend> PointGuard<'r, B> {
    fn label_mut(&mut self) -> DurableStringGuard<'_, B> {
        let offset = 4;                                  // after `x`, whose size is fixed
        let len = self.inner.label.encoded_len();
        self.inner.label.guard(self.region.child(offset, len, None, 1))
    }
    fn n_mut(&mut self) -> SetGuard<'_, u32, B> {
        let offset = 4 + self.inner.label.encoded_len(); // after a field whose size varies
        let len = self.inner.n.encoded_len();
        self.inner.n.guard(self.region.child(offset, len, None, 2))
    }
}
```

**A field's accessor makes a child region at the sum of the sizes of the fields before it**: a constant up to the first field whose `FIXED_SIZE` is `None`, folded by the compiler, and after that the sum of the earlier fields' encoded sizes, which the in-memory values give in constant time.
The child region reports a change of the field's size to the struct's region, which passes it on up.

**`parts()` makes every field's guard at once**, from a `FieldSizes` node it fills with the fields' sizes: a field after the first variable-size one is *indexed*, and looks its start up in the node at every write, so that a sibling before it may change size meanwhile ([guards made together](../impl/values.md#guards-made-together)).

## The aggregate records nothing of its own

A struct is its fields' encodings back to back, so its guard has nothing of its own to write beyond `set`.
Every accessor just makes a child region and the field's own guard, recursively.
This is why the macro is small: it computes offsets and forwards; all the actual work is in the leaf types.

## Fixed forms

**`#[kladde(bounded)]` implements `Bounded`**, the fixed form that `Fixed<T>` holds: every field's fixed form, back to back, and for an enum the discriminant at a fixed width and its variant padded with zeros to the largest.
It requires every field's type to be `Bounded`, failing at the type's definition otherwise, with the message `#[diagnostic::on_unimplemented]` gives, and bounds each type parameter by `Bounded`, so that a generic type such as `Tagged<T>` is `Bounded` where its parameter is.

**The derive cannot implement `Bounded` unasked, exactly where the fields' types allow it**: for a type without parameters, rustc refuses an impl whose where-clause names a concrete type that lacks the trait (E0277) rather than dropping the impl, short of the unstable `trivial_bounds`.
So the author opts in.
Opting in marks only the types that `Fixed` wraps and those inside them, none in kladde-svg; opting out would mark every type that holds text, a container or itself, 6 of kladde-svg's 15 derived types.

## Primitives are `Durable` too

`i32`, `bool`, `char` and friends have implementations, specifically so the macro can treat **every** field uniformly rather than special-casing leaves.
Those implementations must live in the crate that *defines* `Durable`, because `impl Durable for i32` is `impl ForeignTrait for ForeignType` from anywhere else, which the orphan rules forbid; the same reasoning forces the scalar guard types into that crate.

## Enums

An enum is its discriminant as a varint, followed by the selected variant's fields, each variant encoded like a struct in its own right.
It is fixed-size if every discriminant is below 128 and every variant takes the same fixed size.
Positional fields are named by position — `"0"`, `"1"` — which keeps the schema model uniform.
The discriminant is Rust's own: the explicit value where the author wrote one, otherwise one more than the previous variant's.

**The discriminant's width in the fixed form is as wide as its values need, unless an integer `#[repr]` fixes it.**
`#[repr(u8)]`, `u16`, `u32` or `u64` sets the width; without one, it is the smallest of 1, 2, 4 and 8 bytes that holds the largest discriminant value, the widths the [Enum descriptor](../spec/schema/type-descriptors.md#enum) allows.
A signed or pointer-sized `repr`, and a negative discriminant, do not compile, since descriptors store discriminants unsigned and `usize` has no fixed width.
Without a `repr`, the width is a cliff: a 257th variant, or a discriminant of 256 or more, widens every `Fixed` value of the type; a `repr` keeps the fixed form's size when variants are added.

**The guard's `parts()` hands out the guards of the current variant's fields**, as a generated enum `{Enum}Parts` with the same variants, so a field is mutated in place by matching:

```rust
match guard.parts() {
    ShapeParts::Circle(mut radius) => radius.set(2)?,
    ShapeParts::Rectangle { mut width, .. } => width.set(3)?,
    ShapeParts::Origin => {}
}
```

Each field guard is based at the field's offset past the discriminant's varint and writes inside its field only, so the discriminant cannot change underneath it.
And `parts()` borrows the guard, so `set` cannot switch the variant while a field guard is alive.
`set` is how the variant changes: it splices the new value's encoding over the old one's.
`parts()` on a temporary guard works in a `match` but not in a `let … else`, which drops the temporary at the end of the statement; a consuming `into_parts(self)` would make the `let` form work too, and is proposed rather than built.
An enum without any fields gets no `parts()`, since it has nothing to mutate in place.

## Generics and transparency

Type parameters are supported: each gets a `Durable` bound, the heuristic `#[derive(Debug)]` uses.
Lifetime and const parameters are rejected.

`#[kladde(transparent)]` on a single-field struct persists it exactly as its field — same bytes, same descriptor, same fingerprint — and gives its guard one `get_mut()` returning the field's guard, as `#[serde(transparent)]` does.

## Path resolution

Generated code names items by absolute path, because a macro cannot know what the user called their imports.
It roots them at **`::kladde`**, and `kladde` re-exports every item generated code or a hand-written impl names, so an application depends on `kladde` and nothing below it.
That matters for more than convenience: if the facade did *not* re-export them, an author would add `kladde-persist` themselves, and a version that disagreed with the facade's would produce two distinct copies of the `Durable` trait and an error that prints the expected and found types identically.

A library built directly on `kladde-persist` has no reason to pull the facade in, and redirects the macro instead, as `serde`'s `#[serde(crate = "...")]` does:

```rust
#[derive(Durable)]
#[kladde(crate = "kladde_persist")]
struct Node { /* ... */ }
```

## What is missing

**Field attributes.**
There is no `#[kladde(skip)]` for a non-persisted field, no `#[kladde(id = ...)]` for rename-robust identity, and no way to declare a default for a field added later.
The last two become necessary when [evolution](../spec/schema/evolution.md) lands.
Two more, modeled on `serde`'s, would serve types the derive cannot handle today:

- **`#[kladde(with = "path")]` on a field** of a type that is not `Durable`, a foreign one or one wanting an encoding of its own, delegating its encoding, descriptor and guard to a module of functions, so that a foreign type can be encoded inline with a real descriptor rather than wrapped in a `DurableBlob`.
- **`#[kladde(bound = "...")]` on the type**, overriding the `Durable` bound the derive puts on every type parameter, for a parameter used only in `PhantomData`, or only inside a wrapper that needs a different bound.

**Const generic parameters**, which the derive rejects today.

**Conveniences on guards**: `set_<field>(value)` on a struct's guard, sparing the step through the field's accessor, and `field_{i}_mut()` on a tuple's guard, which today offers only `parts()` and `set`, as tuple structs' guards offer it.
A tuple's accessors cannot be generated by `macro_rules!` without a crate such as `paste` to build their names.

## Testing compile failures

The macro's *compile errors* are part of its contract — a `String` field must fail to compile, and that is the mechanism preventing silent data loss.
These are tested with rustdoc `compile_fail` doctests rather than a separate harness, which keeps the expected-failure cases next to the documentation that explains them.
