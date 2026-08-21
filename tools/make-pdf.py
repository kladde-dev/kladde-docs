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
       fonts-dejavu, librsvg2-bin (pandoc shells out to rsvg-convert to turn
       the generated SVGs into PDF), and `npm install` for the mermaid
       renderer in tools/render-mermaid.mjs.
"""

import argparse
import json
import re
import shutil as _shutil
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


SVG_CALL = re.compile(r"(var|color-mix)\(([^()]*)\)")   # innermost call only
SVG_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _css_args(s):
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _rgb(c):
    m = SVG_HEX.match(c.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def flatten_svg(svg):
    """Resolve CSS custom properties and color-mix() into literal colours.

    Every non-browser SVG converter -- rsvg-convert, cairosvg, resvg, and
    ImageMagick, which delegates to librsvg anyway -- supports neither, and
    silently paints the whole diagram black instead of failing.  Flattening here
    is what makes the SVG portable to any of them.
    """
    decls = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;}\"\']+)", svg))

    def one(m):
        kind, args = m.group(1), _css_args(m.group(2))
        if kind == "var":
            return decls.get(args[0], args[1] if len(args) > 1 else "none")
        parts = [a for a in args if not a.startswith("in ")]
        if len(parts) != 2:
            return "none"
        pct = re.search(r"([\d.]+)%", parts[0])
        ratio = float(pct.group(1)) / 100 if pct else 0.5
        a, b = (_rgb(re.sub(r"[\d.]+%", "", x)) for x in parts)
        if not a or not b:
            return "none"
        return "#%02x%02x%02x" % tuple(
            round(x * ratio + y * (1 - ratio)) for x, y in zip(a, b))

    for _ in range(20):
        svg, n = SVG_CALL.subn(one, svg)
        if not n:
            break
        decls = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;}\"\']+)", svg))
    if SVG_CALL.search(svg):
        sys.exit("flatten_svg: unresolved CSS in a diagram; see tools/render-mermaid.mjs")
    return re.sub(r"@import\s+url\([^)]*\)\s*;", "", svg)   # offline: no webfont


def _fences(body):
    """Yield (index, info_string, [content lines]) for every fenced block."""
    opener, block = None, []
    for i, line in enumerate(body.splitlines()):
        m = FENCE.match(line)
        if m and opener is None:
            opener, block = line.strip().strip("`~").strip(), []
        elif m:
            yield opener, block
            opener = None
        elif opener is not None:
            block.append(line)


def collect_mermaid(body):
    return ["\n".join(b) for info, b in _fences(body) if info == "mermaid"]


def render_mermaid(sources, figdir):
    """Render every mermaid source to a flattened SVG file, in one node call."""
    script = Path(__file__).resolve().parent / "render-mermaid.mjs"
    if not _shutil.which("node"):
        sys.exit("node not found; needed to render the mermaid diagrams")
    proc = subprocess.run(
        ["node", str(script)], input=json.dumps({"sources": sources}),
        capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"mermaid renderer failed:\n{proc.stderr}")
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if not _shutil.which("rsvg-convert"):
        sys.exit("rsvg-convert not found (install librsvg2-bin)")
    figdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, svg in enumerate(json.loads(proc.stdout)["svgs"]):
        if svg is None:
            sys.exit(f"mermaid diagram {i + 1} did not render")
        stem = figdir / f"diagram-{i + 1:02d}"
        svg_path = stem.with_suffix(".svg")
        svg_path.write_text(flatten_svg(svg))
        # LaTeX cannot size an SVG.  Pandoc converts them for its *own* image
        # elements, but these are injected as raw LaTeX, so do it here -- which
        # also puts the failure in one obvious place if rsvg-convert is missing.
        pdf_path = stem.with_suffix(".pdf")
        conv = subprocess.run(
            ["rsvg-convert", "-f", "pdf", "-o", str(pdf_path), str(svg_path)],
            capture_output=True, text=True)
        if conv.returncode != 0:
            sys.exit(f"rsvg-convert failed on diagram {i + 1}:\n{conv.stderr}")
        paths.append(pdf_path)
    return paths


def substitute_mermaid(body, paths):
    """Replace each mermaid fence with the figure rendered from it.

    `paths` is empty under --check-only, which renders nothing; the fence is
    then left alone rather than exploding, since that mode only reports on
    links and page coverage.
    """
    out, opener, block = [], None, []
    for line in body.splitlines():
        if FENCE.match(line) and opener is None:
            opener, block = line, []
        elif FENCE.match(line):
            figure = (next(paths, None)
                      if opener.strip().strip("`~").strip() == "mermaid" else None)
            if figure is not None:
                # Raw LaTeX rather than `![](...)`: pandoc only centres an image
                # that carries a caption, and these want no caption.
                out += ["```{=latex}", "\\begin{center}",
                        # `max width` (adjustbox) rather than `width`: a small
                        # diagram keeps its natural size, a wide one is scaled
                        # down to the text block instead of running off it.
                        f"\\includegraphics[max width=\\linewidth]{{{figure}}}",
                        "\\end{center}", "```"]
            else:
                out += [opener, *block, line]
            opener = None
        elif opener is not None:
            block.append(line)
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


def build_markdown(pages, problems, figdir=None):
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
        # Injected as raw LaTeX, so pandoc never sees an Image element and
        # does not load graphicx itself.  The \\maxwidth dance is pandoc's own
        # idiom: natural size, capped at the text width.
        "  \\usepackage{graphicx}",
        "  \\usepackage[export]{adjustbox}",
        "  \\DefineVerbatimEnvironment{kladdediagram}{Verbatim}"
                f"{{baselinestretch={DIAGRAM_LEADING},samepage=true}}",
        "fontsize: 10pt",
        "---",
        "",
    ]
    # Two passes: every mermaid source in the document is collected first so
    # that node is started once rather than once per diagram.
    bodies = {}
    for rel, page in pages.items():
        body = rewrite_links(rel, page["body"], pages, problems)
        bodies[rel] = stamp_headings(body, page["prefix"])
    sources = [s for body in bodies.values() for s in collect_mermaid(body)]
    figures = iter(render_mermaid(sources, figdir) if sources and figdir else [])

    for rel, page in pages.items():
        body = substitute_mermaid(bodies[rel], figures)
        chunks.append(f"# {page['title']} {{#{page['prefix']}}}\n")
        chunks.append(size_tables(wrap_diagrams(body)).strip())
        chunks.append("")
    return "\n".join(chunks), len(sources)


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
    figdir = Path(args.output).with_suffix("").parent / (
        Path(args.output).stem + "-figures")
    merged, diagrams = build_markdown(
        pages, problems, None if args.check_only else figdir)

    for p in problems:
        print(f"link: {p}", file=sys.stderr)
    print(f"{len(pages)} pages, {len(merged.splitlines())} lines, "
          f"{diagrams} diagram(s), {len(problems)} link problem(s)", file=sys.stderr)

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
        _shutil.rmtree(figdir, ignore_errors=True)
    print(f"wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
