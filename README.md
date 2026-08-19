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
