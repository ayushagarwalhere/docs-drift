"""Dependency drift: documented runtime/dependency claims and code-versus-manifest usage, all offline."""

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from typer.testing import CliRunner

from docs_drift.cli import app
from docs_drift.config import Config, DepsConfig, load_config
from docs_drift.deps import scan_dependencies
from docs_drift.deps.check import check_dependency_claims
from docs_drift.deps.claims import extract_dependency_claims, install_commands, runtime_claims_in_text
from docs_drift.deps.manifests import load_manifests
from docs_drift.deps.names import node_package, python_candidates
from docs_drift.deps.releases import ReleaseLabel, prose_label, release_cell
from docs_drift.deps.report import apply_baseline, load_deps_baseline, render_markdown, run_deps, save_deps_baseline
from docs_drift.deps.specs import (
    npm_contains,
    npm_minimum,
    npm_overlap,
    npm_range,
    parse_specifier,
    python_minimum,
    specifiers_overlap,
)
from docs_drift.deps.usage import analyse_usage
from docs_drift.docs.pipeline import scan_claims
from docs_drift.docs.sections import parse_document
from docs_drift.fix.rules import plan_rule_fixes
from docs_drift.index.base import CodeIndex
from docs_drift.match import match_claims
from docs_drift.models import Claim, ClaimKind, DependencyIssueKind, DocFile, DocFormat, Finding, Verdict
from docs_drift.report import render
from docs_drift.scanning import run_scan


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8", newline="\n")
    return root


def _paths(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)


def _findings(root: Path, config: Config | None = None) -> list[Finding]:
    config = config or Config()
    claims = scan_claims(root, config).claims
    return scan_dependencies(root, config, claims, _paths(root)).findings


def _by_value(findings: list[Finding]) -> dict[str, Finding]:
    return {f.claim.value: f for f in findings}


PYPROJECT = """
[project]
name = "acme-tool"
version = "2.0.0"
requires-python = ">=3.11"
dependencies = [
    "requests>=2.31",
    "rich",
]

[project.optional-dependencies]
web = ["fastapi>=0.110"]
dev = ["pytest>=8"]
"""


# Version arithmetic ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "minimum"),
    [
        (">=3.11", "3.11"),
        (">=3.8,!=3.9.*", "3.8"),
        ("~=3.10", "3.10"),
        ("^3.10", "3.10"),
        (">3.9", "3.9.1"),
        (">=3.8.1", "3.8.1"),
        ("^3.8.1", "3.8.1"),
        (">=3.8.1,<4.0", "3.8.1"),
        ("==3.12.*", "3.12"),
    ],
)
def test_python_minimum(spec: str, minimum: str) -> None:
    parsed = parse_specifier(spec)
    assert parsed is not None
    assert python_minimum(parsed) == Version(minimum)


def test_poetry_caret_and_bad_specifiers() -> None:
    assert parse_specifier("^0.2.3") == SpecifierSet(">=0.2.3,<0.3")
    assert parse_specifier("~1.4") == SpecifierSet(">=1.4,<1.5")
    assert parse_specifier("*") == SpecifierSet()
    assert parse_specifier("not a spec") is None
    assert parse_specifier("x" * 300) is None


def test_specifier_overlap() -> None:
    assert not specifiers_overlap(SpecifierSet(">=2"), SpecifierSet("<1.5"))
    assert specifiers_overlap(SpecifierSet("==1.2"), SpecifierSet(">=1.0"))
    assert specifiers_overlap(SpecifierSet(">=1,<3"), SpecifierSet(">=2.5"))
    assert not specifiers_overlap(SpecifierSet("==1.2"), SpecifierSet(">=2.31"))
    assert specifiers_overlap(SpecifierSet(), SpecifierSet("<1"))


def test_npm_ranges() -> None:
    caret_two, caret_three = npm_range("^2"), npm_range("^3.0.0")
    assert caret_two is not None and caret_three is not None
    assert not npm_overlap(caret_two, caret_three)
    union = npm_range(">=1 <2 || ^3")
    assert union is not None and npm_overlap(union, caret_three)
    assert npm_minimum(npm_range("^18 || >=20") or []) == (18, 0, 0)
    assert npm_contains(npm_range("1.x") or [], "1.9.0") is True
    assert npm_contains(npm_range("1.2 - 2.3") or [], "2.3.9") is True
    assert npm_contains(npm_range("1.2 - 2.3") or [], "2.4.0") is False
    assert npm_contains(npm_range("~1.2.3") or [], "1.3.0") is False
    assert npm_contains(npm_range("^0.2.3") or [], "0.3.0") is False
    assert npm_contains(npm_range("v16.x") or [], "16.4.1") is True
    assert npm_contains(npm_range("<=2") or [], "2.9.9") is True
    for unsupported in ("latest", "workspace:*", "github:user/repo", "next", "file:../x"):
        assert npm_range(unsupported) is None
    assert npm_range("*") is not None


# Documentation extraction ----------------------------------------------------------------------------------


def _parsed(text: str, path: str = "README.md") -> list[Claim]:
    doc = DocFile(path=path, content_hash="0" * 64, format=DocFormat.markdown)
    return extract_dependency_claims(parse_document(doc, textwrap.dedent(text).lstrip("\n")))


def test_runtime_statements_are_extracted_conservatively() -> None:
    assert runtime_claims_in_text("Requires Python 3.8+ and Git.")[0][0] == "python>=3.8"
    assert runtime_claims_in_text("Node.js >= 16 is needed")[0][0] == "node>=16"
    assert runtime_claims_in_text("Use Node 18 or later.", "Installation")[0][0] == "node>=18"
    assert runtime_claims_in_text("Use Node 18 or later with npm.")[0][0] == "node>=18"
    assert runtime_claims_in_text("This package requires Python 3.10.")[0][0] == "python>=3.10"
    assert runtime_claims_in_text("Supports Python 3.9 - 3.12")[0][0] == "python>=3.9"
    badge = "![py](https://img.shields.io/badge/python-3.9%2B-blue)"
    assert runtime_claims_in_text(badge)[0][0] == "python>=3.9"
    for text in (
        "Measured on Windows 11, Python 3.13.7.",
        "Python 3+ only",
        "run python3.8 -m venv env",
        "CI tests Python 3.11 and 3.13",
        "![py](https://img.shields.io/badge/python-3.9-blue)",
        "Python 7.1+",
    ):
        assert runtime_claims_in_text(text) == [], text


def test_install_commands_are_parsed_without_a_shell() -> None:
    commands = install_commands('python -m pip install -U "acme-tool[web]>=2" -r requirements.txt rich')
    assert commands[0].requirements == ["acme-tool[web]>=2", "rich"]
    assert install_commands('pip install -e ".[dev,test]"')[0].requirements == [".[dev,test]"]
    assert install_commands("pip install --editable=.[web]")[0].requirements == [".[web]"]
    assert install_commands("pip install -e . && npm i -D jest@^29 ./local git+https://x/y")[1].requirements == [
        "jest@^29"
    ]
    assert install_commands("npm install -g node-tool")[0].global_install
    assert install_commands("uvx --from 'acme-tool[mcp]' acme serve")[0].requirements == ["acme-tool[mcp]"]
    assert install_commands("uv tool install acme-tool")[0].requirements == ["acme-tool"]
    assert install_commands("yarn global add tool@1")[0].global_install
    for text in ("npm install", "npm ci", "pip freeze", "python -m venv .venv", "pip install -r req.txt", "uvx acme"):
        assert install_commands(text) == [], text


