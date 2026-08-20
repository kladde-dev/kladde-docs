#!/usr/bin/env python3
"""Render the whole of content/ as one PDF.

Concatenates every page in reading order into a single Markdown document,
then hands it to pandoc.  The fiddly parts, and why they are needed:

  * Pages carry their title in YAML front matter and have no H1 of their own
    (Quartz injects one).  We strip the front matter and synthesise the H1,
    so each page becomes a chapter.
  * Every heading gets an explicit, document-scoped id, because titles like
    "Schema" and "Containers" occur in more than one page and would otherwise
    collide once merged.
  * Relative cross-page links (`../heap/compaction.md#frontier-slide`) are
    rewritten to those ids.  Unresolvable ones are reported, not silently
    dropped -- the report doubles as a link checker for the website.

Usage:  tools/make-pdf.py [-o kladde.pdf] [--keep-markdown]

Needs: pandoc, texlive-xetex, lmodern, fonts-texgyre, fonts-texgyre-math,
       fonts-dejavu.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Prose and headings.  Alternatives that ship with `fonts-texgyre` and have a
# matching math companion: "TeX Gyre Termes" (Times), "TeX Gyre Schola"
# (Century Schoolbook -- more legible, but wider, so more pages), or
# "Latin Modern Roman" (the classic TeX look, already in texlive-base).
# Change MAIN_FONT and MATH_FONT together, or the formulas stop matching
# the prose around them.
MAIN_FONT = "TeX Gyre Pagella"
MATH_FONT = "TeX Gyre Pagella Math"
SANS_FONT = "TeX Gyre Heros"

# Deliberately independent of the choice above, and deliberately DejaVu: the
# architecture diagrams are Unicode box-drawing (U+2500 block), which most
# text fonts' mono companions do not cover.  Changing MAIN_FONT cannot break
# them; changing this can.
MONO_FONT = "DejaVu Sans Mono"

# Leading for the box-drawing diagrams only.  DejaVu Sans Mono's U+2502 spans
# 10.2pt of the 12pt line pitch LaTeX uses at 10pt, so stacked bars show a
# 1.8pt seam at the default 1.0.  10.2/12 = 0.85 closes it exactly; anything
# lower only risks ascenders and descenders in the box labels colliding.
# `samepage` additionally stops a diagram being split across a page break,
# which the tighter leading makes more likely by shifting where things land.
DIAGRAM_LEADING = "0.85"

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"

# Reading order.  Explicit rather than derived: alphabetical would interleave
# the tutorial and the design docs, and put `conformance` before `file-format`.
ORDER = [
    "index.md",
    "spec/index.md",
    "spec/file-format.md",
    "spec/allocations.md",
    "spec/journal.md",
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
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
LINK = re.compile(r"\]\((?!https?:)([^)]*)\)")
BOX_DRAWING = re.compile(r"[\u2500-\u257f]")
TABLE_DELIM = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
MD_CODE = re.compile(r"`([^`]*)`")

# Pandoc derives a pipe table's column widths from the *dash counts* in its
# delimiter row, so a uniform `| --- | --- |` splits every table 50/50 no
# matter what is in it.  These two control the estimate that replaces it.
MONO_RATIO = 1.35      # a monospace character against an average prose one
TABLE_CHARS = 90.0     # prose characters that fit across the text block
TABLE_PAD = 2.0        # slack per column, see _allocate


def slug(text):
    """Lowercase-hyphen slug, matching the ids GitHub and Quartz generate."""
    text = re.sub(r"`([^`]*)`", r"\1", text)          # drop code ticks
    text = re.sub(r"[*_]", "", text)                   # drop emphasis
    text = text.lower()
    text = re.sub(r"[^a-z0-9 \-]", "", text)
    return re.sub(r"-+", "-", text.replace(" ", "-")).strip("-")


def doc_slug(rel):
    """A unique id prefix per page: spec/schema/fingerprints.md -> spec-schema-fingerprints."""
    return slug(rel[:-3].replace("/", "-"))


def split_front_matter(text):
    m = FRONT_MATTER.match(text)
    if not m:
        return None, text
    t = TITLE_LINE.search(m.group(1))
    return (t.group(1).strip('"\'') if t else None), text[m.end():]


def collect(order):
    """Read every page, returning per-page title, body, and heading ids."""
    pages = {}
    for rel in order:
        path = CONTENT / rel
        if not path.is_file():
            sys.exit(f"listed in ORDER but missing: {rel}")
        title, body = split_front_matter(path.read_text())
        if title is None:
            sys.exit(f"no `title:` in front matter: {rel}")
        prefix = doc_slug(rel)
        anchors, in_fence = {"": prefix}, False
        for line in body.splitlines():
            if FENCE.match(line):
                in_fence = not in_fence
            elif not in_fence:
                h = HEADING.match(line)
                if h:
                    anchors[slug(h.group(2))] = f"{prefix}--{slug(h.group(2))}"
        pages[rel] = {"title": title, "body": body, "prefix": prefix, "anchors": anchors}
    return pages


def rewrite_links(rel, body, pages, problems):
    """Point every relative link at the merged document's own ids."""
    here = Path(rel).parent

    def replace(m):
        target = m.group(1)
        path, _, frag = target.partition("#")
        if not path:                                    # same-page anchor
            dest = pages[rel]["anchors"].get(frag)
            if dest is None:
                problems.append(f"{rel}: no such heading '#{frag}'")
                return m.group(0)
            return f"](#{dest})"
        if path.endswith("/"):
            path += "index.md"
        try:
            resolved = (CONTENT / here / path).resolve().relative_to(CONTENT).as_posix()
        except ValueError:
            problems.append(f"{rel}: link escapes content/: {target}")
            return m.group(0)
        page = pages.get(resolved)
        if page is None:
            problems.append(f"{rel}: no such page: {target}")
            return m.group(0)
        dest = page["anchors"].get(frag)
        if dest is None:
            problems.append(f"{rel}: no heading '#{frag}' in {resolved}")
            dest = page["prefix"]                       # fall back to the chapter
        return f"](#{dest})"

    return LINK.sub(replace, body)


