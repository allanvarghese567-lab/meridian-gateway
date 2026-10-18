#!/usr/bin/env bash
# Unzip the newest uploaded zip from uploads/ and mirror its contents onto the dev branch.
set -euo pipefail

BEFORE="${BEFORE:-0000000000000000000000000000000000000000}"
SHA="${GITHUB_SHA:-$(git rev-parse HEAD)}"
ZERO="0000000000000000000000000000000000000000"

# 1. Find the zip added/changed by this push (fall back to all zips on a first push)
if [ "$BEFORE" = "$ZERO" ] || ! git cat-file -e "$BEFORE" 2>/dev/null; then
  FILES=$(git ls-files 'uploads/*.zip')
else
  FILES=$(git diff --name-only --diff-filter=AM "$BEFORE" "$SHA" -- 'uploads/*.zip')
fi
ZIP=$(printf '%s\n' "$FILES" | sed '/^$/d' | tail -n1)
if [ -z "$ZIP" ]; then echo "No zip in uploads/ changed in this push. Nothing to do."; exit 0; fi
echo "Using zip: $ZIP"

# 2. Unzip to a temp dir; if it holds a single top-level folder, use that folder as the source
EX=$(mktemp -d)
unzip -q "$ZIP" -d "$EX"
rm -rf "$EX/__MACOSX"
SRC="$EX"
if [ "$(ls -A "$EX" | wc -l)" = "1" ] && [ -d "$EX/$(ls -A "$EX")" ]; then SRC="$EX/$(ls -A "$EX")"; fi

# 3. Prepare the dev branch in a separate worktree (create it if it does not exist yet)
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
DEV=$(mktemp -d)
git fetch origin dev 2>/dev/null || true
if git show-ref --verify --quiet refs/remotes/origin/dev; then
  git worktree add -B dev "$DEV" origin/dev
else
  git worktree add --detach "$DEV"
  git -C "$DEV" checkout --orphan dev
  git -C "$DEV" rm -rf -q . || true
fi

# 4. Mirror the zip contents onto dev (delete old files, copy new ones). Without a PUSH_TOKEN
#    (workflow scope), GITHUB_TOKEN cannot modify workflow files, so keep dev's own
#    .github/workflows as they are in that case.
KEEP=""
if [ "${HAS_PUSH_TOKEN:-false}" != "true" ]; then
  echo "PUSH_TOKEN not set: leaving .github/workflows on dev unchanged."
  if [ -d "$DEV/.github/workflows" ]; then KEEP=$(mktemp -d); cp -a "$DEV/.github/workflows/." "$KEEP/"; fi
fi
find "$DEV" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a "$SRC"/. "$DEV"/
if [ "${HAS_PUSH_TOKEN:-false}" != "true" ]; then
  rm -rf "$DEV/.github/workflows"
  if [ -n "$KEEP" ]; then mkdir -p "$DEV/.github/workflows"; cp -a "$KEEP/." "$DEV/.github/workflows/"; fi
  rmdir "$DEV/.github" 2>/dev/null || true
fi

# 5. Commit and push
cd "$DEV"
git add -A
if git diff --cached --quiet; then echo "dev already matches the zip. Nothing to commit."; exit 0; fi
git commit -q -m "Import $(basename "$ZIP") from zipfile branch ($SHA)"
git push origin dev
echo "Pushed to dev."
