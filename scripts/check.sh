#!/bin/sh
# The local source gate: exactly what CI's `locked` job runs.
# --all-lanes also runs the suite without the pdf extra, as CI's `dev` lanes do.
set -eu
cd "$(dirname "$0")/.."

all_lanes=0
for arg in "$@"; do
  case "$arg" in
    --all-lanes) all_lanes=1 ;;
    *) echo "usage: scripts/check.sh [--all-lanes]" >&2; exit 2 ;;
  esac
done

paths="pageledger/ tests/ examples/ scripts/"
run="uv run --frozen --extra dev --extra pdf"

set -x
uv sync --frozen --extra dev --extra pdf
$run ruff check $paths
$run ruff format --check $paths
$run mypy pageledger/
$run python -m pytest tests/pageledger/ -q -m "not stress"
if [ "$all_lanes" = 1 ]; then
  uv run --isolated --frozen --extra dev python -m pytest tests/pageledger/ -q -m "not stress"
fi
