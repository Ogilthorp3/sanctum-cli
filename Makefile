.PHONY: help venv install dev test lint format typecheck check clean run

PYTHON ?= python3.12
VENV ?= .venv
ACTIVATE = . $(VENV)/bin/activate

help:
	@echo "sanctum-cli — dev targets"
	@echo
	@echo "  make venv        Create $(VENV) using uv"
	@echo "  make install     Install package + dev deps (editable)"
	@echo "  make test        Run pytest"
	@echo "  make lint        Run ruff lint"
	@echo "  make format      Run ruff format"
	@echo "  make typecheck   Run mypy"
	@echo "  make check       Run all gates (lint + typecheck + test)"
	@echo "  make run         sanctum status against live config"
	@echo "  make clean       Remove venv + caches"

venv:
	@if [ -d "$(VENV)" ]; then \
		echo "$(VENV) already exists; skipping (use 'make clean' to recreate)"; \
	else \
		uv venv --python $(PYTHON) $(VENV); \
	fi

install: venv
	uv pip install --python $(VENV)/bin/python -e ".[dev]"

test:
	$(ACTIVATE) && pytest

lint:
	$(ACTIVATE) && ruff check sanctum_cli tests

format:
	$(ACTIVATE) && ruff format sanctum_cli tests
	$(ACTIVATE) && ruff check --fix sanctum_cli tests

typecheck:
	$(ACTIVATE) && mypy sanctum_cli

check: lint typecheck test

run:
	$(ACTIVATE) && sanctum status

clean:
	rm -rf $(VENV) build dist .mypy_cache .pytest_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
