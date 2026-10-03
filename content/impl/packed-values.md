---
title: Packed values
---

What an implementation keeps in memory, and does on a mutation, so that values in [packed places](../spec/schema/type-descriptors.md#places) can change size: offsets that exist only in memory, values that find themselves by asking their parents, changes of size that travel up to the nearest allocation, and small values that move their content between two forms.

## Why packing is cheap here

**Kladde [reads in bulk and searches nothing on disk](index.md#the-shape-of-an-implementation), and that is what makes packed layouts cheap.**
A format that looked values up on disk would need offsets on disk — an offset table per packed sequence, or a stride — to find element `i` without decoding the elements before it.
Kladde needs offsets only in memory, where they are built during the load that decodes everything anyway.
That load is sequential, one field after another, so decoding a packed value front to back costs it nothing, and neither does decoding a varint into the integer the application declared, or reading a small string's content from beside its tag rather than from an allocation.
A packed sequence's length needs no field, just as a slotted one's does not: the load decodes elements until the allocation ends.

Reads do not change at all: they go to the native in-memory values, which have no offsets and no varints.

## Storing a value: prepare, then encode

**A value is stored in two steps: create every allocation it owns but lacks, then encode it into one buffer and write that buffer with one record.**
A pointer's packed encoding depends on the id its allocation gets, so a value that owns content it has no allocation for yet — a vector built in memory and never stored — has no settled encoding until that allocation exists.
The first step, *prepare*, creates and fills those allocations, recursively; the second, *encode*, writes the value's bytes in the encoding its place holds; and the store writes them as one `Write`, or, where the value replaces one of another size, one `Splice`.

One record per value is also the cheapest way to journal it: a slotted enum written field by field would take a record for its discriminant, one per field and one for its padding.

## Loading: decode front to back

A load reads each allocation's bytes once and decodes its values from them front to back, through a cursor that checks every bound, following pointers into the allocations they name.
Every packed encoding marks its own end, and the decoder refuses one that is not [canonical](../spec/schema/type-descriptors.md#canonical-form): an overlong varint, an integer out of its type's range, UTF-8 that is not well-formed, an unknown discriminant.
The offsets of a packed sequence's elements fall out of the decode, as the cursor's position before each element.

## Offsets in memory

**Offsets that are static in slotted values are sums of encoded sizes in packed ones, which an implementation keeps where it needs them.**

- **A packed sequence** needs the offset of every element to find one, so it keeps an index of offsets beside its elements: an array of prefix sums, with one more entry than there are elements, the last being the content's size.
  A size change or an insertion updates the entries behind it in `O(n)`, as the memmove of an insertion into an in-memory vector already costs; a Fenwick tree would make a size change `O(log n)`, at the price of `O(log n)` lookups.
- **A composite value** — a struct, a variant, a tuple — keeps no index while nobody mutates it.
  The handle through which an application mutates it, a *guard* in the reference implementation, computes its fields' offsets from the in-memory value when it is made, and keeps them while it lives.
- **A slotted value at a fixed location** needs nothing: its fields' offsets are the static sums of their slot sizes.

## Finding a value: ask the parent

**A value in a packed place finds its location by asking its parent every time it writes, rather than holding a location.**
A held location would go stale while the value's handle lives, in two ways:

- the handles of all of a value's fields can be alive at once, so a field can grow while the handle of a field behind it is in use;
- an ancestor small value can move its content to an allocation of its own while a handle inside it is in use.

So a handle holds a **link** instead: either a fixed location with nobody to tell — the start of an allocation, or a slotted value in a slotted context — or its parent's **node** and its own index among the parent's children.
The node works out the child's location from its owner's location and the offsets it keeps, asking its own parent in turn:

```rust
fn location(link) -> Location {
    match link {
        At(location) => location,
        In { node, owner, index } => node.location_of(owner, index),
    }
}

// a composite value's node
fn location_of(&self, owner, index) -> Location {
    location(owner) + self.offsets[index]
}

// a packed sequence's node
fn location_of(&self, _owner, index) -> Location {
    Location(self.allocation, self.offsets[index])
}

// a small value's node
fn location_of(&self, owner, index) -> Location {
    let content = match self.spilled {
        Some(allocation) => Location(allocation, 0),
        None => location(owner) + 1,             // past the tag
    };
    content + self.offsets[index]
}
```

The walk costs a few additions per level per write, which is nothing next to recording the write.
A slotted value inside a packed one, behind a `Slotted` wrapper, is found the same way, since its siblings can grow, although it never changes size itself.

## What a mutation costs

### The size stays the same

**A mutation that keeps a value's size is a `Write` in place.**
That covers every mutation inside a slotted value, a change of an enum's variant included, which rewrites its slot.
In a packed value, it covers every write to a float, a byte, a `bool`, a slotted place, or a field of the current variant that keeps its size: moving a point, recoloring a shape without changing how its paint is expressed, or editing a short string without changing its length.

An integer in a packed place keeps its size until its value crosses a power of 128: setting a `u32` from 100 to 200 makes it two bytes, and the mutation becomes a size change.
That is the price of varints, and the reason to declare a field that is mutated often slotted.

### The size changes

**A mutation that changes a value's size is one `Splice` on the allocation that contains the value: the old encoding out, the new one in, and whatever follows shifted.**
Only a value in a packed place can change size, and every value around a packed place is packed as well, up to the pointer whose allocation it lies in, so the change reaches that allocation, where nothing would absorb it.
No pointer has to be rewritten, because a pointer names an [id](../spec/allocations.md), not a position.

The value then **reports** the change through its link, and each node up the chain does what the change requires of it, inside the same transaction:

- a **composite value** shifts the offsets of the fields behind the child, and reports its own change of size, which is the child's, to its parent;
- a **packed sequence** shifts the offsets of the elements behind the child and stops: its encoding is a pointer, which stays as it is, and the store tracks its allocation's size;
- an **inline small value** rewrites its tag to state the content's new length, and reports its own change to its parent — or, if the content crosses a threshold, moves it, as [below](#small-values);
- a **spilled small value** stops, as a packed sequence does, unless the content shrinks below its threshold and moves back inline.

```rust
fn resized(&self, owner, index, old, new) -> Result<()> {
    let before = self.offsets.end();
    report(owner, before, before + new - old)?;   // records first ...
    self.offsets.shift(index + 1, new - old);       // ... memory after
    Ok(())
}
```

**Each node records what the change requires before it updates what it keeps in memory**, and reports upwards before that too, so that a failure anywhere leaves every node matching the unchanged value, as a failed mutation leaves the value itself.

On file, the `Splice` moves no data bytes, but the address table restates every statement of the allocation behind the splice point, `O(fragments behind it)` [in table bytes](../spec/address-table.md#design-directions); on an allocation that the current journal created, it [costs nothing](flush.md#what-falls-out-unasked).

A value that owns allocations is replaced as any value is: the new value's allocations are prepared before the record that publishes them, and the old ones are freed after it.
A container that moves elements with `Copy` or `Move` records can do so only between places with the same choice of encoding; between a packed place and a slotted one, it encodes them again.

## Small values

**When a [small value](../spec/schema/type-descriptors.md#small) moves its content is the policy of the library that defines it, with hysteresis between the two thresholds.**
The reference implementation moves content to an allocation once it takes more than 128 bytes, and back inline only once it takes fewer than 64; in between, a value keeps the form it has, and a new value is inline if its content takes at most 128 bytes.
Without the gap, a value edited around a single threshold would allocate and free an allocation at every edit that crosses it; with it, the edits between the two crossings span 64 bytes of content.
Content of 255 bytes or more is never inline, since the tag cannot state it, so an upper threshold is at most 254.

**Moving content follows the [ordering discipline](../spec/journal.md#ordering): prepare, publish, clean up, in one transaction.**
Spilling writes the content to a new allocation, then splices the tag 255 and the pointer in where the content was; folding back splices the content in where the pointer was, then frees the allocation.
A small vector keeps its offsets, which count from the content's start in either form.

**The content is written in one of two ways, depending on who crosses the threshold.**
When the small value's own handle does — a push, an insertion, a removal, an edit of a small string — it encodes the content from memory.
When an element inside a small vector does, by growing or shrinking through its own handle, that element is borrowed and cannot be encoded again, so the vector moves the bytes already on file: spilling copies the content into the new allocation with a `Copy` record before splicing the tag and the pointer in, and folding splices in a tag and room for the content, copies the content over, and frees the allocation.
Either way, the handles inside keep working, since they ask their parents where they are; and since the content's bytes are the same in both forms, a [`Move`](../drafts/move-op.md) could hand them over without writing them again.

## What was considered and declined

**Three alternatives save part of the same space with less change, and packed places are preferred because none of them removes the padding while values stay typed and walkable.**

**A durable box for large variants**, a library type that owns an allocation and lets a rare, large variant hold a pointer instead of its payload.
It needs no format change, but every boxed value is an allocation of its own, with its statements in the address table and its entry in the allocation map, which costs more than the padding it saves unless the variant is both large and rare.

**Struct of arrays, in the application**: a path as one vector of segment kinds and one of coordinates.
Nothing is more compact, but the values are no longer typed in the schema, every edit spans two vectors and so needs a transaction, and every application hand-writes what the type system could derive.

**Compressing data pages.**
Padding is zeros, and zeros compress to almost nothing, so compressed pages would make padding nearly free on disk while types and offsets stay as they are.
But it changes the page framing, makes consolidation account for pages whose compressed size differs from their content, and adds compression to every flush; the journal keeps its padding unless its transactions are compressed too.
It is orthogonal to packing and would combine with it.

## Open questions

**The rope and `Move`.**
A rope would make insertions into a long packed sequence cheap, and [`Move`](../drafts/move-op.md) would let a rope's nodes shift packed ranges between them without copying, since all of a rope's nodes would share one choice of encoding; how the two fit together is not worked out.
