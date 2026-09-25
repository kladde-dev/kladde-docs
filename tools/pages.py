"""The set of documentation pages, and the front matter they carry.

Shared by `make-pdf.py` and `check-examples.py` so the reading order has one
definition.  Nothing here renders or checks anything; it is just the page list
plus the front-matter split both tools need before they can look at a body.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"

# Reading order.  Explicit rather than derived: alphabetical would interleave
# the tutorial and the design docs, and put `conformance` before `file-format`.
# It also fixes the order in which an example's code blocks are concatenated
# when one example spans more than one page.
ORDER = [
    "index.md",
    # The normative layer, bytes upward.
    "spec/index.md",
    "spec/file-format.md",
    "spec/durability.md",
    "spec/address-table.md",
    "spec/allocations.md",
    "spec/journal.md",
    "spec/schema/index.md",
    "spec/schema/type-descriptors.md",
    "spec/schema/canonical-encoding.md",
    "spec/schema/fingerprints.md",
    "spec/schema/evolution.md",
    "spec/tooling.md",
    "spec/conformance.md",
    # The language-agnostic algorithms: state first, then what acts on it.
    "impl/index.md",
    "impl/in-memory-state.md",
    "impl/address-table-operations.md",
    "impl/liveness.md",
    "impl/id-recycling.md",
    "impl/write-phase-state.md",
    "impl/flush.md",
    "impl/consolidation.md",
    "impl/consolidator-state.md",
    "impl/transactions-and-batches.md",
    "impl/related-work.md",
    # Rust: tutorial before design, since the tutorial motivates the traits.
    "rust/index.md",
    "rust/tutorial/index.md",
    "rust/tutorial/getting-started.md",
    "rust/tutorial/containers.md",
    "rust/tutorial/deriving.md",
    "rust/tutorial/comparison-to-serde.md",
    "rust/tutorial/durability.md",
    "rust/tutorial/custom-persistable.md",
    "rust/crates.md",
    "rust/pointers.md",
    "rust/persistable-and-guards.md",
    "rust/containers.md",
    "rust/derive-macro.md",
    "rust/freeing.md",
    "rust/transactions.md",
    "rust/schema-binding.md",
    "rust/store.md",
    # Measurements of the Rust implementation, on the design's behalf.
    "evaluation/index.md",
    "evaluation/consolidation.md",
    # Kept for the reasoning, not the result.
    "superseded/index.md",
    "superseded/relocatable-heap.md",
    "superseded/incremental-compaction.md",
    "superseded/in-place-flush.md",
    "superseded/whole-entry-address-table.md",
    # Work in progress; last, because nothing else may depend on it.
    "drafts/index.md",
    "drafts/open-issues.md",
    "drafts/move-op.md",
    "drafts/segments.md",
    "drafts/ripeness.md",
]

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
TITLE_LINE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)


def split_front_matter(text):
    """Returns (title, body). `title` is None when there is no front matter."""
    m = FRONT_MATTER.match(text)
    if not m:
        return None, text
    t = TITLE_LINE.search(m.group(1))
    return (t.group(1).strip('"\'') if t else None), text[m.end():]