def test_extract_claims_from_fences_inline_tables_and_badges() -> None:
    claims = _parsed(
        """
        # Tool

        ![python](https://img.shields.io/badge/python-3.8%2B-blue)

        | Runtime | Version |
        | --- | --- |
        | Node.js | >= 16 |

        Install with `pip install "acme-tool[wbe]"`.

        ```sh
        $ pip install requests==1.0 leftpad-py
        $ npm install express@^3
        ```

        ```python
        # pip install inside a Python block is not a command
        ```
        """
    )
    kinds = {(c.kind, c.value) for c in claims}
    assert (ClaimKind.runtime_version, "python>=3.8") in kinds
    assert (ClaimKind.runtime_version, "node>=16") in kinds
    assert (ClaimKind.dependency, "acme-tool[wbe]") in kinds
    assert (ClaimKind.dependency, "requests==1.0") in kinds
    assert (ClaimKind.dependency, "express@^3") in kinds
    assert all(c.line > 0 and c.column > 0 for c in claims)
    assert next(c for c in claims if c.value == "express@^3").qualifier == "npm"


def test_historical_suppressed_and_disabled_claims_are_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "README.md": """
            # Tool

            Requires Python 3.11+.

            <!-- docs-drift-ignore-next-line -->
            Requires Python 3.6+.

            ## Migrating from v1

            Version 1 required Python 3.7+.
            """,
        },
    )
    claims = [c for c in scan_claims(tmp_path, Config()).claims if c.kind == ClaimKind.runtime_version]
    assert [c.value for c in claims] == ["python>=3.11"]
    disabled = Config(deps=DepsConfig(enabled=False))
    assert not [c for c in scan_claims(tmp_path, disabled).claims if c.kind == ClaimKind.runtime_version]


# Documentation versus manifests ----------------------------------------------------------------------------


def test_runtime_overstated_understated_and_matching(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "README.md": "# Tool\n\nRequires Python 3.8+.\n",
            "docs/a.md": "# A\n\nUse Python 3.12 or newer.\n",
            "docs/b.md": "# B\n\nPython >= 3.11 is supported.\n",
        },
    )
    findings = {f.claim.doc_path: f for f in _findings(tmp_path)}
    stale = findings["README.md"]
    assert stale.verdict == Verdict.version_mismatch and stale.is_drift
    assert stale.suggestion == "3.11"
    assert stale.evidence[0].file == "pyproject.toml" and stale.evidence[0].line == 4
    assert "requires-python" in stale.evidence[0].text
    assert findings["docs/a.md"].verdict == Verdict.ok and "understates" in findings["docs/a.md"].reasons[0]
    assert findings["docs/b.md"].verdict == Verdict.ok
    strict = Config(deps=DepsConfig(report_understated=True))
    understated = {f.claim.doc_path: f for f in _findings(tmp_path, strict)}["docs/a.md"]
    assert understated.verdict == Verdict.version_mismatch and understated.confidence == 0.6


def test_runtime_from_setup_cfg_setup_py_poetry_and_pins(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "a/setup.cfg": "[metadata]\nname = a\n[options]\npython_requires = >=3.9\ninstall_requires =\n    six\n",
            "a/README.md": "# A\n\nRequires Python 3.7+.\n",
            "b/setup.py": "from setuptools import setup\nREQ = ['six']\nsetup(name='b', python_requires='>=3.10', "
            "install_requires=REQ, extras_require={'x': ['rich']})\n",
            "b/README.md": "# B\n\nRequires Python 3.10+.\n",
            "c/pyproject.toml": '[tool.poetry]\nname = "c"\n[tool.poetry.dependencies]\npython = "^3.12"\n'
            'httpx = {version = "^0.27", optional = true}\n',
            "c/README.md": "# C\n\nPython 3.9+ works.\n",
            "d/.python-version": "3.12.1\n",
            "d/requirements.txt": "six\n",
            "d/README.md": "# D\n\nRequires Python 3.8+.\n",
        },
    )
    findings = {f.claim.doc_path: f for f in _findings(tmp_path, Config(docs=["**/*.md"]))}
    assert findings["a/README.md"].verdict == Verdict.version_mismatch
    assert findings["a/README.md"].evidence[0].file == "a/setup.cfg"
    assert findings["b/README.md"].verdict == Verdict.ok
    assert findings["c/README.md"].suggestion == "3.12"
    pinned = findings["d/README.md"]
    assert pinned.verdict == Verdict.version_mismatch and pinned.confidence < 0.6
    manifests = load_manifests(tmp_path, _paths(tmp_path))
    poetry = next(m for m in manifests.manifests if m.path == "c/pyproject.toml")
    assert poetry.dependencies[0].group == "optional"
    assert next(m for m in manifests.manifests if m.root == "b").extras == {"x"}


def test_node_engines_and_package_versions(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "package.json": json.dumps(
                {
                    "name": "node-tool",
                    "version": "1.4.0",
                    "engines": {"node": ">=18"},
                    "dependencies": {"express": "^4.18.0", "commander": "workspace:*"},
                },
                indent=2,
            ),
            "README.md": """
            # node-tool

            Requires Node.js 16 or later.

            ```sh
            npm install -g node-tool@^2
            npm install express@^3 commander@^1
            npm install express@4
            ```
            """,
        },
    )
    findings = _by_value(_findings(tmp_path))
    assert findings["node>=16"].verdict == Verdict.version_mismatch
    assert findings["node>=16"].suggestion == "18"
    assert findings["node>=16"].evidence[0].text == '"node": ">=18"'
    assert findings["node-tool@^2"].verdict == Verdict.version_mismatch
    assert findings["node-tool@^2"].suggestion == "1.4.0"
    assert findings["express@^3"].verdict == Verdict.version_mismatch
    assert findings["express@4"].verdict == Verdict.ok
    assert "commander@^1" not in findings  # A workspace protocol cannot be compared.


