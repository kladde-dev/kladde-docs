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
