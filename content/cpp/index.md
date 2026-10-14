---
title: kladde-cpp
---

The C++ implementation of kladde: a second, independent implementation of the [specification](../spec/), and the library behind the Inkscape demo.

Everything here is **specific to C++**.
The [file format](../spec/) and the [algorithms](../impl/) are shared with [kladde-rs](../rust/); the description of types, the handles and the error handling are not.
Its design was worked out in [the C++ draft](../drafts/cpp.md), which this section records as built, with the places where building it went another way.

## What it is

A CMake project, C++20, with no required dependencies: a compiled schema layer and store, and a header-only typed layer on top.

```cpp
#include <kladde/kladde.hpp>

struct Contact {
    std::string email;
    kladde::Vector<PhoneNumber> phones;
    bool starred = false;
};
KLADDE_STRUCT(Contact, email, phones, starred)

auto file = kladde::File<Contact>::create("contacts.kladde", Contact{.email = "ada@example.com"}).value();
Contact const& contact = file.get();   // reading is plain C++
auto c = file.edit();                  // kladde::Mut<Contact>
c.starred().set(true);                 // one Write of one byte
c.email().insert(0, "lady.");          // one Splice
c.phones().push_back(Mobile{"555-0100"});  // one transaction
```

## Three layers

| layer | headers | holds |
| --- | --- | --- |
| **the schema** | `kladde/schema.hpp` | descriptors, their canonical encoding, fingerprints, sizes, and a textual dump for comparing tables across implementations |
| **the store** | `kladde/store.hpp` | the file format, the journal, the flush, consolidation, and storage; positions and bytes only |
| **typed values** | `kladde/kladde.hpp` | which C++ types are durable, how they encode, their handles, and `kladde::File` |

The store and the schema are compiled into `kladde::kladde`; the typed layer is templates over a small virtual interface, `kladde::Backend`, which the store implements and which a `kladde::VectorBackend` — a plain byte vector that applies every mutation at once — implements for tests.

## Which C++ types are durable

**Standard types are durable wherever the in-memory value needs nothing a standard type cannot hold**, and kladde's own containers take over where it does.

| C++ | kind | handle |
| --- | --- | --- |
| `std::uint8_t` … `std::uint64_t`, `std::int8_t` … `std::int64_t`, `float`, `double`, `bool`, `char32_t` | Primitive | `set` |
| `std::string` | Text | `set` (records only the changed middle), `insert`, `erase`, `replace`, `append`, `clear` |
| `std::pair`, `std::tuple` | Struct `TupleN` | `get<I>()`, `parts()` |
| `std::array<T, N>` | Array | `at(i)` |
| `std::optional<T>` | Enum `Option` | `emplace`, `reset`, `value()` |
| `std::vector<T>` for fixed-size `T` | Sequence | `at`, `insert`, `push_back`, `erase`, `pop_back`, `clear` |
| `std::variant<…>` | Enum | `set`, `visit`, `if_holds<A>` |
| `enum class` with `KLADDE_ENUM` | Enum of unit variants | `set` |
| structs with `KLADDE_STRUCT` | Struct | an accessor per field, `at<&T::m>()`, `parts()` |
| structs with `KLADDE_TRANSPARENT` | as their one member | the member's |
| `kladde::Vector<T>` | Sequence | `at`, `insert`, `push_back`, `remove`, `move`, `cut`, `insert_cut` |
| `kladde::List<T>` | Sequence | as `Vector`, plus `detach` and `insert_element`; long-lived `kladde::Ref<T>` |
| `kladde::Map<K, V>` | Map | `at(key)`, `insert`, `remove` |
| `kladde::Box<T>` | as `T` | `T`'s |
| `kladde::Fixed<T>` for scalars and `enum class` | Fixed | `set`, one Write |

**Text must be UTF-8.**
A `std::string` used as a byte buffer — file contents, a serialized message, a binary identifier — is not text, and storing one fails; hold bytes in a `std::vector<std::uint8_t>`, a Sequence of `u8`.

A `std::vector` of elements whose size depends on their value fails to compile, with a message that names `kladde::Vector`: it is the one place where the plain type would quietly cost what kladde exists to avoid.

## Handles

**`kladde::Mut<T>` is the short-lived handle**, kladde-rs's `Mut`, valid until something is written through a handle that is not itself or made from it.
C++ cannot enforce that rule, so it is documented, like iterator invalidation; and since a handle made from another refers to its parent's place in the sequence, **a handle must not be moved while a handle made from it lives**.

**`kladde::Ref<T>` is the long-lived handle**, to an element of a `kladde::List`, valid until the element is destroyed, as [Values and their positions](../impl/values.md#handles-that-outlive-edits-elsewhere) describes.
Each list element is an *anchor*: it knows its list and its index, and finds its start by asking its list, which asks the anchor of the value that holds it, up to the file's root.
The list finds its own offset inside that value by walking the value's fields to it, which costs the sum of the sizes of the fields before it, each of which every durable type knows in constant time.

## Errors

**Opening and creating return `kladde::expected<T>`**, a C++20 stand-in for `std::expected<T, kladde::error>`, since their failures are ordinary and the caller must handle them, with or without exceptions.

**A mutation or a flush fails only when the disk does**, and the file is then poisoned.
With exceptions, the call throws `kladde::exception`; without, it calls the handler that `kladde::set_error_handler` installs and returns, leaving the value as it was, since memory changes only after the store recorded the change.

Inside the library, every fallible function returns an `expected`, and `KLADDE_TRY` propagates it as Rust's `?` does, so the port of the store follows kladde-rs line by line.

## Where kladde-cpp departs from the draft

- **Struct fixed forms are not implemented yet**: `kladde::Fixed<T>` takes scalars and `enum class` only.
- **`kladde::Box<T>` reports no fixed size**, where kladde-rs's `Box<T>` reports `T`'s: a recursive type reaches itself through a box, and asking would make the compiler instantiate the type inside itself.
  The fixed size is an optimization of the typed layer, not part of the schema, so files are unaffected; only a `std::vector` of boxes is refused that kladde-rs would accept.
- **An `enum class`'s discriminant width in the fixed form is the smallest of 1, 2, 4 and 8 bytes that holds the largest discriminant**, as for a Rust enum without `#[repr]`, not the size of the underlying type, which C++ defaults to `int`.
- **Class hierarchies (`kladde::Poly<Base>`), debug checking of handles, and reflection are not built yet.**
- **The Inkscape demo observes Inkscape's XML tree rather than replacing it**; see [the evaluation](../evaluation/cpp-port.md).
