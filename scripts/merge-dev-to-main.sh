#!/usr/bin/env bash
# merge-dev-to-main.sh — Merge dev → main, patch GITHUB_BRANCH, then sync dev back.
#
# Usage: bash scripts/merge-dev-to-main.sh
#
# Can be run from any branch — it will checkout main, do the merge, then
# checkout dev and sync it so dev is never "behind" main.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

ORIGINAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# ── Step 1: Switch to main ──────────────────────────────────────────────────
if [[ "$ORIGINAL_BRANCH" != "main" ]]; then
  echo "→ Switching to main branch..."
  git checkout main
fi

# ── Step 2: Fetch and merge dev ─────────────────────────────────────────────
echo "→ Fetching origin/dev..."
git fetch origin dev

echo "→ Merging origin/dev into main..."
git merge origin/dev --no-edit -m "chore: merge dev → main"

# ── Step 3: Patch GITHUB_BRANCH=main ────────────────────────────────────────
echo "→ Patching GITHUB_BRANCH=main in install.sh and arrhub.sh..."
# Support both GNU sed (Linux) and BSD sed (macOS)
if sed --version 2>/dev/null | grep -q GNU; then
  sed -i 's/^GITHUB_BRANCH="dev"$/GITHUB_BRANCH="main"/' install.sh arrhub.sh
else
  sed -i '' 's/^GITHUB_BRANCH="dev"$/GITHUB_BRANCH="main"/' install.sh arrhub.sh
fi

# Only commit patch if files actually changed
if ! git diff --quiet install.sh arrhub.sh; then
  git add install.sh arrhub.sh
  git commit --amend --no-edit  # fold into the merge commit
fi

# ── Step 4: Push main ───────────────────────────────────────────────────────
echo "→ Pushing main..."
git push origin main

# ── Step 5: Sync dev back with main ─────────────────────────────────────────
# This ensures dev is never "behind" main after a merge.
echo "→ Syncing dev branch back with main..."
git checkout dev
git merge main --no-edit
git push origin dev

# ── Step 6: Return to original branch ───────────────────────────────────────
if [[ "$ORIGINAL_BRANCH" != "dev" && "$ORIGINAL_BRANCH" != "main" ]]; then
  git checkout "$ORIGINAL_BRANCH"
fi

echo ""
echo "✓  Done."
echo "   main  → up to date, GITHUB_BRANCH=\"main\""
echo "   dev   → synced with main, GITHUB_BRANCH=\"dev\""
