---
title: Variable-size values
---

**Status: a proposal, not adopted.**
It would give every type a packed encoding beside its fixed one, describe containers structurally, with pointers that say what they point to, add a kind of value that has only a packed encoding, and add a choice of encoding wherever a value is laid out.
Nothing in the file layers below the schema would change, and nothing in memory either.

**Separate two questions that a value's slot answers together today: how many bytes the value's encoding takes, and how much room the place that holds it reserves.**
A type's encoding is *fixed-size* or *variable-size*; a place is *slotted*, holding a type's fixed encoding, the same size for every value, or *packed*, holding its packed encoding, as many bytes as the value needs.
Most types have both encodings: in the packed one, an enum takes only its current variant, an integer wider than a byte and a pointer are LEB128 varints, and a `char` is UTF-8.
A new kind of descriptor, `Small(C, T)`, has only the packed one: a size tag, then a short `C` inline, or the tag 255 and a `T` — for kladde-types' `SmallPersistableString`, the string's text inline, or a pointer to it.
For pointers to pack, the schema must see them, so the containers that hold them stop being opaque and become structural: a `Pointer(T)` to an allocation that holds a `T`.
A slotted value is slotted throughout, up to its pointers; a packed value packs everything inline in it, unless a place inside it declares itself slotted.
The choice changes only the file: in memory, a `u16` stays a `u16`, and a `SmallPersistableString` holds its text however it is laid out.
The slot stays the default, and files that use no packing keep every byte they have, though types that hold containers get new fingerprints, since the containers' descriptors become structural.

## The problem