def stamp_headings(body, prefix):
    """Attach `{#page--heading}` to every heading, leaving fenced blocks alone."""
    out, in_fence = [], False
    for line in body.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        h = HEADING.match(line) if not in_fence else None
        if h:
            out.append(f"{h.group(1)} {h.group(2)} {{#{prefix}--{slug(h.group(2))}}}")
        else:
            out.append(line)
    return "\n".join(out)


def _cells(line):
    s = line.strip().strip("|")
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _width(cell):
    """Roughly the horizontal space a cell wants, in prose-character units."""
    cell = MD_LINK.sub(r"\1", cell)                     # links render as their text
    mono = sum(len(m.group(1)) for m in MD_CODE.finditer(cell))
    rest = re.sub(r"[*_\\]", "", MD_CODE.sub("", cell))
    return mono * MONO_RATIO + len(rest)


def _allocate(natural):
    """Share the line out the way a browser's automatic table layout would.

    Every column first gets the smaller of what it wants and an equal share, so
    a column of short identifiers is never squeezed below its natural size; what
    is left over then goes to the columns that still want more, in proportion to
    how much more they want.  That is what stops one prose column from dragging
    a neighbouring column of crate names down to half the page.
    """
    # Pandoc sizes each column as a share of (columnwidth - 2*tabcolsep) but
    # an n-column table actually spends 2n*tabcolsep on padding, so it
    # under-allocates by (2n-2)*tabcolsep.  A couple of characters per column
    # absorbs that, and incidentally favours the narrow columns, which are the
    # ones a rounding error actually hurts.
    natural = [w + TABLE_PAD for w in natural]
    if sum(natural) <= TABLE_CHARS:
        return natural
    share = TABLE_CHARS / len(natural)
    alloc = [min(w, share) for w in natural]
    demand = [n - a for n, a in zip(natural, alloc)]
    spare, wanted = TABLE_CHARS - sum(alloc), sum(demand)
    if spare > 0 and wanted > 0:
        alloc = [a + spare * d / wanted for a, d in zip(alloc, demand)]
    return alloc


