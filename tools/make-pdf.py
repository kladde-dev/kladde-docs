#!/usr/bin/env python3
"""Render content/ as one PDF, or named pages as one PDF each.

With no arguments, concatenates every page in reading order into a single
Markdown document and hands it to pandoc.  Given one or more `.md` files, it
renders each on its own instead, writing `<basename>.pdf` to the working
directory -- an `article` rather than a chapter of a `report`, with links into
other pages left as written, since there is no merged document to point into.

The fiddly parts of the whole-set build, and why they are needed:

  * Pages carry their title in YAML front matter and have no H1 of their own
    (Quartz injects one).  We strip the front matter and synthesise the H1,
    so each page becomes a chapter.
  * Every heading gets an explicit, document-scoped id, because titles like
    "Schema" and "Containers" occur in more than one page and would otherwise
    collide once merged.
  * Relative cross-page links (`../heap/compaction.md#frontier-slide`) are
    rewritten to those ids.  Unresolvable ones are reported, not silently
    dropped -- the report doubles as a link checker for the website.

Usage:  tools/make-pdf.py [-o kladde.pdf] [--keep-markdown] [--check-examples]
        tools/make-pdf.py PAGE.md [PAGE.md ...]
        tools/make-pdf.py --diff OLD[..NEW] [PAGE.md ...]

Needs: pandoc, texlive-xetex, lmodern, fonts-texgyre, fonts-texgyre-math,
       fonts-dejavu, librsvg2-bin (pandoc shells out to rsvg-convert to turn
       the generated SVGs into PDF), and `npm install` for the mermaid
       renderer in tools/render-mermaid.mjs.  --diff additionally needs
       latexdiff and texlive-plain-generic (for ulem.sty).
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil as _shutil
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pages import (  # noqa: E402
    ORDER, ROOT, CONTENT, FRONT_MATTER, split_front_matter)

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

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
LINK = re.compile(r"\]\((?!https?:)([^)]*)\)")
WIKILINK = re.compile(r"\[\[([^\]|#]*?)(?:#([^\]|]*))?(?:\|([^\]]*))?\]\]")
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
    """Lowercase-hyphen slug, matching the ids GitHub and Quartz generate.

    github-slugger, which is what Quartz uses, lowercases, drops every
    character that is not a word character, space, or hyphen, and turns
    spaces into hyphens.  Two consequences are easy to get wrong and are
    what this function exists to reproduce: runs of hyphens are *not*
    collapsed, so `A — b` becomes `a--b`, and `_` is a word character, so
    `replacement_anchor(id)` keeps its underscore.
    """
    text = re.sub(r"`([^`]*)`", r"\1", text)          # drop code ticks
    text = re.sub(r"[^\w \-]", "", text.lower(), flags=re.UNICODE)
    return text.replace(" ", "-")


def doc_slug(rel):
    """A unique id prefix per page: spec/schema/fingerprints.md -> spec-schema-fingerprints."""
    return slug(rel[:-3].replace("/", "-"))


def read_page(text, rel, fallback_title=None):
    """One page's title, body, id prefix, and heading anchors."""
    title, body = split_front_matter(text)
    if title is None:
        if fallback_title is None:
            sys.exit(f"no `title:` in front matter: {rel}")
        title = fallback_title
    prefix = doc_slug(rel)
    anchors, in_fence = {"": prefix}, False
    for line in body.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            h = HEADING.match(line)
            if h:
                anchors[slug(h.group(2))] = f"{prefix}--{slug(h.group(2))}"
    return {"title": title, "body": body, "prefix": prefix, "anchors": anchors}


def collect(order, source=None):
    """Read every page, returning per-page title, body, and heading ids.

    `source(rel)` supplies the text, so a build can be fed from a git ref
    instead of the working tree; returning None drops the page, which is how
    a page that did not exist at the old ref is handled.
    """
    pages = {}
    for rel in order:
        if source is None:
            path = CONTENT / rel
            if not path.is_file():
                sys.exit(f"listed in ORDER but missing: {rel}")
            text = path.read_text()
        else:
            text = source(rel)
            if text is None:
                continue
        pages[rel] = read_page(text, rel)
    return pages


def expand_wikilinks(rel, body, pages, problems, standalone=False):
    """Turn Obsidian `[[page#heading|alias]]` into an ordinary Markdown link.

    Worth doing rather than telling authors not to write them, because a
    heading wikilink is the *only* anchor form that works in both editors:
    Obsidian matches a `](#fragment)` against the heading's literal text,
    while Quartz and pandoc want a slug, so no single `](#...)` satisfies
    both.  A wikilink does -- Quartz slugifies the fragment (verified: it
    emits `href="#raii-types"` for `[[#RAII types]]`), and Obsidian is where
    the syntax comes from.  Pandoc alone has no idea, and prints it verbatim.

    Targets resolve by filename stem across the whole set, matching the
    `markdownLinkResolution: shortest` in quartz.config.yaml.
    """
    stems = {}
    for r in pages:
        stems.setdefault(Path(r).stem, []).append(r)
    here = Path(rel).parent

    def replace(m):
        target, frag, alias = m.group(1), m.group(2), m.group(3)
        label = alias or frag or target
        if not target:                                  # [[#Heading]]
            return f"[{label}](#{slug(frag or '')})"
        if "/" in target:                               # [[spec/journal]]
            found = [target if target.endswith(".md") else target + ".md"]
        else:
            found = stems.get(target, [])
        if not found and not standalone:
            problems.append(f"{rel}: wikilink to a page that does not exist: [[{target}]]")
            return label
        if len(found) > 1:
            problems.append(f"{rel}: ambiguous wikilink [[{target}]]: "
                            + ", ".join(sorted(found)))
        dest = found[0] if found else target + ".md"
        path = os.path.relpath(dest, here or ".")
        return f"[{label}]({path}#{slug(frag)})" if frag else f"[{label}]({path})"

    return WIKILINK.sub(replace, body)


def rewrite_links(rel, body, pages, problems, standalone=False):
    """Point every relative link at the merged document's own ids.

    `standalone` is single-page mode: there is no merged document, so a link
    into another page has nothing to point at.  Those are left exactly as
    written rather than reported -- they are correct on the website, and a
    single-page PDF is a view of that page, not a claim about the whole set.
    """
    here = Path(rel).parent

    def replace(m):
        target = m.group(1)
        path, _, frag = target.partition("#")
        if path and standalone:
            return m.group(0)
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


SVG_NODE = re.compile(
    r'<g class="node" data-id="([^"]+)"[^>]*>\s*<rect[^>]*?x="([-\d.]+)"[^>]*?y="([-\d.]+)"'
    r'[^>]*?width="([\d.]+)"[^>]*?height="([\d.]+)"')
SVG_EDGE = re.compile(
    r'(<polyline class="edge" data-from="([^"]+)" data-to="([^"]+)"[^>]*points=")([^"]+)(")')


def _svg(nodes, edges):
    """A minimal laid-out diagram, in the shape the renderer emits."""
    body = "".join(
        f'<g class="node" data-id="{i}" data-label="{i}" data-shape="rectangle">'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="0" ry="0"/></g>'
        for i, x, y, w, h in nodes)
    body += "".join(
        f'<polyline class="edge" data-from="{a}" data-to="{b}" data-style="solid"'
        f' data-arrow-start="false" data-arrow-end="true" points="{p}"/>'
        for a, b, p in edges)
    return f"<svg>{body}</svg>"


def _points(svg, frm, to):
    m = re.search(rf'data-from="{frm}" data-to="{to}"[^>]*points="([^"]+)"', svg)
    return [tuple(round(float(v), 1) for v in q.split(",")) for q in m.group(1).split()]


