# kladde docs

Specifications and design documentation for [kladde-rust](https://github.com/robamler/kladde-rust), published as a browsable site with [Quartz](https://quartz.jzhao.xyz/).

## Editing

Write and edit markdown in [`content/`](content/). Open that folder directly as an Obsidian vault — `[[wikilinks]]`, backlinks, and the folder tree all carry through to the published site. `.obsidian/` is already excluded from the build (see `ignorePatterns` in `quartz.config.yaml`).

## Local preview

```sh
npm install
npx quartz build --serve
```

Serves the site at `http://localhost:8080` and rebuilds on save.

## Tooling

Two scripts in [`tools/`](tools/), sharing the page list in [`tools/pages.py`](tools/pages.py).

### The PDF

```sh
tools/make-pdf.py                  # -> kladde.pdf
tools/make-pdf.py --check-only     # links and page coverage, no pandoc
tools/make-pdf.py --self-test      # the diagram layout passes
```

Concatenates every page in reading order and hands it to pandoc, rendering the mermaid diagrams to figures on the way.
See the module docstring for what it needs installed.

Given one or more `.md` files it renders each on its own instead, writing `<basename>.pdf` to the working directory:

```sh
tools/make-pdf.py content/rust/tutorial/getting-started.md
tools/make-pdf.py content/spec/*.md          # -> ./file-format.pdf, ./journal.pdf, ...
```

A single page becomes an `article` rather than a chapter of a `report`, with no table of contents.
Links into *other* pages are left exactly as written — they are correct on the website, and a single-page PDF makes no claim about the rest of the set — so only same-page anchors are checked.
Any markdown file works, not just one under `content/`; without front matter the title comes from its first `#` heading, or failing that its filename.
Basenames must differ, since `content/` has an `index.md` per section.

### Diffs

`--diff` renders the *change* between two versions rather than one version:

```sh
tools/make-pdf.py --diff staged notes.md        # -> ./notes-diff.pdf
tools/make-pdf.py --diff HEAD~3                 # -> kladde-diff.pdf
tools/make-pdf.py --diff v0.1..HEAD page.md
```

The argument is `OLD` or `OLD..NEW` — git's own range syntax, since `OLD:NEW` already means "path NEW at rev OLD" to git.
`NEW` defaults to the working tree, and either side may be `staged` for the index.
Output is `<basename>-diff.pdf` per page, or `kladde-diff.pdf` for the whole set.

Both versions are rendered to LaTeX and compared with [latexdiff](https://ctan.org/pkg/latexdiff), so the result is a typeset document with insertions underlined and deletions struck through — not a diff of the sources.
Diffing the Markdown would be worse than useless: `+`/`-` in column one is a list bullet to the Markdown reader, so the document falls apart before it reaches pandoc.

Two things are deliberately different in a diff: code blocks are unhighlighted, and box-drawing diagrams keep the default leading.
latexdiff marks up a code block by rewriting it into a `listings`-based environment, which it only does for the plain `verbatim` that `--no-highlight` produces; left as pandoc's coloured fancyvrb blocks, a change confined to code renders with nothing marked at all.
Mermaid figures are named by a hash of their source, so an untouched diagram is not reported as a change.

Changes are marked twice over, so a diff survives a grayscale print: insertions in blue with a wavy underline, deletions in red struck through, and a change bar in the margin beside both.
That is latexdiff's `CULINECHBAR` style, the only one that adds the bar without taking something away — `CCHANGEBAR` and `CFONTCHBAR` drop the underline and strikeout, which is exactly the cue that separates an insertion from a deletion once the colour is gone.
The bars come from the `changebar` package, which needs a third xelatex pass to place them — it records each bar's position on one pass and draws it on the next.
The diff is recompiled until those positions come out the same twice, rather than until changebar stops printing "Rerun to get the bars right": on a 25-page diff the positions are stable from the third pass but that message still appears on the sixth, so believing it costs several 30-second passes for output that has stopped changing.

### Checking the Rust examples

```sh
tools/check-examples.py            # compile, and run what can be run
tools/check-examples.py --list     # what is marked, and where
tools/check-examples.py -v         # with rustc's own diagnostics
tools/make-pdf.py --check-examples # both, in one go
```

Needs a cargo toolchain and a checkout of the Rust workspace next door (`--workspace`, default `../kladde-rust`), so it is opt-in rather than part of every build.

A Rust code block is compiled only if an HTML comment right above it says so:

    <!-- kladde-example: name=journal file=src/main.rs mode=run deps=kladde,kladde-types -->

The marker has to sit *outside* the fence.
Pandoc refuses to open a fenced block on a multi-word bare info string, so ```` ```rust name=x ```` would silently turn the block into a paragraph in the PDF; an HTML comment is invisible to both renderers instead.

Blocks sharing a `name` become one generated crate, and blocks sharing `name` *and* `file` are concatenated in reading order — so one example can be built up across several blocks and pages.
Lines the example needs but the reader should not see go in the marker too, for the same reason:

    <!-- kladde-example: name=notes file=src/main.rs
    before:
      use kladde::Kladde;
      fn main() {
    after:
      }
    -->

`mode` is `build` (the default), `run`, or `compile_fail`.
Errors are reported against the markdown line they came from, not the generated crate.
Unmarked blocks are ignored, which is the right answer for the design documents: they describe the system as intended, so most of their code does not compile against what exists today.

Generated crates and their shared target directory live in `.examples/` (gitignored), rewritten only where the content changed, so a re-run costs about a second.

## Deployment

Pushing to `main` builds the site and deploys it to GitHub Pages via [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml). Pull requests are validated (type-check, format-check, build) by [`.github/workflows/check.yml`](.github/workflows/check.yml).

One-time repo setup on GitHub: **Settings → Pages → Source → GitHub Actions**.

## Updating Quartz itself

This repo vendors the Quartz site generator directly (in `quartz/`, alongside the root config files) as a plain snapshot — there's no shared git history with the [upstream Quartz repo](https://github.com/jackyzha0/quartz), so updates are manual rather than `git merge`:

```sh
git clone --branch v5 https://github.com/jackyzha0/quartz.git /tmp/quartz-upstream
diff -rq /tmp/quartz-upstream/quartz quartz          # see what changed in the engine
```

Review the diff, copy over what you want (the `quartz/` directory and, if needed, root files like `quartz.config.default.yaml`, `tsconfig.json`, `package.json` dependencies), then re-apply the local customizations described in this README (title, `analytics: null`, `.prettierignore`, removed upstream project files) if they got overwritten. Re-run `npm install` and `npx quartz build` afterwards to confirm nothing broke.
