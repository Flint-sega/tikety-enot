.PHONY: check lint test run

check: lint test

lint:
	uv run ruff check .

test:
	uv run pytest -q

run:
	uv run uvicorn app.main:app --reload --port 8072