def self_test():
    """Exercise the layout passes and the checker on synthetic diagrams.

    Every one of these corresponds to a defect that reached a rendered PDF and
    was found by eye rather than by a check. They need neither node nor pandoc,
    so they run anywhere in well under a second.
    """
    cases, failures = [], []

    def check(name, ok):
        cases.append(name)
        if not ok:
            failures.append(name)

    # Three boxes in a column, so an edge from the top to the bottom one has the
    # middle box in its way.
    stack = [("A", 0, 0, 80, 40), ("C", 0, 100, 80, 40), ("B", 0, 200, 80, 40)]

    errs, _ = lint_diagram(_svg(stack, [("A", "B", "40,40 40,200")]))
    check("an edge through a node is an error", any("through node C" in e for e in errs))

    errs, _ = lint_diagram(_svg(stack, [("A", "B", "300,40 300,200"),
                                        ("A", "B", "300,40 300,200")]))
    check("an edge hidden under another is an error", any("lies entirely" in e for e in errs))

    errs, warns = lint_diagram(_svg(stack, [("A", "B", "300,40 300,200"),
                                            ("C", "B", "250,120 250,150 350,150 350,200")]))
    check("a plain crossing only warns", not errs and any("crosses" in w for w in warns))

    errs, warns = lint_diagram(_svg(stack, [("A", "B", "300,40 300,150 200,150 200,200"),
                                            ("C", "B", "400,120 400,150 250,150 250,200")]))
    check("a shared channel only warns", not errs and any("channel" in w for w in warns))

    errs, warns = lint_diagram(_svg(stack, [("A", "C", "40,40 40,100")]))
    check("a clean diagram is silent", not errs and not warns)

    # straighten_svg: the jog collapses only when the result stays on both faces.
    wide = [("A", 0, 0, 200, 40), ("B", 0, 200, 200, 40)]
    out = straighten_svg(_svg(wide, [("A", "B", "40,40 40,120 90,120 90,200")]))
    check("a needless jog is straightened", len(_points(out, "A", "B")) == 2)

    narrow = [("A", 0, 0, 20, 40), ("B", 300, 200, 20, 40)]
    out = straighten_svg(_svg(narrow, [("A", "B", "10,40 10,120 310,120 310,200")]))
    check("a jog that must travel sideways is kept", len(_points(out, "A", "B")) == 4)

    # Straightening may legitimately pick either column, so assert the invariant
    # it actually promises: it never makes the diagram worse than it found it.
    # `C` blocks the left column, so the pass must route right or not at all.
    blocked = [("A", 0, 0, 200, 40), ("C", 20, 100, 40, 40), ("B", 0, 200, 200, 40)]
    edge = [("A", "B", "40,40 40,60 160,60 160,200")]
    before, _ = lint_diagram(_svg(blocked, edge))
    after, _ = lint_diagram(straighten_svg(_svg(blocked, edge)))
    check("straightening introduces no new defect", len(after) <= len(before))

    # spread_ports: a fan from one shared point gets one exit per edge.
    fan = [("A", 0, 0, 200, 40), ("B", 0, 200, 40, 40), ("C", 300, 200, 40, 40)]
    out = spread_ports(_svg(fan, [("A", "B", "100,40 100,120 20,120 20,200"),
                                  ("A", "C", "100,40 100,120 320,120 320,200")]))
    check("a shared fan-out point is spread",
          _points(out, "A", "B")[0][0] != _points(out, "A", "C")[0][0])

    # separate_channels: two horizontals on one channel get their own.
    # The two horizontals must overlap in x, or there is nothing to separate.
    conv = [("A", 0, 0, 60, 40), ("D", 200, 100, 60, 40), ("B", 100, 300, 120, 40)]
    shared = [("A", "B", "30,40 30,250 190,250 190,300"),
              ("D", "B", "230,140 230,250 150,250 150,300")]
    _, before = lint_diagram(_svg(conv, shared))
    out = separate_channels(_svg(conv, shared))
    _, after = lint_diagram(out)
    check("a shared channel is detected", any("channel" in w for w in before))
    check("a shared channel is separated", not any("channel" in w for w in after))

    # latexdiff's shape for a table whose column spec changed: a pair that
    # opens outside the table and closes inside it, next to a rule.
    table = ("\\begin{document}\n"
             "\\DIFaddbegin \\begin{longtable}[]{ll}\n"
             "\\DIFaddend \\toprule\n"
             "a & \\DIFdelbegin \\DIFdel{b}\\DIFdelend \\DIFaddbegin \\DIFadd{c}\\DIFaddend \\\\\n"
             "\\bottomrule\n"
             "\\end{longtable}\n")
    out = bar_whole_tables(table)
    inside = out[out.index("\\begin{longtable}"):out.index("\\end{longtable}")]
    check("a changed table loses its inner brackets",
          not REGION_MARK.search(inside))
    check("a changed table keeps its cell markup",
          "\\DIFdel{b}" in inside and "\\DIFadd{c}" in inside)
    check("a changed table is barred as a whole",
          "\\DIFmodbegin \\begin{longtable}" in out
          and "\\end{longtable}\\DIFmodend" in out)
    check("the bars around a changed table pair up", unpaired_bar(out) is None)
    check("an unpaired bar is found", unpaired_bar(table.replace(
        "\\DIFaddend \\toprule", "\\toprule")) is not None)
    plain = "\\begin{longtable}[]{ll}\n\\toprule\na & b \\\\\n\\end{longtable}\n"
    check("an unchanged table is left alone", bar_whole_tables(plain) == plain)
    merged = ADJACENT_BARS.sub("", "\\DIFdelbegin \\DIFdel{a}\\DIFdelend \\DIFaddbegin "
                                   "\\DIFadd{b}\\DIFaddend \n")
    check("touching bars are merged into one",
          merged == "\\DIFdelbegin \\DIFdel{a}\\DIFadd{b}\\DIFaddend \n")

    for name in cases:
        print(f"  {'FAIL' if name in failures else 'ok  '}  {name}")
    print(f"{len(cases) - len(failures)}/{len(cases)} passed")
    return 1 if failures else 0


def lint_diagram(svg):
    """Check a laid-out diagram for the defects this pipeline has produced before.

    Returns (errors, warnings), split by whether a human could ever want it:

    * An edge crossing a *node*, or lying entirely along another edge, is always
      a bug -- it silently deletes information, and every instance so far looked
      perfectly plausible in the rendered PDF. These fail the build.
    * A crossing, or two horizontals sharing a channel, is ugly but can be
      unavoidable -- a non-planar graph has to cross somewhere. These warn, and
      the count reaches the summary line so it cannot scroll past unseen.
    """
    boxes = {m.group(1): tuple(float(v) for v in m.groups()[1:])
             for m in SVG_NODE.finditer(svg)}
    edges = []
    for m in SVG_EDGE.finditer(svg):
        _, src, dst, pts, _ = m.groups()
        edges.append((src, dst,
                      [tuple(float(v) for v in q.split(",")) for q in pts.split()]))

    errors, warnings = [], []

    for src, dst, p in edges:
        for (x1, y1), (x2, y2) in zip(p, p[1:]):
            for node, (bx, by, bw, bh) in boxes.items():
                if node in (src, dst):
                    continue
                if (min(x1, x2) < bx + bw - 1 and max(x1, x2) > bx + 1
                        and min(y1, y2) < by + bh - 1 and max(y1, y2) > by + 1):
                    errors.append(f"{src}->{dst} passes through node {node}")

    for i, (a, b, p) in enumerate(edges):
        for c, d, q in edges[i + 1:]:
            if len(p) <= len(q) and all(pt in q for pt in p):
                errors.append(f"{a}->{b} lies entirely along {c}->{d}")
            for (ax1, ay1), (ax2, ay2) in zip(p, p[1:]):
                for (bx1, by1), (bx2, by2) in zip(q, q[1:]):
                    a_vert = abs(ax1 - ax2) < .01
                    b_vert = abs(bx1 - bx2) < .01
                    if a_vert != b_vert:
                        (vx, vy1, vy2), (hy, hx1, hx2) = (
                            ((ax1, ay1, ay2), (by1, bx1, bx2)) if a_vert
                            else ((bx1, by1, by2), (ay1, ax1, ax2)))
                        if (min(vy1, vy2) < hy < max(vy1, vy2)
                                and min(hx1, hx2) < vx < max(hx1, hx2)):
                            warnings.append(f"{a}->{b} crosses {c}->{d}")
                    elif not a_vert and abs(ay1 - by1) < .1:
                        lo = max(min(ax1, ax2), min(bx1, bx2))
                        hi = min(max(ax1, ax2), max(bx1, bx2))
                        if hi - lo > 1:
                            warnings.append(
                                f"{a}->{b} and {c}->{d} share {hi - lo:.0f}px of channel")

    return errors, warnings


