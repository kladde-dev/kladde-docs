#!/usr/bin/env python3
"""Compile (and where possible run) the worked examples in content/.

A Rust code block is checked only if an HTML comment marks it:

    <!-- kladde-example: name=journal file=src/main.rs mode=run deps=kladde,kladde-types -->
    ```rust
    fn main() { ... }
    ```

The marker has to sit *outside* the fence.  Pandoc will not open a fenced block
on a multi-word bare info string at all -- ```` ```rust name=x ```` parses as a
paragraph of inline code -- so an info-string attribute would silently wreck the
block in the PDF.  An HTML comment is a `RawBlock` that pandoc's LaTeX writer
drops and that HTML renders invisibly, so it costs neither renderer anything.

Blocks sharing a `name` become one crate; blocks sharing `name` *and* `file`
are concatenated in reading order (`pages.ORDER`, then line number), so a
tutorial can build a program up across several blocks and pages.

Lines that must exist for the example to compile but should not be shown go in
the marker, not the fence -- again because the marker is already invisible to
both renderers, whereas rustdoc's `# ` prefix inside the fence would need a
Quartz transformer *and* a pandoc pass that agree with each other:

    <!-- kladde-example: name=notes file=src/main.rs
    before:
      use kladde::Kladde;
      fn main() {
    after:
      }
    -->

Attributes:

    name=          required; groups blocks into one generated crate
    file=          path within that crate; default src/main.rs
    mode=          build (default) | run | compile_fail
    deps=          comma-separated dependencies, each optionally with
                   features: kladde-types[serde].  A name under the
                   workspace's crates/ becomes a path dependency; any other
                   name is looked up in its [workspace.dependencies], so an
                   example can only use what the workspace itself uses.

Usage:  tools/check-examples.py [--workspace ../kladde-rs] [--list] [-v]

Needs: a cargo toolchain and a checkout of the Rust workspace next door.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pages import ORDER, ROOT, CONTENT  # noqa: E402

# Generated crates and the shared target directory live here.  A stable path
# rather than a temp dir, and files are rewritten only when their content
# changes, so cargo's fingerprints survive between runs -- the difference
# between a second and a minute once there are a handful of examples.
WORKDIR = ROOT / ".examples"

MARKER_OPEN = re.compile(r"^\s*<!--\s*kladde-example:")
FENCE = re.compile(r"^\s*(```|~~~)\s*(\S*)\s*$")
ATTR = re.compile(r"([a-z_]+)=(\S+)")
DEP = re.compile(r"([A-Za-z0-9_-]+)(?:\[([^\]]*)\])?")
MODES = ("build", "run", "compile_fail")

# Snippet-shaped code defines things nothing else calls and binds values it
# never reads.  `unused_imports` is deliberately *not* allowed: a `use` line in
# a tutorial that the example does not need is a documentation bug.
PREAMBLE = "#![allow(dead_code, unused_variables)]"


class Block:
    def __init__(self, attrs, before, after, code, rel, line, page_index):
        self.name = attrs["name"]
        self.file = attrs.get("file", "src/main.rs")
        self.mode = attrs.get("mode")
        self.deps = attrs.get("deps")
        self.before, self.after, self.code = before, after, code
        self.rel, self.line, self.page_index = rel, line, page_index

    @property
    def order(self):
        return (self.page_index, self.line)

    def where(self):
        return f"content/{self.rel}:{self.line}"


def parse_deps(spec, where, problems):
    """`kladde,kladde-types[serde]` -> {"kladde": set(), "kladde-types": {"serde"}}."""
    out = {}
    for m in DEP.finditer(spec):
        feats = {f for f in (m.group(2) or "").split(",") if f}
        out.setdefault(m.group(1), set()).update(feats)
    if not out:
        problems.append(f"{where}: could not parse deps={spec!r}")
    return out


def parse_marker(text, where, problems):
    """Split a marker comment's body into its attributes and hidden sections."""
    body = text.strip()
    body = body[body.index("kladde-example:") + len("kladde-example:"):]
    body = body[: body.rindex("-->")] if "-->" in body else body
    head, _, rest = body.partition("\n")

    attrs = dict(ATTR.findall(head))
    leftover = ATTR.sub("", head).strip()
    if leftover:
        problems.append(f"{where}: unparsed text in marker: {leftover!r}")
    unknown = set(attrs) - {"name", "file", "mode", "deps"}
    if unknown:
        problems.append(f"{where}: unknown attribute(s): {', '.join(sorted(unknown))}")
    if "name" not in attrs:
        problems.append(f"{where}: marker has no name=")
        return None, [], []
    if attrs.get("mode") and attrs["mode"] not in MODES:
        problems.append(f"{where}: mode={attrs['mode']}, expected one of {', '.join(MODES)}")
        attrs["mode"] = None

    # `before:`/`after:` sections, dedented as a unit so the marker can be
    # indented to read as a block without that indentation reaching rustc.
    sections, current = {"before": [], "after": []}, None
    for line in rest.splitlines():
        key = line.strip().rstrip(":")
        if line.strip() in ("before:", "after:"):
            current = key
        elif current is None:
            if line.strip():
                problems.append(f"{where}: text before any `before:`/`after:`: {line.strip()!r}")
        else:
            sections[current].append(line)
    dedent = {k: textwrap.dedent("\n".join(v)).splitlines() for k, v in sections.items()}
    return attrs, dedent["before"], dedent["after"]


