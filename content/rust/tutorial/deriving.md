---
title: Deriving your own types
---

`#[derive(Persistable)]` turns your own struct or enum into a durable type, as long as every persisted field is itself `Persistable`.

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
That is deliberate: if you had to add `kladde-persist` yourself, a version disagreeing with `kladde`'s would give you two distinct copies of the `Persistable` trait and an error message that prints the expected and found types identically.

Here is a complete program:

<!-- kladde-example: name=phonebook file=src/main.rs mode=run deps=kladde,kladde-types -->
```rust
use kladde::{Kladde, Persistable};
use kladde_types::{PersistableString, PersistableVec};

#[derive(Persistable)]
enum PhoneNumber {
    Mobile(PersistableString),
    Landline(PersistableString),
}

#[derive(Persistable)]
struct Contact {
    email: PersistableString,
    phones: PersistableVec<PhoneNumber>,
    starred: bool,
}

fn main() -> kladde::Result<()> {
    let mut contact = Kladde::new(Contact {
        email: PersistableString::from("ada@example.com"),
        phones: PersistableVec::new(),
        starred: false,
    });

    let mut guard = contact.guard();
    guard.starred_mut().set(true)?;
    guard
        .phones_mut()
        .push(PhoneNumber::Mobile(PersistableString::from("555-0100")))?;
    Ok(())
}
```

The `Persistable` in that `use` is the derive *macro*.
It shares its name with the `Persistable` *trait*, which is legal and deliberate — they live in different namespaces, exactly as `serde`'s `Serialize` does.
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

**A struct owns no allocation of its own.**
Its fields are laid out consecutively in whatever allocation contains it, so its inline size is just the sum of its fields' inline sizes, and each field's offset is the sum of the earlier ones'.
Those offsets are compile-time constants, which is why field access costs nothing at runtime.

That includes primitive fields.
`bool`, `i32`, `char` and friends all implement `Persistable`, specifically so the macro can treat every field uniformly rather than special-casing leaves.
So do tuples of up to twelve `Persistable` types, laid out like a tuple struct.

## Newtypes

A single-field struct marked `#[kladde(transparent)]` is persisted exactly as its field — the same bytes and the same [fingerprint](../../spec/schema/fingerprints.md) — so wrapping a field in a newtype changes nothing in the file:

<!-- kladde-example: name=newtype file=src/lib.rs deps=kladde
before:
  use kladde::{Kladde, Persistable};
-->
```rust
#[derive(Persistable)]
#[kladde(transparent)]
struct Meters(u32);

fn walk(distance: &mut Kladde<Meters>) -> kladde::Result<()> {
    distance.guard().get_mut().set(42)
}
```

Its guard's `get_mut()` returns the field's guard.

## Enums

An enum is a discriminant followed by the selected variant's fields, each variant laid out like a struct in its own right.
Its inline size is the discriminant width plus the largest variant's field sum — so, like a struct, it owns no allocation.
Positional fields are named by their position: `Mobile(PersistableString)` has a field called `"0"`.

**To mutate the current variant in place, match on the guard's `parts()`.**
It hands out the guards of the current variant's fields, in an enum named after yours, `PhoneNumberParts`, with the same variants:

<!-- kladde-example: name=enum-parts file=src/main.rs mode=run deps=kladde,kladde-types -->
```rust
use kladde::{Kladde, Persistable};
use kladde_types::PersistableString;

#[derive(Persistable)]
enum PhoneNumber {
    Mobile(PersistableString),
    Landline(PersistableString),
}

fn main() -> kladde::Result<()> {
    let mut number = Kladde::new(PhoneNumber::Mobile(PersistableString::from("555-01")));

    let mut guard = number.guard();
    match guard.parts() {
        PhoneNumberParts::Mobile(mut digits) => digits.push_str("00")?,
        PhoneNumberParts::Landline(mut digits) => digits.set("555-0199")?,
    }

    // To switch to another variant, replace the whole value.
    guard.set(PhoneNumber::Landline(PersistableString::from("555-0123")))?;
    assert!(matches!(number.get(), PhoneNumber::Landline(digits) if digits == "555-0123"));
    Ok(())
}
```

Each field guard writes inside its field, so the variant stays what it was.
`set` replaces the whole value, which is how the variant changes: it stores the new value, then frees whatever the old one owned.
`parts()` borrows the guard, so the compiler stops you from calling `set` while you still hold a field's guard.

**The discriminant is Rust's own**: the one you write (`Mobile = 1`), or else one more than the previous variant's, counting from 0.
On file it takes the smallest of 1, 2, 4 or 8 bytes that holds the largest discriminant, unless an integer `#[repr]` such as `#[repr(u16)]` fixes the width; negative discriminants and signed or pointer-sized `repr`s are not supported.
Explicit values on variants with fields need a `#[repr]` anyway, as Rust requires, and this one also makes the discriminant 4 bytes wide where 1 would do:

<!-- kladde-example: name=pinned-discriminants file=src/lib.rs deps=kladde,kladde-types
before:
  use kladde::Persistable;
  use kladde_types::PersistableString;
-->
```rust
#[derive(Persistable)]
#[repr(u32)]
enum PhoneNumber {
    Mobile(PersistableString) = 1,
    Landline(PersistableString) = 2,
}
```

## Generics

Type parameters work, and each gets a `Persistable` bound:

```rust
#[derive(Persistable)]
struct Labelled<T> {
    label: PersistableString,
    value: T,
}
```

Lifetime and const parameters are not supported.

## What the derive needs

Every persisted field must be `Persistable`.
In practice that means:

- primitives — fine as-is;
- kladde containers — fine as-is;
- your own derived types — fine, and they nest arbitrarily;
- `String`, `Vec<T>`, `HashMap<K, V>` — **not** `Persistable`.
  Use the kladde equivalents.
  This is deliberate: the compile error is the mechanism that stops you from silently persisting nothing.

## Non-persisted fields

Sometimes a struct has a field that should not be stored — a cache, a handle, something derived.
The derive macro has no attribute for this yet; the workaround is a [hand-written implementation](custom-persistable.md) that reads and writes only the fields you want persisted.

Doing so does **not** make your type opaque to the format.
A hand-written implementation that writes exactly the bytes of a struct with two fields *has* that struct's [descriptor](../../spec/schema/type-descriptors.md).
The presence of a third, non-persisted field in your Rust source is invisible.

## Layout stability

A few things you should know before you ship a file format built on a derived type.

**Field order is layout.**
Reordering fields in the source changes every subsequent field's offset, and therefore changes the type's [fingerprint](../../spec/schema/fingerprints.md), and `Kladde::open` refuses a file whose fingerprint differs from your type's.
Once [evolution](../../spec/schema/evolution.md) lands, reordering will be non-breaking, because fields are identified by name.

**Variant order is layout only through implicit discriminants.**
A variant without an explicit discriminant takes the next number, so inserting or reordering variants renumbers the ones after them, and changes the fingerprint.
With [explicit discriminants](#enums), reordering changes nothing, since enum variants are canonicalized by discriminant value.

**The discriminant's width is layout too, unless you pin it.**
Without a `#[repr]`, adding a 257th variant, or giving one a discriminant of 256 or more, widens the discriminant of every value of the type, which moves every field behind it in every struct that contains it, and changes the fingerprint.
An integer `#[repr]`, such as `#[repr(u16)]`, fixes the width up front.

**Renaming a type is free.**
A struct's or enum's own name is not fingerprinted.
Renaming a *field* or a *variant* is not free, since those are the identities evolution reconciles by.
