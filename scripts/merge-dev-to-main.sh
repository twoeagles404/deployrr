#!/usr/bin/env bash
# merge-dev-to-main.sh — Merge dev → main, patch GITHUB_BRANCH, then sync dev back.
#
# Usage: bash scripts/merge-dev-to-main.sh
#
# Safe to run even after a failed previous attempt — it always resets local
# main to exactly match origin/main before merging, so stale commits from
# previous runs never cause conflicts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

ORIGINAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# ── Step 1: Switch to main ──────────────────────────────────────────────────
echo "→ Switching to main branch..."
git checkout main

# ── Step 2: Reset local main to EXACTLY match origin/main ───────────────────
# This is the key fix: if a previous merge attempt patched main locally but
# the push failed, local main is now ahead of origin/main with stale commits.
# Resetting hard ensures we always start from a clean, conflict-free base.
echo "→ Resetting local main to match origin/main (removes any stale local commits)..."
git fetch origin main
git reset --hard origin/main

# ── Step 3: Fetch dev ────────────────────────────────────────────────────────
echo "→ Fetching origin/dev..."
git fetch origin dev

# ── Step 4: Merge dev into main (dev wins on any conflict) ──────────────────
# -X theirs = when a conflict exists, prefer the incoming branch (dev).
# Dev is always the source of truth — main is just a "release" copy.
echo "→ Merging origin/dev into main (dev wins on conflict)..."
git merge -X theirs origin/dev --no-edit -m "chore: merge dev → main"

# ── Step 5: Patch GITHUB_BRANCH=main ────────────────────────────────────────
echo "→ Patching GITHUB_BRANCH=main in install.sh and arrhub.sh..."
# Support both GNU sed (Linux) and BSD sed (macOS)
if sed --version 2>/dev/null | grep -q GNU; then
  sed -i 's/^GITHUB_BRANCH="dev"$/GITHUB_BRANCH="main"/' install.sh arrhub.sh
else
  sed -i '' 's/^GITHUB_BRANCH="dev"$/GITHUB_BRANCH="main"/' install.sh arrhub.sh
fi

# Only amend if files actually changed (idempotent)
if ! git diff --quiet install.sh arrhub.sh; then
  git add install.sh arrhub.sh
  git commit --amend --no-edit
fi

# ── Step 6: Push main ───────────────────────────────────────────────────────
echo "→ Pushing main..."
git push origin main

# ── Step 7: Sync dev back with main ─────────────────────────────────────────
# Ensures dev is never "behind" main after the merge.
echo "→ Syncing dev branch back with main..."
git checkout dev
git merge main --no-edit
git push origin dev

# ── Step 8: Return to original branch ───────────────────────────────────────
if [[ "$ORIGINAL_BRANCH" != "dev" && "$ORIGINAL_BRANCH" != "main" ]]; then
  git checkout "$ORIGINAL_BRANCH"
fi

echo ""
echo "✓  Done."
echo "   main  → up to date, GITHUB_BRANCH=\"main\""
echo "   dev   → synced with main, GITHUB_BRANCH=\"dev\""