def scan(rel, page_index, problems):
    """Every marked block on one page, with markdown line numbers."""
    # Line numbers stay absolute -- front matter is counted, not stripped, so
    # they match what an editor shows.
    lines = (CONTENT / rel).read_text().splitlines()
    blocks, i = [], 0
    while i < len(lines):
        if not MARKER_OPEN.match(lines[i]):
            i += 1
            continue
        start = i
        where = f"content/{rel}:{i + 1}"
        while i < len(lines) and "-->" not in lines[i]:
            i += 1
        if i == len(lines):
            problems.append(f"{where}: marker comment is never closed")
            break
        marker = "\n".join(lines[start:i + 1])
        i += 1
        attrs, before, after = parse_marker(marker, where, problems)

        while i < len(lines) and not lines[i].strip():
            i += 1
        m = FENCE.match(lines[i]) if i < len(lines) else None
        if not m:
            problems.append(f"{where}: marker is not followed by a code block")
            continue
        if m.group(2) != "rust":
            problems.append(f"{where}: marked block is `{m.group(2) or 'plain'}`, not `rust`")
        i += 1
        first, code = i + 1, []
        while i < len(lines) and not FENCE.match(lines[i]):
            code.append(lines[i])
            i += 1
        i += 1
        if attrs:
            blocks.append(Block(attrs, before, after, code, rel, first, page_index))
    return blocks


class _Synthetic:
    """A block that came from no markdown, for a generated crate root."""

    def __init__(self, code):
        self.before, self.after, self.code = [], [], code
        self.rel, self.line, self.order = None, 0, (-1, 0)


class Example:
    """All the blocks sharing one `name`, assembled into one cargo package."""

    def __init__(self, name):
        self.name, self.mode, self.deps, self.files = name, None, {}, {}

    @property
    def package(self):
        return "ex-" + self.name.replace("_", "-")

    def add(self, block, problems):
        for attr in ("mode", "deps"):
            new = getattr(block, attr)
            if new is None:
                continue
            if attr == "deps":
                new = parse_deps(new, block.where(), problems)
                for crate, feats in new.items():
                    self.deps.setdefault(crate, set()).update(feats)
                continue
            if self.mode is not None and self.mode != new:
                problems.append(
                    f"{block.where()}: example `{self.name}` is mode={self.mode} "
                    f"elsewhere, but mode={new} here")
            self.mode = new
        self.files.setdefault(block.file, []).append(block)

    def render(self, rel_of):
        """(text, linemap) per file. linemap[i] is the markdown line line i came from."""
        out = {}
        files = dict(self.files)
        if not ({"src/main.rs", "src/lib.rs"} & set(files)):
            # No crate root among the marked blocks, so synthesise one that
            # declares the rest.  When there *is* a root the author declares
            # its modules themselves -- only they know whether a submodule
            # needs `use crate::Something` from it.
            mods = [f"mod {Path(f).stem};" for f in sorted(files)]
            files["src/lib.rs"] = [_Synthetic(mods)]
        for name, blocks in files.items():
            lines, linemap = [PREAMBLE], [None]
            for b in sorted(blocks, key=lambda b: b.order):
                lines += b.before
                linemap += [None] * len(b.before)
                lines += b.code
                linemap += [None if b.rel is None else (b.rel, b.line + k)
                            for k in range(len(b.code))]
                lines += b.after
                linemap += [None] * len(b.after)
            out[rel_of(name)] = ("\n".join(lines) + "\n", linemap)
        return out