def size_tables(body):
    """Rewrite each pipe table's delimiter row to reflect what the cells hold."""
    lines, out, i, in_fence = body.splitlines(), [], 0, False
    while i < len(lines):
        line = lines[i]
        if FENCE.match(line):
            in_fence = not in_fence
        if in_fence or not TABLE_DELIM.match(line) or i == 0:
            out.append(line); i += 1; continue

        delim = _cells(line)
        rows, j = [_cells(lines[i - 1])], i + 1
        while j < len(lines) and TABLE_ROW.match(lines[j]) and not TABLE_DELIM.match(lines[j]):
            rows.append(_cells(lines[j])); j += 1

        natural = [max((_width(r[c]) for r in rows if c < len(r)), default=1.0)
                   for c in range(len(delim))]
        alloc = _allocate(natural)
        total = sum(alloc) or 1.0
        cells = []
        for spec, a in zip(delim, alloc):
            left, right = spec.startswith(":"), spec.endswith(":")
            dashes = "-" * max(3, round(a / total * 72))
            cells.append((":" if left else "") + dashes + (":" if right else ""))
        out.append("| " + " | ".join(cells) + " |")
        i += 1
    return "\n".join(out)


def wrap_diagrams(body):
    """Re-emit box-drawing code blocks through a tighter-leading environment.

    Only blocks that actually contain box-drawing characters are touched, so
    ordinary code and pseudocode keep their normal, comfortable leading.
    """
    out, block, opener, in_fence = [], [], None, False
    for line in body.splitlines():
        if FENCE.match(line):
            if not in_fence:
                in_fence, opener, block = True, line, []
            else:
                in_fence = False
                if opener.strip() == "```" and any(BOX_DRAWING.search(b) for b in block):
                    out += ["```{=latex}", "\\begin{kladdediagram}", *block,
                            "\\end{kladdediagram}", "```"]
                else:
                    out += [opener, *block, line]
        elif in_fence:
            block.append(line)
        else:
            out.append(line)
    return "\n".join(out)


def build_markdown(pages, problems):
    chunks = [
        "---",
        'title: "Kladde"',
        'subtitle: "Specification and design documentation"',
        "documentclass: report",
        "papersize: a4",
        "geometry: margin=2.5cm",
        "toc: true",
        "toc-depth: 2",
        "numbersections: true",
        "colorlinks: true",
        "linkcolor: RoyalBlue",
        f'mainfont: "{MAIN_FONT}"',
        f'mathfont: "{MATH_FONT}"',
        f'sansfont: "{SANS_FONT}"',
        f'monofont: "{MONO_FONT}"',
        "header-includes: |",
        "  \\usepackage{fancyvrb}",
        "  \\DefineVerbatimEnvironment{kladdediagram}{Verbatim}"
                f"{{baselinestretch={DIAGRAM_LEADING},samepage=true}}",
        "fontsize: 10pt",
        "---",
        "",
    ]
    for rel, page in pages.items():
        body = rewrite_links(rel, page["body"], pages, problems)
        chunks.append(f"# {page['title']} {{#{page['prefix']}}}\n")
        chunks.append(size_tables(wrap_diagrams(stamp_headings(body, page["prefix"]))).strip())
        chunks.append("")
    return "\n".join(chunks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default=str(ROOT / "kladde.pdf"))
    ap.add_argument("--keep-markdown", action="store_true")
    ap.add_argument("--check-only", action="store_true",
                    help="report link problems and exit without running pandoc")
    args = ap.parse_args()

    on_disk = {p.relative_to(CONTENT).as_posix() for p in CONTENT.rglob("*.md")}
    missing = on_disk - set(ORDER)
    if missing:
        sys.exit("not listed in ORDER: " + ", ".join(sorted(missing)))

    problems = []
    pages = collect(ORDER)
    merged = build_markdown(pages, problems)

    for p in problems:
        print(f"link: {p}", file=sys.stderr)
    print(f"{len(pages)} pages, {len(merged.splitlines())} lines, "
          f"{len(problems)} link problem(s)", file=sys.stderr)

    if args.check_only:
        return

    # The merged Markdown pandoc actually consumes.  Removed on success,
    # but deliberately left behind when pandoc fails -- it is the input you
    # need to look at to work out why.
    md_path = Path(args.output).with_suffix(".md")
    md_path.write_text(merged)

    if not shutil.which("pandoc"):
        sys.exit("pandoc not found; install it or use --check-only")
    cmd = [
        "pandoc", str(md_path), "-o", args.output,
        "--from", "markdown+pipe_tables+backtick_code_blocks+tex_math_dollars"
                  "+header_attributes+fenced_code_attributes+raw_attribute",
        "--pdf-engine", "xelatex",
        "--top-level-division=chapter",
        "--highlight-style=tango",
        "-V", "linkcolor=RoyalBlue",
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        sys.exit(f"pandoc failed; its input is at {md_path}")
    if not args.keep_markdown:
        md_path.unlink()
    print(f"wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
