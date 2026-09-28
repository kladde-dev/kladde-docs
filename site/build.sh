#!/usr/bin/env bash
# Build the website from content/ into public/, with a pinned upstream Quartz.
#
#   site/build.sh             # -> public/
#   site/build.sh --serve     # and serve it at http://localhost:8080, rebuilding on save
#
# Quartz is not vendored.  The first run fetches it at QUARTZ_COMMIT into
# site/.quartz/ (gitignored) and installs its dependencies; every run then
# copies the two files that customize it over its own and builds.  Any further
# arguments go to `quartz build`.
#
# To update Quartz, change QUARTZ_COMMIT, build, and check the result: nothing
# else pins its version.
set -euo pipefail

QUARTZ_REPO=https://github.com/jackyzha0/quartz.git
QUARTZ_COMMIT=075afd3f712da0088a07f5284a7b3aba37dd61b6  # branch v5, 2026-08-12

site=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$site")
quartz=$site/.quartz

if [ "$(git -C "$quartz" rev-parse HEAD 2>/dev/null)" != "$QUARTZ_COMMIT" ] ||
    [ ! -d "$quartz/node_modules" ]; then
    rm -rf "$quartz"
    git init -q "$quartz"
    git -C "$quartz" fetch -q --depth 1 "$QUARTZ_REPO" "$QUARTZ_COMMIT"
    git -C "$quartz" checkout -q FETCH_HEAD
    (cd "$quartz" && npm ci --no-audit --no-fund)
fi

cp "$site/quartz.config.yaml" "$quartz/quartz.config.yaml"
cp "$site/custom.scss" "$quartz/quartz/styles/custom.scss"

cd "$quartz"
npx quartz build -d "$root/content" -o "$root/public" "$@"
