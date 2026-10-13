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

**Both deserve first-class support**, since they serve different code rather than different tastes.
New code with a handful of small alternatives is better off with `std::variant`, which keeps values inline and visits them exhaustively; code that already has a hierarchy — Inkscape's `SPObject` and its dozens of subclasses — needs the hierarchy, and could not reasonably be rewritten into variants.
The hierarchy is also the only form in which shared fields are written without dispatch, which a document model with many kinds of element and many common attributes uses constantly.

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
A `kladde::Ref<T>` to an element is then valid until that element is removed, as an iterator of `std::list` is, and computes its start when it writes by climbing from the element to the root: at each level, the element's offset among its siblings, from an order-statistics tree over the siblings' sizes in which each element can find its own rank, plus the offset of the list in its parent.
That is `O(depth · log m)` per write, the cost of [guards made together](../impl/values.md#guards-made-together) at every level, and a change of size updates the same path back up.
It costs a parent link and a node in the siblings' tree per element, 24 to 40 bytes, which a document tree whose elements each hold a name, attributes and children affords.

So `kladde::Vector` and `kladde::List` differ the way `std::vector` and `std::list` do — compact, or stable — and an application chooses per container.

## Errors, transactions and exception safety

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
- **A generous budget**, so that a gesture fits in the journal however long it lasts.

A flush on a background thread is a different matter: mutations appended while it runs would belong to the next journal, and an application crash before the flush's header is written would lose them although their calls had returned.
That needs a change to the journal, which [is proposed below](#a-flush-off-the-callers-thread), and then removes the stall altogether.

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
No value's encoding changes, and the [fixed form](../spec/schema/type-descriptors.md#encodings) of an Enum is the payload's fixed form instead of its fields', which is the same bytes.
kladde-rs's derive describes a struct variant as a Struct and a one-field tuple variant by its field's type.
Files of today stop opening, which a pre-stable format [allows](../spec/file-format.md#header-pages).

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

### Cuts that outlive their transaction

**Proposed: a removed value may be kept as a cut until the session ends, in a fourth chain that the header names, so that inserting it again, in any later transaction, is a paste rather than a second encoding.**
[Values and their positions](../impl/values.md#moving-a-value-between-containers) weighed this and deferred it "until a need is proven".
A document editor with an undo system is that need: Inkscape keeps a deleted node alive for its undo log and inserts the same node again when the deletion is undone, by which time the transaction that removed it is long over.
Without it, undoing the deletion of a large group writes all of the group's bytes again, into the journal and then into data pages.

It costs what the deferral listed: records that name a chain, the fourth chain in the header, and a rule for cuts nobody pastes — a splice in the cut chain when the in-memory value is destroyed, and the whole chain cleared by a session's first flush, since no value of an earlier session can name a cut.
Whether it pays depends on how large the values are that Inkscape inserts again, which the demo can count before anything changes.

### Smaller points

- **The rationale for SHA-256 says that it is "present in every mainstream language's standard library"**, which is not true of C or C++.
  That is no reason to change the hash — SHA-256 is a few hundred lines, and in C++ it can run in a `constexpr` function, so that a type's fingerprint is a compile-time constant and the [fast path](../spec/schema/fingerprints.md#as-a-fast-path) compares two constants — but the sentence should say so.
- **No change is needed for class hierarchies.**
  Flattening a class and its bases into one struct, bases first, is a convention of kladde-cpp, and a descriptor for "a struct that extends another" would describe the C++ source rather than the bytes, against [the guiding principle](../spec/schema/index.md#the-guiding-principle).
- **No change is needed for C++ strings either**, although `std::string` holds bytes of any kind and the specification requires UTF-8: a handle validates the bytes it inserts, which costs a pass over what it writes anyway.

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
- **The minimal splice of `set`** for text, and perhaps for any value whose encoding is long and changes little: worth stating in [the built-in containers](../impl/values.md#the-built-in-containers)?
- **Builds without exceptions.**
  Some applications and platforms disable them; a parallel API returning `std::expected` would double the surface, and aborting on error would serve no one well.
- **Debug checking of handles**: is a stack of live handles cheap enough to keep on in the test builds of applications, or only in kladde-cpp's own?
- **The name of the handle**: `Mut<T>` is short and says what it is for, but reads oddly to C++ programmers who have not met Rust; `Edit<T>` or `Handle<T>` are the alternatives.
