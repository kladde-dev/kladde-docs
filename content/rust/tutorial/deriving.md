---
title: Deriving your own types
---

`#[derive(Durable)]` turns your own struct or enum into a durable type, as long as every persisted field is itself `Durable`.

## What to import

Two crates: `kladde` for the machinery, `kladde-types` for the containers.
Neither is on crates.io yet, so depend on the repository:

```toml
[dependencies]
kladde = { git = "https://github.com/kladde-dev/kladde-rs" }
kladde-types = { git = "https://github.com/kladde-dev/kladde-rs" }
```

You do **not** need `kladde-derive` or `kladde-persist`.
The macro is re-exported from `kladde` behind its `derive` feature, which is on by default, and generated code is rooted at `::kladde`, which re-exports every item it names.
That is deliberate: if you had to add `kladde-persist` yourself, a version disagreeing with `kladde`'s would give you two distinct copies of the `Durable` trait and an error message that prints the expected and found types identically.

Here is a complete program:

<!-- kladde-example: name=phonebook file=src/main.rs mode=run deps=kladde,kladde-types -->
```rust
use kladde::{Kladde, Durable};
use kladde_types::{DurableString, DurableVec};

#[derive(Durable)]
enum PhoneNumber {
    Mobile(DurableString),
    Landline(DurableString),
}

#[derive(Durable)]
struct Contact {
    email: DurableString,
    phones: DurableVec<PhoneNumber>,
    starred: bool,
}

fn main() -> kladde::Result<()> {
    let mut contact = Kladde::new(Contact {
        email: DurableString::from("ada@example.com"),
        phones: DurableVec::new(),
        starred: false,
    });

    let mut guard = contact.guard();
    guard.starred_mut().set(true)?;
    guard
        .phones_mut()
        .push(PhoneNumber::Mobile(DurableString::from("555-0100")))?;
    Ok(())
}
```

The `Durable` in that `use` is the derive *macro*.
It shares its name with the `Durable` *trait*, which is legal and deliberate — they live in different namespaces, exactly as `serde`'s `Serialize` does.
If you need to name the trait explicitly in the same file, import it under an alias.

## Structs

The macro generates a guard type — `ContactGuard` — with a `_mut()` accessor per field, and a `set` that replaces the whole value:

```rust
let mut contact = /* ... a ContactGuard ... */;
contact.starred_mut().set(true)?;
contact.phones_mut().push(number)?;
```

It also generates a `ContactParts` struct, and a `parts()` method on the guard that returns the guards of all fields at once, for when you need more than one at a time.
A tuple struct's accessors are named by position — `field_0_mut()`, `field_1_mut()` — and its `parts()` returns a tuple struct.

**A struct is its fields' encodings, back to back**, inline in whatever contains it.
A field's guard finds where the field starts: at a constant offset up to the first field whose size depends on its value — a varint, text, a container — and after that at the sum of the encoded sizes of the fields before it, which the in-memory values know.
Integers wider than a byte are varints, so a `u32` takes one byte below 128 and up to five above.

That includes primitive fields.
`bool`, `i32`, `char` and friends all implement `Durable`, specifically so the macro can treat every field uniformly rather than special-casing leaves.
So do tuples of up to twelve `Durable` types, encoded like a tuple struct, `Option<T>`, and `Box<T>`, which is persisted as the `T` it holds.

## Fixed-size values

**`Fixed<T>` keeps a value at the same size whatever it is**: every integer at its full width, every `char` in four bytes, and every enum with its discriminant at a fixed width and its variant padded with zeros to the largest.
Use it for a field that changes often, such as a counter, which would otherwise change size whenever it crosses a power of 128, and for the elements of a large vector, which then finds element `i` by multiplication and keeps no index of where its elements start.

A derived type can be wrapped in `Fixed` only if it is marked `#[kladde(bounded)]`, which requires every field's type to have a fixed form too:

<!-- kladde-example: name=fixed file=src/lib.rs deps=kladde,kladde-types
before:
  use kladde::{Fixed, Durable};
  use kladde_types::DurableVec;
  #[derive(Durable)]
  #[kladde(bounded)]
  enum ShapeKind {
      Circle,
      Square,
  }
-->
```rust
#[derive(Durable)]
#[kladde(bounded)]
struct Shape {
    x: u32,
    y: u32,
    kind: ShapeKind,       // also #[kladde(bounded)]
}

#[derive(Durable)]
struct Drawing {
    shapes: DurableVec<Fixed<Shape>>,   // eight bytes per shape, plus the kind's
    revision: Fixed<u64>,
}
```

