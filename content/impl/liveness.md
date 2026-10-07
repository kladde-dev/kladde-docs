---
title: Liveness accounting
---

What makes a statement worth keeping, what makes a page worth reclaiming, and how both are decided in `O(1)`.

Nothing here is persisted.
All of it is rebuilt during the bulk read at open and maintained incrementally afterwards, and all of it is **policy**: an over-count only delays cleaning, an under-count only cleans a page earlier than ideal, and neither can make a resolved read wrong.

## Coverage

A page's **coverage** is the number of bytes in it that current state still relies on.
It is what makes a page a consolidation victim, so it has to fall as the page's contents become useless.

One rule covers both page kinds, and it is the rule data pages already follow.

**Content bytes are charged to the page that physically holds them.**
Every `Bytes` fragment names a page and an offset, so when a fragment is destroyed, decrement that page's coverage by the fragment's length.
A `Ref`'s fragments point into a `Data` page; an `Inline`'s fragments point into the `Table` page carrying the payload; a zero fragment points nowhere and costs nothing.
Splitting a fragment changes nothing, since both halves still cover the same bytes.

An inline payload is therefore just content that happens to live in a table page, and it gets per-byte accounting for free — no per-statement payload counters, no special case, the same line of code.

*Why per-byte accounting for inline payloads is not optional.*
For a `Ref`, partial shadowing strands only ~10 bytes of framing in the address-table page, while the content bytes it no longer reaches are tracked exactly in the data page's counter, which duly falls.
For an `Inline` there is no data page: the payload *is* the content, so charging it all-or-nothing would let shadowing half of a 200-byte inline strand 100 bytes that no counter in the system ever notices, and a page holding sixty mostly-shadowed inlines would report itself nearly full and never be cleaned.

**Framing bytes are charged to the statement's own page**, and released in one step when the statement loses its last reason to live.
The two charges cover disjoint byte ranges of the encoding, so they cannot double-count.