def discover(problems):
    examples = {}
    for index, rel in enumerate(ORDER):
        if not (CONTENT / rel).is_file():
            continue                                   # make-pdf.py reports this
        for block in scan(rel, index, problems):
            examples.setdefault(block.name, Example(block.name)).add(block, problems)
    for ex in examples.values():
        ex.mode = ex.mode or "build"
        if not ex.deps:
            ex.deps = {"kladde": set()}
        bad = [f for f in ex.files if not re.fullmatch(r"src/[a-z_][a-z0-9_]*\.rs", f)]
        if bad:
            problems.append(f"example `{ex.name}`: file= must be src/<module>.rs, got "
                            + ", ".join(sorted(bad)))
        if ex.mode == "run" and "src/main.rs" not in ex.files:
            problems.append(f"example `{ex.name}` is mode=run but has no src/main.rs")
    return examples


def workspace_deps(workspace):
    """The `[workspace.dependencies]` table, for examples naming a third-party crate."""
    text = (workspace / "Cargo.toml").read_bytes()
    return tomllib.loads(text.decode()).get("workspace", {}).get("dependencies", {})


def _toml_value(v):
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v))


def manifest(ex, workspace, problems):
    shared = workspace_deps(workspace)
    deps = []
    for crate in sorted(ex.deps):
        feats = sorted(ex.deps[crate])
        path = workspace / "crates" / crate
        if (path / "Cargo.toml").is_file():
            spec = {"path": str(path)}
        elif crate in shared:
            # Not a workspace member, so take the workspace's own pin rather
            # than inventing a version: an example must not be able to depend
            # on something the library itself does not.
            spec = shared[crate]
            spec = dict(spec) if isinstance(spec, dict) else {"version": spec}
            spec.pop("path", None)
        else:
            problems.append(
                f"example `{ex.name}`: `{crate}` is neither a crate in "
                f"{workspace.name}/crates/ nor in its [workspace.dependencies]")
            continue
        if feats:
            spec["features"] = sorted(set(spec.get("features", [])) | set(feats))
        deps.append(f"{crate} = {_toml_value(spec)}")
    return "\n".join([
        "# Generated by tools/check-examples.py -- edit the markdown, not this.",
        "[package]",
        f'name = "{ex.package}"',
        'version = "0.0.0"',
        'edition = "2021"',
        "publish = false",
        "",
        "[dependencies]",
        *deps,
        "",
    ])


def write_if_changed(path, text):
    if path.is_file() and path.read_text() == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def materialize(examples, workspace, problems):
    """Write two workspaces -- one expected to build, one expected not to.

    They are separate because a `compile_fail` member would sink a
    `cargo build --workspace` of the rest. They share a target directory, so
    kladde itself is still compiled once.
    """
    linemaps, dirs = {}, {}
    for kind in ("build", "fail"):
        want = {ex.package: ex for ex in examples.values()
                if (ex.mode == "compile_fail") == (kind == "fail")}
        root = WORKDIR / kind
        root.mkdir(parents=True, exist_ok=True)
        for stale in root.iterdir():
            if stale.is_dir() and stale.name not in want:
                shutil.rmtree(stale)
        for pkg, ex in want.items():
            crate = root / pkg
            write_if_changed(crate / "Cargo.toml", manifest(ex, workspace, problems))
            rendered = ex.render(lambda name: crate / name)
            for path, (text, linemap) in rendered.items():
                write_if_changed(path, text)
                linemaps[path.resolve()] = linemap
            for stale in (crate / "src").glob("*.rs"):
                if stale.resolve() not in linemaps:
                    stale.unlink()
        write_if_changed(root / "Cargo.toml", "\n".join([
            "# Generated by tools/check-examples.py.",
            "[workspace]",
            'resolver = "2"',
            "members = [" + ", ".join(json.dumps(p) for p in sorted(want)) + "]",
            "",
        ]))
        dirs[kind] = (root, want)
    return linemaps, dirs


def cargo(args, cwd):
    env = dict(os.environ, CARGO_TARGET_DIR=str(WORKDIR / "target"))
    return subprocess.run(["cargo", *args], cwd=cwd, env=env,
                          capture_output=True, text=True)