def spread_ports(svg):
    """Give each of a node's outgoing edges its own exit, ordered by destination.

    The layout engine fans every out-edge from a single point on the node's face.
    That looks fine until `straighten_svg` drags one of them sideways onto its
    own column, at which point it can cut straight across a sibling.  Spreading
    the exits across the face in left-to-right destination order keeps each edge
    on its own side, so straightening cannot produce a crossing.
    """
    boxes = {m.group(1): tuple(float(v) for v in m.groups()[1:])
             for m in SVG_NODE.finditer(svg)}
    paths = {}
    for m in SVG_EDGE.finditer(svg):
        _, src, dst, pts, _ = m.groups()
        paths[(src, dst)] = [tuple(float(v) for v in q.split(",")) for q in pts.split()]

    outgoing = {}
    for key in paths:
        outgoing.setdefault(key[0], []).append(key)

    remap = {}
    for src, keys in outgoing.items():
        # Only when they really do all leave from one shared point.
        if len(keys) < 2 or src not in boxes:
            continue
        if len({tuple(round(c, 2) for c in paths[k][0]) for k in keys}) != 1:
            continue
        bx, _, bw, _ = boxes[src]
        for i, k in enumerate(sorted(keys, key=lambda k: paths[k][-1][0])):
            remap[k] = bx + bw * (i + 1) / (len(keys) + 1)

    def one(m):
        head, src, dst, pts, tail = m.groups()
        p, xe = paths[(src, dst)], remap.get((src, dst))
        if xe is not None:
            x0 = p[0][0]
            p = [(xe, y) if abs(x - x0) < .01 and i < len(p) - 1 else (x, y)
                 for i, (x, y) in enumerate(p)]
        return head + " ".join(f"{x},{y}" for x, y in p) + tail

    return SVG_EDGE.sub(one, svg)


def separate_channels(svg, clear=12.0):
    """Move a horizontal run off a channel another edge is already using.

    Two edges heading for the same node are routed along the same y by default,
    so their horizontal runs lie on top of each other and read as a single line
    -- worse than a crossing, because it hides that there are two edges at all.
    An edge with vertical slack is slid to its own channel instead.

    Only the y of a horizontal moves; every x stays put, so this cannot disturb
    the port ordering or straightening that ran before it.
    """
    boxes = {m.group(1): tuple(float(v) for v in m.groups()[1:])
             for m in SVG_NODE.finditer(svg)}
    paths = {}
    for m in SVG_EDGE.finditer(svg):
        _, s, d, pts, _ = m.groups()
        paths[(s, d)] = [tuple(float(v) for v in q.split(",")) for q in pts.split()]

    def horizontals(exclude):
        for k, p in paths.items():
            if k == exclude:
                continue
            for (x1, y1), (x2, y2) in zip(p, p[1:]):
                if abs(y1 - y2) < .01:
                    yield min(x1, x2), max(x1, x2), y1

    def usable(key, y, lo_x, hi_x):
        for bx, by, bw, bh in boxes.values():          # never cut a node
            if by - clear < y < by + bh + clear and bx < hi_x and bx + bw > lo_x:
                return False
        for ox1, ox2, oy in horizontals(key):          # nor share a channel
            if abs(oy - y) < clear and ox1 < hi_x and ox2 > lo_x:
                return False
        for k, p in paths.items():                     # nor cut a vertical
            if k == key:
                continue
            for (x1, y1), (x2, y2) in zip(p, p[1:]):
                if abs(x1 - x2) < .01 and lo_x < x1 < hi_x and min(y1, y2) < y < max(y1, y2):
                    return False
        return True

    for key, p in list(paths.items()):
        if len(p) != 4 or abs(p[1][1] - p[2][1]) > .01:
            continue
        lo_x, hi_x = sorted((p[1][0], p[2][0]))
        if usable(key, p[1][1], lo_x, hi_x):
            continue
        # Slide it towards the source, which is where the slack usually is.
        top, bottom = p[0][1], p[3][1]
        step = max(clear, (bottom - top) / 24)
        y = top + step
        while y < bottom - step:
            if usable(key, y, lo_x, hi_x):
                paths[key] = [p[0], (p[1][0], y), (p[2][0], y), p[3]]
                break
            y += step

    def one(m):
        head, s, d, _, tail = m.groups()
        return head + " ".join(f"{x},{y}" for x, y in paths[(s, d)]) + tail

    return SVG_EDGE.sub(one, svg)