def test_extras_versions_and_missing_dependencies(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "README.md": """
            # acme-tool

            ```sh
            pip install "acme-tool[web]"
            pip install "acme-tool[wbe]"
            pip install -e ".[dev]"
            pip install "requests==1.0"
            pip install "requests>=2.0"
            pip install rich leftpad-py
            pip install pre-commit
            pip install -U pip setuptools wheel rich
            pip install acme-tool==1.0
            pip install acme-tool==3.0
            ```
            """,
        },
    )
    findings = _by_value(_findings(tmp_path))
    assert findings["acme-tool[web]"].verdict == Verdict.ok
    typo = findings["acme-tool[wbe]"]
    assert typo.verdict == Verdict.unknown_extra and typo.suggestion == "web"
    assert "available extras: dev, web" in typo.reasons[0]
    assert findings[".[dev]"].verdict == Verdict.ok
    assert findings["requests==1.0"].verdict == Verdict.version_mismatch
    assert findings["requests==1.0"].suggestion == ">=2.31"
    assert findings["requests==1.0"].evidence[0].line == 6
    assert findings["requests>=2.0"].verdict == Verdict.ok
    assert findings["leftpad-py"].verdict == Verdict.missing
    assert "rich" in findings["leftpad-py"].reasons[0]
    assert "pre-commit" not in findings and "setuptools" not in findings and "wheel" not in findings
    # An older pin of the project is often intentional; only a release the manifest has not reached is drift.
    assert "acme-tool==1.0" not in findings
    assert findings["acme-tool==3.0"].verdict == Verdict.version_mismatch
    assert findings["acme-tool==3.0"].confidence < 0.7


def test_dynamic_extras_and_missing_manifests_are_not_guessed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "dyn"\ndynamic = ["optional-dependencies"]\n',
            "README.md": "# dyn\n\n`pip install dyn[anything]`\n\nNode 12+ only.\n",
        },
    )
    assert _findings(tmp_path) == []
    _write(tmp_path / "empty", {"README.md": "# x\n\nRequires Python 2.7+.\n\n`pip install a b`\n"})
    assert _findings(tmp_path / "empty") == []


def test_requirement_pins_beside_a_package_are_not_runtime_dependencies(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "requirements.txt": "requests==2.32.0\nsniffio==1.3\n",
            "app/requirements.txt": "flask==3.0  # via -r requirements.in\nwerkzeug==3.0\n",
            "docs/requirements.txt": "sphinx\n",
        },
    )
    manifests = load_manifests(tmp_path, _paths(tmp_path))
    groups = {(d.manifest, d.name): d.group for m in manifests.manifests for d in m.dependencies}
    assert groups[("requirements.txt", "sniffio")] == "dev"
    assert groups[("pyproject.toml", "requests")] == "runtime"
    assert groups[("app/requirements.txt", "werkzeug")] == "optional"
    assert groups[("docs/requirements.txt", "sphinx")] == "dev"


def test_malformed_manifests_warn_instead_of_failing(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": "[project\nname=",
            "package.json": "{not json",
            "setup.py": "def broken(:\n",
            "requirements.txt": "-r ../../outside.txt\nflask\n",
        },
    )
    manifests = load_manifests(tmp_path, _paths(tmp_path))
    assert len(manifests.warnings) == 3
    assert [d.name for d in manifests.declared("python")] == ["flask"]


def test_run_scan_includes_dependency_findings_and_drops_undecidable_claims(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "acme.py": "import requests\nimport rich\n",
            "README.md": "# acme-tool\n\nRequires Python 3.9+.\n\n`pip install pre-commit`\n",
        },
    )
    result = run_scan(tmp_path, Config(), history=False)
    drift = [f for f in result.findings if f.claim.kind == ClaimKind.runtime_version]
    assert drift[0].verdict == Verdict.version_mismatch and drift[0].doc_text == "Requires Python 3.9+."
    assert not [c for c in result.claims if c.kind == ClaimKind.dependency]
    assert result.dependency_issues == []
    assert result.summary()["by_verdict"]["version_mismatch"] == 1
    terminal = render(result, "terminal")
    assert "VERSION README.md:3" in terminal and "VERSION 1" in terminal
    sarif = json.loads(render(result, "sarif"))
    assert "DD006" in {rule["id"] for rule in sarif["runs"][0]["tool"]["driver"]["rules"]}
    assert "| version_mismatch | 1 |" in render(result, "markdown")
    plan = plan_rule_fixes(result)
    assert [(e.old_text, e.new_text) for e in plan.edits] == [("3.9", "3.11")]
    usage = run_scan(tmp_path, Config(deps=DepsConfig(usage=True)), history=False)
    assert usage.dependency_issues == []
    off = run_scan(tmp_path, Config(deps=DepsConfig(enabled=False)), history=False)
    assert not [f for f in off.findings if f.claim.kind == ClaimKind.runtime_version]


def test_summary_line_hides_zero_dependency_verdicts(tmp_path: Path) -> None:
    _write(tmp_path, {"README.md": "# x\n\nSet `APP_MODE`.\n", "app.py": "import os\nos.environ['APP_MODE']\n"})
    terminal = render(run_scan(tmp_path, Config(), history=False), "terminal")
    assert "VERSION" not in terminal and "EXTRA" not in terminal


def test_unknown_extra_fix_rewrites_single_extra(tmp_path: Path) -> None:
    _write(tmp_path, {"pyproject.toml": PYPROJECT, "README.md": '# t\n\nRun `pip install "acme-tool[wbe]"`.\n'})
    result = run_scan(tmp_path, Config(), history=False)
    assert [(e.old_text, e.new_text) for e in plan_rule_fixes(result).edits] == [("wbe", "web")]


# Code versus manifests -------------------------------------------------------------------------------------


def _python_repo(root: Path) -> Path:
    return _write(
        root,
        {
            "pyproject.toml": """
            [project]
            name = "svc"
            dependencies = ["requests>=2", "PyYAML", "unusedlib", "uvicorn",
                "alembic", "beautifulsoup4", "django-cors-headers"]

            [dependency-groups]
            dev = ["pytest"]
            """,
            "Makefile": "migrate:\n\talembic upgrade head\n",
            "requirements.txt": "trio-typing==0.10\n",
            "src/svc/__init__.py": """
            import os
            import requests
            import yaml
            from bs4 import BeautifulSoup
            from svc import helpers
            from . import sibling
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                import boto3
            try:
                import ujson
            except ImportError:
                ujson = None
            import pytest
            import sys
            from _typeshed import StrPath
            INSTALLED_APPS = ["corsheaders"]
            try:
                from http.server import HTTPServer
            except ImportError:
                from BaseHTTPServer import HTTPServer
            else:
                import cryptography
            if sys.version_info < (3, 8):
                import importlib_metadata
            if HAS_SIMPLEJSON:
                import simplejson

            def lazy():
                import numpy
            """,
            "src/svc/helpers.py": "from PIL import Image\n",
            "tests/test_svc.py": "import pytest\nimport hypothesis\nimport svc\n",
            "src/svc/broken.py": "def oops(:\n",
        },
    )