`Fixed<T>`'s guard replaces the value whole, with `set(Fixed(...))`: changing one field means building the whole value, and writing all of it, which for a fixed-size value is an overwrite in place.
Types holding text, a container, or themselves have no fixed form, and wrapping one in `Fixed` fails to compile with an error that says so.
A generic type marked `#[kladde(bounded)]` has a fixed form wherever its parameters do.

## Newtypes

A single-field struct marked `#[kladde(transparent)]` is persisted exactly as its field — the same bytes and the same [fingerprint](../../spec/schema/fingerprints.md) — so wrapping a field in a newtype changes nothing in the file:

<!-- kladde-example: name=newtype file=src/lib.rs deps=kladde
before:
  use kladde::{Kladde, Durable};
-->
```rust
#[derive(Durable)]
#[kladde(transparent)]
struct Meters(u32);

fn walk(distance: &mut Kladde<Meters>) -> kladde::Result<()> {
    distance.guard().get_mut().set(42)
}
```

Its guard's `get_mut()` returns the field's guard.

## Enums

An enum is its discriminant, as a varint, followed by the selected variant's fields, each variant encoded like a struct in its own right.
So a value takes the size of its own variant, not of the largest.
Positional fields are named by their position: `Mobile(DurableString)` has a field called `"0"`.

**To mutate the current variant in place, match on the guard's `parts()`.**
It hands out the guards of the current variant's fields, in an enum named after yours, `PhoneNumberParts`, with the same variants:

<!-- kladde-example: name=enum-parts file=src/main.rs mode=run deps=kladde,kladde-types -->
```rust
use kladde::{Kladde, Durable};
use kladde_types::DurableString;

#[derive(Durable)]
enum PhoneNumber {
    Mobile(DurableString),
    Landline(DurableString),
}

fn main() -> kladde::Result<()> {
    let mut number = Kladde::new(PhoneNumber::Mobile(DurableString::from("555-01")));

    let mut guard = number.guard();
    match guard.parts() {
        PhoneNumberParts::Mobile(mut digits) => digits.push_str("00")?,
        PhoneNumberParts::Landline(mut digits) => digits.set("555-0199")?,
    }

    // To switch to another variant, replace the whole value.
    guard.set(PhoneNumber::Landline(DurableString::from("555-0123")))?;
    assert!(matches!(number.get(), PhoneNumber::Landline(digits) if digits == "555-0123"));
    Ok(())
}
```

Each field guard writes inside its field, so the variant stays what it was.
`set` replaces the whole value, which is how the variant changes: it splices the new value's encoding over the old one's.
`parts()` borrows the guard, so the compiler stops you from calling `set` while you still hold a field's guard.

**The discriminant is Rust's own**: the one you write (`Mobile = 1`), or else one more than the previous variant's, counting from 0; negative discriminants and signed or pointer-sized `repr`s are not supported.
The file holds it as a varint, so discriminants below 128 take one byte.
Inside a `Fixed` value it takes a fixed width: the smallest of 1, 2, 4 or 8 bytes that holds the largest discriminant, unless an integer `#[repr]` such as `#[repr(u16)]` fixes it.
Explicit values on variants with fields need a `#[repr]` anyway, as Rust requires:

<!-- kladde-example: name=pinned-discriminants file=src/lib.rs deps=kladde,kladde-types
before:
  use kladde::Durable;
  use kladde_types::DurableString;
-->
```rust
#[derive(Durable)]
#[repr(u32)]
enum PhoneNumber {
    Mobile(DurableString) = 1,
    Landline(DurableString) = 2,
}
```

## Trait objects

A `Box<dyn Shape>` is not durable: a file can hold only values whose types it can name.
**Hold an enum of the shapes instead, and let it implement the trait by forwarding to its variants**: mark the trait `#[kladde::dispatch]`, and the enum `#[kladde(dispatch(Shape))]`.

