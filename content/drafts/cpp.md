---
title: A C++ implementation
---

**Status: a proposal for the high-level design of kladde-cpp.**
Nothing here is built.
It answers two questions that were open from the start — what stands in for Rust's `#[derive(Durable)]`, and how C++ types map to the schema's enums — and raises a third that turned out to matter as much: how long a handle to a value may live.

The specification is not frozen, and finding out what a second language needs from it is half the point of a second implementation.
So this draft does not take the specification as given: where a change to it would make kladde-cpp simpler or its API more idiomatic, [Changes to the specification](#changes-to-the-specification) proposes it, with what it costs the format and kladde-rs.
Four candidates came up, besides a few smaller points; one of them, on how an enum's variants hold their fields, changes how C++ maps enums enough that the sections on enums assume it, and say what they would do without it.

Like every draft, it mixes what belongs in [kladde-rs](../rust/)'s counterpart of a language section with what may turn out to be a proposal for [Implementation](../impl/) or [Specification](../spec/); the open questions at the end say which parts might travel.

## What drives the design

**A library for many applications, with Inkscape as the first.**
The first demo is a fork of Inkscape that edits a kladde file with an SVG schema and has no save button: every dragged control point and every keystroke is durable when the call that made it returns.
So the design has to suit an existing, large, object-oriented C++ code base that holds pointers to its document's nodes for as long as it likes, and not only programs written for kladde from scratch.

**C++20 as the baseline, C++26 reflection as the destination.**
Inkscape builds with C++20, and the compilers of today's long-term-support distributions (GCC 13 on Ubuntu 24.04) know nothing of reflection.
Static reflection is in C++26, and compiler support for it is arriving (GCC 16 has an implementation behind `-freflection` at the time of writing), so the design must work without it and get better with it, without a second API.

**Idiomatic C++, not Rust transliterated.**
The [overview](../index.md#the-ambition) asks for this, and the differences that matter are few but deep:

| | Rust | C++ |
| --- | --- | --- |
| deriving code from a type | procedural macros see the type's tokens | the preprocessor sees tokens it cannot parse; C++26 reflection sees the type |
| exclusive access while mutating | enforced by the borrow checker | a convention, checkable at run time in debug builds |
| long-lived references into a structure | fight the borrow checker | ordinary, and expected by existing code |
| trait objects | need an enum and a `dispatch` attribute to be durable | class hierarchies with virtual functions are the native idiom |
| errors | `Result` | exceptions, which [Durability](../spec/durability.md#what-is-guaranteed) already names for C++ |

**Everything below the typed values is the reference algorithms, ported.**
The storage engine — pages, span table, rope, journal, flush, consolidation — is language-agnostic by construction, and [Implementation](../impl/) describes it closely enough to port.
Most of the effort of kladde-cpp is there, and C++ asks almost nothing of it that Rust does not; the exceptions are [a flush that does not block the caller](#a-flush-off-the-callers-thread) and [values detached for longer than a transaction](#cuts-that-outlive-their-transaction), both of which an interactive document editor needs more than C++ does.
This draft is mostly about the part on top, where C++ and Rust part ways.

## Three layers

| layer | holds | form |
| --- | --- | --- |
| **the store** | the file format, durability, journal, flush, consolidation; positions and bytes only | a compiled library with a small non-template interface |
| **the schema** | descriptors, their canonical encoding, fingerprints, and later resolution | compiled, plus `constexpr` helpers that build descriptors from types |
| **typed values** | which C++ types are durable, how they encode, and the handles that mutate them | header-only templates over the store's interface |

**The store knows nothing about types**, exactly as in the reference algorithms: it takes `write`, `splice`, `move` and `paste` at positions in the sequence, opens and ends transactions and batches, and flushes.
Keeping it free of templates keeps compile times and code size of applications down, keeps the hard part testable on its own against a plain byte vector ([the differential oracle](../impl/flush.md#correctness-the-differential-oracle)), and gives it a C ABI later almost for free, should another language want to bind it.

*Why a native port rather than binding kladde-rs.*
Linking the Rust store through a C ABI would be quicker to a first demo, and is a fair fallback if the port lags.
But a second, independent implementation is how the specification finds out where it is ambiguous, which is worth more at this stage than the code it saves; and a C++ library that needs a Rust toolchain to build is a hard sell to the applications it is for.

## The shape of the API

```cpp
#include <kladde/kladde.hpp>

struct Contact {
    std::string email;
    kladde::Vector<PhoneNumber> phones;
    bool starred = false;
};
KLADDE_STRUCT(Contact, email, phones, starred)

void example() {
    auto file = kladde::File<Contact>::create("contacts.kladde",
                                              Contact{.email = "ada@example.com"});

    Contact const& contact = file.get();   // reads: the plain value, no cost
    std::size_t n = contact.phones.size();

    auto c = file.edit();                  // kladde::Mut<Contact>
    c.starred().set(true);                 // a Write of one byte
    c.email().insert(0, "lady.");          // one Splice
    c.phones().push_back(Mobile{"555-0100"});  // one transaction
}
```

**`kladde::File<T>` is the root**, Rust's `Kladde<T>`: it owns the value and the store behind it, and is movable but not copyable.
`get()` hands out `T const&`, so reading is plain C++: members, iterators, range-for, algorithms.

**`kladde::Mut<T>` is the handle that mutates**, the guard of kladde-rs under a name that says what it is for.
It holds a pointer to the in-memory value and the value's region of the sequence, and every mutating method records first and changes memory second.
It dereferences to `T const&`, so reading through a handle works as reading the value does.
The only way to mutate a value inside a `File` is through a `Mut`, as in Rust; C++ cannot forbid `const_cast`, but it does not have to make it easy.

## The analog of `#[derive(Durable)]`

What the derive gives a Rust type is five things: the encoding, the decoding, the descriptor, the fixed size, and the guard with an accessor per field.
The first four follow mechanically from the list of fields — their names, their types, and how to reach them — and the fifth needs names that the program can call.
So the question is how C++ gets at the list of fields.

### The options

| option | C++ | how it looks | verdict |
| --- | --- | --- | --- |
| hand-written traits | any | specialize `kladde::Durable<T>` by hand | always available; the escape hatch, as in Rust |
| a describing macro | 20 | `KLADDE_STRUCT(Contact, email, phones, starred)` | **the baseline** |
| member enumeration without a macro | 20 | nothing, for aggregates, by the tricks of Boost.PFR | too fragile for something that decides a file format |
| code generation from a schema file | any | an IDL and a generator, as Protocol Buffers do | wrong default for a code-first library; right for a later tool |
| static reflection | 26 | `struct [ [=kladde::durable] ] Contact { ... };` | **the destination** |

**Recommended: one internal description of a type, `kladde::describe<T>`, fed by a macro today and by reflection later.**
Everything else in the library — encoding, decoding, descriptors, fixed sizes, handles — is written once against that description, so a type described by the macro and a type described by reflection behave identically and have the same fingerprint, and an application can switch per type when its compiler allows.

### The macro

`KLADDE_STRUCT` follows the pattern that C++ libraries have converged on for exactly this job, Boost.Describe's `BOOST_DESCRIBE_STRUCT` and nlohmann/json's `NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE`: it sits next to the type, in the type's namespace, and lists the members to persist, in encoding order.
It expands to two things, found through argument-dependent lookup so that the user never opens namespace `kladde`:

```cpp
// Roughly what KLADDE_STRUCT(Contact, email, phones, starred) expands to.
constexpr auto kladde_describe(kladde::type_t<Contact>) {
    return kladde::struct_description("Contact",
        kladde::field<&Contact::email>("email"),
        kladde::field<&Contact::phones>("phones"),
        kladde::field<&Contact::starred>("starred"));
}

template <class Self>   // mixed into kladde::Mut<Contact>
struct kladde_accessors_Contact {
    auto email()   { return static_cast<Self&>(*this).template at<&Contact::email>(); }
    auto phones()  { return static_cast<Self&>(*this).template at<&Contact::phones>(); }
    auto starred() { return static_cast<Self&>(*this).template at<&Contact::starred>(); }
};
template <class Self>
kladde_accessors_Contact<Self> kladde_accessors(kladde::type_t<Contact>, Self*);
```

**The description is a `constexpr` list of member pointers and names**, from which templates compute the encoding, the decoding, the descriptor and the fixed size at compile time.
**The accessors give `c.email()` its name**, which is what Rust's `email_mut()` is, with completion in any editor that expands macros.
Where a field's name collides with a method of `Mut` itself, such as a field named `set`, the member pointer reaches it instead: `c.at<&Contact::set>()`.

`auto [email, phones, starred] = c.parts();` hands out the handles of all fields at once, as Rust's `parts()` does, through the tuple protocol that structured bindings use.

**A second form goes inside the class**, `KLADDE_MEMBERS(email, phones, starred)`, for types whose persisted members are private: it defines the same two things as a hidden friend and a nested template, which may name private members.

**What the macro buys over Rust's derive, besides working at all:**

- **Members it does not list are not persisted.**
  A cache, a handle, a pointer to a widget stay in the C++ type and out of the file, with no hand-written implementation; Rust's derive has no attribute for this yet.
- **Field names on file can differ from names in C++**, through a longer form, `KLADDE_FIELD(member, "name")`.
  Field names are fingerprinted and are what [evolution](../spec/schema/evolution.md#what-is-settled) reconciles by, so this matters for files that applications in two languages, with two naming conventions, share.
- **Options ride along as arguments**: a field held in its fixed form, a single-field struct marked transparent, a struct marked bounded, as `#[kladde(...)]` attributes do in Rust.

What it costs is that the member list repeats the declaration, and nothing checks that it lists every member that should be listed.
That is the price C++20 asks of every library of this kind, and the reason to move on as soon as reflection allows.

### Reflection

With C++26, the description comes from the type itself:

```cpp
struct [ [=kladde::durable] ] Contact {
    std::string email;
    kladde::Vector<PhoneNumber> phones;
    [ [=kladde::fixed] ] std::uint64_t revision = 0;
    [ [=kladde::skip] ] Cache cache;   // not persisted
};
```

Annotations are written `[ [=…] ]` on these pages, which C++ accepts as readily as the usual form without the spaces, because the site would take the usual form for a link.
The annotation is the opt-in, as `#[derive(Durable)]` is: a file format should not change because someone added a member to an unrelated struct.
`kladde::describe<T>` walks the non-static data members with `std::meta::nonstatic_data_members_of`, takes their names from `identifier_of`, and their options from annotations.

Reflection cannot generate member functions, so `c.email()` stays the macro's; but `std::meta::define_aggregate` can generate a struct with a data member per field, which gives `c.parts().email` with its name, and that is the form to recommend for reflected types.

### Generics, and what is durable

A class template is described once, with a variant of the macro that takes its template head, and is durable wherever its parameters are.
Lifetimes have no counterpart in C++, and non-type parameters can simply be allowed, since the description is computed per instantiation.

## Which C++ types are durable

**Standard types are durable wherever the in-memory value needs nothing that a standard type cannot hold**, and kladde's own containers take over where it does.
Rust refuses `String` and `Vec` to make durability visible in the type; in C++ the handle, `Mut<std::string>`, is a type of its own whatever the value is, so the plain type costs nothing in clarity and lets existing types persist unchanged.

@Claude: the rust API has grown over many iterations with vastly different designs of kladde. An earlier design of kladde made it impossible to use `String`, `Vec`, or `HashMap` directly in a durable struct, which motivated the dedicated types `DurableString`, `DurableVec`, `DurableHashMap`, and possibly others back then. But I'm not sure if these dedicated types are still necessary in today's design of kladde. Which of the `Durable` types that kladde-rs defines are still necessary and which could be replaced with their standard rust types without cost?

**Claude:** `DurableString` could be replaced by `String` at no cost, `DurableVec<T>` by `Vec<T>` for fixed-size `T` only, and `DurableMap` and `DurableBlob` are still necessary.

- **`DurableString` is a `String` and nothing else** (`struct DurableString(String)`), and its guard is a type of its own anyway, so `impl Durable for String` in kladde-persist, where the trait lives, would give the same encoding, the same guard and the same cost.
  The only argument left for it is the one the tutorial gives, that a field's type should say it is durable, which a guard of its own already says.
- **`DurableVec<T>` is a `Vec<T>` plus a Fenwick tree of the elements' sizes**, and the tree is empty for a fixed-size `T`.
  A `Vec<T>` has nowhere to keep the tree, so it can stand in only where no tree is needed; Rust has no specialization, so that would take a marker trait implemented by every fixed-size type (the derive can implement it where all fields are fixed-size, as it implements `Bounded`), and `impl<T: FixedSize> Durable for Vec<T>`.
  For variable-size elements a `Vec<T>` would have to find element `i` by summing the sizes before it, `O(m)` per guard, which is what the tree exists to avoid.
- **`DurableMap` is necessary**: a `HashMap` iterates in an order that changes when it rehashes, while the file keeps entries in one order that each entry's position depends on, and neither `HashMap` nor `BTreeMap` can keep a Fenwick tree of entry sizes by rank.
- **`DurableBlob` is necessary as an opt-in**: `impl<T: Serialize> Durable for T` would conflict with every other implementation, so a serde type needs a wrapper to say "persist me through serde"; it is also where the serialized bytes are cached.
- `Option`, `Box`, tuples and the primitives are already the standard types, and `Fixed<T>` is a choice of encoding that only a type can carry.

**Decision:** remove `DurableString`; implement `Durable` for `Vec<T>` where `T: Durable` with a const assertion that `T::FIXED_SIZE.is_some()`. Update the documentation and any examples in the tutorial, if applicable. `DurableVec<T>` stays and should work on all `T: Durable`, even fixed-sized ones. For fixed-size `T` it should not allocate a Fenwick tree (but it still has the field for the empty Fenwick tree).

So kladde-rs could follow the C++ table: `String` for text, `Vec<T>` for fixed-size elements, and a dedicated vector for variable-size ones.

| C++ | kind | note |
| --- | --- | --- |
| `std::uint8_t` … `std::uint64_t`, `std::int8_t` … `std::int64_t` | Primitive | matched by width and signedness, so `long` maps to whichever it is |
| `float`, `double`, `bool`, `char32_t` | Primitive | `char32_t` is the schema's `char`; plain `char` is refused, since its signedness and its meaning are ambiguous |
| `std::string` | Text | `Mut<std::string>` has `set`, `insert`, `erase`, `replace`, `append`, each one splice |
| `std::array<T, N>` | Array | |
| `std::pair`, `std::tuple` | Struct | positional fields `"0"`, `"1"`, … as Rust's tuples |
| `std::optional<T>` | Enum | `None` = 0, and `Some` = 1 holding the `T`, as `Option<T>` |
| `std::vector<T>` for fixed-size `T` | Sequence | element `i` lies at `i` times its size, so no index is needed |
| `kladde::Vector<T>` | Sequence | a `std::vector<T>` and a Fenwick tree of sizes, for variable-size `T` |
| `kladde::Map<K, V>` | Sequence of entries | the index map of [the built-in containers](../impl/values.md#the-built-in-containers); entries named as [proposed below](#maps-as-a-kind-of-their-own) |
| `kladde::List<T>` | Sequence | elements with stable addresses, for long-lived references, [below](#long-lived-references) |
| `kladde::Box<T>` | as `T` | a non-null `std::unique_ptr<T>`; how a recursive type gets a size, as Rust's `Box` |
| `std::unique_ptr<T>` | as `std::optional<T>` | nullable, so an option |
| `kladde::Fixed<T>` | Fixed | or `[ [=kladde::fixed] ]` and the macro's option on a field |
| `kladde::Blob` | Opaque | bytes with a length, rewritten whole |
| `enum class`, `std::variant`, class hierarchies | Enum | [below](#enums) |

A `std::vector` of variable-size elements is refused at compile time with a message that names `kladde::Vector`, rather than accepted with a hidden linear scan: it is the one place where the plain type would quietly cost what kladde exists to avoid.

**`Mut<std::string>::set` splices only what changed**: it finds the common prefix and suffix of the old and the new text, which costs one pass over memory the application has just written, and records one splice of the middle.
Code that sets a whole string at every change, such as code that writes a path's whole `d` attribute for every step of a dragged control point, then records a few bytes rather than the whole attribute, without having to be rewritten in terms of edits.
This belongs in every implementation's text type and is worth proposing for [the built-in containers](../impl/values.md#the-built-in-containers).

**Decision:** yes, state this as a guarantee in the spec for `Text` and `Blob`. More precisely, the guarantee should be that setting them to a new whole value should result in records that edit at most the smallest subrange that contains all changes (there's probably a more eloquent way to phrase this). Whether an implementation goes further and tries to detect if the changes are limited to multiple subranges that are far enough apart to justify more complicated optimizations is up to the implementation, but the spec should guarantee an optimization for the common case where some substring of a larger string (or `Blob`) has changed and the application author is simply too lazy to call the appropriate splice method and instead resets the whole string.

**Claude:** Agreed, with one refinement for blobs, and one question of where it goes.
The guarantee holds as you state it for Text, which has no count: one splice of the smallest range that holds every changed byte.
A blob is an [Opaque](../spec/schema/type-descriptors.md#opaque) without a fixed size, whose encoding starts with its length, so a blob whose length changes changes its first bytes as well, and the smallest range holding every change would then reach from the length to the last change — possibly the whole blob.
So for anything with a length prefix the guarantee should be stated per part: the length is rewritten on its own, and the payload is spliced in the smallest range that holds its changes, which is two records in one transaction.
The natural home is the specification's [guarantees an implementation must provide](../spec/index.md#guarantees-an-implementation-must-provide), beside the asymptotic complexity it already promises; I have not written it there yet, since the blob refinement is yours to accept.

@Claude: agreed, with one nit-pick: the phrasing shouldn't make it necessary to set the length on its own. If a blob changes at its start, an implementation may decide to write the length prefix and the changed content in one go.

## Enums

**The schema has one kind of enum, a discriminant followed by the fields of the variant it selects, and C++ has three ways to say it.**
All three map to the same descriptor kind, so which one a program uses is invisible in the file: one program may read with a `std::variant` what another wrote from a class hierarchy, as long as names and discriminants agree — [a descriptor describes a representation, not an identity](../spec/schema/index.md#the-guiding-principle).

### `enum class`: variants without fields

```cpp
enum class Join : std::uint8_t { miter, round, bevel };
KLADDE_ENUM(Join, miter, round, bevel)
```

The discriminant is the enumerator's value, the variant's name its identifier, and the fixed form's discriminant width the underlying type's.
The macro exists only to supply names; with reflection, `std::meta::enumerators_of` does, and the annotation `[ [=kladde::durable] ]` is all a reflected enum needs.

### `std::variant`: a tagged union with value semantics

```cpp
struct Mobile   { std::string digits; };
struct Landline { std::string digits; };
KLADDE_STRUCT(Mobile, digits)
KLADDE_STRUCT(Landline, digits)

using PhoneNumber = std::variant<Mobile, Landline>;
```

**Each alternative is a variant, named after the alternative's described name, whose payload is the alternative** — which presumes [variants that hold one value](#variants-that-hold-one-value), the first of the proposed changes to the specification.
With it, one rule maps every Rust enum: kladde-svg's `Node`, which mixes a tuple variant holding a struct, `Element(Element)`, a tuple variant holding text, `Text(Str)`, and a struct variant, `ProcessingInstruction { target, value }`, is in C++

```cpp
struct Text    { std::string content; };   // transparent: a Text is its string
struct Comment { std::string content; };
struct ProcessingInstruction { std::string target; std::string value; };
KLADDE_TRANSPARENT(Text, content)
KLADDE_TRANSPARENT(Comment, content)
KLADDE_STRUCT(ProcessingInstruction, target, value)

using Node = std::variant<Element, Text, Comment, ProcessingInstruction>;
```

with the same descriptor and the same fingerprint.

**Without the change, every alternative needs a choice**, and nothing in the C++ type can make it: today a Rust tuple variant `Element(Element)` holds one field `"0"` of type `Element`, while a struct variant `ProcessingInstruction { target, value }` holds two fields of its own, and the two descriptors differ although the encodings agree.
kladde-cpp would then hold each alternative as a field `"0"` by default, which matches tuple variants and kladde-rs's [enums in place of trait objects](../rust/tutorial/deriving.md#trait-objects), and offer a flattening option per alternative for struct variants; `Node` would need it on `ProcessingInstruction` alone.

**Discriminants default to the alternative's index**, which is fragile in exactly the way Rust's implicit discriminants are: inserting an alternative renumbers the ones after it.
`KLADDE_VARIANT(PhoneNumber, (Mobile, 1), (Landline, 2))` pins them, and the [encoding-stability advice](../rust/tutorial/deriving.md#encoding-stability) of kladde-rs carries over word for word.

`Mut<std::variant<…>>` offers `set` to switch alternatives, and `visit`, which calls a visitor with the handle of the current alternative, `Mut<Mobile>` or `Mut<Landline>`, as `std::visit` does with values; `get_if<Mobile>()` returns a handle or nothing.

### Class hierarchies: a closed set of classes, held polymorphically

```cpp
struct Shape {
    float x = 0, y = 0;
    virtual ~Shape() = default;
    virtual float area() const = 0;
};
struct Circle : Shape { float r = 1;    float area() const override; };
struct Rect   : Shape { float w, h;     float area() const override; };
struct Frame  : Rect  { float border;   float area() const override; };

KLADDE_STRUCT(Shape, x, y)
KLADDE_DERIVED(Circle, Shape, r)
KLADDE_DERIVED(Rect, Shape, w, h)
KLADDE_DERIVED(Frame, Rect, border)
KLADDE_HIERARCHY(Shape, (Circle, 1), (Rect, 2), (Frame, 3))

struct Drawing { kladde::Vector<kladde::Poly<Shape>> shapes; };
```

**`kladde::Poly<Base>` is a non-null `std::unique_ptr<Base>` whose dynamic type must be one of the classes registered for `Base`**, and it is durable as an enum with a variant per concrete class.
In memory it is exactly what object-oriented C++ code holds already, so `file.get().shapes[0]->area()` is a virtual call and nothing more — no enum to forward through, and nothing like kladde-rs's `#[kladde::dispatch]`, because C++ has the real thing.

- **Every concrete class is a variant, at any depth**: `Frame` derives from `Rect`, and both are variants, flat, of one enum; abstract classes are not.
- **A variant's payload is a struct of the members of its class and of its bases, bases first**, `Frame`'s holding `x`, `y`, `w`, `h`, `border`, just as for an alternative of a `std::variant`.
- **Discriminants are explicit and belong to the hierarchy**, not to a field, so a field typed `Poly<Rect>` is an enum of `Rect` and `Frame` with the same discriminants, 2 and 3, as in a `Poly<Shape>`.
  Explicit, because classes are added to a hierarchy far more often than alternatives to a variant, and nothing orders them reliably.
- **The registry maps the dynamic type to its variant** through `typeid`, once per encode, so the classes need no virtual function of kladde's, and a class hierarchy that cannot be changed can still be made durable.
- **Decoding constructs the class**, by default construction, or through a factory registered with it for classes that have none, as Inkscape's objects do.

**Mutating through a `Mut<Poly<Shape>>` needs no dispatch for the base's members**: every variant's payload starts with them, right after the discriminant, so `s.x().set(10)` writes at the same offset whatever the shape is.
For a derived class's members, `s.as<Circle>()` returns a `Mut<Circle>` if the shape is a circle, as `dynamic_cast` does for pointers, and `s.set(std::make_unique<Rect>(…))` replaces the shape with another, by splicing the new encoding over the old.

### Which to use

| | `enum class` | `std::variant` | class hierarchy |
| --- | --- | --- | --- |
| fields per variant | none | any | any, inherited ones included |
| in memory | the enumerator | inline, the size of the largest alternative | on the heap, the size of its own class |
| adding a variant | an enumerator | an alternative, and every `visit` must handle it | a class and a line in the registry |
| behaviour | free functions | `std::visit` | virtual functions |
| shared fields | — | repeated in each alternative | in the base, and writable without dispatch |
| suits | flags and choices | small closed sets of values, new code | existing object-oriented code, and open-ended sets of kinds |

**All three deserve first-class support**, since they serve different code rather than different tastes.
New code with a handful of small alternatives is better off with `std::variant`, which keeps values inline and visits them exhaustively; code that already has a hierarchy — Inkscape's `SPObject` and its dozens of subclasses — needs the hierarchy, and could not reasonably be rewritten into variants.
The hierarchy is also the only form in which shared fields are written without dispatch, which a document model with many kinds of element and many common attributes uses constantly.
And for mere flags, an `enum class` is often still the simplest choice.

## Handles: where a value lies

**A handle has to know where its value starts in the sequence whenever it writes, and has to tell its ancestors when its value changes size**; [Values and their positions](../impl/values.md) gives the algorithms, and C++ needs both of its two forms.

### Short-lived handles

**`Mut<T>` is the guard of kladde-rs: made from its parent's handle by one lookup in the parent's index, holding its start, and valid while nothing outside its own subtree is written.**
Rust enforces that by borrowing the parent exclusively; C++ cannot, so it is a documented rule, the same kind as iterator invalidation, which C++ programmers already keep: *a handle is valid until something is written through a handle that is not its descendant.*
Debug builds check it, with a stack of the live handles in the store that a write pops down to the handle it went through — the discipline that Miri's stacked borrows check for Rust — so a stale handle fails an assertion rather than writing at the wrong position.
Release builds pay nothing.

[Guards made together](../impl/values.md#guards-made-together) carry over unchanged: `parts()` hands out handles that compute their start at every write, so that writing one field and then another works whatever the first did to its size.

### Long-lived references

**An application that keeps pointers into its document for as long as it likes needs handles that survive edits elsewhere**, and Rust's design deliberately has none.
Inkscape holds `Inkscape::XML::Node*` everywhere — in the selection, in tools, in dialogs, in the undo log — and writes through any of them at any time.

**`kladde::List<T>` keeps its elements at stable addresses, and each element knows its parent and where it stands among its siblings.**
A `kladde::Ref<T>` to an element is then valid until that element is removed, as an iterator of `std::list` is, and computes its start when it writes by climbing from the element to the root: at each level, the element's offset among its siblings, from a Fenwick tree over the siblings' sizes at the index the element keeps of itself, plus the offset of the list in its parent.
@Claude: is there a specific reason you chose an order-statistics tree here over a Fenwick tree? Would a Fenwick tree not work here or does it have an unfavorable trade-off concerning memory or runtime?

**Claude:** No good reason: a Fenwick tree works, and it is the better default, so I changed the paragraph to it.
I had reached for a tree because a `Ref` must find its element's rank from the element alone, and in a tree with parent links that survives insertions before it in `O(log m)`.
With a Fenwick tree, each element keeps its own index instead, and an insertion or removal renumbers the elements behind it and rebuilds the tree, both `O(m)` — the same `O(m)` that moving the elements' pointers in the list's array costs anyway, as [the built-in containers](../impl/values.md#what-a-container-keeps-to-find-its-children) argue for `kladde::Vector`.
In return an element costs 4 bytes of the tree and 4 of its own index instead of a tree node of 24 to 40, and finding its rank is a load rather than a climb.
An Inkscape element has tens to a few thousand siblings, for which renumbering costs microseconds; a tree would pay only for lists of hundreds of thousands that are edited in the middle, and can be an option for them, as it is for the vector.

That is `O(depth · log m)` per write, the cost of [guards made together](../impl/values.md#guards-made-together) at every level, and a change of size updates the same path back up.
It costs a parent link, an index and a cell of the siblings' Fenwick tree per element, about 16 bytes, which a document tree whose elements each hold a name, attributes and children affords.

So `kladde::Vector` and `kladde::List` differ the way `std::vector` and `std::list` do — compact, or stable — and an application chooses per container.

## Errors, transactions and exception safety

@Claude: The statement in the specification that a C++ implementation of kladde should use exceptions was not based on any deep argument and shouldn't be taken as a law. I haven't been involved in any large C++ code bases for almost 20 years, so I'm not up to date about the state of exceptions in C++, neither in terms of technical changes nor in terms of community acceptance. I remember that some experts used to advise against the use of exceptions, or even against enabling exceptions in the compiler. Has the advice changed since? And is there a consensus in the community on how to do error handling in C++? What to popular code bases like chromium, llvm, react-native, llama.cpp (or others that might be more trend setting) use?

**Claude:** There is no consensus, and the split has barely moved in twenty years; what has changed is that the case against exceptions is now made for particular environments rather than for C++ as a whole.
So kladde-cpp should work with and without them, and should use them only for failures that end a session.

*Who disables them.*
Google's style guide still says "we do not use C++ exceptions", for the reason it always gave — its existing code is not exception-safe — and Chromium builds with `-fno-exceptions`.
LLVM and Clang build without exceptions and without RTTI, and report errors through `llvm::Error` and `llvm::Expected<T>`, which abort if left unchecked in builds with LLVM's ABI-breaking checks enabled.
Firefox, most game engines (Unreal disables them by default), and most embedded code do the same.

*Who uses them.*
The standard library and Boost throw, and so do glibmm and gtkmm, which is why Inkscape builds with exceptions and catches `Glib::Error`.
LibreOffice, KDE applications, Bloomberg's libraries and much scientific code use them, and the C++ Core Guidelines recommend them for "a function that cannot perform its assigned task".
React Native's C++ core (JSI, Fabric, Folly) builds with exceptions; llama.cpp's C++ throws internally while loading a model and catches at its C API, while ggml, being C, returns codes and aborts on failed assertions.

*What has changed.*
C++23 standardized `std::expected`, so value-based error handling no longer needs a homemade type, and it has become the usual choice for failures a caller expects, such as a file that does not parse.
The cost argument has weakened: the happy path of an exception costs nothing on the common ABIs, and recent work on embedded targets (Khalil Estell's talks at CppCon 2024) found exceptions producing smaller binaries than result types.
Herb Sutter's proposal of cheaper, deterministic exceptions (P0709) was not adopted, so the language itself is where it was.
Widely used libraries cope with both camps by making throwing configurable — nlohmann/json and {fmt} both build without exceptions and abort or call a handler instead.

*What that suggests for kladde-cpp.*
Its failures come in two kinds, which deserve different mechanisms:

- **Opening and creating a file can fail in ordinary ways** — the file is missing, damaged, or of another schema — and the caller always has to handle them, so `File<T>::open` returns an expected value (`std::expected` on C++23, a type of kladde's own on C++20), with or without exceptions.
- **A mutation or a flush fails only when the disk does**, and the session ends: the `File` is poisoned.
  With exceptions, the call throws; without, it poisons the `File`, calls a handler the application installs (Inkscape would show a dialog), and returns as if nothing had been recorded, which is true of the value, since memory changes only after recording succeeds.
  Making every `set` and `push_back` return a result that must be checked would put a branch after every edit in the application for a failure that, once it happens, makes all further edits moot.

The specification's "in C++ by throwing" should then say only that the implementation reports the failure and refuses further work.

**Decision:** I agree with your suggestion for error handling in kladde-cpp.

**Errors are exceptions**, as the specification already says of C++: a `kladde::Error` hierarchy, with a failed `fsync` [poisoning](../spec/durability.md#fsync-failure-is-fatal) the `File`, so that every later call throws until it is reopened.
Applications built without exceptions are an open question.

**Every mutation prepares, records, then commits, and only the first two may throw.**
Rust treats a failed allocation as fatal; C++ reports it, so a `push_back` that recorded its splice and then failed to grow the vector in memory would leave the file and the value disagreeing.
So a mutation first does everything in memory that can fail without changing the value — reserving capacity, constructing the new element — then records, then changes memory by operations that cannot throw.
This is the strong exception guarantee, earned in the order the reference algorithms [already recommend](../impl/values.md#a-value-is-a-region).

**Transactions take a callable**, which is the one form that tells success from failure without asking a destructor to do I/O:

```cpp
file.transaction([] (kladde::Mut<Accounts> a) {
    a.checking().set(70);
    a.savings().set(30);
});   // appended here, as one transaction
```

Returning commits; an exception leaves the callable with memory already changed and no way to change it back, so it poisons the `File` and propagates, as a panic does in kladde-rs.
`file.batch(...)` has the same shape, and transactions nest inside batches as [the state machine](../impl/transactions-and-batches.md#nesting) allows.
An RAII object for transactions that span functions can come later; its destructor must not do I/O, so it would need an explicit `commit()` and would poison if destroyed without one.

## Flushing on an interactive thread

**A flush ends in an `fsync`, and a flush can be triggered from inside any mutating call**, when [the journal's budget](../impl/transactions-and-batches.md#flush-triggers) would overflow.
In Inkscape, mutating calls run on the GUI thread, so a drag could stall for as long as the disk takes to sync — milliseconds on an SSD, more on a slow disk.

Two remedies need nothing new:

- **Flush when the application is idle**: `file.flush()` from an idle callback, which an event loop offers, keeps the journal short, so the automatic flush rarely fires mid-gesture.
  kladde-cpp can offer the policy, `file.flush_if_journal_exceeds(fraction)`, and leave the scheduling to the application's loop.
  @Claude: explain: is the idea to add a method `flush_if_journal_exceeds` on `kladde::File<T>` that flushes if the journal is filled up to at least the given fraction? And does inkscape already have an idle loop where this method could be called?

  **Claude:** Yes to both.
  `file.flush_if_journal_exceeds(0.5)` appends a batch's ready run if there is one and flushes if the journal pages then hold at least half of their budget, and otherwise does nothing; the check is a subtraction of two cursors, so calling it often costs nothing.
  Inkscape runs GLib's main loop, and already does its own deferred work in idle callbacks: `SPDocument::requestModified` connects `SPDocument::idle_handler` with `Glib::signal_idle()` after every change, to update the objects built from the XML tree.
  The fork would connect a second idle callback the same way, at a priority below redrawing, so that it runs once events and frames are done — between gestures rather than during one.
  An idle flush ends the journal's epoch early, which matters for [cuts that outlive their transaction](#cuts-that-outlive-their-transaction) as answered below, so the fraction should stay well above zero.
- **A generous budget**, so that a gesture fits in the journal however long it lasts.

A flush on a background thread is a different matter: mutations appended while it runs would belong to the next journal, and an application crash before the flush's header is written would lose them although their calls had returned.
That needs a change to the journal, which [is proposed below](#a-flush-off-the-callers-thread), and then removes the stall altogether.

**Decision:** go with the flush when the application is idle. Document the flush on the background thread as a possible improvement, pending experience with the demo app.

## Files and platforms

**The store reads and writes through a `Storage` interface**, as kladde-rs does: a file, and an in-memory storage for tests that can simulate power cuts by dropping any subset of the writes since the last sync.

- **POSIX**: `pread` and `pwrite`, `fsync`, and `posix_fallocate` to grow the file, so that a full disk is [reported before a flush rather than discovered at its `fsync`](../spec/durability.md#detecting-that-the-disk-is-full).
- **macOS**: `fsync` does not ask the drive to empty its cache, so the store calls `fcntl(fd, F_FULLFSYNC)`; anything less breaks [invariant I1](../spec/durability.md#the-two-invariants) on power loss.
- **Windows**: `FlushFileBuffers`, and `SetFileValidData` or `SetEndOfFile` with care for growth.
- **No `mmap`**, for the [reasons the reference algorithms give](../impl/index.md#the-mirror).

The store owns its file exclusively, and kladde-cpp locks it (`flock`, `LockFileEx`) so that a second process opening it fails cleanly rather than [corrupting it](../spec/file-format.md#concurrency).

## The Inkscape demo

**Inkscape funnels every change to a document through one abstract class, `Inkscape::XML::Node`**, whose implementation is `SimpleNode`: `setAttributeImpl`, `setContent`, `appendChild`, `removeChild`, `changeOrder`, and a few more.
The objects Inkscape draws (`SPObject` and its subclasses) are built from that tree and updated by observing it, and its undo log records and replays changes through the same interface.
So a `KladdeNode` that implements `Node` over a durable tree makes every change durable without touching the tools, the canvas or the undo system.

**The first schema should be XML's, not SVG's**:

```cpp
struct Attribute { std::string name; std::string value; };
struct Element;
using Node = std::variant<kladde::Box<Element>, Text, Comment, ProcessingInstruction>;
struct Element {
    std::string name;
    kladde::Vector<Attribute> attributes;   // in their order, as SimpleNode keeps them
    kladde::List<Node> children;            // stable, since Inkscape holds Node*
};
```

Every Inkscape operation is then one or two records: an attribute set is a splice of its value's changed middle, a reorder is a `Move`, an insertion a splice of the new subtree's encoding.
Two details need care: Inkscape creates nodes detached and appends them later, so a detached node is an ordinary value until it is appended, which encodes it; and a node removed and appended elsewhere in one undo step can be a [cut and paste](../impl/values.md#moving-a-value-between-containers) rather than a second encoding, if the step is a transaction.
A node removed in one step and inserted again in another — undoing a deletion is the common case — is encoded again, subtree and all, unless [cuts outlive their transaction](#cuts-that-outlive-their-transaction).
Inkscape's undo steps (`DocumentUndo::done`) are natural batch boundaries.

The fork's schema is its own, and free to follow Inkscape's needs rather than any other program's.

**A typed SVG schema is the showcase that should follow**, with Inkscape's element classes as a hierarchy and attributes as typed fields, but it touches much more of Inkscape, and the XML schema already delivers the demo: no save button, and nothing lost to a crash.
@Claude: I'd like to have this eventually, so that I can compare file sizes, memory usage, and run times in the XML-based and the typed schema. But I agree that the XML-based schema should go first. Implement the typed SVG schema once you're done with the XML-based schema, and implement it on a new branch `typed` in `inkscape-kladde`.

**Claude:** Noted: the XML schema first, then the typed schema on a branch `typed` of inkscape-kladde, and the comparison of file sizes, memory and run times between the two.

## Changes to the specification

**Four changes would make kladde-cpp simpler or its API more idiomatic, and none of them is about C++ alone**: each came up because a second language, or an application of a kind kladde-rs has not met yet, looks at the format from a different side.
They are ordered by how much they would change kladde-cpp's design, and each says what it costs.

### Variants that hold one value

**Proposed: an enum's variant holds at most one value, its payload, of any type, instead of a list of fields.**
A fieldless variant has no payload; a Rust tuple variant of one field, `Element(Element)` or `Text(Str)`, holds that field's value; a struct variant, `ProcessingInstruction { target, value }`, holds a Struct of its fields, named after the variant; and a tuple variant of several fields a Struct of positional ones.
A payload that is a Struct without fields counts as no payload, since both encode to nothing.

*What it buys.*
Every C++ form of an enum — an alternative of a `std::variant`, a class of a hierarchy, `Some` of a `std::optional` — holds exactly one value, and with this rule that value is the payload, with no choice for the programmer to make and none to get wrong.
Rust's two spellings of "a variant that holds a struct", `Element(Element)` and `Element { kind, attrs, … }`, get one descriptor, so the choice between them stops being a file-format decision, as a struct's own name already is not.
And every language with tagged unions of one value — Swift's enums with associated values, TypeScript's discriminated unions, Python's union types — maps the same way.

*What it costs.*
The [canonical encoding](../spec/schema/canonical-encoding.md#descriptor-encoding) of an Enum writes `reference(payload) + 1`, or `0` for none, in place of `varint(field_count)` and the fields; [the fingerprint](../spec/schema/fingerprints.md#the-computation-exactly) follows, and the vector for `List` changes, since `Cons` then holds a Struct of `"0"` and `"1"`.
@Claude: I didn't understand the last part about the vector for `List`. Explain in more detail.

**Claude:** "Vector" there meant a test vector: the second row of the table of [expected fingerprints](../spec/schema/fingerprints.md#vectors), which the proposal would change.
That row hashes this type graph:

```
[0] Enum "List", width 4 { 0 "Nil" {}, 1 "Cons" { "0": [1], "1": [0] } }
[1] i32
```

Under the proposal, `Cons` no longer holds two fields but one payload, a Struct of them, which becomes a descriptor of its own:

```
[0] Enum "List", width 4 { 0 "Nil": [2], 1 "Cons": [3] }
[1] i32
[2] Struct { }
[3] Struct { "0": [1], "1": [0] }
```

(`Nil` holds the empty Struct if variants always hold one value, as recommended below.)
The graph is a different graph, so the hash input of `List` changes and the row's expected fingerprint, `af12af01…`, has to be recomputed; the bytes of every `List` value stay exactly as they were.
No value's encoding changes, and the [fixed form](../spec/schema/type-descriptors.md#encodings) of an Enum is the payload's fixed form instead of its fields', which is the same bytes.
kladde-rs's derive describes a struct variant as a Struct and a one-field tuple variant by its field's type.
Files of today stop opening, which a pre-stable format [allows](../spec/file-format.md#header-pages).

@Claude: I like this proposal. One detail: I'm unsure whether variants should have *at most* one value or *always* one value (giving valueless variants a payload of type `()`). Briefly discuss the trade-off.

**Claude:** *Always* one value is the better rule: it makes every variant the same shape, and it removes the one special case that *at most* one needed.

- **What *always* buys.**
  A variant is a discriminant and a payload, with no "or nothing" in the descriptors, the fingerprint, the fixed form, or any code that walks them.
  And a fieldless variant, a C++ alternative that is an empty struct, `std::monostate`, Rust's `()` and an `enum class` enumerator all get the same descriptor by construction — an empty Struct, whose name is not fingerprinted — where *at most* one needed a rule that "an empty Struct counts as no payload" to make them agree.
- **What it costs.**
  One descriptor in the table, the empty Struct, shared by every valueless variant, of three bytes; each variant then writes a reference to it, one byte, where *at most* one would have written a `0`, also one byte.
  The hash input of each valueless variant grows by a token of 17 bytes, which no one will measure.
- **What *at most* one would have bought**: an `enum class` described without an extra descriptor, and nothing else.

**Decision:** adopt this change, with the *always* one value refinement.

### Maps as a kind of their own

**Proposed: a descriptor kind Map, with a key type and a value type, whose encoding is that of a Sequence of entries, each its key's encoding and then its value's.**
Today a map is described as a Sequence of Structs of two fields, and the specification [names no fields](../spec/schema/type-descriptors.md#sequence): kladde-rs calls them `"key"` and `"value"`, and a C++ `std::pair` would call them `"0"` and `"1"`, so the same map has two fingerprints until a convention is written down.

The least change is to write down kladde-rs's names.
A kind of its own does more: every language has a native map, and a reader that knows a Sequence is a map can decode it into one — a `std::map` or a `kladde::Map` in C++, a `dict` in Python — and [tools](../spec/tooling.md#level-3--structural-export) can render it as a JSON object rather than an array of pairs.
It costs a kind tag, of which [132 is free](../spec/schema/canonical-encoding.md#descriptor-encoding), and nothing in any encoding.

### A flush off the caller's thread

**Proposed: a journal may end in a seal that names where the next epoch's journal begins, so that appends continue there while a flush folds the sealed one.**
A flush today blocks the mutating call that triggered it until its `fsync` returns, which is a stall on the GUI thread of an interactive application, mid-gesture.
It cannot simply run beside new appends: those would go to the journal of the next epoch, which only the flush's header names, and an application crash before that header is written would lose them although their calls had returned — the [guarantee](../spec/durability.md#what-is-guaranteed) that matters most.

With a seal, a flush starts by appending to journal `E`, as one more record, the page where journal `E + 1` begins, and then folds journal `E` up to the seal on a thread of its own, while the application appends to journal `E + 1`.
Recovery from header `E − 1` replays journal `E`, reaches the seal, and continues into journal `E + 1` with its own epoch's salt; recovery from header `E` starts at the same page.
So the transactions appended during the flush survive an application crash whichever header governs, and a power cut costs what it costs today.
The seal needs no new encoding, only a meaning for the [escape](../spec/journal.md#records) that a `Write` of no bytes reserves, and the pages of journal `E + 1` are live from the moment the seal reaches them, which the [reuse rule](../spec/durability.md#the-reuse-rule) then protects like any other.
The flush's work moves to its thread, and the mutating call waits only when journal `E + 1` fills its budget before the flush ends.

This is not a matter of C++, but of the applications kladde-cpp is for: a document editor wants durability at every keystroke and a frame every few milliseconds.
It should be measured in the Inkscape demo before it is adopted; idle-time flushing may turn out to be enough.

@Claude: would this proposal require making all of kladde thread save, i.e., add locks or synchronization primitives where necessary, and test for race conditions? If so, how intrusive would that change be? Or would a kladde implementation that can be reasonably used in inkscape have to be thread save anyway?

**Claude:** No to both: the application-facing API stays single-threaded, and only the flush hands work to a thread of its own, which with the seal can be as little as the `fsync` and the header write.

*What Inkscape needs.*
Inkscape changes its document on the main thread only: the XML tree, the objects built from it and the undo log are all main-thread state.
Its canvas renders on a thread pool, but from a snapshot of its own drawing tree (`Drawing::snapshot`, in `display/drawing.h`), not from the XML tree, so nothing else would touch kladde's values.
So kladde-cpp, like kladde-rs, is used by one thread at a time, and its values, handles and containers need no locks.

*What the seal needs.*
What takes long in a flush is the `fsync`; folding a journal is `O(r · log k)` in memory, and writing a flush's few dozen pages puts them in the page cache, both well under a millisecond for files of the target sizes.
So the mutating call that triggers a flush can still seal, fold, place and write the pages itself, and hand only the `fsync` and the header write to a worker; the worker touches the file and nothing else.
The shared state is then one flag, "a commit is in flight", with a condition variable: the next flush waits for it before taking any page, since the pages the previous commit freed become reusable only through its `fsync`, and the page pool learns of the commit when the worker reports it.
Appending to journal `E + 1` while the worker syncs needs nothing at all: `pwrite` to other pages of the same file is safe beside an `fsync`.

*How intrusive.*
One mutex, one condition variable, a worker thread, and a commit step that runs when the worker reports, all inside the store; the typed layer does not change.
It is tested by ThreadSanitizer, and by the in-memory storage simulating a crash at every point of the interleaving, which the store's crash tests do already for one thread.
Moving the fold and the writing to the worker as well would be more intrusive — the span table, the rope and the page table would then be the worker's during a flush, and journal appends would share the page pool with it — and is not needed while the `fsync` dominates.

**Decision:** implement the flush in the idle loop first. Make the change that flushes in a separate thread (and its change to the specification of the journal) in separate commits to kladde-docs, kladde-rs, and kladde-cpp. The spec should make it optional to actually use separate threads. Only the journal seal should be normative.

### Cuts that outlive their transaction

**Proposed: a removed value may be kept as a cut until the session ends, in a fourth chain that the header names, so that inserting it again, in any later transaction, is a paste rather than a second encoding.**
[Values and their positions](../impl/values.md#moving-a-value-between-containers) weighed this and deferred it "until a need is proven".
A document editor with an undo system is that need: Inkscape keeps a deleted node alive for its undo log and inserts the same node again when the deletion is undone, by which time the transaction that removed it is long over.
Without it, undoing the deletion of a large group writes all of the group's bytes again, into the journal and then into data pages.

It costs what the deferral listed: records that name a chain, the fourth chain in the header, and a rule for cuts nobody pastes — a splice in the cut chain when the in-memory value is destroyed, and the whole chain cleared by a session's first flush, since no value of an earlier session can name a cut.
Whether it pays depends on how large the values are that Inkscape inserts again, which the demo can count before anything changes.

@Claude: help me think about the design of this in a bit more detail before I commit to a decison.
My main objection is that I want a deletion of many items from a document or drawing to actually show up as a reduction in file size within a reasonably short time frame because I think that's what users expect. Therefore, keeping the whole undo buffer on file seems dangerous.
- Could we bound the capacity of this "side chain" by evicting old cuts?
  Could such a bound be implemented on the application layer (if we maybe give it some way to query encoding length of objects) or would it have to live in the store layer?
- Alternatively: instead of putting cuts on a durable side chain, could we turn them into some kind of "weak" pointers that stay alive as long as the corresponding spans still happen to be accessible somehow?
  For example, we could guarantee that cuts that haven't been pasted anywhere stay alive at least until the next flush.
  Flushes never happen within a transaction, so cut and paste within a transaction remains to always be possible, but across transactions they might require re-encoding.
  When pasting a cut, we would have to check somehow if the cut is still alive (a first implementation could make a conservative check and consider cuts to be still alive if they're from the current epoch, but a future implementation could also allow older cuts if their spans happen to still be alive).
  If a cut is still alive, it can be pasted cheaply; if not, it gets re-encoded.
  It's probably a bit more complicated than this since the transaction in which the `paste` record sits could trigger a flush before it gets written to a journal page, which might then invalidate the cut, and whether or not the `paste`'s transaction triggers a flush can't be known for sure at the time the `paste` record is issued.

**Claude:** Your second idea is the better design, and it settles the first: a cut should be a *weak* promise — a paste is cheap while the cut's bytes are still at hand, and falls back to encoding the value otherwise — and it should live as long as the journal it was made in, rather than as long as its transaction.
Nothing goes to file that would not be there anyway, so deleted content becomes garbage at the next flush, as it does today, and the file shrinks as consolidation reclaims it.
The proposal above, a durable side chain, would not meet your requirement and should be replaced by this; I have left its text as it is until you decide.

*Cuts that live as long as their journal.*
Three rules of the journal change, and nothing else in the format:

- **A `Paste` may name any cut that an earlier record of the same journal made and that is not pasted yet**, counted by distance among the journal's cuts, 0 for the latest; today it may name only cuts of its own transaction.
- **The fold keeps every cut until it ends**, rather than freeing a transaction's cuts when the next transaction starts, and replay at open does the same; both already hold every byte a paste can name, since it lies in the committed sequence or in the journal being folded.
- **A reader keeps the length of every cut of the journal**, not only of the open transaction, which the journal's page budget bounds.

*The flush you worried about.*
A flush that lands between the cut and the paste does end the cut, and the way out is to decide the paste's form when its transaction is appended rather than when the application calls `paste`.
Until then the buffer holds the operation as "paste cut `c`, or else encode this value", and the value is still the cut's own, since a cut gives no mutable access to it.
When the transaction closes, [the flush triggers](../impl/transactions-and-batches.md#flush-triggers) run with the paste's size; if the transaction fits, it is appended with the `Paste`, into the journal that made the cut; if a flush must come first, the cut's journal is folded, so the paste is encoded as a `Splice` of the value's bytes instead, the transaction's size is computed again, and the rules continue from there, an overlong transaction included.
Two cases need no fallback at all: a cut and its paste in one transaction, which no flush can separate, as today; and the ready run of a batch, which [is appended before](../impl/transactions-and-batches.md#flush-triggers) the flush that cuts the batch.
With the [seal](#a-flush-off-the-callers-thread), the rule is the same: a paste is a `Paste` only if it is appended to the journal that made its cut.

*What the application sees.*
Nothing but cheaper inserts: Inkscape's removed node remembers its cut, and inserting it again asks the store whether the cut is still pasteable, which is one comparison of epochs and one bit.
The application never needs an encoded length; the fallback encodes from the in-memory value, which Inkscape keeps anyway for its undo log.
How long a cut lives is then how long a journal lives, so [idle flushes](#flushing-on-an-interactive-thread) shorten it; an undo right after a deletion is cheap, an undo after the next flush encodes again.

*Cuts that outlive a flush, if measurements ask for them.*
A cut can survive a flush only if its bytes stay reachable from the header, which is the side chain again; so the side chain is not an alternative to weak cuts but their extension.
Bounding it would then be the store's job, since only the store knows which bytes each cut holds and only a store record can drop them: it would evict the oldest cuts at a flush once the chain exceeds a byte budget, and an evicted cut would fall back to encoding like any other dead one.
The application would need no lengths for this either, only the budget as an option.
Whether it is worth a fourth chain depends on how often Inkscape inserts a node again after a flush, which the demo can count once weak cuts exist.

**Decision:** go with your formulation of "Cuts that live as long as their journal".

### Smaller points

- **The rationale for SHA-256 says that it is "present in every mainstream language's standard library"**, which is not true of C or C++.
  That is no reason to change the hash — SHA-256 is a few hundred lines, and in C++ it can run in a `constexpr` function, so that a type's fingerprint is a compile-time constant and the [fast path](../spec/schema/fingerprints.md#as-a-fast-path) compares two constants — but the sentence should say so.
- **No change is needed for class hierarchies.**
  Flattening a class and its bases into one struct, bases first, is a convention of kladde-cpp, and a descriptor for "a struct that extends another" would describe the C++ source rather than the bytes, against [the guiding principle](../spec/schema/index.md#the-guiding-principle).
- **No change is needed for C++ strings either**, although `std::string` holds bytes of any kind and the specification requires UTF-8: a handle validates the bytes it inserts, which costs a pass over what it writes anyway.
  @Claude: we could relax the rules about string encoding a bit if this helps. The one rule we need is that kladde uses `0xFF` as the string terminator, so a C++ implementation has to deny strings that contain `0xFF`. How realistic is it for `0xFF` to appear in a string in an app that uses either a different string encoding or that claims to use utf8 but doesn't enforce it as strictly as rust?

  **Claude:** In text that really is UTF-8, `0xFF` never appears, and in text that is not, it appears often enough that a relaxed rule would not make such strings safe to store — so the rule is best kept as it is.

  - **Other encodings**: in Latin-1 and Windows-1252, `0xFF` is "ÿ", rare but real (in French place names, say); in UTF-16 and UTF-32 it is in the byte-order mark `FF FE` and in every code unit of "ÿ" or of the fullwidth forms `U+FF01…`; an application keeping such text in a `std::string` meets it routinely.
  - **`std::string` as a byte buffer**, for file contents, serialized messages or binary identifiers, which is common C++: any byte occurs.
  - **"UTF-8, but not enforced"**: the bytes come from outside — a file declared UTF-8 that is really Windows-1252, a Linux file name, the clipboard of another program — and then `0xFF` appears exactly as often as the foreign encoding produces it.
  - **Inkscape**: its XML goes through libxml2, which transcodes documents to UTF-8 and rejects invalid sequences, and GLib's strings are UTF-8, so in practice never.

  Relaxing Text to "any bytes but `0xFF`" would save C++ little, since validating the bytes a handle inserts runs at several gigabytes a second (the simdutf library, or a plain loop for short edits), and it would cost every other reader: Rust's `String`, Python's `str` and a tool's JSON export all need UTF-8, and would have to validate at open or fail there.
  Applications with genuinely binary or foreign-encoded strings are better served by a byte type that says so — a Sequence of `u8`, or a Bytes kind if editing them in place should cost one splice as text does.

**Decision:** I agree, `Text` should stay to require UTF-8. The API documentation of kladde-cpp should warn users that they must use `std::string` in kladde only for UTF-8 text, not as a byte buffer, and should point them to the appropriate type to use for byte buffers in kladde (presumably `std::vector<std::uint8_t>`).

## Building, packaging and testing

- **CMake and C++20**, no required dependencies: CRC-32C and SHA-256 are a few hundred lines in the tree, with the hardware CRC instruction where the platform has one.
  A target `kladde::kladde` for `find_package` and `FetchContent`; packages for vcpkg and Conan once the API settles.
- **One namespace, `kladde`, and headers under `<kladde/…>`**: `kladde/store.hpp` for the untyped layer, `kladde/kladde.hpp` for everything.
- **Tests**: the specification's [fingerprint vectors](../spec/schema/fingerprints.md#vectors) first; the store against a byte vector, with flushes, reopens and simulated power cuts; values against their canonical encoding after every operation; and the crash tests of [Conformance](../spec/conformance.md#the-suite).
  GoogleTest, which Inkscape uses already, and libFuzzer harnesses for every decoder, under AddressSanitizer and UndefinedBehaviorSanitizer.
- **Cross-implementation tests from the start**: kladde-cpp and kladde-rs each write a corpus that the other reads, compared through a textual dump of the schema and the sequence, the [canonical textual representation](../spec/tooling.md#a-canonical-textual-representation) in embryo.
  This is the first time the compatibility contract is tested by two implementations, and the most valuable thing kladde-cpp can do for the specification.

## Order of work

1. **The schema**: descriptors, canonical encoding and fingerprints, checked against the specification's vectors and kladde-rs — after deciding on the two proposed changes to descriptors, so that neither implementation writes this layer twice.
2. **The store**, without consolidation: storage, pages, the span table, the rope, the journal, the flush, opening; the differential oracle and the crash tests.
3. **Typed values with the macro**: primitives, standard types, `kladde::Vector`, `kladde::Map`, structs, `enum class`, `std::variant`, `File`, `Mut`, transactions and batches.
4. **Cross-implementation tests** against kladde-rs.
5. **`kladde::List` and `Ref`**, and the Inkscape `KladdeNode` with the XML schema: the demo.
6. **Consolidation**, the header as write buffer and the span policy, ported from the reference algorithms.
7. **Class hierarchies**, and a typed SVG schema.
8. **Reflection**, once a compiler that the target applications build with supports it.

Consolidation comes late on purpose: a store that never consolidates is [conforming](../spec/sequence.md#reclamation-is-not-part-of-this-specification), and only its files grow, which a demo can live with for a while.

## Open questions

- **The four [changes to the specification](#changes-to-the-specification)**: which to adopt now, which once the Inkscape demo has measured the need, and in what order relative to kladde-cpp's own work.
  Variants that hold one value and maps as a kind of their own are cheapest before kladde-cpp's schema layer is written, since both change descriptors and fingerprints.
- **Long-lived references in the reference algorithms.**
  [Values and their positions](../impl/values.md) describes handles that live as long as a guard; `kladde::List` adds a form that lives as long as its element.
  Is it a C++ matter, or does every language without a borrow checker — Python, Java — need the same, so that it belongs in Implementation?
  **Decision:** I think it belongs in Implementation.
  **Claude:** Agreed; it would go into [Values and their positions](../impl/values.md) as a second kind of handle beside the guard, once the design above has settled, with the Fenwick tree and the per-element index as its default.
- **The minimal splice of `set`** for text, and perhaps for any value whose encoding is long and changes little: worth stating in [the built-in containers](../impl/values.md#the-built-in-containers)?
- **Builds without exceptions.**
  Some applications and platforms disable them; a parallel API returning `std::expected` would double the surface, and aborting on error would serve no one well.
- **Debug checking of handles**: is a stack of live handles cheap enough to keep on in the test builds of applications, or only in kladde-cpp's own?
- **The name of the handle**: `Mut<T>` is short and says what it is for, but reads oddly to C++ programmers who have not met Rust; `Edit<T>` or `Handle<T>` are the alternatives.
  @Claude: the name `Guard` in kladde-rs is historic. The name `Mut` was considered as an alternative when guards were first introduced, but discarded. I don't remember anymore whether the arguments against `Mut` were based on parts of the kladde design that have since gone. Would it make sense to rename `Guard` to `Mut` in kladde-rs at this point? It might lead to some confusion given that I think guards hold a shared reference (i.e., *not* a `&mut`) to the backend or store via a `RefCell` (correct me if I'm wrong here). What are the arguments for and against renaming `Guard` to `Mut` in kladde-rs?

  **Claude:** Your picture of kladde-rs is right, and I would rename, but to per-type names ending in `Mut`, as Rust's standard library names such wrappers, rather than to one generic `Mut`.
  A guard holds `&'r mut T`, the value, and a backend that is `Copy` — a shared `&Store` — whose state sits in a `RefCell<Inner>`.

  - **For.**
    The standard library calls a wrapper that grants exclusive access to a value and does something on the side `RefMut` or `PeekMut`, and reserves `Guard` for RAII objects whose drop releases something, as `MutexGuard` unlocks.
    A kladde guard's drop does nothing, so `Guard` promises a release that never happens.
    What a guard is exclusive about is the value, which it does hold by `&mut`; the shared reference to the store is invisible in its API, and a `RefMut` holds a shared reference to its `RefCell` in just the same way, so it should not confuse anyone.
    And kladde already names its accessors `entries_mut()`, which would then return a `DurableVecMut`; `Kladde::guard()` would become `Kladde::get_mut()`, beside `get()`; and kladde-rs and kladde-cpp would share one word.
  - **Against.**
    The rename touches everything that names guards: the trait, every derived `…Guard` type, the tutorial, and [Values and their positions](../impl/values.md), which uses "guard" as the language-neutral word and would need another one, "handle" being the obvious choice.
    And a trait called `Mut` alone would say little, which is why the per-type names matter.

  Nothing I can find in the documents explains why `Mut` was discarded before, so I cannot tell whether the reason still holds.

**Decision:** rename `TGuard` to `TMut` (and associated types `Guard` to `Mut`, etc.) in kladde-rs and in kladde-docs. Use the name `Mut` in kladde-cpp too, even if it is foreign to C++. It's short, pretty self-explanatory, and avoids a needless deviation between the rust and C++ API.