**Every type has one fixed inline size, and an enum's is its discriminant plus its largest variant, so every value of an enum pays for the largest variant.**
That is [what makes offsets static](../spec/schema/type-descriptors.md#inline-size), and with them field access, element access by index, and the reading of a writer's layout under [evolution](../spec/schema/evolution.md).
Rust pays the same padding in memory, so the in-memory representation loses nothing it would not lose anyway.
The file and the journal do lose: a store writes the whole slot, padding included, and the padding then occupies data pages like any other bytes.

Take SVG path data, a sequence of segments of ten kinds, as a typical case:

```rust
enum PathSegment {
    MoveTo { abs: bool, x: f32, y: f32 },
    LineTo { abs: bool, x: f32, y: f32 },
    HorizontalLineTo { abs: bool, x: f32 },
    VerticalLineTo { abs: bool, y: f32 },
    CurveTo { abs: bool, x1: f32, y1: f32, x2: f32, y2: f32, x: f32, y: f32 },
    SmoothCurveTo { abs: bool, x2: f32, y2: f32, x: f32, y: f32 },
    Quadratic { abs: bool, x1: f32, y1: f32, x: f32, y: f32 },
    SmoothQuadratic { abs: bool, x: f32, y: f32 },
    EllipticalArc { abs: bool, rx: f32, ry: f32, x_axis_rotation: f32,
                    large_arc: bool, sweep: bool, x: f32, y: f32 },
    ClosePath { abs: bool },
}
```

With a one-byte discriminant, its slot is 26 bytes, the size of `CurveTo`.
Its variants need 2 bytes (`ClosePath`), 6 (`HorizontalLineTo`, `VerticalLineTo`), 10 (`MoveTo`, `LineTo`, `SmoothQuadratic`), 18 (`SmoothCurveTo`, `Quadratic`), 24 (`EllipticalArc`) and 26 (`CurveTo`).
A path that is mostly curves wastes little, but the outline of an icon, `M L L L Z`, takes 130 bytes in slots where its content is 42.
An element tree has the same shape one level up: a node that is either an element or a short run of text pays for the element in both cases.

Integers are the same problem in a different form.
A count, a length, or an index is stored at the width its largest possible value needs, 4 or 8 bytes, while nearly every value written fits in one.
A pointer is one too: it takes four bytes, while the ids of a file's allocations, which kladde [recycles lowest first](../impl/id-recycling.md), mostly fit in two.

**Short content is the problem in a third form: every `PersistableString` and every `PersistableVec` owns an allocation, however little it holds.**
In kladde-svg's model of an SVG drawing, strings are the most common source of allocations, nearly all of them `id`s, followed by the attribute lists of elements and by path data:

| drawing | allocations | attribute lists, median size | path data, median size | `id`s, median length |
| --- | --- | --- | --- | --- |
| tiger | 1,453 | 482, 34 bytes | 240, 130 bytes | 482, 4 bytes |
| coat of arms | 2,007 | 609, 34 bytes | 494, 182 bytes | 609, 8 bytes |
| world map | 8,542 | 2,529, 17 bytes | 2,213, 78 bytes | 2,528, 8 bytes |

Strings, attribute lists and path data together are 83 to 93 % of the allocations, counted from the model with a throwaway program, with each allocation's size in today's fixed encodings.
All but two of the 4,390 strings are shorter than 128 bytes, the attribute lists hold one or two attributes, and much of the path data is short as well.
On file, such an allocation is cheap already, since content below the [`Inline` threshold](../impl/flush.md#the-inline-threshold) lives in its statement, inside pages a flush writes anyway; what remains is the pointer in the parent and the statement's framing.
In memory, it costs an entry in the allocation map, a statement in the slab, and a fragment in the fragment map, as [in-memory state](../impl/in-memory-state.md) describes them, and every change to it is journaled against an allocation of its own.

**Two existing mechanisms reach part of this, and neither reaches the padding between elements.**

- A [`Zero` statement](../spec/address-table.md#statement-types) describes a range of zeros without data bytes, and the address table already names "data types with unbalanced variant payloads" as a use for it.
  But a statement costs on the order of ten bytes of address table, more than the padding between two elements of a vector, so stating padding as `Zero` would grow the file rather than shrink it.
- A narrower discriminant, which the specification allows (1, 2, 4 or 8 bytes) and kladde-rs picks by the largest discriminant value, saves on the tag and nothing on the padding.

## The proposal

### Encodings: fixed and packed

**Every type has a packed encoding, which a reader who knows the type's descriptor decodes front to back, learning where the value ends as it goes, and most types also have a fixed encoding, which takes the same number of bytes for every value.**
They differ by kind:

| kind | fixed encoding | packed encoding |
| --- | --- | --- |
| `u8`, `i8`, `bool` | one byte | the same byte |
| `u16`, `u32`, `u64` | fixed width, little-endian | unsigned LEB128: 1–3, 1–5 and 1–10 bytes |
| `i16`, `i32`, `i64` | fixed width, two's complement | zigzag, then LEB128: 1–3, 1–5 and 1–10 bytes |
| `char` | the scalar value in 4 bytes, little-endian | UTF-8: 1–4 bytes |
| `f32`, `f64` | IEEE-754, little-endian | the same bytes |
| `Pointer(T)` (reserved today, defined below) | the allocation's id in 4 bytes, little-endian; 0 for null | unsigned LEB128 of the id, 1–5 bytes; 0 for null |
| Struct | its fields' fixed encodings, concatenated | its fields' encodings in their places, concatenated |
| Enum | the discriminant at its width, the variant's fields, and zeros up to the largest variant | the discriminant as unsigned LEB128, then the variant's fields |
| Opaque | its inline bytes, as its library defines them | the same bytes |
| `Small(C, T)` (new, below) | none | a size tag, then a `C` in 0 to 254 bytes, or the tag 255 and a `T` |
| Array (reserved) | its elements' fixed encodings, concatenated | its elements' encodings, concatenated |
| `Sequence(T)` (new, below; never inline) | its elements' fixed encodings, back to back | its elements' packed encodings, back to back |

**A type is *fixed-size* if its packed encoding takes the same number of bytes for every value, and then its two encodings are one; it is *variable-size* otherwise.**
Floats, bytes, `bool`s, opaque types, and structs of them are fixed-size.

**A type is *slottable* if it has a fixed encoding, and *packed-only* if it has none.**
Every primitive is slottable, every integer and `char` included, and so is every pointer and every opaque type, while every `Small(C, T)` is packed-only.
A struct or an enum is slottable exactly when the types of all its fields are, so a type with a packed-only field inline is packed-only as well.
The two distinctions are independent but for one direction: a fixed-size type is always slottable, since its one encoding is fixed, while a variable-size type may be either — a `u16` is slottable, a `SmallPersistableString` packed-only.
A `Sequence` is no inline type at all: it only ever fills an allocation or a small value's content, and its elements are slotted or packed as that content is.
The fixed encoding of a type is today's layout of it, so a type that existing files contain encodes there as it always has.

The packed encodings of the structural kinds obey three rules of canonical form, so that a value has one encoding and a reader can refuse anything else.
A varint must be minimal — no trailing `0x80` groups — and decode to a value within its type's range, a pointer's within 32 bits.
`u8` and `i8` keep their byte, since no varint is shorter than one byte and LEB128 would spend two on half their values.
A `char` is UTF-8 rather than a varint of its scalar value, so that a packed sequence of `char`s is byte for byte UTF-8 text, which is how a string [describes itself](#pointers-and-what-they-point-to); LEB128 would be a byte shorter for U+0800 to U+3FFF and past U+FFFF, kana and emoji among them, which is too little to give that up, and UTF-8's own rules make it canonical: only the shortest form, and no surrogates.
A `Small(C, T)` is the exception: [hysteresis](#small-values) gives some of its values two encodings.

A packed encoding is not always the shorter one.
A `u16` of $2^{14}$ or more takes three bytes packed against two fixed, a `u32` of $2^{28}$ or more five against four, so packing integers pays only where their values are mostly small, which counts, lengths, indices and allocation ids usually are.
And it is not the fixed encoding with its zeros dropped: converting a value from one encoding to the other means encoding it again, from the value.
Nothing relies on the conversion being cheaper than that.
A reader always knows from the schema which encoding a place holds, and kladde-rs stores a value by encoding it from memory anyway, while the allocations a value owns, which move between containers without copying, are laid out by those containers and not by the value.

A decimal encoding of floats, which would shrink them too, is [left for later](#left-for-later-floats-as-decimals).

### Pointers, and what they point to

**A pointer becomes a structural kind, `Pointer(T)`: an owning pointer to an allocation whose content is a `T`.**
In a packed place, it is the unsigned LEB128 varint of its allocation's id, as a packed `u32` would be, with 0 for null, an absent allocation; in a slotted place, it keeps its four bytes.
Allocation ids are [32 bits](../spec/address-table.md#bounds), so a packed pointer takes one to five bytes: one below 128, two below 16,384.
Since kladde [recycles the lowest free id first](../impl/id-recycling.md), a file's ids stay about as dense as its allocations are many, and each of the drawings above has fewer than 16,384, so every one of their pointers would take at most two bytes.
A pointer changes size only when it changes at all: when a container first gets an allocation, gives it up, or has its whole value replaced by one with an allocation of its own.

**With it, the containers of kladde-types need no opaque descriptors, and are described by what they are:**

- a `PersistableString` is a `Pointer(Packed(Sequence(char)))`;
- a `PersistableVec<U>` is a `Pointer(Sequence(U))`;
- a `PackedPersistableVec<U>` is a `Pointer(Packed(Sequence(U)))`;
- a `PersistableHashMap<K, V>` is a `Pointer(Sequence(Slot))`, where `Slot` is a struct of a liveness flag, a `K` and a `V`.

`Sequence(T)` is its elements back to back, as many as fill the range it is given, which only a pointer's allocation or a small value's content gives it.
What an allocation holds starts slotted, as a vector's elements are laid out today, and `Packed(T)`, a wrapper that may stand only as what a pointer points to, starts it packed.

**A string describes itself as the text it holds, a packed sequence of `char`s, rather than as bytes.**
A packed `char` is UTF-8, so the descriptor fits a string's content byte for byte, and says more about it than `Sequence(u8)` would.
A tool that knows nothing of kladde-types sees text, and can show it.
A reader refuses a string that is not valid UTF-8, by the packed `char`'s canonical rules, as it refuses an overlong varint; kladde-rs has to check that anyway before it hands out a `&str`.
And a string no longer shares its descriptor with a vector of bytes, so a reader that expects text fails closed on bytes, and the other way round.

The description has three consequences to keep in mind.
It counts in `char`s, while the string's length is kept in bytes: the allocation's size gives the bytes, and a tool that wants the characters decodes them, while kladde-rs keeps a `String` and edits byte ranges at `char` boundaries, with no offset index per `char`.
It holds a string to UTF-8: any interface that writes bytes into a string must keep its content valid, or leave a file that readers refuse, and text in another encoding needs a type of its own, described as `Sequence(u8)`.
And it needs its `Packed` wrapper, without which a `Sequence(char)` behind a pointer would be slotted, four bytes to a `char`.

**A pointer must say what it points to, or the containers' descriptors would lose their elements' types.**
A `PersistableVec<u32>` and a `PersistableVec<u64>` differ only behind their pointers; a descriptor that showed the pointer alone would give them one fingerprint, and a reader would open one as the other.
Describing what an allocation holds is option 1 of [the container problem](../spec/tooling.md#the-container-problem), which the specification leaves open; this proposal takes it, at the cost the specification names there: the containers' layouts become part of the format, so changing one changes descriptors that files contain.

**`PersistableBlob<T>` stays opaque.**
Its content is a payload that serde serialized, which no descriptor describes, and its opaque descriptor's parameter keeps `T` in its fingerprint.
Its pointer stays hidden inside it, and so keeps its four bytes in a packed place too: the price of being opaque.

### Which library a type belongs to, later

**A structural descriptor says how a type's bytes are laid out, not which library defines what they mean, and a later revision should let any descriptor say that too.**
A `PersistableString` and a `PackedPersistableVec<char>` get one descriptor and one fingerprint, as the schema's [guiding principle](../spec/schema/index.md#the-guiding-principle) intends for byte-identical representations, although one keeps text in memory and the other a vector of four-byte `char`s.
And two libraries could lay out text alike and mean different things by it, one keeping it in a Unicode normalization form and the other not, or one comparing it without regard to case; the hash map's liveness flag, likewise, means what its library says it means.
Today, only an opaque descriptor carries the name and version of the library that defines its type, so the containers lose theirs by becoming structural.
A generic way to annotate any descriptor with the name and version of a foreign library that defines it — fingerprinted by the version's compatibility component, as an opaque descriptor's is — would give structural types that identity back.
It is left for a later revision, and nothing in this proposal depends on it.

### Small values

**`Small(C, T)` describes a value that keeps its content inline while it is short, and as a `T` once it grows: a one-byte size tag, then a `C` in that many bytes if the tag is 0 to 254, or a `T` if the tag is 255.**
`C` says what the inline form holds, and fills the tag's number of bytes exactly, as a `Sequence` does.
`T` says what replaces it, and may be any type: a pointer, a struct that keeps metadata beside its pointer, or one that spreads its content over several allocations.
The inline content takes the small value's place, which is packed, since a small value can stand only in packed places.
Both forms are structural, so a reader that knows neither type's library still decodes either form, and at the least finds where the value ends — the tag's number of bytes after a tag below 255, or a `T` after the tag 255.
That is what puts `Small` in the schema rather than in a library: a value around a small value, a packed element holding a short string, stays readable for a tool that knows nothing of strings.

**kladde-types would offer `SmallPersistableString`, a `Small(Sequence(char), Pointer(Packed(Sequence(char))))`, and `SmallPersistableVec<U>`, a `Small(Sequence(U), Pointer(Packed(Sequence(U))))`, for any `U`, packed-only ones included.**
Spilled, each is the container its name says: a string, or a [packed vector](#containers), whose pointer, after the tag 255, makes two to six bytes, and two or three in the drawings above.
Inline, each holds exactly what its allocation would hold — a string's text in UTF-8, or a vector's elements in their packed encodings, back to back — so spilling and folding back move bytes without changing them.
The inline `Sequence(char)` needs no `Packed` wrapper, since it inherits the small value's packed place.
A small vector of attributes whose `id`s are small strings, the attribute lists of the table above, is then a `SmallPersistableVec<Attr>`, and holds its first attributes' `id`s inline in its own inline content.

**The small vector spills into a packed vector rather than a slotted one, since only the packed vector holds every `U`, and its elements' bytes are the same inline and spilled.**
Three ways of building it were weighed:

- **Spilling into a `PersistableVec<U>`** would lay out the spilled elements slotted, so `U` would have to be slottable: an attribute list whose attributes hold small strings could not be small itself.
  And its inline elements, packed like everything in a small value, would differ from its allocation's, so every spill would encode every element again.
- **Two small vectors, one for each kind of vector**, would add a type for no capability the packed one lacks: where `U` is fixed-size, its packed elements are byte for byte its slotted ones, and where `U` is variable-size but slottable, a field that changes size often can be declared slotted inside `U`.
- **Spilling into a `PackedPersistableVec<U>`**, the choice, takes any `U`, keeps the inline content identical to the spilled allocation's, and lets the small vector reuse the packed vector's offset index, inline as well as spilled.
  What it costs is what packing always costs: a spilled vector whose elements change size splices its tail, where a slotted one would write in place.

**When content spills and when it comes back is the policy of the library that defines the small type, with hysteresis between the two.**
kladde-types would move content to an allocation once it takes more than an upper threshold of bytes, 128 by default, and back inline only once it takes fewer than a lower threshold, 64 by default; in between, a value keeps the form it has, and a new value is inline if its content takes at most the upper threshold.
Without the gap, a value edited around a single threshold would allocate and free an allocation at every edit that crosses it; with it, the edits between the two crossings span 64 bytes of content.
Content of 255 bytes or more is never inline, so an upper threshold can be at most 254.
The thresholds are not part of the encoding: the tag says which form a value takes, so a reader decodes either form, whatever thresholds wrote it, and the schema records neither.
What hysteresis gives up is a single encoding per value: content of 64 to 128 bytes may be inline or not, depending on its history, as the order of a hash map's slots already depends on the order of its insertions and deletions.

With the default thresholds, a small value takes 1 to 129 bytes inline.
In memory, a `SmallPersistableString` is text, and a `SmallPersistableVec` a packed vector with its offset index, plus the handle of their allocation while they have one; *small* describes the file, not the memory.
The guard of an element of an inline small vector is anchored in the allocation of the value around the vector, after its tag, and that of a spilled one in the vector's own allocation.
A spilled value owns its allocations, through its `T`, and frees them when it is replaced or deleted; an inline one owns nothing.

**A small value has no fixed encoding, since one would reserve its whole inline capacity.**
A fixed `SmallPersistableString` would take 129 bytes in every slot, while the `id`s above have a median of 4 to 8; a struct that held one inline in a slotted place would reserve that room in every value, which is the footgun small values must not set.
So `Small(C, T)` is packed-only, and the rules below keep it, and every type that holds one inline, out of slotted places altogether.

### Who chooses, and how the choice nests

**The encoding is chosen where a value is laid out, and the places inside a value inherit it, so packing a value packs everything inline in it, unless a place inside declares itself slotted.**
Every place a value is laid out in — a struct's field, a variant's field, an array's or a sequence's element, a small value's content — is slotted or packed.
A place takes the choice of the value it sits in, unless its declaration fixes one.
The root value is slotted if its type has a fixed encoding, as every root is today, and packed otherwise; its type's descriptors show which.

**Inheritance stops at pointers and at opaque types, the two boundaries the schema can see.**
What a pointer points to starts afresh: slotted, unless the pointer holds a `Packed(T)`, so a `PersistableVec` lays out its elements slotted and a `PackedPersistableVec` packed, wherever the vector itself sits.
The pointer itself follows its place: a packed struct holding a `PersistableVec` packs the vector's pointer into a varint, and a slotted struct holding a packed vector keeps its pointer at four bytes while its elements are packed.
An opaque type says nothing of what it holds, so the rules stop at it too, and its inline bytes are the same in either place.

**A slotted value is slotted throughout, up to its pointers and opaque types: no place inside it may be packed, and no packed-only type may stand in it.**
A packed place inside a slotted value would never save a byte.
The slot reserves the largest encoding its content can take, and a packed encoding's largest is never smaller than the fixed one — three bytes for a `u16` against two, five for a pointer against four — so the slot could only grow, and a change of size inside it would rewrite the slot's tail where a fixed field is written in place.
The same rule is what keeps small values out of slots: a packed-only type can stand only where every value around it, up to the nearest pointer, is packed.

**Declarations work in one direction only: a place inside a packed value may be declared slotted, against what it would inherit, and a place inside a slotted value may not be declared packed.**
A slotted place inside a packed value holds a fixed encoding, so its value never changes size, which suits a field that changes often: a counter, which as a varint would change size at every power of 128, or an enum whose variant switches often.
The one other declaration, `Packed(T)` as what a pointer points to, does not override anything: it chooses for a place that inherits nothing.

So when a packed vector holds `struct T { x: MyEnum }`, the enum `x` is packed: the struct is laid out packed, and `x` inherits that.
Had `T` declared `x` slotted, it would stay slotted inside the packed `T`, occupying its slot whichever variant it holds.
And `struct T { id: SmallPersistableString }` is packed-only: a packed vector can hold it, and today's vector cannot, which kladde-rs can [check at compile time](#kladde-rs).

**Inheritance is chosen over the alternative, in which a choice would apply only to the value it is declared for, and its fields would keep the choices their own declarations give them.**
Under that rule, packing a struct would change none of its bytes, since a struct has neither padding nor integers of its own, and packing a composite value would need every type inside it to declare its fields packed in advance.
Inheritance lets a container pack values whose types know nothing of it, and leaves to the types only the exceptions.

### In the schema

**The schema gains five kinds: `Pointer(T)`, `Sequence(T)`, the wrappers `Packed(T)` and `Slotted(T)`, and `Small(C, T)`.**

- `Pointer(T)` takes the descriptor of what its allocation holds, and defines the reserved [`Pointer` kind](../spec/schema/type-descriptors.md#pointer-reserved).
- `Sequence(T)` takes its element's descriptor, and may stand only as what a pointer points to, inside a `Packed` wrapper there, or as a small value's content.
- `Packed(T)` may stand only as what a pointer points to, and lays that out packed.
- `Slotted(T)` declares a place slotted against what it would inherit; `T` must be slottable.
- `Small(C, T)` takes the descriptors of its two forms.

Each takes a kind tag of its own in the [canonical encoding](../spec/schema/canonical-encoding.md#descriptor-encoding), so that every existing kind keeps its encoding.
The containers' descriptors do change, since they stop being opaque, and with them the [fingerprint](../spec/schema/fingerprints.md) of every type that holds a container.
Recursive containers need nothing new: a tree whose children are a `PersistableVec` of trees refers to itself through a `Pointer(Sequence(...))`, a [cycle](../spec/schema/fingerprints.md#cycles) the fingerprint already handles.

A descriptor table must keep the rules of nesting: a reference to a packed-only type may stand only where no value around it, up to the nearest pointer, is slotted; a `Sequence` and a `Packed` wrapper only where they may; and a reader refuses a table that breaks any of them.

A file whose types hold no `Packed` wrapper and no small value has no packed place, so every plain reference in it resolves to slotted, as it always has.
A bit on every reference would carry the same information as the wrappers, but would change the encoding, and so the fingerprint, of every type that has a field.
An older reader could not decode a descriptor table that uses the new kinds, which would raise the minimum reader version once the format promises compatibility; until kladde-rs is declared production ready, it promises none, and the version stays `0`, as the [header](../spec/file-format.md#header-pages) says.

## What a mutation costs

The cost depends on whether the mutation changes the size of the value it touches.

### The size stays the same

**A mutation that keeps a value's size is a `Write` in place, exactly as today.**
That covers every mutation inside a slotted value, a change of an enum's variant included, which rewrites its slot.
In a packed value, it covers every write to a float, a byte, a `bool`, a slotted place, or a field of the current variant that keeps its size: moving a point, recoloring a shape without changing how its paint is expressed, or editing a short string without changing its length.
A field sits at its value's start plus the sizes of the fields before it, and those are static up to the first variable-size one; a field inside a packed enum adds the discriminant's encoded size, which only a change of variant can change.

An integer in a packed place keeps its size until its value crosses a power of 128: setting a `u32` from 100 to 200 makes it two bytes, and the mutation becomes a size change.
That is the price of varints, and the reason to declare a field that is mutated often slotted.

### The size changes

**A mutation that changes a value's size is one `Splice` on the allocation that contains the value: the old encoding out, the new one in, and whatever follows shifted.**
Only a value in a packed place can change size, and every value around a packed place is packed as well, up to the pointer whose allocation it lies in, so the change reaches that allocation, where nothing would absorb it.
No pointer has to be rewritten, because a pointer names an [id](../spec/allocations.md), not a position.
The `Splice` moves no data bytes on file, but the address table restates every statement of the allocation behind the splice point, `O(fragments behind it)` [in table bytes](../spec/address-table.md#design-directions).
On an allocation that the current journal created, it [costs nothing](../impl/flush.md#what-falls-out-unasked).

**An inline small value whose content changes size rewrites its tag along with the splice, in one transaction.**
Typing into an inline string, inserting an attribute into an inline attribute list, or an attribute's own small string growing inside it all change the content's length, which the tag states.

**A small value whose content crosses a threshold moves the content between its parent and its `T`, in one transaction.**
For both small types of kladde-types, spilling writes the content to a new allocation, then splices the tag and the pointer in where the content was; folding back splices the content in where the pointer was, then frees the allocation: prepare, publish, clean up, as the [ordering discipline](../spec/journal.md#ordering) demands.
Since the content's bytes are the same in both places, a [`Move`](move-op.md) could hand them over without writing them again, and a small vector keeps its offset index, which counts from the content's start in either place.
A `T` with several allocations does the same for each, in the order its library sets.

A value that owns allocations is replaced as today: the new value's allocations are stored before the record that publishes them, and the old ones are freed after it.
A container that moves elements with `Copy` or `Move` records can do so only between places with the same choice; between a packed place and a slotted one, it encodes them again.

### In memory

**Offsets that are compile-time constants today become runtime sums wherever a variable-size value precedes them.**
A field after a variable-size field sits at an offset that depends on that field's current value, which the in-memory value supplies, since every encoded size is computable from a value and its place without touching the file.
A packed vector needs the offset of every element to hand out a guard for one, so it keeps an index of element offsets alongside its elements.
As an array of prefix sums, a size change or an insertion updates the entries behind it in `O(n)`, as the memmove of an insertion into `Vec` already costs.
A Fenwick tree would make a size change `O(log n)`; that is for containers such as a packed vector to choose, while a derived struct keeps no index at all and adds up its fields' sizes when it hands out a field's guard.
Reads do not change at all: they go to the native in-memory values, which have no offsets and no varints.

**Slotted values keep every optimization they have today, as long as the choice of encoding is part of a guard's type rather than a value it carries.**
Today, a field's offset is a sum of constant inline sizes, which the compiler folds, and a guard holds only the value, the backend and the location, never a size.
If a guard for a slotted value and a guard for a packed one are different types, generated together by the derive macro, the slotted one compiles to exactly today's code.
The packed one has constant offsets too, up to its first variable-size field, and sums sizes it computes from the in-memory value after that, still storing none.
The possibility of packing then costs nothing at run time, only the code for whichever of the two guards a program uses, compiled once per type.
A guard that carried the choice as a flag would instead branch on it at every field access, whether the program ever packs or not.

## Why it fits kladde

**Kladde [reads in bulk and searches nothing on disk](../impl/index.md#the-shape-of-an-implementation), and that is what makes packed layouts cheap here.**
A format that looked values up on disk would need offsets on disk — an offset table per packed vector, or a stride — to find element `i` without decoding the elements before it.
Kladde needs offsets only in memory, where they are built during the load that decodes everything anyway.
That load is already [sequential](../rust/persistable-and-guards.md), one field after another, so decoding a packed value front to back costs it nothing, and neither does decoding a varint into the integer the application declared, or reading a small string's content from beside its tag rather than from an allocation.
A packed vector's length needs no field either, just as a slotted vector's does not: the load decodes elements until the allocation ends.

## What else it touches

### Containers

**Today's vector and hash map keep their contents slotted, so their elements must be slottable, and the packed vector and small values hold the rest.**
A packed vector, `PackedPersistableVec<U>`, holds its elements back to back in their packed encodings, with the offset index above; a push is one `Write` past the end, as for a slotted vector, and an insertion, removal, or size change in the middle is one `Splice`.
It is also what a small vector spills into, so its content is laid out exactly as a small vector's inline content is.
A `PersistableVec` of a packed-only type does not compile, nor does a `PersistableHashMap` with `SmallPersistableString` keys or values.
The hash map's slots have to stay slotted for as long as an insertion reuses a tombstoned slot, since a new entry must fit any old entry's slot; a map that stopped reusing slots could pack its entries as a packed vector does.
Every container but the blob is described structurally, as above, so that its pointer packs where it sits in a packed place.
In kladde-svg's model, the attribute lists of the table above would become `SmallPersistableVec<Attr>`, inline in their elements while they hold a few attributes, with their `id`s as `SmallPersistableString`s inline in them; short path data could become `SmallPersistableVec<PathSegment>`, its segments packed.

### Evolution

**Packed layouts keep the writer's layout readable, but turn the per-type read plan into a per-type parse.**
Under [evolution](../spec/schema/evolution.md#what-is-settled), a reader reconciles the writer's types with its own once, and then reads every value at offsets the writer's descriptors determine.
For slotted layouts that stays as it is.
For packed ones, the writer's offsets depend on each value, but the writer's descriptors, and the choices they record, still determine them, so the plan becomes a small program that decodes the writer's encoding, run during the sequential load.
A changed choice is a changed layout like any other: a reader whose type declares a field slotted that the writer packed reads the writer's varint and stores a fixed-width integer on the next write.
Mutating a value in place at the writer's layout, the *retain* mode, gets harder, since its offsets would have to be computed from the writer's descriptors at every mutation; the *upgrade* mode, which rewrites a value in the reader's layout, would be the natural default for packed values.

### Tooling

**A tool can walk a file from its descriptors alone, through packed values, small values and containers, and stops only at opaque types.**
Every packed encoding marks its own end, so a level-3 [tool](../spec/tooling.md) decodes it without knowing the application, once it has resolved each place's choice from the wrappers and from inheritance.
It follows a pointer into its allocation and reads the content the pointer's descriptor gives — a vector's elements, a string's text, a hash map's slots — and decodes a small value in either form.
That answers [the container problem](../spec/tooling.md#the-container-problem) by its option 1 for every container of kladde-types but the blob, which stays opaque and is skipped by its inline size, as today.
And since every owning pointer outside an opaque type is visible, the leak checker that [conformance](../spec/conformance.md#the-suite) describes no longer needs the application's types.
What a tool still cannot tell is what a library makes of a structure — that a string is kept normalized, or that a hash map's slot whose flag is clear holds no entry — which waits for [library annotations](#which-library-a-type-belongs-to-later).

### kladde-rs

The Rust side would need:

- a choice of encoding passed to `store`, `load`, and a new `encoded_size`, as a type parameter like the guards' below;
- a trait for slottable types, `Slottable`, with the size of their fixed encoding as an associated constant in place of today's `INLINE_SIZE`, required by every slotted container and by every field declared slotted, so that a packed-only type there fails to compile, at `cargo check`, with a trait error;
- a derive macro that implements `Slottable` for a struct or an enum by requiring it of every field, and an attribute, `#[kladde(packed_only)]`, for a type that holds a packed-only field;
- both encodings for every scalar and for pointers, so that an application keeps declaring `u16` or `i64` and the place decides only the bytes on file, and structural descriptors for the built-in containers, built from `Pointer`, `Sequence` and `Packed`;
- a `#[kladde(slotted)]` attribute on fields, the only declaration of a place a derived type needs, since a field can be packed only by inheriting it, and derived offsets that are computed at runtime after the first variable-size field;
- guards that take the choice of encoding as a type parameter, as [In memory](#in-memory) argues, and report a change in their value's size to the parent that keeps the offset index, which the parent's borrow of the child makes possible;
- `PackedPersistableVec`, and on it `SmallPersistableVec`, with `SmallPersistableString` beside them in kladde-types, and a packed root for a packed-only root type.

The attribute is needed because a derive macro sees a field's type only as written, and cannot tell whether it implements `Slottable`.
It could not simply require the trait conditionally either: Rust rejects an `impl` whose `where` clause names a concrete type that lacks the trait, such as `SmallPersistableString: Slottable`, even where nothing uses the `impl`.
So without the attribute, a struct with a `SmallPersistableString` field fails to compile at the derive, with a message that the trait's `#[diagnostic::on_unimplemented]` can word to name the attribute; with it, the derive leaves `Slottable` out, and the struct fails only where something tries to slot it.
A generic type needs no attribute: `impl<T: Slottable> Slottable for Labelled<T>` holds for exactly the `T` that have a fixed encoding.
`Kladde<T>` cannot ask whether `T` implements a trait without specialization, so the root's choice comes from an associated constant on `Persistable`, the fixed size or none, which the derive sets along with the trait.
An associated constant checked by a `const` assertion in each slotted container would need no attribute at all, but would report a misuse inside the container's code, where it is instantiated, rather than at the type that is misused.

## Stages

**The proposal can land in two stages, each worth having on its own.**

1. **Packed vector elements.**
   A new container lays out its elements packed throughout — enums without padding, integers as varints, `char`s as UTF-8 — and keeps their offsets in memory.
   The format does not change while the container is described as `Opaque`, since how an opaque container lays out its content is its own business, and no element type has to change, since inheritance packs them whole; pointers keep their four bytes, since the opaque containers that hold them cannot say where they are.
   This stage covers the largest cases of padding, sequences of heterogeneous values such as path data, attribute lists and child nodes.
2. **The schema's part**: the `Pointer`, `Sequence`, `Packed`, `Slotted` and `Small` kinds, the containers described structurally, and with them slotted places inside packed values, small values, packed pointers, and packed roots.
   These belong together because each changes the bytes of types that describe themselves structurally, or adds a kind of descriptor that existing ones cannot express.
   It is the stage that removes the allocations of short strings and of short attribute lists, the bulk of the table above; its small vector is the first stage's packed vector, kept inline while it is short.

## Left for later: floats as decimals

**Not part of this proposal, and recorded for a later round: packing a float as the shortest decimal that reads back as it — its significant digits and a power of ten — would shrink the numbers of SVG drawings from 4 bytes to between 1.5 and 2.8, where binary schemes that encode one float at a time come out larger than a plain `f32`.**
Under the proposal as it stands, `f32` and `f64` keep their IEEE bytes in both encodings.
In memory they would stay what the application declares in any case; only the bytes on file would change, as they do for integers.

### What it would save

**On kladde-svg's corpus, the shortest decimal takes 1.54 to 2.78 bytes per number, and the two binary schemes 2.64 to 4.89, more than four bytes on every drawing.**
The numbers are every `f32` that kladde-svg's model stores for the drawings: coordinates, lengths, transforms, opacities and view boxes.
They were measured with a throwaway program, not kept, on three drawings and on resvg's test suite, whose files are small and whose numbers are mostly integers.

| bytes per number | tiger | coat of arms | world map | resvg's tests |
| --- | --- | --- | --- | --- |
| numbers | 11,001 | 22,422 | 168,554 | 34,072, in 1,800 files |
| share that are integers | 19 % | 9 % | 2 % | 90 % |
| fixed `f32` | 4.00 | 4.00 | 4.00 | 4.00 |
| byte-reversed bits, then a varint | 4.31 | 4.57 | 4.83 | 2.64 |
| a tag, then `f16` where exact, else `f32` | 4.39 | 4.64 | 4.89 | 2.84 |
| shortest decimal | 2.12 | 2.78 | 2.38 | 1.54 |

The decimal encoding measured is one LEB128 varint holding `zigzag(digits) × 16 + (exponent + 8)` for a power of ten from $10^{-8}$ to $10^{7}$, and otherwise an escape followed by the four IEEE bytes, five bytes in all.
On the world map, 4.5 % of the numbers take one byte, 55 % two, 38 % three, and 2 % four or five.
The byte-reversed variant is what Go's `encoding/gob` does: reversing the bytes puts a float's trailing zero mantissa bits first, where a varint drops them.
The tagged variant follows CBOR's preferred serialization, with zero in the tag alone.

**The binary schemes lose because SVG's numbers are short decimals, whose binary mantissas are full.**
A float's trailing mantissa bits are zero only for integers and fractions with a power of two below them, such as halves and quarters; `12.35` or `0.1` fill all 23 bits.
On resvg's tests, nine tenths of whose numbers are integers, the binary schemes do save, but less than the decimal one does.
The decimal encoding keeps what the source wrote, and that is short whenever the source's text was.

### What others do

**Encodings of one float at a time, the kind a packed place needs, are rare, and the decimal ones among them come from columnar databases.**

- *Go's gob* reverses a float's bytes before writing it as an integer; 17.0 takes three bytes.
- *CBOR's preferred serialization* ([RFC 8949](https://www.rfc-editor.org/rfc/rfc8949)) writes a float at the shortest of half, single and double precision that holds it exactly; *Amazon Ion* writes zero in no bytes, and other floats in four or eight.
- *SQLite* writes a `REAL` without a fractional part as an integer of one to eight bytes, which is the decimal encoding restricted to a power of ten of one.
- *Ion's decimal type* stores a coefficient and an exponent, each of variable length.
- *BtrBlocks*' pseudodecimal encoding (SIGMOD 2023) splits a double into its significant digits and a power of ten, with exceptions for those that do not split, and *ALP* (SIGMOD 2024, used in DuckDB) finds the doubles that are short decimals by scaling them to integers.
  Both compress columns rather than single values, but the split itself would work one value at a time.

**Compressors of float sequences reach further, and would not fit, since each value's encoding depends on the values before it.**
*Gorilla* (VLDB 2015) writes each value as its XOR with the one before; *Chimp* (VLDB 2022) and *Elf* (VLDB 2023) refine it, Elf by erasing the mantissa bits beyond a value's decimal precision first.
*FPC* predicts each value from earlier ones, *zfp* and *fpzip* compress arrays of scientific data, and Parquet's byte-stream split regroups a column's bytes for a general-purpose compressor.
Under any of them, changing one number would change the encoding of the numbers after it, so a mutation that kladde makes as one `Write` would rewrite a run of values.
They belong with [compressing data pages](#compressing-data-pages), which could use them, rather than with packing.

### What adopting it would take

- **A canonical rule.**
  The specification would have to fix which digits a float is written with: the shortest decimal that reads back as the float, the closest such if there are several, and how a tie between two equally close ones breaks, as the Ryū algorithm and its ports do in most languages.
- **Escapes** for what has no short decimal: infinities, NaNs with their payloads, negative zero, and exponents outside the encoding's range.
  The prototype above wrote negative zero as zero, which a real encoding must not.
- **Time.**
  A store would compute a float's shortest decimal, and a load would parse one back with correct rounding; for the world map's 169,000 numbers, that should be milliseconds against the 78 ms its open takes now.
- **No gain for computed numbers.**
  A coordinate that a rotation or a scaling produced has up to nine significant digits and takes the escape, five bytes, one more than an `f32`.
- **More size changes.**
  A float would become variable-size, so moving a point, an in-place `Write` under the proposal as it stands, would change the point's size whenever its digits change in number.
  A field that is moved often could be declared slotted, as a frequently changed integer can.
- **Tuning.**
  The varint's layout above is a first guess; giving integers a one-byte form, or fitting the exponent's range to where SVG's numbers lie, would likely save more.

**It might also settle whether an application should declare `f32` or `f64`.**
The shortest decimal of the `f64` that `12.35` parses to is `12.35`, as it is for the `f32`, so for numbers that came from decimal text, an `f64` should take about as many bytes on file as an `f32`, and its extra precision would cost space only where it is used.
That has not been measured.

## Alternatives

**Four alternatives save part of the same space with less change, and the proposal is preferred because none of them removes the padding while values stay typed and walkable.**
Compressing pages is orthogonal and could be combined with it.

### A durable `Box` for large variants

A library type that owns an allocation would let a rare, large variant hold a pointer instead of its payload, shrinking the slot of every value to the size of the next largest variant.
It would need no change to the format, since it would be a container like any other.
But every boxed value would be an allocation of its own, with its statements in the address table and its entry in the allocation map, which would cost more than the padding it saves unless the variant were both large and rare.
It would suit an element kind with a dozen attributes that occurs once per document; it would be wrong for path segments.

### Struct of arrays, in the application

A path could be two vectors, one byte per segment kind and four bytes per coordinate, with no padding at all.
It would need no change to kladde, and nothing would be more compact.
But the values would no longer be typed in the schema, so a tool would see two arrays of numbers; every edit would span two vectors and so need a transaction to stay consistent; and every application would hand-write what the type system could derive.

### Compressing data pages

Padding is zeros, and zeros compress to almost nothing, so compressed data pages would make padding nearly free on disk while types and offsets stay exactly as they are.
But it would change the page framing, make consolidation account for pages whose compressed size differs from their content, and add compression to every flush.
The journal would keep its padding unless its transactions were compressed too, and so would every byte written while the journal is folded.
It is orthogonal to packing and would combine with it.

### `Zero` statements for padding

The address table can already state a range as zeros without writing them, but at on the order of ten bytes per statement it would cost more than the padding between two vector elements.
It pays only for long runs of zeros, which padding between small elements is not.

## Open questions

- **Should `Slotted(T)` be written where `T` is fixed-size?**
  It changes no byte, only the fingerprint, so the derive could leave it out, or the fingerprint could ignore it.
- **Small values with a fixed size.**
  Whether kladde-types should also offer types that keep a few bytes inline with a fixed size, such as a string of at most 15 bytes in a 16-byte slot, which a slotted struct could hold, is open.
- **The hash map's layout.**
  Described structurally, the hash map shows its tombstoned slots to a tool as entries whose flag is clear; whether it should keep that layout, or change it when its tombstones are [reconsidered](#containers), is open, and a change would now change its descriptor.
- **The rope and `Move`.**
  A [rope](../rust/containers.md) would make insertions into a long packed vector cheap, and [`Move`](move-op.md) would let a rope's nodes shift packed ranges between them without copying, since all of a rope's nodes would share one choice; how the two fit together is not worked out.
- **What it saves.**
  The proposal's numbers are arithmetic and counts, not measurements of savings; only the floats' [left for later](#left-for-later-floats-as-decimals) are measured.
  A realistic workload should compare slotted with packed layouts, and small values with strings and vectors that own allocations, in file size, journal bytes, bytes written per byte the application writes, memory, and flush time, before anything here is adopted.