def test_python_usage_issues(tmp_path: Path) -> None:
    root = _python_repo(tmp_path)
    result = analyse_usage(root, _paths(root), load_manifests(root, _paths(root)), DepsConfig())
    issues = {(i.kind, i.name): i for i in result.issues}
    assert set(issues) == {
        (DependencyIssueKind.undeclared, "pillow"),
        (DependencyIssueKind.undeclared, "numpy"),
        (DependencyIssueKind.undeclared_dev, "boto3"),
        (DependencyIssueKind.undeclared_dev, "hypothesis"),
        (DependencyIssueKind.dev_only, "pytest"),
        (DependencyIssueKind.unused, "unusedlib"),
    }
    pillow = issues[(DependencyIssueKind.undeclared, "pillow")]
    assert pillow.import_name == "PIL" and pillow.confidence == 0.85
    assert pillow.evidence[0].file == "src/svc/helpers.py" and pillow.evidence[0].text == "from PIL import Image"
    assert issues[(DependencyIssueKind.undeclared, "numpy")].confidence == 0.55
    assert issues[(DependencyIssueKind.unused, "unusedlib")].evidence[0].line == 3
    assert result.warnings == ["Dependency usage skipped unparsable Python file: src/svc/broken.py"]
    ignored = analyse_usage(
        root, _paths(root), load_manifests(root, _paths(root)), DepsConfig(ignore=["unused*", "PIL"])
    )
    assert {i.name for i in ignored.issues} == {"numpy", "boto3", "hypothesis", "pytest"}
    mapped = analyse_usage(
        root, _paths(root), load_manifests(root, _paths(root)), DepsConfig(import_map={"numpy": "unusedlib"})
    )
    assert "unusedlib" not in {i.name for i in mapped.issues} and "numpy" not in {i.name for i in mapped.issues}


def test_usage_issue_ids_are_stable_and_bounded(tmp_path: Path) -> None:
    root = _python_repo(tmp_path)
    manifests = load_manifests(root, _paths(root))
    first = analyse_usage(root, _paths(root), manifests, DepsConfig())
    second = analyse_usage(root, list(reversed(_paths(root))), manifests, DepsConfig())
    assert [i.issue_id for i in first.issues] == [i.issue_id for i in second.issues]
    capped = analyse_usage(root, _paths(root), manifests, DepsConfig(max_files=1))
    assert "skipped unused-dependency checks" in capped.warnings[0]
    assert DependencyIssueKind.unused not in {i.kind for i in capped.issues}


def test_python_usage_requires_a_manifest(tmp_path: Path) -> None:
    _write(tmp_path, {"tool.py": "import requests\n"})
    assert (
        analyse_usage(tmp_path, _paths(tmp_path), load_manifests(tmp_path, _paths(tmp_path)), DepsConfig()).issues == []
    )


def _node_repo(root: Path, *, private: bool) -> Path:
    return _write(
        root,
        {
            "package.json": json.dumps(
                {
                    "name": "web",
                    "private": private,
                    "scripts": {"start": "serve-static dist"},
                    "dependencies": {"express": "^4", "lodash": "^4", "unused-pkg": "^1", "serve-static": "^1"},
                    "devDependencies": {"jest": "^29", "typescript": "^5", "@types/only-types": "^1", "zod": "^3"},
                },
                indent=2,
            ),
            "tsconfig.json": '{\n  // aliases\n  "compilerOptions": {"paths": {"@app/*": ["src/*"]},},\n}\n',
            "src/index.ts": """
            import express from "express";
            import fp from "lodash/fp";
            import fs from "node:fs";
            import path from "path";
            import local from "./local";
            import alias from "@app/thing";
            import type { T } from "only-types";
            import chalk from "chalk";
            import { z } from "zod";
            const vscode = require("vscode");
            const later = await import("dynamic-dep");
            export { x } from "re-exported";
            """,
            "src/components/Button.tsx": "import utils from 'components/utils';\nexport const B = () => <b/>;\n",
            "src/components/utils.ts": "export default 1;\n",
            "test/app.test.ts": "import supertest from 'supertest';\nimport { jest } from 'jest';\n",
            "gulpfile.js": "import jest from 'jest';\n",
            "packages/inner/package.json": json.dumps({"name": "@acme/inner", "dependencies": {}}),
            "packages/inner/index.js": "const web = require('web');\nconst express = require('express');\n",
        },
    )


def test_node_usage_issues(tmp_path: Path) -> None:
    root = _node_repo(tmp_path, private=False)
    result = analyse_usage(root, _paths(root), load_manifests(root, _paths(root)), DepsConfig())
    issues = {(i.kind, i.name, i.manifest) for i in result.issues}
    assert issues == {
        (DependencyIssueKind.undeclared, "chalk", "package.json"),
        (DependencyIssueKind.undeclared, "dynamic-dep", "package.json"),
        (DependencyIssueKind.undeclared, "re-exported", "package.json"),
        (DependencyIssueKind.undeclared_dev, "supertest", "package.json"),
        (DependencyIssueKind.dev_only, "zod", "package.json"),
        (DependencyIssueKind.unused, "unused-pkg", "package.json"),
    }
    private = _node_repo(tmp_path / "private", private=True)
    kinds = {
        i.kind
        for i in analyse_usage(private, _paths(private), load_manifests(private, _paths(private)), DepsConfig()).issues
    }
    assert DependencyIssueKind.dev_only not in kinds


def test_companion_imports_and_documentation_examples_are_weak_signals(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "api"\ndependencies = ["pydantic", "opentelemetry-api"]\n',
            "api/core.py": "import pydantic\nfrom pydantic_core import CoreSchema\nfrom opentelemetry import context\n",
            "docs_src/tutorial.py": "import strawberry\n",
        },
    )
    result = analyse_usage(tmp_path, _paths(tmp_path), load_manifests(tmp_path, _paths(tmp_path)), DepsConfig())
    assert {(i.kind, i.name, i.confidence) for i in result.issues} == {
        (DependencyIssueKind.undeclared, "pydantic-core", 0.55),
        (DependencyIssueKind.undeclared_dev, "strawberry", 0.55),
    }


def test_node_false_positive_guards(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "package.json": json.dumps(
                {
                    "name": "lib",
                    "scripts": {"build": "rollup -c"},
                    "dependencies": {"systemjs": "^6"},
                    "devDependencies": {"picocolors": "^1"},
                }
            ),
            "src/index.ts": "import colors from 'picocolors';\n"
            "let pnp: typeof import('pnpapi') | undefined;\n"
            "export default 'ok';\n"
            "const s = require.resolve('systemjs/dist/s.min.js');\n",
            "test/fixtures/app/package.json": json.dumps({"name": "fixture-app", "dependencies": {"left-pad": "1"}}),
            "playground/demo/main.js": "import missing from 'missing-modules';\n",
        },
    )
    (tmp_path / "bom").mkdir()
    (tmp_path / "bom" / "package.json").write_text("﻿" + json.dumps({"name": "bom"}), encoding="utf-8")
    assert load_manifests(tmp_path, ["bom/package.json"]).warnings == []
    result = analyse_usage(tmp_path, _paths(tmp_path), load_manifests(tmp_path, _paths(tmp_path)), DepsConfig())
    assert [(i.kind, i.name) for i in result.issues] == [(DependencyIssueKind.undeclared_dev, "missing-modules")]


def test_blog_posts_are_not_checked(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "docs/blog/announcing-v2.md": "# v2\n\nRequires Python 3.6+.\n",
            "docs/announcing-v3.md": "# v3\n\nRequires Python 3.7+.\n",
        },
    )
    assert _findings(tmp_path) == []