<!-- kladde-example: name=dispatch file=src/main.rs mode=run deps=kladde -->
```rust
use kladde::{Durable, Kladde};

#[kladde::dispatch]
trait Shape {
    fn area(&self) -> f32;
}

#[derive(Durable)]
struct Circle { r: f32 }
impl Shape for Circle {
    fn area(&self) -> f32 { 3.0 * self.r * self.r }
}

#[derive(Durable)]
struct Rect { w: f32, h: f32 }
impl Shape for Rect {
    fn area(&self) -> f32 { self.w * self.h }
}

#[derive(Durable)]
struct Frame { outer: Rect, hole: Rect, angle: f32, label: u32 }
impl Shape for Frame {
    fn area(&self) -> f32 { self.outer.area() - self.hole.area() }
}

#[derive(Durable)]
#[kladde(dispatch(Shape))]
#[repr(u32)]
enum AnyShape {
    Circle(Circle) = 1,
    Rect(Rect) = 2,
    Frame(Box<Frame>) = 3,  // boxed, so that a circle stays small in memory
}

fn main() -> kladde::Result<()> {
    let mut shape = Kladde::new(AnyShape::from(Circle { r: 1.0 }));
    assert_eq!(shape.get().area(), 3.0);

    let frame = Frame {
        outer: Rect { w: 3.0, h: 3.0 },
        hole: Rect { w: 1.0, h: 1.0 },
        angle: 0.0,
        label: 7,
    };
    shape.guard().set(frame.into())?;   // `From<Frame>` boxes it
    if let AnyShapeParts::Frame(mut f) = shape.guard().parts() {
        f.hole_mut().w_mut().set(2.0)?;
    }
    assert_eq!(shape.get().area(), 7.0);
    Ok(())
}
```

The enum implements every method of the trait, each forwarding to the value its current variant holds, and converts `From` each variant's value.
It is an ordinary durable enum otherwise: you mutate a variant through `parts()`, and switch variants with `set`.

**Each variant holds one value, whose type implements the trait**, or the enum fails to compile, at the variant.
The discriminants are optional, as on every enum, but explicit ones keep your files readable when you add or reorder implementations.
**A variant may box its value**, which keeps one large implementation from making every value of the enum as large in memory; it changes nothing in the file, so you can box or unbox a variant later.

The trait's methods must take `self`, `&self` or `&mut self`, and mention `Self` nowhere else, and the trait and the enum must live in one crate; [the derive macro](../derive-macro.md#trait-objects-as-an-enum) says why.

## Generics

Type parameters work, and each gets a `Durable` bound:

```rust
#[derive(Durable)]
struct Labelled<T> {
    label: DurableString,
    value: T,
}
```

Lifetime and const parameters are not supported.

## What the derive needs

Every persisted field must be `Durable`.
In practice that means:

- primitives — fine as-is;
- kladde containers — fine as-is;
- your own derived types — fine, and they nest arbitrarily;
- `String`, `Vec<T>`, `HashMap<K, V>` — **not** `Durable`.
  Use the kladde equivalents.
  This is deliberate: a field's type says whether it is durable, and the compile error tells you which type to use.

## Non-persisted fields

Sometimes a struct has a field that should not be stored — a cache, a handle, something derived.
The derive macro has no attribute for this yet; the workaround is a [hand-written implementation](custom-durable.md) that encodes only the fields you want persisted.

Doing so does **not** make your type opaque to the format.
A hand-written implementation that encodes exactly the bytes of a struct with two fields *has* that struct's [descriptor](../../spec/schema/type-descriptors.md).
The presence of a third, non-persisted field in your Rust source is invisible.

## Encoding stability

A few things you should know before you ship a file format built on a derived type.

**Field order is encoding.**
Reordering fields in the source changes the order of their encodings, and therefore changes the type's [fingerprint](../../spec/schema/fingerprints.md), and `Kladde::open` refuses a file whose fingerprint differs from your type's.
Once [evolution](../../spec/schema/evolution.md) lands, reordering will be non-breaking, because fields are identified by name.

**Variant order is encoding only through implicit discriminants.**
A variant without an explicit discriminant takes the next number, so inserting or reordering variants renumbers the ones after them, and changes the fingerprint.
With [explicit discriminants](#enums), reordering changes nothing, since enum variants are canonicalized by discriminant value.

**The discriminant's width in a `Fixed` value is encoding too, unless you pin it.**
Without a `#[repr]`, adding a 257th variant, or giving one a discriminant of 256 or more, widens the discriminant of every `Fixed` value of the type, and changes the fingerprint.
An integer `#[repr]`, such as `#[repr(u16)]`, fixes the width up front.

**Renaming a type is free.**
A struct's or enum's own name is not fingerprinted.
Renaming a *field* or a *variant* is not free, since those are the identities evolution reconciles by.
