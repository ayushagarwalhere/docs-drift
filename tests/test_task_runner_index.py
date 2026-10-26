"""Task-runner coverage: Justfile recipes, Makefile catch-alls and package-manager built-ins.

Each case reproduces a false positive from the popular-repository sweep (datasette, llm, vercel/serve, np)
with a minimal offline snippet, and keeps a genuinely missing target or script reported.
"""

import json
from pathlib import Path

import pytest

from docs_drift.config import Config
from docs_drift.index import build_index
from docs_drift.index.base import language_for
from docs_drift.models import SymbolKind
from docs_drift.scanning import run_scan

DATASETTE_JUSTFILE = """export DATASETTE_SECRET := "not_a_secret"
set dotenv-load

# Run tests and linters
@default: test lint

@test *options: init
  uv run pytest -n auto {{options}}

# Install Playwright browser support, Chromium by default
@playwright-install browser="chromium":
  uv run --group playwright playwright install {{browser}}

@playwright browser="chromium" *options:
  uv run pytest tests/test_playwright.py --browser {{browser}} {{options}}

@playwright-all *options:
  uv run pytest tests/test_playwright.py {{options}}

serve url="http://localhost:8001" +args='':
  echo {{url}}

alias pa := playwright-all
"""

SPHINX_MAKEFILE = """SPHINXBUILD   = sphinx-build
SOURCEDIR     = .
BUILDDIR      = _build

help:
\t@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help Makefile

%: Makefile
\t@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

livehtml:
\tsphinx-autobuild -b html "$(SOURCEDIR)" "$(BUILDDIR)"
"""


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def _targets(repo: Path) -> dict[str, list[str]]:
    index = build_index(repo, Config(), no_cache=True)
    return {s.name: s.aliases for s in index.symbols if s.kind == SymbolKind.make_target}


@pytest.mark.parametrize("name", ["Justfile", "justfile", ".justfile", "Makefile", "makefile", "GNUmakefile"])
def test_task_files_are_routed_case_insensitively(name: str) -> None:
    assert language_for(Path(name)) == ("just" if "just" in name.casefold() else "make")


def test_capitalised_justfile_indexes_quiet_recipes_with_parameters(tmp_path: Path) -> None:
    targets = _targets(_repo(tmp_path, {"Justfile": DATASETTE_JUSTFILE}))
    assert {"default", "test", "playwright-install", "playwright", "playwright-all", "serve"} <= set(targets)
    assert "pa" in targets["playwright-all"]
    # Variables, settings and aliases are not recipes.
    assert not {"export", "set", "alias", "DATASETTE_SECRET", "pa"} & set(targets)


def test_justfile_recipe_parameters_are_recorded(tmp_path: Path) -> None:
    index = build_index(_repo(tmp_path, {"Justfile": DATASETTE_JUSTFILE}), Config(), no_cache=True)
    playwright = next(s for s in index.symbols if s.kind == SymbolKind.make_target and s.name == "playwright")
    assert playwright.default == "browser options"
    assert playwright.line == 14


def test_sphinx_catch_all_makefile_accepts_builder_targets(tmp_path: Path) -> None:
    targets = _targets(_repo(tmp_path, {"docs/Makefile": SPHINX_MAKEFILE}))
    assert {"help", "livehtml", "html", "dirhtml", "linkcheck"} <= set(targets)
    assert "%" not in targets
    assert "Makefile" not in targets  # `%: Makefile` names a prerequisite, not a target.


def test_generic_pattern_rule_does_not_invent_targets(tmp_path: Path) -> None:
    makefile = "build: lint test\n\tcc main.c\n\n%.o: %.c\n\tcc -c $<\n\nlint test:\n\truff .\nVAR := x:y\n"
    targets = _targets(_repo(tmp_path, {"Makefile": makefile}))
    assert set(targets) == {"build", "lint", "test"}


def test_package_manager_builtins_are_derived_not_scripts(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"package.json": json.dumps({"name": "serve", "scripts": {"build": "tsc"}})})
    index = build_index(repo, Config(), no_cache=True)
    scripts = {s.name: s for s in index.symbols if s.kind == SymbolKind.package_script}
    assert scripts["build"].authoritative and not scripts["build"].derived
    assert scripts["install"].derived and not scripts["install"].authoritative
    assert scripts["install"].framework == "package-manager"
    # Script-backed verbs fail without a script, so they are never assumed to exist.
    assert "test" not in scripts and "start" not in scripts


def test_documented_task_commands_resolve_and_real_drift_remains(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "package.json": json.dumps({"name": "serve", "scripts": {"build": "tsc"}}),
            "Justfile": DATASETTE_JUSTFILE,
            "docs/Makefile": SPHINX_MAKEFILE,
            "README.md": (
                "# Contributing\n\n"
                "Install dependencies with `pnpm install`, then run `pnpm build`.\n\n"
                "Run `just playwright-all` or `just playwright-install`.\n\n"
                "Build the docs with `make html`.\n\n"
                "Lint with `pnpm lint` and run `just deploy-site`.\n"
            ),
        },
    )
    result = run_scan(repo, Config(), history=False, no_cache=True)
    verdicts = {(f.claim.kind.value, f.claim.value): f.verdict.value for f in result.findings}
    # Extraction drops package-manager built-ins outright; if one is claimed, the index must resolve it.
    assert verdicts.get(("package_script", "install")) in {None, "ok"}
    for key in [
        ("package_script", "build"),
        ("make_target", "playwright-all"),
        ("make_target", "playwright-install"),
        ("make_target", "html"),
    ]:
        assert verdicts.get(key) == "ok", (key, verdicts)
    assert verdicts[("package_script", "lint")] == "missing"
    assert verdicts[("make_target", "deploy-site")] == "missing"
