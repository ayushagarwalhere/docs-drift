# Use native PowerShell on Windows; Unix keeps just's default sh.
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

default:
    @just --list

setup:
    uv sync --locked --all-extras
    uv run pre-commit install

test:
    uv run pytest --cov=docs_drift --cov=scripts --cov-report=term-missing

lint:
    uv run ruff check

fmt:
    uv run ruff format

typecheck:
    uv run mypy

# Check formatting without changing files; `just fmt` is the explicit fixer.
format-check:
    uv run ruff format --check

guard:
    uv run python scripts/guard.py

hooks:
    uv run pre-commit run --all-files

check: setup lint format-check typecheck test guard hooks

demo:
    # Build the fixture repositories first (see README).
    uv run docs-drift scan --repo tests/fixtures/repos/py-cli --no-color
