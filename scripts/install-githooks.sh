#!/usr/bin/env sh
# Install the repo's git enforcement (fresh clones need this once):
#   sh scripts/install-githooks.sh
#
# 1. core.hooksPath -> .githooks
#    - pre-commit:  blocks commits until `uv run ruff check .` + `uv run mypy` pass
#    - post-commit: auto-undoes commits that bypassed the gate via
#      `--no-verify` / `-n` (post-commit always runs even with --no-verify)
# 2. The fully unbypassable layer is CI on push (.github/workflows/ci.yml);
#    `git -c core.hooksPath=/dev/null` can disable local hooks, but CI still
#    refuses dirty pushes.
#
# NOTE: `core.hooksPath` is local git config, not tracked in the repo.
# Run this after every clone.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

git config core.hooksPath .githooks

echo "git enforcement installed:"
echo "  core.hooksPath = $(git config core.hooksPath)"
echo "=> commits run 'uv run ruff check .' + 'uv run mypy' first,"
echo "   and '--no-verify' / '-n' commits are auto-reverted."