def test_component_scripts_in_markdown_count_as_usage(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "package.json": json.dumps({"name": "site", "dependencies": {"carousel": "^4", "gone": "^1"}}),
            "index.md": "<script setup>\nimport Carousel from 'carousel';\n</script>\n",
            "notes.md": "We used to depend on gone.\n",
        },
    )
    result = analyse_usage(tmp_path, _paths(tmp_path), load_manifests(tmp_path, _paths(tmp_path)), DepsConfig())
    assert [(i.kind, i.name) for i in result.issues] == [(DependencyIssueKind.unused, "gone")]


def test_import_name_helpers() -> None:
    assert "scikit-learn" in python_candidates("sklearn.linear_model")
    assert "google-cloud-storage" in python_candidates("google.cloud.storage")
    assert "protobuf" in python_candidates("google.protobuf")
    assert node_package("@scope/pkg/sub") == "@scope/pkg"
    assert node_package("lodash/fp") == "lodash"
    for specifier in ("node:fs", "fs/promises", "./x", "@/alias", "#internal", "virtual:pwa", "@scope", "vscode"):
        assert node_package(specifier) is None, specifier


# Command line, baselines and pull-request mode -------------------------------------------------------------


def test_deps_command_formats_check_and_baseline(tmp_path: Path) -> None:
    root = _python_repo(tmp_path)
    (root / "README.md").write_text("# svc\n\nRequires Python 3.8+.\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        (root / "pyproject.toml")
        .read_text(encoding="utf-8")
        .replace('name = "svc"', 'name = "svc"\nrequires-python = ">=3.10"'),
        encoding="utf-8",
    )
    runner = CliRunner()
    terminal = runner.invoke(app, ["deps", "--repo", str(root), "--no-color"])
    assert terminal.exit_code == 0, terminal.output
    assert "VERSION README.md:3" in terminal.output and "UNDECLARED python pillow" in terminal.output
    assert "boto3" not in terminal.output  # Below the default confidence threshold.
    assert "boto3" in runner.invoke(app, ["deps", "--repo", str(root), "--all"]).output
    data = json.loads(runner.invoke(app, ["deps", "--repo", str(root), "--format", "json"]).output)
    assert data["schema_version"] == 1 and data["claims_checked"] == 1
    assert data["manifests"][0]["runtime"] == ">=3.10"
    assert data["findings"][0]["verdict"] == "version_mismatch"
    markdown = runner.invoke(app, ["deps", "--repo", str(root), "--format", "markdown"]).output
    assert "## Code vs manifests" in markdown and "| undeclared | pillow |" in markdown
    assert runner.invoke(app, ["deps", "--repo", str(root), "--format", "sarif"]).exit_code == 2
    assert runner.invoke(app, ["deps", "--repo", str(root), "--check"]).exit_code == 1
    written = runner.invoke(app, ["deps", "--repo", str(root), "--write-baseline", "deps-baseline.json", "-q"])
    assert written.exit_code == 0 and (root / "deps-baseline.json").is_file()
    clean = runner.invoke(app, ["deps", "--repo", str(root), "--baseline", "deps-baseline.json", "--check"])
    assert clean.exit_code == 0, clean.output
    assert "baselined" in clean.output
    (root / "bad.json").write_text("{}x", encoding="utf-8")
    assert runner.invoke(app, ["deps", "--repo", str(root), "--baseline", "bad.json"]).exit_code == 2


def test_baseline_round_trip(tmp_path: Path) -> None:
    root = _python_repo(tmp_path)
    report = run_deps(root, Config())
    save_deps_baseline(tmp_path / "b.json", report)
    filtered = apply_baseline(report, load_deps_baseline(tmp_path / "b.json"))
    assert filtered.issues == [] and filtered.baselined == len(report.issues)


def test_config_section_loads_from_toml(tmp_path: Path) -> None:
    (tmp_path / ".docs-drift.toml").write_text(
        '[deps]\nenabled = false\nusage = true\nignore = ["six"]\nimport_map = {foo = "foo-dist"}\n', encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert not config.deps.enabled and config.deps.usage
    assert config.deps.import_map == {"foo": "foo-dist"}


def _git(repo: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_AUTHOR_DATE="2026-10-01T00:00:00+00:00",
        GIT_COMMITTER_DATE="2026-10-01T00:00:00+00:00",
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "core.autocrlf=false", "-c", "commit.gpgSign=false", *args],
        check=True,
        capture_output=True,
        env=env,
    )


def test_base_mode_reports_only_new_drift_and_manifest_changes(tmp_path: Path) -> None:
    root = _write(
        tmp_path / "repo",
        {
            "pyproject.toml": '[project]\nname = "svc"\nrequires-python = ">=3.8"\n'
            'dependencies = ["requests", "rich"]\n',
            "svc.py": "import requests\nimport rich\nimport leftover\n",
            "README.md": "# svc\n\nRequires Python 3.8+.\n\n```sh\npip install requests rich\n```\n",
        },
    )
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _write(
        root,
        {
            "pyproject.toml": '[project]\nname = "svc"\nrequires-python = ">=3.10"\ndependencies = ["rich", "httpx"]\n',
            "svc.py": "import httpx\nimport rich\nimport leftover\nimport newthing\n",
        },
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "head")
    report = run_deps(root, Config(), base="HEAD~1")
    assert report.base_sha is not None and len(report.base_sha) == 40
    assert {f.claim.value: f.verdict for f in report.findings} == {
        "python>=3.8": Verdict.version_mismatch,
        "requests": Verdict.missing,
    }
    kinds = {(i.kind, i.name) for i in report.issues}
    assert kinds == {
        (DependencyIssueKind.undeclared, "newthing"),
        (DependencyIssueKind.added_undocumented, "httpx"),
        (DependencyIssueKind.removed_documented, "requests"),
    }
    removed = next(i for i in report.issues if i.kind == DependencyIssueKind.removed_documented)
    assert removed.evidence[0].file == "README.md" and removed.evidence[0].line == 6
    with pytest.raises(ValueError, match="revision"):
        run_deps(root, Config(), base="does-not-exist")


def test_dependency_claims_are_ignored_by_symbol_matching(tmp_path: Path) -> None:
    _write(tmp_path, {"README.md": "# x\n\nRequires Python 3.8+.\n"})
    claims = scan_claims(tmp_path, Config()).claims
    assert match_claims(claims, CodeIndex(), tmp_path) == []
    assert check_dependency_claims(claims, load_manifests(tmp_path, []), tmp_path, DepsConfig()) == []


# Precision regressions: correct documentation is never flagged or rewritten ----------------------------------


@pytest.mark.parametrize("requires", [">=3.8.1", ">3.8", ">=3.8.1,<4.0"])
def test_patch_level_requires_python_matches_minor_docs(tmp_path: Path, requires: str) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": f'[project]\nname = "lc"\nrequires-python = "{requires}"\n',
            "README.md": "# lc\n\nRequires Python 3.8+.\n",
            "docs/old.md": "# Old\n\nRequires Python 3.7+.\n",
            "docs/exact.md": "# Exact\n\nRequires Python 3.8.0+.\n",
        },
    )
    findings = {f.claim.doc_path: f for f in _findings(tmp_path)}
    assert findings["README.md"].verdict == Verdict.ok
    expected = "3.8.1"
    for path in ("docs/old.md", "docs/exact.md"):
        assert findings[path].verdict == Verdict.version_mismatch
        assert findings[path].suggestion == expected, path  # The real bound, never the next minor.
    result = run_scan(tmp_path, Config(), history=False)
    edits = plan_rule_fixes(result).edits
    assert {(e.file, e.new_text) for e in edits} == {("docs/old.md", expected), ("docs/exact.md", expected)}


