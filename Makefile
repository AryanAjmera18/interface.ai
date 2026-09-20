.PHONY: install lint typecheck test test-live target-app demo update-goldens

install:
	uv sync --locked
	uv pip install -r src/cua/target_app/requirements.txt

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked lint-imports

typecheck:
	uv run --locked mypy --strict src/cua

test:
	uv run --locked pytest

update-goldens:
	uv run --locked pytest --update-goldens

test-live:
	uv run --locked pytest -m live --no-cov

target-app:
	uv run --locked --with-requirements src/cua/target_app/requirements.txt python -m cua.target_app

demo:
	@echo Demo path: evidence/demo/ - TODO: record the demo bundle.
