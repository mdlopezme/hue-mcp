PYTHON ?= .venv/bin/python

# An inherited PYTHONPATH (e.g. a sourced ROS workspace) can inject foreign pytest plugins.
unexport PYTHONPATH

.PHONY: check lint typecheck test format

check: lint typecheck test

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m pytest --cov --cov-report=term-missing:skip-covered

format:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .
