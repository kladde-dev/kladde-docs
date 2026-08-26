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
    "spec/index.md",
    "spec/file-format.md",
    "spec/allocations.md",
    "spec/journal.md",
    "spec/transactions-and-batches.md",
    "spec/schema/index.md",
    "spec/schema/type-descriptors.md",
    "spec/schema/canonical-encoding.md",
    "spec/schema/fingerprints.md",
    "spec/schema/evolution.md",
    "spec/tooling.md",
    "spec/conformance.md",
    "rust/index.md",
    "rust/tutorial/index.md",
    "rust/tutorial/getting-started.md",
    "rust/tutorial/containers.md",
    "rust/tutorial/deriving.md",
    "rust/tutorial/comparison-to-serde.md",
    "rust/tutorial/durability.md",
    "rust/tutorial/custom-persistable.md",
    "rust/design/index.md",
    "rust/design/heap/index.md",
    "rust/design/heap/relocatable-heap.md",
    "rust/design/heap/placement.md",
    "rust/design/heap/compaction.md",
    "rust/design/heap/evacuation-index.md",
    "rust/design/heap/pathologies.md",
    "rust/design/journal/index.md",
    "rust/design/journal/semantics.md",
    "rust/design/journal/fold-and-schedule.md",
    "rust/design/journal/crash-consistency.md",
    "rust/design/persistence/index.md",
    "rust/design/persistence/pointers.md",
    "rust/design/persistence/persistable-and-guards.md",
    "rust/design/persistence/containers.md",
    "rust/design/persistence/derive-macro.md",
    "rust/design/persistence/freeing.md",
    "rust/design/schema/index.md",
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