def diagnostics(proc, linemaps, verbose):
    """Rewrite rustc's spans to point at the markdown they came from."""
    errors, warnings = [], []
    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-message":
            continue
        d = msg["message"]
        if d.get("level") not in ("error", "warning"):
            continue
        if not d.get("spans"):
            continue                                   # "aborting due to N errors"
        span = next((s for s in d["spans"] if s.get("is_primary")), d["spans"][0])
        origin = None
        for base in (WORKDIR / "build", WORKDIR / "fail", Path.cwd()):
            candidate = (base / span["file_name"]).resolve()
            if candidate in linemaps:
                lm = linemaps[candidate]
                index = span["line_start"] - 1
                origin = lm[index] if 0 <= index < len(lm) else None
                break
        code = f"[{d['code']['code']}]" if d.get("code") else ""
        head = (f"content/{origin[0]}:{origin[1]}" if origin
                else f"(hidden line in {span['file_name']})")
        entry = f"{head}: {d['level']}{code}: {d['message']}"
        if verbose and d.get("rendered"):
            entry += "\n" + textwrap.indent(d["rendered"].rstrip(), "    ")
        (errors if d["level"] == "error" else warnings).append(entry)
    # A derive macro names a bad type in several places at once, so one mistake
    # in the markdown can come back as the same message several times.
    return dedupe(errors), dedupe(warnings)


def dedupe(entries):
    seen, out = set(), []
    for e in entries:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=str(ROOT.parent / "kladde-rs"),
                    help="checkout of the Rust workspace (default: ../kladde-rs)")
    ap.add_argument("--list", action="store_true", help="list the marked examples and exit")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="include rustc's own rendering of each diagnostic")
    ap.add_argument("--keep-going", action="store_true",
                    help="run the run/compile_fail examples even if the build failed")
    args = ap.parse_args()

    problems = []
    examples = discover(problems)

    if args.list:
        for name in sorted(examples):
            ex = examples[name]
            deps = ", ".join(sorted(ex.deps))
            print(f"{name:<20} {ex.mode:<12} [{deps}]")
            for f, blocks in sorted(ex.files.items()):
                for b in sorted(blocks, key=lambda b: b.order):
                    print(f"    {f:<16} {b.where()}")
        return 0

    if not examples:
        print("no marked examples found", file=sys.stderr)
        return 0

    workspace = Path(args.workspace).resolve()
    if not (workspace / "crates" / "kladde" / "Cargo.toml").is_file():
        print(f"no kladde workspace at {workspace}; pass --workspace", file=sys.stderr)
        return 2
    if not shutil.which("cargo"):
        print("cargo not found", file=sys.stderr)
        return 2

    linemaps, dirs = materialize(examples, workspace, problems)
    if problems:
        for p in problems:
            print(f"example: {p}", file=sys.stderr)
        return 1

    failures, warnings = [], []
    build_root, build_pkgs = dirs["build"]
    if build_pkgs:
        proc = cargo(["build", "--workspace", "--message-format=json"], build_root)
        errs, warns = diagnostics(proc, linemaps, args.verbose)
        failures += errs
        warnings += warns
        if proc.returncode != 0 and not errs:
            failures.append("cargo build failed without a mapped diagnostic:\n"
                            + textwrap.indent(proc.stderr.strip(), "    "))
        if proc.returncode != 0 and not args.keep_going:
            build_pkgs = {}

    for ex in sorted(examples.values(), key=lambda e: e.name):
        if ex.mode != "run" or ex.package not in build_pkgs:
            continue
        proc = cargo(["run", "--quiet", "-p", ex.package], build_root)
        if proc.returncode != 0:
            failures.append(f"example `{ex.name}` compiled but failed to run:\n"
                            + textwrap.indent((proc.stderr or proc.stdout).strip(), "    "))

    fail_root, fail_pkgs = dirs["fail"]
    for pkg in sorted(fail_pkgs):
        proc = cargo(["build", "-p", pkg, "--message-format=json"], fail_root)
        if proc.returncode == 0:
            ex = fail_pkgs[pkg]
            first = min((b for bs in ex.files.values() for b in bs), key=lambda b: b.order)
            failures.append(f"{first.where()}: example `{ex.name}` is mode=compile_fail "
                            "but compiled successfully")

    for w in warnings:
        print(w, file=sys.stderr)
    for f in failures:
        print(f, file=sys.stderr)
    counts = {}
    for ex in examples.values():
        counts[ex.mode] = counts.get(ex.mode, 0) + 1
    shape = ", ".join(f"{n} {m}" for m, n in sorted(counts.items()))
    blocks = sum(len(bs) for ex in examples.values() for bs in ex.files.values())
    print(f"{len(examples)} example(s) ({shape}) from {blocks} block(s), "
          f"{len(warnings)} warning(s), {len(failures)} failure(s)", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
