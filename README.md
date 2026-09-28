# kladde docs

The specification, reference algorithms, and design documentation of **kladde**: durable data structures that you mutate in memory, and it's on disk.
Kladde is a cross-language file format with implementations, the first of which is [kladde-rs](https://github.com/kladde-dev/kladde-rs).

Read it at **<https://kladde-dev.github.io/>**, or as [one PDF](https://kladde-dev.github.io/kladde.pdf).

## Editing

Write and edit markdown in [`content/`](content/).
Open that folder directly as an Obsidian vault — `[[wikilinks]]`, backlinks, and the folder tree all carry through to the published site.
`.obsidian/` is already excluded from the build (see `ignorePatterns` in [`site/quartz.config.yaml`](site/quartz.config.yaml)).

## Local preview

```sh
site/build.sh --serve
```

Serves the site at `http://localhost:8080` and rebuilds on save; without `--serve`, it builds into `public/`.
It needs Node.js 22 and git, and the first run fetches and installs [Quartz](https://quartz.jzhao.xyz/) into `site/.quartz/` (see [Updating Quartz](#updating-quartz)).

After a build, `tools/check-site-links.py` checks every internal link and anchor in `public/`.
That is what Quartz made of the links, which `tools/make-pdf.py --check-only` cannot see, since it reads the markdown.

## Tooling

Scripts in [`tools/`](tools/).
The first two share the page list in [`tools/pages.py`](tools/pages.py); the other two draw the figures and check the drafts of [Evaluation](content/evaluation/), and need the Python packages in [`tools/requirements.txt`](tools/requirements.txt).

### The PDF

```sh
tools/make-pdf.py                  # -> kladde.pdf
tools/make-pdf.py --check-only     # links and page coverage, no pandoc
tools/make-pdf.py --self-test      # the diagram layout passes
```

Concatenates every page in reading order and hands it to pandoc, rendering the mermaid diagrams to figures on the way.
The mermaid renderer is an npm package: run `npm ci` in `tools/` once.
See the module docstring for everything else it needs installed.

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

Needs a cargo toolchain and a checkout of [kladde-rs](https://github.com/kladde-dev/kladde-rs) next door (`--workspace`, default `../kladde-rs`), so it is opt-in rather than part of every local build.
CI runs it on every push, against kladde-rs's `main`.

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

### The figures and the ripeness simulation

[`tools/plot-evaluation.py`](tools/plot-evaluation.py) draws the figures of [Evaluation](content/evaluation/) from the benchmark tables kept next to them, and [`tools/simulate-ripeness.py`](tools/simulate-ripeness.py) checks the estimators of the ripeness drafts.
Each evaluation page lists the commands that reproduce its figures.
The figures are SVG, byte for byte the same whenever the data and matplotlib's version are, which is why [`tools/requirements.txt`](tools/requirements.txt) pins it.

## Deployment

[`.github/workflows/site.yml`](.github/workflows/site.yml) builds the site and the PDF on every push and pull request, and keeps both as artifacts of the run.
A broken link in the built site fails the run and so holds up publishing; the check of the Rust examples does not.
On `main`, it also publishes them to <https://kladde-dev.github.io/>, by pushing to the `gh-pages` branch of [kladde-dev/kladde-dev.github.io](https://github.com/kladde-dev/kladde-dev.github.io): only a repository of that name can serve the organization's root URL.

The push needs a deploy key, set up once:

1. `ssh-keygen -t ed25519 -N "" -C "kladde-docs deploy" -f pages-deploy-key`
2. In kladde-dev/kladde-dev.github.io, under **Settings → Deploy keys**, add `pages-deploy-key.pub` with write access.
3. In kladde-dev/kladde-docs, under **Settings → Secrets and variables → Actions**, add the content of `pages-deploy-key` as the repository secret `PAGES_DEPLOY_KEY`.
   Then delete both files.
4. After the first deployment has created the branch, set **Settings → Pages → Source** of kladde-dev/kladde-dev.github.io to *Deploy from a branch*, `gh-pages`, `/ (root)`.

## Updating Quartz

Quartz is not vendored.
[`site/build.sh`](site/build.sh) fetches it at the commit it names in `QUARTZ_COMMIT`, and copies the two files that customize it over its own: [`site/quartz.config.yaml`](site/quartz.config.yaml) and [`site/custom.scss`](site/custom.scss).
To update, change `QUARTZ_COMMIT` to a newer commit of the [upstream repository](https://github.com/jackyzha0/quartz), build, and compare the result; if upstream has changed its default configuration, `quartz.config.default.yaml` there shows how.

## License

Everything in this repository — text, figures, data, and code — is available under your choice of [CC BY 4.0](LICENSE-CC-BY-4.0), [MIT](LICENSE-MIT), [Apache 2.0](LICENSE-APACHE), or the [Boost Software License 1.0](LICENSE-BOOST) (SPDX: `CC-BY-4.0 OR MIT OR Apache-2.0 OR BSL-1.0`).
Take whichever suits your use: CC BY 4.0 to reuse text or a figure in a paper or talk, or one of the software licenses to copy code, or spec text into your own code's comments.

**Patent pledge.** I, Robert Bamler, will not assert any patent I own or control against any implementation of the kladde specification.

Unless you explicitly state otherwise, any contribution you intentionally submit for inclusion in this repository shall be licensed as above, without any additional terms or conditions.

[`site/QUARTZ-LICENSE.txt`](site/QUARTZ-LICENSE.txt) is the MIT license of [Quartz](https://github.com/jackyzha0/quartz), which the files in `site/` customize.
