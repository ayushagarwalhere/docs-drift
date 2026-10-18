"""Package scoping and editor behavior with simulated file events."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from docs_drift.config import Config
from docs_drift.index.base import CodeIndex
from docs_drift.models import Package
from docs_drift.scanning import package_indexes, run_scan
from docs_drift.watch import IncrementalScanner
from docs_drift.workspaces import detect_packages, package_for


def put(root: Path, path: str, text: str) -> None:
    dest = root / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")


def test_package_indexes_share_repository_path_lookup() -> None:
    index = CodeIndex(source_paths=[f"packages/p{i}/src/main.py" for i in range(500)])
    packages = [Package(name=f"p{i}", root=f"packages/p{i}", language="python") for i in range(20)]
    locals_ = package_indexes(index, packages)
    assert len(locals_) == len(packages)
    assert all(local.path_lookup is index.path_lookup for local in locals_.values())
    assert all(local.folded_paths is index.folded_paths for local in locals_.values())
    assert "packages/p499/src/main.py" in locals_["packages/p0"].path_lookup


@pytest.mark.parametrize(
    ("manifest", "text", "member", "member_text", "language"),
    [
        ("package.json", '{"workspaces":["packages/*"]}', "package.json", '{"name":"web"}', "javascript"),
        ("pnpm-workspace.yaml", "packages:\n - packages/*\n", "package.json", '{"name":"web"}', "javascript"),
        ("package.json", '{"workspaces":{"packages":["packages/*"]}}', "package.json", '{"name":"web"}', "javascript"),
        (
            "pyproject.toml",
            '[tool.uv.workspace]\nmembers=["packages/*"]',
            "pyproject.toml",
            '[project]\nname="web"',
            "python",
        ),
        ("Cargo.toml", '[workspace]\nmembers=["packages/*"]', "Cargo.toml", '[package]\nname="web"', "rust"),
        ("go.mod", "module root", "go.mod", "module web", "go"),
        ("nx.json", "{}", "project.json", '{"name":"web"}', "javascript"),
        ("turbo.json", "{}", "package.json", '{"name":"web"}', "javascript"),
    ],
)
def test_workspace_types(  # noqa: PLR0913, PLR0917 - workspace format matrix.
    tmp_path: Path, manifest: str, text: str, member: str, member_text: str, language: str
) -> None:
    put(tmp_path, manifest, text)
    put(tmp_path, "packages/web/" + member, member_text)
    packages = detect_packages(tmp_path)
    package = package_for("packages/web/docs/intro.md", packages)
    assert package.name == "web" and package.language == language
    assert package_for("README.md", packages).root == "."


def test_local_resolution_and_filter(tmp_path: Path) -> None:
    for name, default in [("web", 8000), ("worker", 9000)]:
        put(tmp_path, f"packages/{name}/package.json", json.dumps({"name": name}))
        put(
            tmp_path,
            f"packages/{name}/cli.py",
            f'import argparse\np=argparse.ArgumentParser()\np.add_argument("--port", default={default})\n',
        )
        put(tmp_path, f"packages/{name}/README.md", f"Use `--port` (default {default}).\n")
    result = run_scan(tmp_path, Config(), package="web")
    assert result.findings
    assert all(f.package == "web" for f in result.findings)
    assert all(not f.matched_symbol or f.matched_symbol.file.startswith("packages/web/") for f in result.findings)
    assert not any(f.is_drift for f in result.findings)


def test_mdx_and_rst_and_separate_docs(tmp_path: Path) -> None:
    code, docs = tmp_path / "code", tmp_path / "docs"
    code.mkdir()
    docs.mkdir()
    put(code, "pyproject.toml", '[project]\nname="tool"\n[project.scripts]\ntool="cli:main"\n')
    put(code, "cli.py", 'import argparse\np=argparse.ArgumentParser()\np.add_argument("--port")\n')
    put(docs, "mint.json", "{}")
    put(docs, "guide.mdx", 'import X from "x";\n# Guide\n<Tabs>\n<Tab>\n```sh\ntool --port 80\n```\n</Tab>\n</Tabs>\n')
    put(
        docs,
        "guide.rst",
        "Guide\n=====\nUse :option:`--port` and :envvar:`APP_HOST`.\n\n.. code-block:: sh\n\n   tool --port 80\n",
    )
    r = run_scan(code, Config(), docs_repo=docs)
    assert r.repo_path == code and r.docs_repo_path == docs
    assert any(c.doc_path.endswith(".mdx") and c.value == "--port" for c in r.claims)
    assert any(
        c.doc_path.endswith(".rst") and c.value == "APP_HOST" and c.extraction_confidence == 0.99 for c in r.claims
    )


def test_incremental_doc_and_source(tmp_path: Path) -> None:
    put(tmp_path, "README.md", "Use `--port`.\n")
    put(tmp_path, "cli.py", 'import argparse\np=argparse.ArgumentParser()\np.add_argument("--port")\n')
    scanner = IncrementalScanner(tmp_path, Config())
    assert not any(f.is_drift for f in scanner.result.findings)
    put(tmp_path, "cli.py", 'import argparse\np=argparse.ArgumentParser()\np.add_argument("--listen")\n')
    fixed, new = scanner.update(["cli.py"])
    assert new and not fixed
    fixed, new = scanner.update(["README.md"], contents={"README.md": "Use `--listen`.\n"})
    assert fixed and not new
    assert all(c.value == "--listen" for c in scanner.result.claims)


def test_lsp_diagnostics_actions_and_definition(tmp_path: Path) -> None:
    pytest.importorskip("pygls")
    from lsprotocol import types as t  # noqa: PLC0415

    from docs_drift.lsp import create_server  # noqa: PLC0415

    put(tmp_path, "README.md", "Use `--port`.\n")
    put(tmp_path, "cli.py", 'import argparse\np=argparse.ArgumentParser()\np.add_argument("--listen")\n')
    server = create_server(tmp_path, Config())
    state = server.drift
    state.update(["README.md"], contents={"README.md": "Use `--port`.\n"})
    uri = (tmp_path / "README.md").as_uri()
    f = state.result.findings[0]
    # Feed a confirmed rename as the history layer would; action safety stays shared with rule fixes.
    from docs_drift.models import Verdict  # noqa: PLC0415

    f.verdict = Verdict.renamed
    f.suggestion = "--listen"
    f.confidence = 0.99
    f.matched_symbol = next(s for s in state.index.symbols if s.name == "--listen")
    state.open_contents["README.md"] = "Use `--port`.\n"
    diagnostics = server.drift_diagnostics(uri)
    assert diagnostics[0].code == "DD002" and diagnostics[0].range.start.character == 5
    actions = server.drift_actions(
        t.CodeActionParams(t.TextDocumentIdentifier(uri), diagnostics[0].range, t.CodeActionContext(diagnostics))
    )
    assert actions and actions[0].edit is not None and actions[0].edit.changes is not None
    assert actions[0].edit.changes[uri][0].new_text == "--listen"
    location = server.drift_definition(t.DefinitionParams(t.TextDocumentIdentifier(uri), t.Position(0, 7)))
    assert location is not None
    assert location.uri == (tmp_path / "cli.py").as_uri()
    state.update(["README.md"], contents={"README.md": "Use `--listen`.\n"})
    assert server.drift_diagnostics(uri) == []


def test_watch_dispatch_without_a_real_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("watchfiles")
    from docs_drift.watch import watch_repository  # noqa: PLC0415

    put(tmp_path, "README.md", "Use `--port`.\n")
    put(tmp_path, "cli.py", 'import argparse\np=argparse.ArgumentParser()\np.add_argument("--port")\n')

    def events(*args: object, **kwargs: object) -> Iterator[set[tuple[int, str]]]:
        put(tmp_path, "cli.py", 'import argparse\np=argparse.ArgumentParser()\np.add_argument("--listen")\n')
        yield {(2, str(tmp_path / "cli.py"))}
        put(tmp_path, "README.md", "Use `--listen`.\n")
        yield {(2, str(tmp_path / "README.md"))}

    monkeypatch.setattr("watchfiles.watch", events)
    watch_repository(tmp_path, Config())
    output = capsys.readouterr().out
    assert "new:" in output and "fixed:" in output
    assert "\x1b" not in output
