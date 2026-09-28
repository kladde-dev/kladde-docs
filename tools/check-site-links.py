#!/usr/bin/env python3
"""Check every internal link of the built website, anchors included.

Reads the HTML that site/build.sh wrote, and reports each `href` or `src`
that leads nowhere: a path that is no file of the site, or a `#fragment`
that is no `id` on the page it points into.  External links are not
followed.  Exits non-zero if anything is broken, so CI can gate on it.

make-pdf.py --check-only checks the links of the markdown; this checks what
Quartz made of them, which is where a link-resolution setting can go wrong.

Usage:  tools/check-site-links.py [public]
"""

import argparse
import html
import re
import sys
import urllib.parse
from pathlib import Path

LINK = re.compile(r'\s(?:href|src)="([^"]*)"')
ID = re.compile(r'\sid="([^"]+)"')
EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z+.-]*:|//)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site", nargs="?", default="public", type=Path,
                    help="the built site (default: public)")
    root = ap.parse_args().site.resolve()
    if not root.is_dir():
        sys.exit(f"{root}: no such directory; run site/build.sh first")

    ids = {}

    def page_ids(page):
        if page not in ids:
            ids[page] = set(ID.findall(page.read_text(errors="replace")))
        return ids[page]

    def resolve(page, path):
        """The file a link's path leads to, as the site is served, or None."""
        base = root if path.startswith("/") else page.parent
        target = (base / urllib.parse.unquote(path.lstrip("/"))).resolve()
        if not target.is_relative_to(root):
            return None
        for candidate in (target, Path(f"{target}.html"), target / "index.html"):
            if candidate.is_file():
                return candidate
        return None

    pages = sorted(root.rglob("*.html"))
    broken, links = [], 0
    for page in pages:
        for href in LINK.findall(page.read_text(errors="replace")):
            href = html.unescape(href)
            if not href or EXTERNAL.match(href):
                continue
            links += 1
            path, _, fragment = href.partition("#")
            path = path.partition("?")[0]
            target = resolve(page, path) if path else page
            if target is None:
                broken.append((page, href, "no such file"))
            elif fragment and target.suffix == ".html" and \
                    urllib.parse.unquote(fragment) not in page_ids(target):
                broken.append((page, href, "no such anchor"))

    for page, href, why in broken:
        print(f"{page.relative_to(root)}: {href} ({why})")
    print(f"{len(pages)} pages, {links} internal links, {len(broken)} broken")
    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
