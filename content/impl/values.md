---
title: Values and their positions
---

How the values an application holds find their bytes in the sequence: regions, what each container keeps to find its children, what that costs per mutation, handles that live long, the built-in containers, and moving a value from one container to another.

**No value stores where it lies, and neither does the store: the store knows bytes, each container knows where its children start relative to itself, and the chain of handles from the root composes the two for as long as it lives.**
A *handle* is what a value is mutated through, `Mut` in kladde-rs and kladde-cpp; a binding in another language may intercept mutation differently, but it needs the same positions, and the algorithms below are the same.

## A value is a region

**A data type sees only its own region of the sequence: where its value starts, resolved when its handle is made, and how long it is.**
It writes and splices at offsets relative to that start, checked against its length, and it neither knows where the region lies nor can reach anything outside it.
A region offers:

- `write(at, bytes)`, which overwrites bytes in place;
- `splice(at, removed, bytes)`, which replaces bytes and shifts what follows; it records a `Write` wherever `removed` equals the number of bytes inserted, so a whole-value `set` calls it whether or not the size changes;
- `splice_value(at, removed, value)`, which encodes the value straight into the record, asking it for its encoded length once, since the record states the length before the bytes, and returns that length to its caller;
- `move_bytes(from, len, to)`, which reorders without writing;
- `paste(at, removed, cut)`, which inserts a cut ([below](#moving-a-value-between-containers));
- `child(at, len, index)`, which makes the region of one of the value's children, together with a note that it is child number `index`, so that when the child changes size its region tells the value's index of its children.

`write` and `splice` return the cut of what they remove.
Every offset is relative to the region's start, and every method refuses one outside it.

**The obligations of a data type are four**: its fixed size and encoded length match what its encoder writes; its encoder and decoder are inverses; every mutation leaves its region holding the value's encoding when its transaction ends; and its descriptor matches the bytes.
The third is checkable, which an ordering discipline over allocations was only by replay: a store in a test can keep the sequence in memory and compare a region with the value's encoding after every method.

A push onto a vector of variable-size elements, for instance, splices the element in at the end and rewrites the count, in one transaction, and only then changes memory:

```rust
fn push(&mut self, value: T) -> Result<(), Error> {
    let n = self.vec.items.len();
    let len = self.region.transaction(|r| {
        let len = r.splice_value(r.len(), 0, &value)?;
        r.splice(0, varint_len(n), &varint(n + 1))?;
        Ok(len)
    })?;
    self.vec.sizes.push(len);     // appends to the Fenwick tree, O(log m); nothing for fixed-size `T`
    self.vec.items.push(value);
    Ok(())
}
```

Recording first and changing memory after means that a failed append leaves the value as it was.
In a language whose allocations can fail without ending the process, such as C++, a mutation also does everything in memory that can fail before it records — reserving capacity, constructing the new element — so that what follows the record cannot fail.

## Positions belong to containers

**A container hands each child a subregion and keeps, in memory, where each child starts relative to itself**: by multiplication for fixed-size children, by an index otherwise.
A child that changes size reports the change to its container, which updates its index and reports its own change to its container in turn, up to the root.

- **A struct keeps no index at all.**
  Its fields up to its first variable-size field start at constants; after that, each starts at the sum of the encoded sizes of the fields before it, a number of additions fixed by the struct's type.
- **A container of fixed-size elements keeps no index either**: element `i` starts at `i` times the element's size, after the count.
- **A container of variable-size elements keeps an index**, [chosen below](#what-a-container-keeps-to-find-its-children).
- **Every container caches its own encoded size**, so that adding up sizes never recurses.

A [fixed-size type](../spec/schema/type-descriptors.md#fixed-size-types) buys three things, all in memory: offsets inside it that the compiler can fold, containers of it without an index, and writes that never change its size, so nothing above it has to hear about them.
The choice is the application author's, per field and per element type: varints and unpadded enums save bytes on file, and `Fixed` saves memory and time in containers that hold many values, and keeps a field that changes often, such as a counter, from changing size whenever it crosses a power of 128.

*Why not a global index in the store.*
One index of anchors, one for every value whose position anyone may ask for, would cost a node of an order-statistics tree per value, where a container's index costs four bytes per variable-size element and nothing per fixed-size one, and it would teach the store about values, which nothing else requires it to know.

## What a container keeps to find its children

**A container of variable-size elements has five ways to keep where they start, and memory decides between them.**

| index | finding an element | a size change | an insertion | memory per element |
| --- | --- | --- | --- | --- |
| an array of starts | `O(1)` | `O(m)` | `O(m)` | 4 bytes |
| a Fenwick tree of sizes | `O(log m)` | `O(log m)` | `O(m)` | 4 bytes |
| a counted B-tree | `O(log m)` | `O(log m)` | `O(log m)` | about 8 bytes |
| a tree weighted by the elements' sizes | `O(1 + log(W / w))`, amortized | the same | the same | about 16 bytes |
| a start every 64 elements | `O(64)` | `O(m / 64)` | `O(m)` | 1/16 byte |

`m` is the number of elements, `W` the container's encoded size and `w` the element's, and memory assumes sizes recorded in 32-bit values; a container that would outgrow them fails cleanly.

**A Fenwick tree is the default**: a size change of an element, typing into a string inside it, say, is the frequent case and costs `O(m)` in an array of starts, while an insertion costs `O(m)` in the array that holds the elements anyway.
The runtimes here are for lookups that happen a handful of times per mutation, when the mutation happens and not during a flush, so they matter less than memory: kladde is meant for interactive applications whose mutations follow user actions, and a Fenwick tree's lookup or update over a million elements is about twenty steps, well under a microsecond.
For bulk operations, such as pasting large content from the clipboard, an application should build the value outside any handle and bring it in whole, which encodes it into one buffer and splices it in.

**A Fenwick tree over blocks of 32 elements** would keep one sum per block, an eighth of a byte per element, and find where element `i` starts from the tree's sum up to `i`'s block plus the encoded sizes of the up to 31 elements before it in the block, which it asks of the elements themselves; for small elements such as varints that scan costs about what the tree's own steps cost.
It is the better choice for vectors of small variable-size elements, where a full tree would double the memory.

Open: whether a Fenwick tree is the right default, what an application with millions of variable-size elements should use, and whether any data model nests deeply enough for [size-weighted indexes](#indexes-weighted-by-size) to pay.

## What a handle costs

**Making a handle costs one lookup in its parent's index, however deep its value lies, because the parent's handle already holds where the parent starts.**
Every handle holds the start of its value, which it needs anyway, since journal records name positions in the sequence.
An application reaches a value one level at a time — `drawing.get_mut().shapes_mut().get_mut(7)?` — and each step adds its child's start relative to the parent to the parent's start:

- a field before its struct's first variable-size field, or an element of a container of fixed-size elements, starts at a constant, or at `i` times the element's size;
- a field after it starts at the sum of the encoded sizes of the fields before it;
- an element of a container of variable-size elements starts where the container's index says.

The start a handle holds stays right for as long as the handle lives: the handle borrows its parent exclusively, so nothing before its value can change size meanwhile, and its value's own changes of size move only what lies behind it.
A language that cannot enforce the exclusive borrow makes it a rule, the kind iterator invalidation is: a handle is valid until something is written through a handle that is not its descendant; a debug build can check it with a stack of the live handles that a write pops down to the handle it went through.
Iterating needs no index at all: a handle that steps through a container's elements adds each element's size to the start of the one before.

**A change of size walks the same chain back up**, since it changes the encoded size of every value around the one that changed: each ancestor updates its cached size and its index's entry for the child the change came from, one update per level.

**So the depth is not a cost of the design, and the logarithm per level is a choice of index.**
The costs fall at three different times: making a handle costs a lookup at each level it descends, once; a write through it that keeps its value's size costs nothing more; and a write that changes its value's size costs an update at each level above the value.
The chain from the root is walked to make the handles anyway, so a constant per level adds nothing that grows with the file.

### Handles made together

**Handles made together — for every field of a struct or of an enum's current variant at once, or for two runs of a container's elements — ask for their start when they write instead of holding it**, since a sibling before a handle can change size while the handle lives.
Such a handle holds a link to its parent's index and its own number there, and computes its start whenever it writes, from the parent's start and the sizes the index keeps for the children before it.
The index keeps those sizes in cells that a sibling's report of a change of size can update while the other handles still hold shared references to it.
A write to an earlier part that changes its size, followed by a write to a later part, then works out by itself: the first write's report updates the parent's index, and the second write computes its start from the updated index.
Such a handle pays a lookup at every write rather than once; a count of changes kept in the index, compared with the count the handle saw last, would let it reuse its start until a sibling's size has changed.
A field whose predecessors are all fixed-size has a constant start even then, since no sibling's change of size can move it.

**A handle made on its own below a handle made together holds its start relative to that handle**, since that handle's own start can move while it lives.
Between the two, every handle borrows the one above it exclusively, so nothing between them can change size before it, and the relative start stays right; above them, a sibling's write can shift everything.
A write then computes its absolute start by climbing only the handles made together: at each, one lookup in the index of the parent it was made from, plus the relative starts of the handles made on their own in between, which are constants.
A write that changes its value's size updates every level up to the root, the handles in between included, so every level keeps its cached size and its index in cells.

The test for this: in a tree, take two sibling handles of the root together, descend two levels in each on their own, take two siblings of each of those together, descend two more levels in each of the four; then write in the four remote cousins, changing their sizes, and check that each writes at the right position after the others have changed size, that the indexes at every level match the in-memory values, and that the file then holds the value's encoding and reopens to it.

### Indexes weighted by size

**Indexes that weigh elements by their encoded size make the whole chain cost `O(depth + log n)`, as one tree over every value's position would, with no such tree.**
In a tree over a container's elements in which an element of `w` bytes, in a container of `W`, lies at depth `O(1 + log(W / w))` — a biased search tree (Bent, Sleator and Tarjan, "Biased Search Trees", SIAM Journal on Computing, 1985), or a splay tree, whose amortized cost of access is the same for any weights (Sleator and Tarjan, "Self-Adjusting Binary Search Trees", Journal of the ACM, 1985) — the lookups from the root, of `n` bytes, down to a value of `w` bytes cost `O(depth + log(n / w))` in all, since each level's `log(W / w)` cancels against the next level's.
Changes of size telescope the same way on the way back up.
That pays for deep nesting of large containers, a document tree whose nodes hold long lists of nodes, say; for shallow nesting a Fenwick tree per container is simpler, and the default.

## Handles that outlive edits elsewhere

**An application that keeps references into its document for as long as it likes needs handles that survive edits elsewhere, and a list whose elements know where they stand provides them.**
A document editor holds references to its nodes everywhere — in the selection, in tools, in dialogs, in the undo log — and writes through any of them at any time; a handle whose start goes stale at the next write elsewhere does not serve it.
A language with a borrow checker can forbid holding such references, at the price of an API that the application must be designed around; every other language — C++, Python, Java — needs these handles for applications that were not.

**A list keeps its elements at stable addresses, each element knows its parent and its own index among its siblings, and the list keeps a Fenwick tree of its elements' sizes.**
A reference to an element is then valid until the element is removed, and computes its start when it writes by climbing from the element to the root: at each level, the element's offset among its siblings, from the Fenwick tree at the element's index, plus the offset of the list in its parent.
That is `O(depth · log m)` per write, the cost of [handles made together](#handles-made-together) at every level, and a change of size updates the same path back up.

- **An insertion or a removal renumbers the elements behind it and rebuilds the Fenwick tree**, `O(m)`, which is what moving the elements' pointers in the list's array costs anyway; finding an element's index is a load.
- **An element costs a parent link, an index and a cell of the tree**, about 16 bytes, which a document tree whose elements each hold a name, attributes and children affords.
- **An order-statistics tree with parent links** would let an element find its index by climbing, and insert in `O(log m)`, at 24 to 40 bytes per element; it pays only for lists of hundreds of thousands of elements edited in the middle, and can be an option for them.

So a vector and a list differ the way an array and a linked list do in the host language — compact, or stable — and an application chooses per container.

## The built-in containers

Their encodings are the specification's [Sequence, Map and Text](../spec/schema/type-descriptors.md#encodings); what they keep in memory is theirs.

- **A vector**: the count, then the elements; in memory, the elements in an array, and for variable-size elements a Fenwick tree of their sizes.
  A push and an insertion are a splice and a count, in one transaction; a removal likewise; a reordering a `Move`, which writes no element's bytes; a size change of an element a report that updates the tree, `O(log m)`.
  A vector of fixed-size elements needs no tree, so where the host language's own vector is durable, it serves for them.
- **A list**: encoded as a vector is, with elements at stable addresses, [above](#handles-that-outlive-edits-elsewhere).
- **A string**: UTF-8, then `0xFF`; in memory, the host language's string, or a rope for long text, which then needs no container of its own, since the file already takes splices in long text at constant cost.
  An edit of text is one splice, since text has no count to rewrite.
  Setting a whole new text compares it with the old one and splices only the range between their common prefix and their common suffix, as [the specification guarantees](../spec/index.md#guarantees-an-implementation-must-provide).
- **A map**: the count, then the entries, each its key and its value; in memory, an *index map* — the entries in an array, in their order on file, and a hash table of their indices, so that each key is stored once — and for variable-size entries a Fenwick tree of their sizes.
  Removal swaps: the last entry moves into the removed one's place, by a `Move` that writes no bytes, the removed entry is spliced out, and the count goes down, so the file stays dense, with no tombstones, and the only index that changes in memory is the moved entry's.
  An insertion appends, and a lookup is a hash and an index.
- **An opaque payload**, such as a value serialized by a foreign library: its bytes, prefixed with their length.
  Setting a new payload splices the range between the common prefix and suffix of the old and the new bytes, and rewrites the length where it changes, in one transaction; where the changed range lies near the start, it writes the length and the range as one splice.

Removing a value is one splice, whatever it held: there is nothing to free, and nothing can leak.

## Moving a value between containers

**Moving a value within one container is one `Move` record and three to six span entries, whatever its size; between containers, cutting the value out and pasting it in costs the same, as long as both happen in one journal.**
Every value is its bytes, so restacking a group, reordering a list, or sorting it are `Move`s inside their container, and moving a subtree to another parent is the question.
Five answers were weighed, and cut and paste within a journal was adopted:

- **Encode it again**: remove the value, and insert its encoding where it goes.
  Simple, and the way to insert a value more than once; but the journal and the flush carry all of its bytes.
  It stays as the fallback wherever a cut has ended.
- **A move at a common ancestor**: a method on the container that holds both places.
  A binding that forbids holding two handles at once, as Rust's borrows do, would have the ancestor take both paths, which is awkward to type and to derive.
- **Cut and paste within a transaction**: the bytes a record removes stay available until the transaction ends.
  It serves a move within one operation, such as dropping a dragged shape into another group, but not an application that removes a value in one transaction and inserts it again in a later one, as undoing a deletion does.
- **Cut and paste across flushes, through a chain of cuts**: the bytes of removed values kept reachable from the header until they are pasted or dropped.
  Deleted content would then stay in the file until the application lets go of the values, where a user deleting it expects the file to shrink; bounding the chain would need the store to evict cuts, after which a paste must fall back to encoding anyway.
- **Cut and paste within a journal**, which is adopted: the bytes a record removes stay available as a [cut](../spec/journal.md#cuts) until the journal that holds the record is folded, and a later `Paste` in the same journal inserts them elsewhere, naming them instead of carrying them.
  Nothing goes to the file that would not be there anyway, so deleted content becomes garbage at the next flush, as after any removal.

**A cut is a weak promise: pasting it is cheap while its journal lasts, and encodes the value again otherwise.**
A container's `cut(i)` removes element `i` as `remove(i)` does, and returns a cut — the in-memory value, the cut's number among the cuts of the journal, and the journal it belongs to.
Methods that take a value — a push, an insertion, a field's `set` — take a cut too, and move the value into memory.
A cut gives no mutable access to its value, since its bytes must stay the value's encoding until they are pasted, and it can be pasted at most once.
Dropping it unpasted does nothing more: the value was removed when it was cut.
Any method that splices a value out can return it as a cut: a field's replacement, an option's `take`, or a change of an enum's variant, for the payload of the old variant.
And an application with references that outlive edits, [above](#handles-that-outlive-edits-elsewhere), can let a removed element remember its cut, so that inserting the same element again pastes it.

**Whether a paste is a `Paste` or a `Splice` is decided when its transaction is appended, not when the application calls it.**
A flush can land between a cut and its paste — triggered by the paste's own transaction, which [is preceded by a flush](transactions-and-batches.md#flush-triggers) when it does not fit in what is left of the journal's budget — and folding the cut's journal ends the cut.
So the buffer holds the operation as "paste cut `c`, or else these bytes", with the value's encoding available, and the transaction's size is computed with the `Paste`; if the transaction fits, it is appended with the `Paste`, into the journal that made the cut, and if a flush must come first, the paste is encoded as a `Splice` of the value's bytes, the size is computed again, and the flush triggers continue from there.
Two cases need no fallback at all: a cut and its paste in one transaction, which no flush can separate; and the ready run of a batch, which is appended before the flush that cuts the batch.

**What it costs**: an implementation keeps what it needs to resolve the journal's cuts until the journal is folded — for the fold, the trees of pieces that the cuts removed — and the length of each cut, to check pastes before they are recorded.
How long a cut lives is how long a journal lives, so an application that flushes often, when idle say, ends its cuts sooner; an undo right after a deletion pastes, and one after the next flush encodes again.
Changing a value between its cut and its paste, say to adjust a shape's coordinates to its new group, would need records whose positions lie inside a cut rather than in the sequence; it is left out, since pasting first and changing the value then does the same.

Cap'n Proto has the same idea within one message: an *orphan* is an object disowned by one parent and adopted by another without copying, move-only, and zeroed if nothing adopts it.
