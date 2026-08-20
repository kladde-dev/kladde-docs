---
title: Deriving your own types
---

`#[derive(Persistable)]` turns your own struct or enum into a backed type, as long as every persisted field is itself `Persistable`.

## Structs

```rust
#[derive(Persistable)]
struct Contact {
    email: PersistableString,
    phones: PersistableVec<PhoneNumber>,
    starred: bool,
}
```

The macro generates a guard type — `ContactGuard` — with a `_mut()` accessor per field:

```rust
let mut contact = /* ... a ContactGuard ... */;
contact.starred_mut().set(true);
contact.phones_mut().push(number);
```

**A struct owns no allocation of its own.**
Its fields are laid out consecutively in whatever allocation contains it, so its inline size is just the sum of its fields' inline sizes, and each field's offset is the sum of the earlier ones'.
Those offsets are compile-time constants, which is why field access costs nothing at runtime.

That includes primitive fields.
`bool`, `i32`, `char` and friends all implement `Persistable`, specifically so the macro can treat every field uniformly rather than special-casing leaves.

## Enums

```rust
#[derive(Persistable)]
enum PhoneNumber {
    Mobile(PersistableString),
    Landline(PersistableString),
}
```

An enum is a discriminant followed by the selected variant's fields, each variant laid out like a struct in its own right.
Its inline size is the discriminant width plus the largest variant's field sum — so, like a struct, it owns no allocation.

Positional fields are named by their position: `Mobile(PersistableString)` has a field called `"0"`.

**Only whole-value replacement is supported so far:**

```rust
guard.set(PhoneNumber::Mobile(PersistableString::from("555-0100")));
```

Mutating a field *within* the current variant in place, and matching directly on a generated guard, are both intended and not yet built.

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

The derive macro has no attribute for this yet.
The workaround is a [hand-written implementation](custom-persistable.md) that reads and writes only the fields you want persisted.

Worth knowing: doing so does **not** make your type opaque to the format.
A hand-written implementation that writes exactly the bytes of a struct with two fields *has* that struct's [descriptor](../../spec/schema/type-descriptors.md).
The presence of a third, non-persisted field in your Rust source is invisible.
The descriptor describes a representation, not a Rust type.

## Generics

Generic types and types with where-clauses are **not yet supported** by the derive macro.
The pattern is understood for plain types; extending it is outstanding work.

## Layout stability

Two things you should know before you ship a file format built on a derived type.

**Field order is layout.**
Reordering fields in the source changes every subsequent field's offset, and therefore changes the type's [fingerprint](../../spec/schema/fingerprints.md).
Once [evolution](../../spec/schema/evolution.md) lands, reordering will be non-breaking, because fields are identified by name.
Until then, it breaks the file.

**Variant order is not layout.**
Enum variants are canonicalized by discriminant value, so reordering them in the source — without changing their discriminants — changes nothing.

**Renaming a type is free.**
A struct's or enum's own name is not fingerprinted.
Renaming a *field* or a *variant* is not free, since those are the identities evolution reconciles by.
