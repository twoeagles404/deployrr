#!/usr/bin/env bash
# merge-dev-to-main.sh — Merge dev → main and fix GITHUB_BRANCH automatically.
#
# Usage: bash scripts/merge-dev-to-main.sh
#
# Must be run from the main branch (or a worktree tracking main).
# Merges origin/dev, then immediately patches GITHUB_BRANCH="main" in
# install.sh and arrhub.sh so the CI branch-consistency check passes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

# Verify we're on main
CURRENT="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$CURRENT" != "main" ]]; then
  echo "❌  Must be on main branch (currently on '$CURRENT')" >&2
  exit 1
fi

echo "→ Fetching origin/dev..."
git fetch origin dev

echo "→ Merging origin/dev into main..."
git merge origin/dev --no-edit -m "chore: merge dev → main"

echo "→ Patching GITHUB_BRANCH=main in install.sh and arrhub.sh..."
sed -i '' 's/^GITHUB_BRANCH="dev"$/GITHUB_BRANCH="main"/' install.sh arrhub.sh

# Only commit if there's a diff (merge may already have "main" if dev was synced)
if ! git diff --quiet install.sh arrhub.sh; then
  git add install.sh arrhub.sh
  git commit --amend --no-edit  # fold into the merge commit
fi

echo "→ Pushing main..."
git push origin main

echo "✓  Done. main is up to date with dev and GITHUB_BRANCH is correct."
