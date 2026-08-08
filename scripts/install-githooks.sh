#!/usr/bin/env sh
# Install the repo's git hooks (currently: the ruff+mypy pre-commit gate).
#
# `core.hooksPath` is a local git config that is NOT tracked, so fresh clones
# need this once:
#   sh scripts/install-githooks.sh
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

git config core.hooksPath .githooks

echo "git hooks installed: core.hooksPath = $(git config core.hooksPath)"
echo "pre-commit gate active: commits run 'uv run ruff check' + 'uv run mypy' first."