def test_poetry_caret_patch_requires_python(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[tool.poetry]\nname = "lc"\nversion = "1.0.0"\n\n'
            '[tool.poetry.dependencies]\npython = "^3.8.1"\n',
            "README.md": "# lc\n\nPython 3.8+ is supported.\n",
        },
    )
    assert [f.verdict for f in _findings(tmp_path)] == [Verdict.ok]


def test_graph_prose_about_nodes_is_not_a_node_runtime_claim(tmp_path: Path) -> None:
    for text in (
        "Deleting the edge requires node 2 to exist in the graph.",
        "Deleting the edge requires node 12 to exist in the graph.",
        "Visit the parent node 3 and up.",
        "Every node 5 and up is a leaf.",
        "Edges from node 4 to 7 are removed.",
        "Requires Node 2 or later.",
        "Needs node 18 in the cluster.",
    ):
        assert runtime_claims_in_text(text) == [], text
    assert runtime_claims_in_text("Use Node 18+ to build.")[0][0] == "node>=18"
    assert runtime_claims_in_text("Requires node >= 16.")[0][0] == "node>=16"
    assert runtime_claims_in_text("Tested on node v16 or later.")[0][0] == "node>=16"
    assert runtime_claims_in_text("Requires Node.js 18 or later.")[0][0] == "node>=18"
    assert runtime_claims_in_text("Works on Node 0.12 and up.")[0][0] == "node>=0.12"
    _write(
        tmp_path,
        {
            "package.json": '{"name": "g", "engines": {"node": ">=18"}}\n',
            "docs/graph.md": "# Graph\n\nDeleting the edge requires node 2 to exist in the graph.\n\n"
            "Visit the parent node 3 and up.\n",
        },
    )
    assert _findings(tmp_path) == []
    assert plan_rule_fixes(run_scan(tmp_path, Config(), history=False)).edits == []


@pytest.mark.parametrize(
    "text",
    [
        "Node 5 and up are leaves of the tree.",
        "Node 6 or later children are pruned.",
        "Node 18+ children are collapsed.",
        "Node 7 and higher keep their weights.",
        "Node 4 or newer is the root of the subtree.",
    ],
)
def test_capitalised_graph_node_prose_is_not_a_runtime_claim(tmp_path: Path, text: str) -> None:
    assert runtime_claims_in_text(text) == []
    assert runtime_claims_in_text(text, "Graph / Traversal") == []
    _write(
        tmp_path,
        {
            "package.json": '{"name": "g", "version": "2.0.0", "engines": {"node": ">=18"}}\n',
            "docs/tree.md": f"# Tree\n\n## Pruning\n\n{text}\n",
        },
    )
    result = run_scan(tmp_path, Config(), history=False)
    assert [c for c in result.claims if c.kind == ClaimKind.runtime_version] == []
    assert plan_rule_fixes(result).edits == []


@pytest.mark.parametrize(
    ("text", "context"),
    [
        ("Node.js 16 or later.", ""),
        ("NodeJS 16+ is fine.", ""),
        ("node >= 16", ""),
        ("Node v16 or later.", ""),
        ("Node version 16 or later.", ""),
        ("Node 16 or later is required.", ""),
        ("Node 16+ and npm 8 are needed.", ""),
        ("Node 16 or later (see engines).", ""),
        ("Node 16 or later.", "Requirements"),
        ("Node 16+", "Install / Prerequisites"),
        ("Node 16 or later.", "Getting started / Setup"),
    ],
)
def test_bare_node_needs_a_runtime_signal(text: str, context: str) -> None:
    assert runtime_claims_in_text(text, context)[0][0] == "node>=16"


def test_bare_node_in_install_section_is_checked(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "package.json": '{"name": "g", "version": "2.0.0", "engines": {"node": ">=18"}}\n',
            "README.md": "# g\n\n## Installation\n\nNode 16 or later.\n\n"
            "| Runtime | Version |\n| --- | --- |\n| Node | 16+ |\n",
        },
    )
    drift = [f.claim.value for f in _findings(tmp_path) if f.is_drift]
    assert drift == ["node>=16"]


def test_version_history_tables_are_not_drift(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "lib"\nversion = "3.1.0"\nrequires-python = ">=3.10"\n',
            "README.md": """
            # lib

            ## Compatibility

            | lib | Python |
            | --- | --- |
            | 3.x | Python 3.10+ |
            | 2.x | Python 3.7+ |
            | 1.x | Python 2.7+ |
            """,
            "docs/platforms.md": """
            # Platforms

            | Platform | Runtime |
            | --- | --- |
            | Linux | Python 3.10+ |
            | Windows | Python 3.11+ |
            """,
            "docs/releases.md": """
            # Releases

            | Release | Runtime |
            | --- | --- |
            | v1 | Python 3.6+ |
            | <2.0 | Python 3.6+ |
            """,
        },
    )
    assert [f for f in _findings(tmp_path) if f.is_drift] == []
    result = run_scan(tmp_path, Config(), history=False)
    readme = [c.value for c in result.claims if c.doc_path == "README.md"]
    assert "python>=3.7" not in readme and "python>=2.7" not in readme


HISTORY_PYPROJECT = '[project]\nname = "demo"\nversion = "3.1.0"\nrequires-python = ">=3.10"\n'


def _runtime_drift(root: Path, readme: str, pyproject: str = HISTORY_PYPROJECT) -> tuple[list[str], list[str]]:
    """Runtime drift (line:value) after a full scan, and the lines `docs-drift fix` would rewrite."""
    _write(root, {"pyproject.toml": pyproject, "README.md": "# demo\n\n" + readme})
    result = run_scan(root, Config(), history=False)
    drift = [
        f"{f.claim.line}:{f.claim.value}"
        for f in result.findings
        if f.is_drift and f.claim.kind == ClaimKind.runtime_version
    ]
    return drift, [f"{edit.line}:{edit.new_text}" for edit in plan_rule_fixes(result).edits]


