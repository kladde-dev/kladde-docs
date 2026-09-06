# Address Table

## On-disk representation

### Logical level

On disk, the address table is a list of *statements*, written in one or more pages, where each statement implicitly inherits its page's `epoch`.
Statements from all live pages are merged, resolving conflicts by recency, see [[#conflict resolution across epochs]].

#### Statement types

| Statement                           | Existence of `id` | Size of `id`                                                     | Content of `id`                                                                    | Usage note                                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------- | ----------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Ref(id, offset, size, address)`    | exists            | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` equals `file[address, address + size)`. | `[address, address + size)` must be within a single `Data` page. We probably want to restrict that every byte in a `Data` page is referenced at most once from a live address table page (TODO: is this necessary?).                                                                                                                                                                  |
| `Undefined(id, offset, size)`       | exists            | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` is `Undefined`.                         | Kladde is allowed to hand out arbitrary data for `Undefined` regions, and consolidation may rewrite `Undefined` regions with `Ref` or `Inline` with arbitrary data (useful for data types that have long sequences of repeated small `[data] [undefined] [data] [undefined] ...` patches, which could happen for arrays of enums with slightly unbalanced payload sizes per variant). |
| `Size(id, size)`                    | exists            | `size`                                                           | Anything beyond `size` is `Undefined`.                                             | Only has an effect if an older live page states a different size or some non-`Undefined` content beyond `size` that is not overwritten by a newer live statement.                                                                                                                                                                                                                     |
| `Tombstone(id)`                     | does not exist    | `0`                                                              | All bytes are `Undefined`.                                                         | Only has an effect if an older live page states existence of `id` and no newer live page states existence of `id` and fully overwrites any size and content remaining from the old incarnation.                                                                                                                                                                                       |
| `Inline(id, offset, size, payload)` | exists            | `>= offset + size`, smallest compatible with all live statements | Content in range `[offset, offset + size)` equals `payload`.                       | Small-size optimization. Requires `1 <= size  <= 252`.                                                                                                                                                                                                                                                                                                                                |

#### No conflicts within each epoch

Statements in the same `epoch` must not conflict with each other:

- no two statements in the same `epoch` may make contradicting statements about the existence of an `id`;
- no two statements in the same `epoch` may name any byte of content twice, not even if the two statements assign the same value to that byte.
  This includes `Undefined`: an assignment of `Undefined` to a given byte — whether by an `Undefined`, `Size`, or `Tombstone` statement — conflicts with any other assignment to the same byte, including a second `Undefined` assignment.
- At most one `Size` statement per `id` is allowed per epoch.
- Multiple `Ref`, `Undefined`, and `Inline` statements for pairwise disjoint ranges within the same allocation are allowed.
  They don't conflict in regards of the size of the allocation because they all merely bound the size of the allocation from below.
- However, a `Ref`, `Undefined`, or `Inline` statement conflicts with a `Size(size)` statement in the same `epoch` if its lower bound `offset + size` is larger than `size`, and this is not allowed in a single `epoch`.

#### Conflict resolution across epochs

Two statements with different `epoch` are allowed to contradict each other.
In this case, the newer statement (the one with the higher `epoch`) takes precedence, but only as far as it contradicts the older statement.
Any part of the older statement that doesn't contradict the newer statement survives, and this is resolved at the granularity of the allocations existence, its size, and the value of each of its content bytes:

- **Existence:** allocation `id` exists if the statement with highest `epoch` that mentions `id` is `Ref`, `Undefined`, `Size`, or `Inline`.
  It does not exist if the statement with highest `epoch` that mentions `id` is `Tombstone(id)` or if no statement mentions `id`.
- **Size:** `0` if the allocation does not exist (note: this is only to simplify the rules; we still distinguish *existent* zero-sized allocations from *non-existent* allocations).
  Otherwise, let `tombstone_epoch` be the largest `epoch` of any `Tombstone(id)` statement (or `tombstone_epoch = -1` if no `Tombstone(id)` exists), and let `size_epoch` be the largest `epoch > tombstone_epoch` of any `Size(id, ...)` statement (or `size_epoch = tombstone_epoch` if no `Size(id, ...)` statement exists).
  The size of allocation `id` is then the maximum over:
	- the `size` field of the `Size(id, size)` statement at `size_epoch`, if this statement exists; and
	- all values `offset + size` of all `Ref(id, offset, size, ...)`, `Undefined(id, offset, size)` and `Inline(id, offset, size, ...)` statements with `epoch > size_epoch`.
  At least one matching `Size`, `Ref`, `Undefined`, or `Inline` statement must exist because otherwise the allocation does not exist.
- **Content at a given offset `probe`:** defined by the latest statement that sets the value of this content, i.e., the statement with highest `epoch` that matches any of the following patterns:
	- `Tombstone(id)`;
	- `Size(id, size)` where `size <= probe`; or
	- `Ref(id, offset, size, ...)`, `Undefined(id, offset, size)`, or `Inline(id, offset, size, ...)` where `offset <= probe < offset + size`.
  There must be at most one winner because at most one statement matching the above criteria is allowed per `epoch` (see [[#no conflicts within each epoch]]).
  Given the winning statement, the content of allocation `id` at offset `probe` is defined as follows:
	- If there is no matching statement or if the winning statement is of type `Tombstone`, `Size`, or `Undefined`: the content of allocation `id` at offset `probe` is `Undefined`.
	- if the winning statement is `Inline(id, offset, size, payload)`: the content of allocation `id` at offset `probe` is `payload[probe - offset]`.
	- If the winning statement is `Ref(id, offset, size, address)`: the content of allocation `id` at offset `probe` is `file[address + (probe - offset)]`.

### Physical level

There is only a single `kind` for address table pages: `kind == AddressTable`.
The header page is always an `AddressTable` page.
Every `AddressTable` page contains zero or more references to other `AddressTable` pages followed by zero or more statements.
References between `AddressTable` pages form a tree rooted at the header page.

```
address_table_page := num_children:varint child_ref{num_children} num_statements:varint statements:statement{num_statements}
child_ref          := page_number:varint  ; the page number (not the start address of the page)
statement          := id_delta (tagged_tombstone | tagged_ref | tagged_undefined | tagged_size | tagged_tombstone | inline)
id_delta           := varint              ; increment from id of last statement (for first statement in page: id)
tagged_ref         := 0:byte offset:varint size:varint address:varint
tagged_undefined   := 1:byte offset:varint size:varint
tagged_size        := 2:byte size:varint
tagged_tombstone   := 3:byte              ; tombstones have no payload apart from the id

; inline is optimized for lots of small payloads. size_tag = size + 3 >= 4 can't clash.
inline             := size_tag:byte offset:varint payload:byte{size_tag - 3}
```

## In-memory representation

@Claude: fill in this section. You'll probably have to think about [[#maintenance]] below to come up with the full data structures for the in-memory representation.

To serve size and content queries (for data type implementations), we probably only need

- a hash map `id → size` and
- a B-tree `(id, offset) → Option<Address>`.

But to maintain the on-disk address table, we probably need more information.
Maybe something like a B-tree `(id, offset) → Statement` pointing to the latest statement that covers every byte in `id` from `offset` to the next `offset` in the tree.

## Maintenance

@Claude: fill this in: how do we maintain the `AddressTable` pages?
Start from posing a set of goals.
I'm not yet sure what exactly the goals are, but I think they probably include:

- minimize the number of live address table pages by:
	- consolidating address tables with low live fraction
	- rewriting large parts of allocations that were scattered across many `Inline` or `Ref`s into a single `Ref` to save allocation space (on disk and in memory)
- minimize the number of `Data` pages by that need to be written in a given flush by:
	- only overwriting parts of older `Ref`s that actually change
- minimize the number of life `Data` pages by:
	- consolidating data pages with low live fraction.

Note that some of these goals are in tension with each other (writing few large `Ref`s to reduce address table size vs. overwriting ownly parts of older `Ref`s that actually change).
Is there a principled way to think about this trade-off, and an efficient data structure to optimize it at least in some approximate way?
Be specific about possible solutions, what they optimize, and what their run-time complexity is.
The runtime cost of every flush should be at most logarithmic in the number of live statements and in the number of live allocations.

Also propose a way to organize the tree of `AddressTable` pages.
The above file format specification is very permissive and allows everything from a fully balanced tree to a linked list.
Since adding or deleting a page requires updating the full path from the root to the page, we probably want something approximately self-balancing (and that maybe opportunistically consolidates pages it has to update anyway).
A B-tree like structure would waste about 25% of space in every non-root page in expectation (which is too much for my taste), and probably typically even more in the root (header) page.
Consider the following cases, which I expect to be fairly common:

- (i) a relatively small address table that fits entirely in the header page
- (ii) an only slightly larger address table that requires maybe one or two spill pages with statements only and no children (leaves) as long as we use any space not taken by `child_ref`s in the header page for further statements (avoiding a full extra leaf page which, in this case of a relatively small file, would probably pose quite a bit of relative overhead).
