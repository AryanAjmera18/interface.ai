#!/usr/bin/env sh
set -eu

uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked lint-imports
uv run --locked mypy --strict src/cua
uv run --locked pytest