def straighten_svg(svg, inset=4.0):
    """Collapse the dog-leg out of an edge when a straight line still lands on
    both of its nodes.

    The layout engine anchors an edge at its source node's centre and then routes
    orthogonally, so two nodes whose centres differ by a few pixels get a visible
    kink for no reason -- and a long edge can end up jogging around nothing.
    Sliding one endpoint along the face it already touches removes the kink
    without detaching the edge.

    Left alone when the slide would leave that face (the edge genuinely travels
    sideways) or when the straightened line would cross another node, so this can
    only ever remove a kink, never introduce a collision.
    """
    boxes = {m.group(1): tuple(float(v) for v in m.groups()[1:])
             for m in SVG_NODE.finditer(svg)}
    others = {}
    for m in SVG_EDGE.finditer(svg):
        _, s, d, pts, _ = m.groups()
        p = [tuple(float(v) for v in q.split(",")) for q in pts.split()]
        others[(s, d)] = list(zip(p, p[1:]))

    def on_face(node, x):
        bx, _, bw, _ = boxes[node]
        return bx + inset <= x <= bx + bw - inset

    def hits_a_node(x, y0, y1, exclude):
        lo, hi = sorted((y0, y1))
        return any(bx + inset < x < bx + bw - inset and lo < by + bh and hi > by
                   for n, (bx, by, bw, bh) in boxes.items()
                   if n not in exclude)

    def hits_an_edge(x, y0, y1, key):
        """Would a straight run down `x` cut across some other edge's horizontal?

        Straightening slides an edge onto its own column, which can drop it right
        through a neighbour that was previously routed around it.  A kink is a
        smaller blemish than a crossing, so when this fires the jog stays.
        """
        lo, hi = sorted((y0, y1))
        for other, segs in others.items():
            if other == key:
                continue
            for (ax, ay), (bx, by) in segs:
                if abs(ay - by) > .01:          # only horizontals can be crossed
                    continue
                if lo < ay < hi and min(ax, bx) < x < max(ax, bx):
                    return True
        return False

    def one(m):
        head, src, dst, pts, tail = m.groups()
        p = [tuple(float(v) for v in q.split(",")) for q in pts.split()]
        if len(p) == 4 and src in boxes and dst in boxes:
            (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p
            if abs(x0 - x1) < .01 and abs(y1 - y2) < .01 and abs(x2 - x3) < .01:
                for x in ([x3] if on_face(src, x3) else []) + \
                         ([x0] if on_face(dst, x0) else []):
                    if (not hits_a_node(x, y0, y3, {src, dst})
                            and not hits_an_edge(x, y0, y3, (src, dst))):
                        p = [(x, y0), (x, y3)]
                        break
        return head + " ".join(f"{x},{y}" for x, y in p) + tail

    return SVG_EDGE.sub(one, svg)


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


def render_mermaid(sources, figdir, warnings, by_content=False):
    """Render every mermaid source to a laid-out, checked, flattened SVG file.

    One node call for the whole document. Layout defects that are always bugs
    stop the build here rather than shipping a plausible-looking PDF.
    """
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
        # In diff mode the two versions render into one directory, named by
        # a hash of the source: an untouched diagram then lands on the same
        # path in both, so latexdiff sees no change rather than a swap.
        stem = figdir / (f"diagram-{hashlib.sha256(sources[i].encode()).hexdigest()[:12]}"
                         if by_content else f"diagram-{i + 1:02d}")
        svg_path = stem.with_suffix(".svg")
        laid_out = separate_channels(straighten_svg(spread_ports(svg)))
        errors, warned = lint_diagram(laid_out)
        svg_path.write_text(flatten_svg(laid_out))
        if errors:
            sys.exit(f"diagram {i + 1} is malformed:\n  " + "\n  ".join(errors)
                     + f"\n(written to {svg_path} for inspection)")
        for w in warned:
            print(f"diagram {i + 1}: {w}", file=sys.stderr)
        warnings.extend(warned)
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


# CULINECHBAR puts a `\cbstart`/`\cbend` pair around every marked *fragment*,
# and changebar is by far the most expensive thing in a diff build: neutralising
# it on a densely-changed 23-page proxy took the compile from 77s to 4s, while
# neutralising ulem's `\uwave`/`\sout` changed nothing measurable. Cost is
# linear in the number of bars (doubling the document doubled the time, 93s ->
# 189s -> 407s), so one lever is emitting fewer of them; the bigger one, not
# drawing them until the pagination has settled, is below.
#
# Moving the bars from each fragment to the region brackets that enclose them
# covers the same text with far fewer pairs, because adjacent fragments
# coalesce into one bar: 3152 bars -> 934, and 77s -> 15s on that proxy.
# `\DIFmodbegin`/`\DIFmodend` are included so that a changed code block, which
# latexdiff marks with neither `\DIFadd` nor `\DIFaddbegin`, still gets a bar.
REGION_BARS = [
    "  \\makeatletter",
    "  \\AtBeginDocument{%",
    "    \\renewcommand{\\DIFaddtex}[1]{{\\protect\\color{blue}\\uwave{#1}}}%",
    "    \\renewcommand{\\DIFdeltex}[1]{{\\protect\\color{red}\\sout{#1}}}%",
    "    \\renewcommand{\\DIFaddbegin}{\\cbstart{}}"
        "\\renewcommand{\\DIFaddend}{\\cbend{}}%",
    "    \\renewcommand{\\DIFdelbegin}{\\cbstart{}}"
        "\\renewcommand{\\DIFdelend}{\\cbend{}}%",
    "    \\renewcommand{\\DIFmodbegin}{\\cbstart{}}"
        "\\renewcommand{\\DIFmodend}{\\cbend{}}}",
    # Drawing the bars is ruinous while the page numbers are still moving: a
    # pass whose `.aux` disagrees with the pagination it produces takes 590s
    # on the 50-page set, against ~4s once the pagination has settled. So
    # `compile_tex` runs the first passes with `\kladdenobars` defined, which
    # turns the bars off and lets the pagination converge cheaply, then turns
    # them on for the rest. Bars are drawn in the margin and move no page
    # break, so what settles without them stays settled with them.
    #
    # `\ifdefined`, not `\@ifundefined`: pandoc only passes a backslash through
    # as raw LaTeX when letters follow it, so a line opening with `\@` comes out
    # as the text "@ifundefined" with its braces escaped.
    "  \\ifdefined\\kladdenobars",
    "    \\AtBeginDocument{\\let\\cbstart\\relax \\let\\cbend\\relax"
        " \\let\\cbdelete\\relax}",
    "  \\fi",
    "  \\makeatother",
]

# How many of those cheap bars-off passes to run before switching the bars on.
SETTLE_PASSES = 2


def preamble(title, subtitle=None, documentclass="report", toc=True,
             for_diff=False):
    """The pandoc YAML metadata block both modes share.

    Only the class and the front matter differ: the whole set is a `report`
    whose chapters are pages, a single page is an `article` with no table of
    contents -- one page rarely needs one, and it would land on the title page.
    """
    yaml_title = title.replace('"', '\\"')
    return [
        "---",
        f'title: "{yaml_title}"',
        *([f'subtitle: "{subtitle}"'] if subtitle else []),
        f"documentclass: {documentclass}",
        "papersize: a4",
        "geometry: margin=2.5cm",
        *(["toc: true", "toc-depth: 2"] if toc else []),
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
        # A class with a separate title page (`report`) ends `titlepage` with
        # `\\setcounter{page}\\@ne`, so the page after the title is page 1 again
        # and every printed folio thereafter trails its physical page by one.
        # That is not only the folios: the table of contents and every
        # cross-reference quote the same counter, so a reader who follows
        # "page 12" lands on physical page 13.
        #
        # Drop the reset and the count runs straight through. The title page
        # keeps its own `\\thispagestyle{empty}` from the start of the
        # environment, so it is counted without being numbered. Patching the
        # reset out rather than setting the counter to 2 after `\\maketitle`
        # also stays correct if the title ever runs to a second page.
        #
        # `article` leaves `\\@titlepagefalse` and sets its title inline, with
        # no reset to undo, so the single-page builds skip this.
        "  \\usepackage{etoolbox}",
        "  \\makeatletter",
        "  \\if@titlepage",
        "  \\patchcmd{\\endtitlepage}{\\setcounter{page}\\@ne}{}{}"
                "{\\PackageError{kladde}{cannot patch endtitlepage;"
                " page numbers would be off by one}{}}",
        "  \\fi",
        "  \\makeatother",
        *(REGION_BARS if for_diff else []),
        "fontsize: 10pt",
        "---",
        "",
    ]


def build_markdown(pages, problems, figdir=None, warnings=None,
                   subtitle="Specification and design documentation",
                   by_content=False, for_diff=False):
    chunks = preamble("Kladde", subtitle, for_diff=for_diff)
    # Two passes: every mermaid source in the document is collected first so
    # that node is started once rather than once per diagram.
    bodies = {}
    for rel, page in pages.items():
        body = expand_wikilinks(rel, page["body"], pages, problems)
        body = rewrite_links(rel, body, pages, problems)
        bodies[rel] = stamp_headings(body, page["prefix"])
    sources = [s for body in bodies.values() for s in collect_mermaid(body)]
    figures = iter(render_mermaid(sources, figdir, warnings if warnings is not None else [],
                                  by_content)
                   if sources and figdir else [])

    for rel, page in pages.items():
        body = substitute_mermaid(bodies[rel], figures)
        chunks.append(f"# {page['title']} {{#{page['prefix']}}}\n")
        chunks.append(size_tables(body if for_diff else wrap_diagrams(body)).strip())
        chunks.append("")
    return "\n".join(chunks), len(sources)


def build_standalone(text, rel, problems, figdir=None, warnings=None,
                     subtitle=None, fallback_title=None, by_content=False,
                     for_diff=False):
    """One page as its own document."""
    page = read_page(text, rel, fallback_title=fallback_title or Path(rel).stem)
    body = expand_wikilinks(rel, page["body"], {rel: page}, problems, standalone=True)
    body = rewrite_links(rel, body, {rel: page}, problems, standalone=True)
    body = stamp_headings(body, page["prefix"])
    sources = collect_mermaid(body)
    figures = iter(render_mermaid(sources, figdir, warnings if warnings is not None else [],
                                  by_content)
                   if sources and figdir else [])
    body = substitute_mermaid(body, figures)
    # No synthesised H1: the title comes from the metadata block, and pandoc
    # renders it properly rather than as a first section.
    chunks = preamble(page["title"], subtitle, documentclass="article", toc=False,
                      for_diff=for_diff)
    chunks.append(size_tables(body if for_diff else wrap_diagrams(body)).strip())
    chunks.append("")
    return "\n".join(chunks), len(sources)


# ---------------------------------------------------------------------------
# Diff mode
#
# Two rendered versions of a document, compared as LaTeX rather than as
# Markdown.  Diffing the sources would put `+`/`-` in column one, where the
# Markdown reader takes them for list bullets and the result stops being the
# document; and the raw diff loses the rendering that is the whole point.
# latexdiff instead compares two *typeset* documents and marks up the result,
# so a changed paragraph reads as a paragraph.
# ---------------------------------------------------------------------------

# `..` rather than `:` -- it is git's own range syntax (`git diff a..b`,
# `git log a..b`), whereas `a:b` already means "path b at rev a" to git.
DIFF_SEP = ".."

# Either side may name the index instead of a commit.
STAGED = ("staged", "index")

# Diff mode renders code blocks plainly: `--no-highlight` so pandoc emits
# `verbatim` rather than its fancyvrb `Highlighting`, and no `kladdediagram`
# wrapper around the box-drawing diagrams.
#
# latexdiff marks up a verbatim block by rewriting it as `DIFverbatim`, which
# it defines on top of `listings` so that its `%DIF <`/`%DIF >` line markers
# become strike-through and underline. That rewrite only happens for the
# environments it ships knowing about. Naming a fancyvrb environment in
# VERBATIMLINEENV instead has it keep the original name and pass
# `alsolanguage=DIFcode`, an `lstlisting` key that fancyvrb rejects; naming it
# in VERBATIMENV suppresses markup altogether, so a change confined to a code
# block silently renders as the new version with nothing marked.
#
# The cost is no syntax colouring in a diff, and the default leading for box
# diagrams. Both are worth a code change actually being visible, and colour
# competes with the red/blue markup anyway.
DIFF_PANDOC = ["--no-highlight"]

# Colour *and* a change bar in the margin, so a change is still findable on a
# grayscale print.  CULINECHBAR is the only style that adds the bar without
# taking something away: it is the default UNDERLINE markup -- blue wavy
# underline for an insertion, red strikeout for a deletion -- with
# `\cbstart`/`\cbend` around it.  CCHANGEBAR and CFONTCHBAR drop the
# underline and strikeout, which is exactly the cue that separates an
# insertion from a deletion once the colour is gone: both would print as
# black text beside an identical bar.
DIFF_MARKUP = "CULINECHBAR"

# latexdiff loads `changebar` with the pdftex driver whatever the engine, and
# the package refuses it outright under xelatex ("PDFTeX option cannot be
# used").
CHANGEBAR_DRIVER = "xetex"

# Inside a code block latexdiff marks each changed line with `%DIF >`/`%DIF <`
# and relies on its `listings` language to turn that into markup.  An added or
# removed *blank* line leaves the marker with nothing after it, which listings
# does not consume, so it prints as literal `%DIF >` in the PDF.  A blank line
# gained or lost is not worth reporting anyway, so drop those markers.
EMPTY_DIF_LINE = re.compile(r"^%DIF [<>][ \t]*\n", re.MULTILINE)

# When a list is deleted, latexdiff comments out its `\begin`/`\end` (as
# `%DIFDELCMD <`, which does not execute) but re-emits each deleted `\item` as
# live code tagged `%DIFAUXCMD`, so that the deleted text still renders as a
# list item.  If the surrounding list went away too, those items are left
# outside any list and the compile dies on "Lonely \item".
#
# Dropping such an item costs the bullet, not the content: the struck-through
# text after it still renders, as a paragraph rather than an item.  Restoring
# the list instead -- uncommenting the deleted `\begin{itemize}` -- would
# rebuild structure the new version does not have, around text that is by then
# interleaved with additions.
LIST_OR_ITEM = re.compile(
    r"\\(?P<delim>begin|end)\{(?:itemize|enumerate|description)\}|\\item\b")
UNESCAPED_COMMENT = re.compile(r"(?<!\\)%")


def executed(line):
    """The part of a line TeX runs: everything before an unescaped `%`."""
    m = UNESCAPED_COMMENT.search(line)
    return line if m is None else line[:m.start()]


# latexdiff replaces a deleted `}` with `\MBLOCKRIGHTBRACE` inside a
# `%DIFDELCMD <` comment, where it never executes, and emits a compensating `}`
# further on in the added branch.  Everything between the two is then inside
# the argument that the brace was supposed to close.  Usually that is merely
# wrong -- deleted prose set in the font of whatever command it landed in --
# but a paragraph break in there is fatal: `\texttt` and friends are not
# `\long`, so the compile dies on "Paragraph ended before \text@command was
# complete".
#
# `\MBLOCKRIGHTBRACE` is never defined, so it is pure residue.  Putting a live
# `}` back where latexdiff commented it out and dropping the compensating one
# leaves the brace count unchanged and returns the deleted text to the outside
# of the argument, which is where both versions of the document had it.
MBLOCK_BRACE = re.compile(r"%DIFDELCMD <\s*\\MBLOCKRIGHTBRACE")


def executed_braces(tex):
    """Yield (offset, brace) for every brace TeX acts on, skipping comments."""
    pos = 0
    for line in tex.splitlines(keepends=True):
        escaped = False
        for i, ch in enumerate(executed(line)):
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch in "{}":
                yield pos + i, ch
        pos += len(line)


def compensating_brace(tex, after):
    """Offset of the `}` closing the group left open at `after`, if any."""
    depth = 0
    for offset, ch in executed_braces(tex):
        if offset < after:
            continue
        if ch == "{":
            depth += 1
        elif depth:
            depth -= 1
        else:
            return offset
    return None


def restore_deleted_braces(tex):
    """Move each deleted closing brace back to where latexdiff commented it out."""
    at = 0
    while True:
        m = MBLOCK_BRACE.search(tex, at)
        if not m:
            return tex
        close = compensating_brace(tex, m.end())
        if close is None:
            # Nothing left open, so latexdiff balanced this one some other way.
            at = m.end()
            continue
        # Later offset first, so the earlier insertion does not shift it.
        tex = tex[:close] + tex[close + 1:]
        tex = tex[:m.start()] + "}" + tex[m.start():]
        at = m.end() + 1


# A deleted table row gets the same treatment as a deleted list: latexdiff
# comments out the row's `&`s and its `\\` but keeps the cell text live.  If
# the commented `\\` was the one that ended the row, that text is stranded
# between the previous row and `\bottomrule`, where only `\noalign` material is
# legal -- "Misplaced \noalign" again, but with real content this time rather
# than the region brackets `bar_whole_tables` removes.
#
# Giving the row its `\\` back makes it a row again.  Its `&`s are revived with
# it, but only within the span the recovered row covers: those are that row's
# own separators, so the cell count is the one the table was built for, whereas
# outside that span the deleted text merges into a surviving row whose cells
# are already accounted for and more would overrun the columns.
TABLE_OPEN = re.compile(r"\\begin\{(?:longtable|tabular[xy*]?)\}")
TABLE_CLOSE = re.compile(r"\\end\{(?:longtable|tabular[xy*]?)\}")
ROW_RULE = re.compile(
    r"\\(?:bottomrule|midrule|toprule|endhead|endfirsthead|endfoot|endlastfoot)\b")
TABLE_TOKEN = re.compile(
    r"\\\\|\\begin\{(?:longtable|tabular[xy*]?)\}"
    r"|\\end\{(?:longtable|tabular[xy*]?)\}"
    r"|\\(?:bottomrule|midrule|toprule|endhead|endfirsthead|endfoot|endlastfoot)\b"
    r"|\S")
DEAD_TAB = re.compile(r"%DIFDELCMD <[ \t]*&")


def revive_tabs(text):
    """`%DIFDELCMD < &` -> `&%DIFDELCMD <`, leaving the comment itself intact."""
    return DEAD_TAB.sub(lambda m: "&" + m.group(0)[: m.group(0).index("&")], text)


def close_dangling_rows(tex):
    """Give back the `\\\\` to a deleted table row latexdiff left without one."""
    out, depth, pending, row_start = [], 0, False, 0
    for line in tex.splitlines(keepends=True):
        insert_at = None
        for tok in TABLE_TOKEN.finditer(executed(line)):
            token = tok.group(0)
            if TABLE_OPEN.match(token):
                depth, pending, row_start = depth + 1, False, len(out)
            elif TABLE_CLOSE.match(token) or ROW_RULE.match(token):
                if depth and pending and insert_at is None:
                    insert_at = tok.start()
                    for i in range(row_start, len(out)):
                        out[i] = revive_tabs(out[i])
                if TABLE_CLOSE.match(token):
                    depth = max(depth - 1, 0)
                pending, row_start = False, len(out)
            elif token == "\\\\":
                pending, row_start = False, len(out)
            elif depth:
                pending = True
        if insert_at is not None:
            line = revive_tabs(line[:insert_at]) + "\\\\ " + line[insert_at:]
        out.append(line)
    return "".join(out)


def drop_orphan_items(tex):
    """Remove the `\\item`s latexdiff leaves outside any list."""
    out, depth = [], 0
    for line in tex.splitlines(keepends=True):
        cuts = []
        for m in LIST_OR_ITEM.finditer(executed(line)):
            if m.group("delim") == "begin":
                depth += 1
            elif m.group("delim") == "end":
                depth = max(depth - 1, 0)
            elif depth == 0 and "%DIFAUXCMD" in line:
                # Only latexdiff's own; a stray `\item` in the source is the
                # document's problem and should still be reported as one.
                cuts.append(m.span())
        for start, end in reversed(cuts):
            line = line[:start] + line[end:]
        out.append(line)
    return "".join(out)


# latexdiff brackets each changed region with `\DIFaddbegin`/`\DIFaddend` (and
# the `del` and `mod` pairs), which REGION_BARS turns into the change bars.
# Inside a table those brackets are useless and harmful at once.  Useless,
# because changebar draws nothing from inside a table cell.  Harmful, because
# between a row's `\\` and the rule after it only `\noalign` material is legal,
# so a bracket there fails the compile with "Misplaced \noalign" -- and
# deleting only that one leaves its partner, typically a `\DIFaddbegin` just
# before a changed `\begin{longtable}`, opening a bar that nothing closes and
# that runs to the end of the document.
#
# So every bracket inside a table goes, and a table that changed at all gets
# one bar around the whole of it instead.  The `\DIFadd{...}`/`\DIFdel{...}`
# markup is left alone, so the changed cells are still underlined and struck
# through.  A pair that straddles the table's edge is closed just before the
# table or reopened just after it, so the brackets outside still pair up.
#
# Suppressing table diffing wholesale via PICTUREENV was the alternative, and
# is worse: a `longtable` cannot sit inside the `\DIFadd{...}` that carries the
# colour, so a changed table would render unmarked.
REGION_MARK = re.compile(r"\\(?P<name>DIF(?:add|del|mod)(?P<edge>begin|end))\b[ \t]*")
TABLE_MARK = re.compile(
    f"(?P<open>{TABLE_OPEN.pattern})|(?P<close>{TABLE_CLOSE.pattern})"
    f"|{REGION_MARK.pattern}")


def bar_whole_tables(tex):
    """Swap the region brackets inside each table for one bar around it."""
    edits, depth, pos = [], 0, 0             # edits: (offset, length, insertion)
    for line in tex.splitlines(keepends=True):
        for m in TABLE_MARK.finditer(executed(line)):
            at = pos + m.start()
            if m.group("open"):
                if depth == 0:
                    opened, strips, still_open, closed_early = at, [], [], []
                depth += 1
            elif m.group("close"):
                if depth == 0:
                    continue
                depth -= 1
                if depth or "DIF" not in tex[opened:pos + m.end()]:
                    continue
                edits += strips
                edits.append((opened, 0, "".join(
                    f"\\{name} " for name in closed_early) + "\\DIFmodbegin "))
                edits.append((pos + m.end(), 0, "\\DIFmodend" + "".join(
                    f" \\{name}" for name in still_open)))
            elif depth:
                strips.append((at, len(m.group(0)), ""))
                if m.group("edge") == "begin":
                    still_open.append(m.group("name"))
                elif still_open:
                    still_open.pop()
                else:
                    closed_early.append(m.group("name"))
        pos += len(line)
    for offset, length, insertion in sorted(edits, reverse=True):
        tex = tex[:offset] + insertion + tex[offset + length:]
    return tex


# latexdiff closes one region and opens the next back to back all the time --
# `\DIFdelend \DIFaddbegin` for every replaced phrase.  In running text
# changebar records a `\cbend` only at the end of the line, via `\vadjust`, but
# a `\cbstart` at once, so the pair is recorded the wrong way round.  That is
# harmless on its own, but when the bar opened there is still open at a page
# break, changebar continues the wrong one onto the next page -- one that has
# already ended -- and the lines at the top of that page go unbarred.
#
# The two bars would touch anyway, so merging them into one loses nothing, and
# there is then no pair to misorder.
ADJACENT_BARS = re.compile(
    r"\\DIF(?:add|del|mod)end\s*\\DIF(?:add|del|mod)begin\b[ \t]*")


def unpaired_bar(tex):
    """The line of the first change bar that does not pair up, or None."""
    start = tex.find("\\begin{document}")     # the preamble *defines* them
    first = tex.count("\n", 0, max(start, 0)) + 1
    opened = []
    for n, line in enumerate(tex[max(start, 0):].splitlines(), first):
        for m in REGION_MARK.finditer(executed(line)):
            if m.group("edge") == "begin":
                opened.append(n)
            elif opened:
                opened.pop()
            else:
                return n
    return opened[0] if opened else None


def parse_diff_spec(spec):
    """`old..new` -> (old, new); `old` -> (old, None), None meaning the working tree."""
    old, sep, new = spec.partition(DIFF_SEP)
    if not old:
        sys.exit(f"--diff needs an old ref: {spec!r}")
    if sep and not new:
        sys.exit(f"--diff {spec!r}: no new ref after '{DIFF_SEP}' "
                 f"(drop it to compare against the working tree)")
    return old, (new if sep else None)


def repo_root(path):
    proc = subprocess.run(
        ["git", "-C", str(path.parent.resolve()), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"not inside a git repository: {path}")
    return Path(proc.stdout.strip())


def version_at(repo, rel, ref):
    """The text of `rel` at `ref`, or None if it did not exist there.

    `ref` is None for the working tree, "staged"/"index" for the index, and
    anything else is handed to git as a revision.
    """
    if ref is None:
        path = repo / rel
        return path.read_text() if path.is_file() else None
    target = f":{rel}" if ref in STAGED else f"{ref}:{rel}"
    proc = subprocess.run(["git", "-C", str(repo), "show", target],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def describe(ref):
    return "the working tree" if ref is None else (
        "the index" if ref in STAGED else f"`{ref}`")


def check_ref(repo, ref):
    """Fail early on a typo, rather than after rendering half the document."""
    if ref is None or ref in STAGED:
        return
    if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                       f"{ref}^{{commit}}"], capture_output=True).returncode != 0:
        sys.exit(f"not a git revision: {ref}")


def to_latex(md_text, tex_path, extra=()):
    """Render one version to standalone LaTeX, for latexdiff to compare."""
    if not shutil.which("pandoc"):
        sys.exit("pandoc not found; needed for --diff")
    md_path = tex_path.with_suffix(".md")
    md_path.write_text(md_text)
    subprocess.run(
        ["pandoc", str(md_path), "-o", str(tex_path), "--standalone", "--to", "latex",
         "--from", "markdown+pipe_tables+backtick_code_blocks+tex_math_dollars"
                   "+header_attributes+fenced_code_attributes+raw_attribute",
         "--highlight-style=tango", *extra],
        check=True)
    return tex_path


def latexdiff(old_tex, new_tex, out_tex):
    if not shutil.which("latexdiff"):
        sys.exit("latexdiff not found; install it (apt: latexdiff) to use --diff")
    proc = subprocess.run(
        ["latexdiff", f"--type={DIFF_MARKUP}", f"--driver={CHANGEBAR_DRIVER}",
         str(old_tex), str(new_tex)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"latexdiff failed:\n{proc.stderr}")
    tex = proc.stdout
    # REGION_BARS renews these, which fails on a macro that does not exist, and
    # `bar_whole_tables` moves them about -- safe only while they are
    # latexdiff's own empty brackets rather than something that prints. Both
    # assumptions hold exactly as long as this line does.
    for command in ("DIFaddbegin", "DIFaddend", "DIFdelbegin", "DIFdelend",
                    "DIFmodbegin", "DIFmodend"):
        if f"\\providecommand{{\\{command}}}{{}}" not in tex:
            sys.exit(f"latexdiff no longer defines \\{command} as empty; "
                     "REGION_BARS and bar_whole_tables both assume it does")
    # `bar_whole_tables` first: it strips the brackets that would otherwise
    # look like stranded row content to `close_dangling_rows`.
    tex = restore_deleted_braces(drop_orphan_items(close_dangling_rows(
        bar_whole_tables(EMPTY_DIF_LINE.sub("", tex)))))
    body = tex.index("\\begin{document}")        # the preamble *defines* them
    tex = tex[:body] + ADJACENT_BARS.sub("", tex[body:])
    out_tex.write_text(tex)
    # A bar left open is not an error to TeX: it silently runs to the end of
    # the document, over every page after it.
    line = unpaired_bar(tex)
    if line is not None:
        sys.exit(f"the change bars in the diff do not pair up, starting at "
                 f"{out_tex}:{line}; rerun with --keep-markdown to keep that file")
    return out_tex


# After the settle passes, changebar still needs one pass to record each bar's
# position and another to read it back and draw it, so the earliest the output
# can be stable is the second bars-on pass.
#
# The stopping test is that the `.cb` and `.aux` came out the same twice, *not*
# changebar's own "Rerun to get the bars right".  Measured on a 25-page diff,
# the bar positions are stable from the third pass on and that message still
# appears on every pass to the sixth -- believing it means paying for passes
# whose output stopped changing.
MAX_PASSES = SETTLE_PASSES + 4


def compile_tex(tex_path, out_path):
    """xelatex until the cross-references, the toc, and the change bars settle."""
    if not shutil.which("xelatex"):
        sys.exit("xelatex not found")
    workdir = tex_path.parent
    # Both, not just the bars: the bars can only be stable once the pagination
    # they are placed against is, and that lives in the `.aux`.
    settling = (tex_path.with_suffix(".cb"), tex_path.with_suffix(".aux"))
    previous = None
    for pass_no in range(1, MAX_PASSES + 1):
        # `-jobname` so the bars-off passes share the `.aux` and `.toc` with
        # the bars-on ones; `\def` before `\input` is how the flag gets in.
        bars_off = pass_no <= SETTLE_PASSES
        source = ([f"-jobname={tex_path.stem}",
                   f"\\def\\kladdenobars{{}}\\input{{{tex_path.name}}}"]
                  if bars_off else [str(tex_path)])
        proc = subprocess.run(
            ["xelatex", "-interaction=nonstopmode", "-halt-on-error",
             f"-output-directory={workdir}", *source],
            capture_output=True, text=True, cwd=workdir)
        if proc.returncode != 0:
            log = tex_path.with_suffix(".log")
            errors = [l for l in proc.stdout.splitlines() if l.startswith("!")]
            sys.exit(f"xelatex failed on the diff:\n  "
                     + "\n  ".join(errors[:5] or ["(see the log)"])
                     + f"\nits input is at {tex_path}, its log at {log}")
        settled = tuple(p.read_bytes() if p.is_file() else b"" for p in settling)
        # Only once the bars are actually being drawn does agreeing twice mean
        # anything; the bars-off passes always agree about having no bars.
        if pass_no > SETTLE_PASSES + 1 and settled == previous:
            break
        previous = settled
    else:
        print(f"warning: the diff had not settled after {MAX_PASSES} passes; "
              "some change bars or page references may be off", file=sys.stderr)
    _shutil.copyfile(tex_path.with_suffix(".pdf"), out_path)


@contextlib.contextmanager
def diff_workdir(out_path, keep):
    """Where the two renders and the merged .tex live."""
    if keep:
        path = out_path.with_name(out_path.stem + "-build")
        path.mkdir(parents=True, exist_ok=True)
        yield path
        print(f"kept the diff sources in {path}", file=sys.stderr)
    else:
        with tempfile.TemporaryDirectory(prefix="kladde-diff-") as tmp:
            yield Path(tmp)


def render_diff(build, old_ref, new_ref, out_path, args, workdir):
    """`build(ref, figdir) -> (markdown, diagrams)` for one side; diff the two."""
    figdir = workdir / "figures"
    texts = {}
    for side, ref in (("old", old_ref), ("new", new_ref)):
        texts[side], _ = build(ref, None if args.check_only else figdir)
    if args.check_only:
        return
    # Both sides get the same `--shift-heading-level-by`, so the preamble
    # latexdiff takes from the new side is the one we want.
    extra = (["--shift-heading-level-by=-1"] if args.pages
             else ["--top-level-division=chapter"]) + DIFF_PANDOC
    old_tex = to_latex(texts["old"], workdir / "old.tex", extra)
    new_tex = to_latex(texts["new"], workdir / "new.tex", extra)
    compile_tex(latexdiff(old_tex, new_tex, workdir / "diff.tex"), out_path)
    print(f"wrote {out_path}", file=sys.stderr)


def run_pandoc(md_path, out_path, extra=()):
    if not shutil.which("pandoc"):
        sys.exit("pandoc not found; install it or use --check-only")
    cmd = [
        "pandoc", str(md_path), "-o", str(out_path),
        "--from", "markdown+pipe_tables+backtick_code_blocks+tex_math_dollars"
                  "+header_attributes+fenced_code_attributes+raw_attribute",
        "--pdf-engine", "xelatex",
        "--highlight-style=tango",
        "-V", "linkcolor=RoyalBlue",
        *extra,
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        sys.exit(f"pandoc failed; its input is at {md_path}")


def render_one_diff(path, out_path, args):
    """One page, as the difference between two of its versions."""
    old_ref, new_ref = parse_diff_spec(args.diff)
    repo = repo_root(path)
    for ref in (old_ref, new_ref):
        check_ref(repo, ref)
    git_rel = path.resolve().relative_to(repo).as_posix()
    doc_rel = (path.resolve().relative_to(CONTENT).as_posix()
               if path.resolve().is_relative_to(CONTENT) else path.name)
    subtitle = f"changes from {describe(old_ref)} to {describe(new_ref)}"
    problems, warnings = [], []

    def build(ref, figdir):
        text = version_at(repo, git_rel, ref)
        if text is None:
            sys.exit(f"{git_rel} does not exist at {describe(ref)}")
        return build_standalone(text, doc_rel, problems, figdir, warnings,
                                subtitle=subtitle, fallback_title=path.stem,
                                by_content=True, for_diff=True)

    with diff_workdir(out_path, args.keep_markdown) as workdir:
        render_diff(build, old_ref, new_ref, out_path, args, workdir)
    report(f"{path} ({subtitle})", problems, warnings)
    return len(problems)


def report(what, problems, warnings):
    for pr in dict.fromkeys(problems):                  # both sides raise the same ones
        print(f"link: {pr}", file=sys.stderr)
    print(f"{what}: {len(warnings)} layout warning(s), "
          f"{len(dict.fromkeys(problems))} link problem(s)", file=sys.stderr)


def render_one(path, out_path, args):
    """Build a single page into `out_path`, returning its problem count."""
    if args.diff:
        return render_one_diff(path, out_path, args)
    # `<stem>.pandoc.md` rather than `<stem>.md`: the output lands in the
    # working directory, so the obvious name is the *input file itself* when
    # you render a page from the directory it lives in -- which would write
    # over the source and then delete it.
    md_path = out_path.with_name(out_path.stem + ".pandoc.md")
    if md_path.resolve() == path.resolve():
        sys.exit(f"the intermediate would overwrite {path}; pass -o elsewhere")
    rel = (path.resolve().relative_to(CONTENT).as_posix()
           if path.resolve().is_relative_to(CONTENT) else path.name)
    problems, warnings = [], []
    figdir = out_path.with_suffix("").with_name(out_path.stem + "-figures")
    merged, diagrams = build_standalone(
        path.read_text(), rel, problems, None if args.check_only else figdir,
        warnings, fallback_title=path.stem)

    for pr in problems:
        print(f"link: {pr}", file=sys.stderr)
    print(f"{path}: {len(merged.splitlines())} lines, {diagrams} diagram(s), "
          f"{len(warnings)} layout warning(s), {len(problems)} link problem(s)",
          file=sys.stderr)
    if args.check_only:
        return len(problems)

    md_path.write_text(merged)
    # Pages have no H1 of their own, so their `##` sections would come out as
    # subsections.  Promoting by one makes them sections of the article.
    run_pandoc(md_path, out_path, ["--shift-heading-level-by=-1"])
    if not args.keep_markdown:
        md_path.unlink()
        _shutil.rmtree(figdir, ignore_errors=True)
    print(f"wrote {out_path}", file=sys.stderr)
    return len(problems)


def render_pages(args):
    """`make-pdf.py a.md b.md` -- one PDF per page, in the working directory."""
    paths, outputs = [], {}
    for name in args.pages:
        path = Path(name)
        if not path.is_file():
            sys.exit(f"no such file: {name}")
        if path.suffix != ".md":
            sys.exit(f"not a markdown file: {name}")
        out = Path.cwd() / (path.stem + ("-diff.pdf" if args.diff else ".pdf"))
        if out in outputs:
            # `content/` has an index.md per section, so this is easy to hit.
            sys.exit(f"{name} and {outputs[out]} would both write {out.name}; "
                     "rename one or render them separately")
        outputs[out] = name
        paths.append((path, out))

    if args.output is not None:
        if len(paths) > 1:
            sys.exit("-o takes a single output path; drop it to write one PDF per page")
        paths = [(paths[0][0], Path(args.output))]

    if args.check_examples:
        checker = Path(__file__).resolve().parent / "check-examples.py"
        if subprocess.run([sys.executable, str(checker)]).returncode != 0:
            return 1

    return 1 if sum(render_one(path, out, args) for path, out in paths) else 0


UNCHANGED_NOTE = "*Unchanged between these two versions; body omitted.*"


def heading_skeleton(text):
    """A page reduced to its front matter and headings, body dropped.

    Used for pages that read the same at both refs. Their headings stay so
    that anchors elsewhere in the document still resolve and the contents
    page still lists them; everything else is weight the diff does not need.
    """
    if text is None:
        return None
    head = FRONT_MATTER.match(text)
    out = [text[: head.end()].rstrip("\n")] if head else []
    out += ["", UNCHANGED_NOTE]
    body, in_fence = text[head.end():] if head else text, False
    for line in body.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and HEADING.match(line):
            out += ["", line]
    return "\n".join(out) + "\n"


def render_whole_diff(args):
    """The whole of content/, as the difference between two of its versions."""
    old_ref, new_ref = parse_diff_spec(args.diff)
    repo = repo_root(CONTENT)
    for ref in (old_ref, new_ref):
        check_ref(repo, ref)
    prefix = CONTENT.resolve().relative_to(repo).as_posix()
    out_path = Path(args.output or (ROOT / "kladde-diff.pdf"))
    subtitle = f"changes from {describe(old_ref)} to {describe(new_ref)}"
    problems, warnings = [], []

    def read(rel, ref):
        return (version_at(repo, f"{prefix}/{rel}", ref) if ref is not None
                else version_at(repo, f"{prefix}/{rel}", None))

    # A page that reads the same at both refs contributes nothing to the diff
    # but still costs its share of the typesetting, so it is reduced to its
    # headings.  That keeps every `#anchor` a cross-reference might target,
    # and keeps the contents page honest about what the document contains.
    untouched = {rel for rel in ORDER if read(rel, old_ref) == read(rel, new_ref)}

    def build(ref, figdir):
        # A page missing at the old ref is simply absent from that side, so it
        # shows up as wholly added.  One deleted since is the mirror image, and
        # does not appear at all -- ORDER is the current reading order.
        def source(rel):
            text = read(rel, ref)
            return heading_skeleton(text) if rel in untouched else text

        pages = collect(ORDER, source)
        return build_markdown(pages, problems, figdir, warnings,
                              subtitle=subtitle, by_content=True, for_diff=True)

    with diff_workdir(out_path, args.keep_markdown) as workdir:
        render_diff(build, old_ref, new_ref, out_path, args, workdir)
    report(f"content/ ({subtitle})", problems, warnings)
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pages", nargs="*", metavar="PAGE.md",
                    help="render each of these on its own, to <basename>.pdf in "
                         "the working directory, instead of building the whole set")
    ap.add_argument("-o", "--output", default=None,
                    help=f"output path (default: {ROOT / 'kladde.pdf'}); "
                         "with a single PAGE.md, where that page goes")
    ap.add_argument("--keep-markdown", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="check the layout passes against synthetic diagrams and exit")
    ap.add_argument("--check-only", action="store_true",
                    help="report link problems and exit without running pandoc")
    ap.add_argument("--check-examples", action="store_true",
                    help="also compile the marked Rust examples (needs cargo)")
    ap.add_argument("--diff", metavar="OLD[..NEW]",
                    help="render the change between two versions instead of one "
                         "version: `<name>-diff.pdf` per page, or kladde-diff.pdf "
                         "for the whole set. NEW defaults to the working tree; "
                         "either ref may be `staged` for the index")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    if args.pages:
        sys.exit(render_pages(args))

    on_disk = {p.relative_to(CONTENT).as_posix() for p in CONTENT.rglob("*.md")}
    missing = on_disk - set(ORDER)
    if missing:
        sys.exit("not listed in ORDER: " + ", ".join(sorted(missing)))

    # Before the default below, so that `render_whole_diff` still sees `None`
    # and can pick `kladde-diff.pdf` instead of overwriting `kladde.pdf`.
    if args.diff:
        sys.exit(render_whole_diff(args))

    args.output = args.output or str(ROOT / "kladde.pdf")
    problems = []
    pages = collect(ORDER)
    figdir = Path(args.output).with_suffix("").parent / (
        Path(args.output).stem + "-figures")
    warnings = []
    merged, diagrams = build_markdown(
        pages, problems, None if args.check_only else figdir, warnings)

    for p in problems:
        print(f"link: {p}", file=sys.stderr)
    print(f"{len(pages)} pages, {len(merged.splitlines())} lines, "
          f"{diagrams} diagram(s), {len(warnings)} layout warning(s), "
          f"{len(problems)} link problem(s)", file=sys.stderr)

    if args.check_examples:
        # Deliberately opt-in rather than part of every build: it needs a cargo
        # toolchain and a checkout of the Rust workspace, neither of which a
        # docs-only build has any other reason to want.
        checker = Path(__file__).resolve().parent / "check-examples.py"
        if subprocess.run([sys.executable, str(checker)]).returncode != 0:
            sys.exit("example check failed")

    if args.check_only:
        return

    # The merged Markdown pandoc actually consumes.  Removed on success,
    # but deliberately left behind when pandoc fails -- it is the input you
    # need to look at to work out why.
    md_path = Path(args.output).with_suffix(".md")
    md_path.write_text(merged)

    run_pandoc(md_path, args.output, ["--top-level-division=chapter"])
    if not args.keep_markdown:
        md_path.unlink()
        _shutil.rmtree(figdir, ignore_errors=True)
    print(f"wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