@pytest.mark.parametrize(
    "readme",
    [
        "| demo | Python |\n| --- | --- |\n| 1.x (legacy) | Python 2.7+ |\n",
        "| demo | Python |\n| --- | --- |\n| 2.x series | Python 3.7+ |\n| 1.x series | Python 2.7+ |\n",
        "| Release | Python | Notes |\n| --- | --- | --- |\n| 2.4 LTS | Python 3.7+ | security fixes |\n",
        "| Releases | Python |\n| --- | --- |\n| 1.0 - 1.9 | Python 2.7+ |\n| 2.0 to 2.4 | Python 3.6+ |\n",
        "| Version | Python |\n| --- | --- |\n| 2.0 " + chr(0x2013) + " 2.4 (maintenance) | Python 3.6+ |\n",
        "| Version | Python |\n| --- | --- |\n| 2.4 and earlier | Python 3.6+ |\n| `v2` | Python 3.7+ |\n",
        "| Release | Python |\n| --- | --- |\n| Hydrogen | Python 3.7+ |\n",
        "| Line | Python |\n| --- | --- |\n| demo 2 | Python 3.7+ |\n",
        "| demo | Python |\n| --- | --- |\n| Ancient | Python 2.7+ |\n",
        "## Compatibility\n\n- demo 2.x supports Python 3.7+.\n- demo 1.x supports Python 2.7+.\n",
        "- Demo 2.4 supports Python 3.7+.\n",
        "- 2.x supports Python 3.7+.\n- Version 1.x supports Python 2.7+.\n",
        "* **1.x**: Python 2.7+\n",
        "Releases before 2.0 supported Python 2.7+.\n",
        "In 2.x, Python 3.7+ was enough.\n",
        "demo 1.4 and earlier support Python 2.7+.\n",
        "Older releases needed Python 3.6+.\n",
        "| Version | Python |\n| --- | --- |\n| 1.x (legacy) | Python 2.7+ |\n",
        "| Version | Python |\n| --- | --- |\n| 2.x series | Python 3.7+ |\n",
        "| Version | Python |\n| --- | --- |\n| 2.4 LTS | Python 3.7+ |\n",
        "| Branch | Requires |\n| --- | --- |\n| 2.4 LTS (until 2025) | Python 3.7+ |\n",
        "| Series | Requires |\n| --- | --- |\n| 2.x - maintained until 2027 | Python >= 3.7 |\n",
        "| Release | Python |\n| --- | --- |\n| 2.4.x LTS, security only | Python 3.7+ |\n",
        "| Version | Python |\n| --- | --- |\n| 2.x (Python 3.7 compatible) | Python 3.7+ |\n",
        "| Version | Python |\n| --- | --- |\n| 2.4 LTS released 2023 | Python 3.7+ |\n",
        "| Version | Python |\n| --- | --- |\n| 2 | Python 3.7+ |\n",
        "| Version | Python |\n| --- | --- |\n| 2.x " + chr(0x2014) + " old stable line | Python 3.7+ |\n",
        "| demo version | Minimum Python |\n| --- | --- |\n| 2.x series (EOL) | 3.7+ |\n",
        "| Version | Python |\n|:---|:---:|\n| **2.x series** | Python 3.7+ |\n",
        "| Python | Version |\n| --- | --- |\n| Python 3.7+ | 2.x series |\n",
        "| Version | Python |\n| --- | --- |\n| 3.x (current) | Python 3.10+ |\n| 2.x series | Python 3.7+ |\n"
        "| 1.x (legacy) | Python 2.7+ |\n",
    ],
)
def test_release_history_in_any_layout_is_not_drift_and_never_rewritten(tmp_path: Path, readme: str) -> None:
    assert _runtime_drift(tmp_path, readme) == ([], [])


@pytest.mark.parametrize(
    ("readme", "line"),
    [
        ("Requires Python 3.8+.\n", 3),
        ("| Requirement | Version |\n| --- | --- |\n| Runtime | Python 3.8+ |\n", 5),
        ("| Package | Version | Notes |\n| --- | --- | --- |\n| Python | 3.8+ | 3.12 (recommended) |\n", 5),
        ("- demo 3.x supports Python 3.8+.\n- demo 2.x supports Python 3.7+.\n", 3),
        ("| demo | Python |\n| --- | --- |\n| 4.x (next) | Python 3.12+ |\n| 3.x | Python 3.8+ |\n", 6),
        ("| demo | Python |\n| --- | --- |\n| 3.0+ | Python 3.8+ |\n", 5),
        ("Python 3.8 or later is required. Tested up to 3.12.\n", 3),
        ("Requires Python 3.8+ and pip v21.\n", 3),
        ("Supports Python 3.8+ and Django 4.x.\n", 3),
        ("Requires Python 3.8+ (version 3.12 recommended).\n", 3),
        ("Python 3.8+ is required; older versions of Python are unsupported.\n", 3),
        ("Since 2.0, demo requires Python 3.8+.\n", 3),
        ("- 2.x supports Python 3.7+.\n\n## Install\n\nRequires Python 3.8+.\n", 7),
        ("| Version | Python |\n| --- | --- |\n| latest | Python 3.8+ |\n| 1.x (legacy) | Python 2.7+ |\n", 5),
        ("| Release | Python |\n| --- | --- |\n| Current (main) | Python 3.8+ |\n| 2.x series | Python 3.7+ |\n", 5),
        ("| Runtime | Supported version |\n| --- | --- |\n| Python | 3.8+ (3.12 recommended) |\n", 5),
    ],
)
def test_current_release_statements_are_still_checked(tmp_path: Path, readme: str, line: int) -> None:
    drift, _ = _runtime_drift(tmp_path, readme)
    assert drift == [f"{line}:python>=3.8"]


def test_release_labels_need_a_declared_version_and_skip_only_history_locations(tmp_path: Path) -> None:
    dynamic = '[project]\nname = "demo"\ndynamic = ["version"]\nrequires-python = ">=3.10"\n'
    assert _runtime_drift(tmp_path / "dynamic", "- demo 3.x supports Python 3.8+.\n", dynamic) == ([], [])
    drift, edits = _runtime_drift(tmp_path / "mixed", "Requires Python 3.8+.\n\n- demo 2.x supports Python 3.8+.\n")
    assert drift == ["3:python>=3.8"] and edits == ["3:3.10"]
    node = _write(
        tmp_path / "node",
        {
            "package.json": '{"name": "@acme/web", "version": "5.0.0", "engines": {"node": ">=18"}}',
            "README.md": "# web\n\n| Release | Node.js |\n| --- | --- |\n| 4.x | Node.js 14+ |\n",
        },
    )
    assert [f for f in run_scan(node, Config(), history=False).findings if f.is_drift] == []


def _cell(text: str) -> ReleaseLabel:
    label = release_cell(text)
    assert label is not None, text
    return label


def test_release_label_parsing() -> None:
    assert _cell("1.x (legacy)") == ReleaseLabel("1.x (legacy)", "exact", (1,))
    assert _cell("2.0 to 2.4") == ReleaseLabel("2.0 to 2.4", "range", (2, 0), (2, 4))
    assert _cell("<2.0") == ReleaseLabel("<2.0", "below", (2, 0))
    assert release_cell("3.12 (recommended)") is None and release_cell("Runtime") is None
    assert _cell("2.4 LTS").covers("2.4.7") and not _cell("2.4 LTS").covers("3.1.0")
    assert _cell("1.0 - 1.9").covers("1.4.0") and not _cell("1.0 - 1.9").covers("2.0")
    assert _cell("2.0+").covers("3.1.0") and _cell("v3").covers("3.1.0") and not _cell("3.x").covers(None)
    assert not ReleaseLabel("older releases").covers("1.0")
    assert _cell("2.4 LTS (until 2025)").low == (2, 4)
    assert _cell("2.4 LTS released 2023") == ReleaseLabel("2.4", "exact", (2, 4))
    assert _cell("latest").covers("3.1.0") and _cell("Current (main)").covers(None)
    assert release_cell("2.x (Python 3.7 compatible)") is None
    assert release_cell("2.x (Python 3.7 compatible)", release_column=True) == ReleaseLabel("2.x", "exact", (2,))
    assert release_cell("2", release_column=True) == ReleaseLabel("2", "exact", (2,))
    assert release_cell("2019 refresh", release_column=True) is None
    assert prose_label("- 2.x supports Python 3.7+.", "Python 3.7+", {}) == ReleaseLabel("2.x", "exact", (2,))
    assert prose_label("Requires Python 3.8+.", "Python 3.8+", {"demo": "1.0"}) is None