**During a flush, releases come first and charges wait for locations.**
A range the flush [takes](address-table-operations.md#takeid-range-pending-dirty) is released from its old owners at once, so victim selection within the same flush already sees what the flush will leave.
Its bytes are charged to a page once they have one — at once when they stay where they are, when `pack` places them when they move, and when the cut picks a table page for an inline payload — and the new statement's framing when the cut binds it.

## Pins

A statement is live exactly while `pins > 0`, and `pins` is a **single counter over heterogeneous holders**.

| pin | held by | taken when | released when |
| --- | --- | --- | --- |
| `F` | a content statement | a fragment resolves *through* it | that fragment is destroyed or re-owned |
| `S` | the **size statement** — an existing id's newest statement that states its size, or a non-existent id's tombstone | the statement is bound as the id's size statement, or the id is freed and its tombstone bound | a newer statement states the size, or the id is freed; a tombstone's, when `mentions` falls to 1, or when the id is allocated again and the new incarnation's size statement is bound |

So a `Size` or a `Tombstone` holds only `S`, a plain content statement only `F`, and a sizing content statement `F`, and `S` as long as it is the size statement.

*Why one counter and not two.*
A sizing content statement holds both kinds at once — `Ref*(id, 0, 60, P)` that states its allocation's size owns `[0, 60)` as well — and no consumer ever asks which kind a pin is.
Two fields would encode a distinction that is never read, while inviting the bug where a predicate checks one and forgets the other.

*Why "pin".*
This is a refcount whose holders are of different kinds, so `liveness` would read as a boolean and `refcount` would say nothing about what is doing the referring.
"Pin", in the buffer-manager sense of *something prevents this from being reclaimed*, is exactly the relationship.

**Pins can rise after creation.** Splitting a fragment gives its statement a second one.

**A fragment the flush has taken holds no pin**, since the statement that will own it does not exist until the cut [binds it](address-table-operations.md#bindstmt-page-run); the old owner's pin was released by the take.

## The droppability rule

> **A statement may be dropped iff it owns no fragment and is not its id's size statement.**

Equivalently: `pins == 0`.

**Why a content statement needs no denial pin.**
A content statement's denial is exactly co-extensive with what it defines.
If a `Ref` covering `[a, b)` has lost every fragment, then every byte of `[a, b)` that is below the size is covered by something strictly newer, and that newer thing also outranks everything the `Ref` was denying.
The size is safe too, since only the size statement decides it, and that statement holds `S`.

**Why a superseded size statement needs none either.**
A statement that states a size but is not the newest one to do so decides no size, and it matches no probe beyond those its `F` pins already count — none at all for a `Size` or `Tombstone`.

### The size pin is not substitutable

**A size statement may own no fragment, and still decide something no other statement does.**
With `Ref*(7, 0, 1000, P1)`@3 and `Size(7, 10)`@5, the `Size` owns nothing, since it matches no probe; yet dropping it would make `Ref*`@3 the newest statement of the size, which is then 1000 again, and `[10, 1000)` would read `P1`'s stale bytes.
This is the hazard a consolidator must respect: **a page holding a size statement cannot be retired without the size being stated again**, which the cut does by [the rules for a touched id](address-table-operations.md#what-the-cut-states-for-a-touched-id).

## Tombstones

A `Tombstone` matches no probe; it states that its id does not exist, and a size of 0.
While the id does not exist, the tombstone is its newest statement and its size statement, so it holds `S` — and here `S` guards existence rather than a size: dropping the tombstone while any older statement mentioning the id is still *physically present* would let that statement decide existence again and resurrect the allocation.
The count must be over physically present statements, not resolution-live ones: a statement with zero pins that has not yet been swept out of its page resurrects just as well.

### The last tombstone

So a tombstone **releases `S` when `mentions` falls to 1**: it is then the id's last statement, the id reads as non-existent without it, and `pins == 0` is once again an exact droppability test.

This is the only reader of `mentions`.

**A re-allocated id's tombstone dies with the flush that allocates the id.**
The new incarnation states its size, and every byte below it by [the coverage rule](../spec/address-table.md#the-coverage-rule), in an epoch above the tombstone, so from then on the tombstone decides neither existence, nor the size, nor any byte; it holds `S` until the new size statement is bound, which releases it.
So at most one tombstone per id is ever live, the newest statement of an id that does not exist, and recycling an id never has to wait for its tombstone to die.

### The cascade

Statements of a freed id contribute no content bytes, so their pages sink toward zero live fraction and become victims; consolidating those pages drops the statements, which decrements `mentions`; when it reaches 1 the tombstone goes dead, sinking *its* page's live fraction, which eventually recycles it too.
Nothing has to reason about epochs to make this happen — only about counters.

## Emission: when a resize writes a statement

**Every resize states the new size, and costs a statement of its own only when the flush states no content that ends at the new size.**
The cut makes a content statement that ends at its id's size sizing whenever there is one — the bytes an append writes at the new end, the zeros a growth exposes, the tail a deleting splice restates — and states `Size(id, n)` otherwise, which a shrink that writes nothing at the new end needs.
Whether a statement is needed is decidable from the flush alone, since the size depends on the newest size statement and on nothing else.

The cut makes **every** content statement it states that ends at its id's size sizing, whether the size changed or not, since it costs no byte: the size statement then moves onto the newest statement of the allocation's tail, and the one it replaces loses `S` at once rather than keeping its page alive.

## Confirmed sound

Three properties that any reimplementation should preserve:

- **The pin graph is acyclic**, trivially so since both kinds point the same way: `F` and `S` both point from the resolved view into statements, and no statement pins another.
- **A statement holds at most two pin kinds**, and there is no third kind in the model.
- **`mentions` is a net count**, so a flush that replaces a statement leaves it unchanged and the order in which a flush applies its writes and drops does not matter.

## Ranking

Cleaning should be ranked by **retirements enabled** as well as bytes reclaimed.

Victim selection ranked purely by live fraction cannot see the benefit of cleaning a page whose dead statements are holding some tombstone's `mentions` above 1, so a page at 99.8 % live fraction is never chosen even though cleaning it would let a tombstone elsewhere retire.
A benefit term counting "dead statements whose removal would let a tombstone retire" makes that visible.

The case is confined to the tombstone of an id that was freed and never re-allocated.