def test_stale_table_row_is_reported_but_never_auto_fixed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "lib"\nrequires-python = ">=3.10"\n',
            "README.md": "# lib\n\n| Requirement | Version |\n| --- | --- |\n| Runtime | Python 3.8+ |\n",
        },
    )
    result = run_scan(tmp_path, Config(), history=False)
    runtime = [f.verdict for f in result.findings if f.claim.kind == ClaimKind.runtime_version]
    assert runtime == [Verdict.version_mismatch]
    assert plan_rule_fixes(result).edits == []


def test_setup_py_kwargs_expansion_leaves_extras_unknown(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "setup.py": 'opts = load()\nsetup(name="demo", install_requires=["requests"], **opts)\n',
            "README.md": '# demo\n\n```sh\npip install "demo[socks]"\npip install -e ".[dev]"\n```\n',
        },
    )
    assert [f for f in _findings(tmp_path) if f.verdict == Verdict.unknown_extra] == []


def test_literal_less_subpackage_manifest_stops_project_lookup(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "root"\n\n[project.optional-dependencies]\nweb = ["fastapi"]\n',
            "plugin/setup.py": "from setuptools import setup\nsetup(**kwargs)\n",
            "plugin/README.md": '# plugin\n\n```sh\npip install -e ".[dev]"\n```\n',
            "README.md": '# root\n\n```sh\npip install -e ".[dve]"\n```\n',
        },
    )
    findings = {f.claim.doc_path: f for f in _findings(tmp_path)}
    assert "plugin/README.md" not in findings
    assert findings["README.md"].verdict == Verdict.unknown_extra  # The root project is still checked.
    manifests = load_manifests(tmp_path, _paths(tmp_path))
    assert manifests.for_path("plugin/README.md", "python")[0].path == "plugin/setup.py"


def test_local_module_named_after_a_dependency_still_counts_as_usage(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "requirements.txt": "celery\nredis\ndjango\n",
            "proj/__init__.py": "from .celery import app\n",
            "proj/celery.py": "from celery import Celery\napp = Celery('proj')\n",
            "proj/redis.py": "import redis\nclient = redis.Redis()\n",
            "proj/settings.py": "import django\n",
        },
    )
    result = analyse_usage(tmp_path, _paths(tmp_path), load_manifests(tmp_path, _paths(tmp_path)), DepsConfig())
    assert [i for i in result.issues if i.kind == DependencyIssueKind.unused] == []


def test_constraint_pins_are_not_runtime_dependencies(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "requirements.txt": "flask\n-c constraints.txt\n--constraint pins.txt\n",
            "constraints.txt": "werkzeug==3.0.1\nclick==8.1.7\njinja2==3.1.3\n",
            "pins.txt": "itsdangerous==2.1.2\n",
            "app.py": "import flask\n",
        },
    )
    manifests = load_manifests(tmp_path, _paths(tmp_path))
    assert [m.path for m in manifests.manifests] == ["requirements.txt"]
    groups = {d.name: d.group for d in manifests.manifests[0].dependencies}
    assert groups == {
        "flask": "runtime",
        "werkzeug": "optional",
        "click": "optional",
        "jinja2": "optional",
        "itsdangerous": "optional",
    }
    result = analyse_usage(tmp_path, _paths(tmp_path), manifests, DepsConfig())
    assert [i for i in result.issues if i.kind == DependencyIssueKind.unused] == []


def test_install_next_to_the_project_is_not_missing(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "README.md": """
            # acme-tool

            To run the plotting example: `pip install acme-tool matplotlib`.

            ```sh
            pip install -e . matplotlib
            pip install rich matplotlib seaborn
            ```
            """,
        },
    )
    assert [f for f in _findings(tmp_path) if f.verdict == Verdict.missing] == []


@pytest.mark.parametrize("version", ["2.1.0.dev0", "2.1.0rc1", "2.1.0+local"])
def test_last_release_pin_is_not_flagged_on_a_development_version(tmp_path: Path, version: str) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.replace('version = "2.0.0"', f'version = "{version}"'),
            "README.md": "# acme\n\n```sh\npip install acme-tool==2.0.3\npip install acme-tool==9.0\n```\n",
        },
    )
    assert [f for f in _findings(tmp_path) if f.is_drift] == []


def test_node_self_pin_older_release_is_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "package.json": '{"name": "kit", "version": "2.0.0"}\n',
            "README.md": "# kit\n\n```sh\nnpm install kit@1.4.0\nnpm install kit@^3\n```\n",
        },
    )
    findings = _by_value(_findings(tmp_path))
    assert "kit@1.4.0" not in findings
    assert findings["kit@^3"].verdict == Verdict.version_mismatch


def test_deps_markdown_escapes_untrusted_names_and_text(tmp_path: Path) -> None:
    package = {
        "name": "web",
        "private": True,
        "engines": {"node": ">=18"},
        "dependencies": {"<img src=https://example.invalid/t.png>": "1.0.0", "@acme/core": "^1"},
    }
    _write(
        tmp_path,
        {
            "package.json": json.dumps(package),
            "README.md": "# web\n\nRequires Node.js 12 or later. [click](https://example.invalid)\n",
            "index.js": "console.log('hi')\n",
        },
    )
    markdown = render_markdown(run_deps(tmp_path, Config()))
    assert "&lt;img" in markdown and "<img" not in markdown
    assert "&#64;acme/core" in markdown and "@acme" not in markdown
    assert "[click]" not in markdown


def test_scan_json_omits_empty_dependency_issues(tmp_path: Path) -> None:
    _write(tmp_path / "plain", {"pyproject.toml": PYPROJECT, "README.md": "# t\n\nRequires Python 3.11+.\n"})
    data = json.loads(run_scan(tmp_path / "plain", Config(), history=False).model_dump_json())
    assert "dependency_issues" not in data
    root = _python_repo(tmp_path / "usage")
    with_issues = run_scan(root, Config(deps=DepsConfig(usage=True)), history=False)
    assert json.loads(with_issues.model_dump_json())["dependency_issues"]


def test_invalid_deps_baseline_names_the_real_flag(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{}x", encoding="utf-8")
    with pytest.raises(ValueError, match=r"docs-drift deps --write-baseline <path>"):
        load_deps_baseline(tmp_path / "bad.json")
