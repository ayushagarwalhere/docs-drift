"""Adversarial parser, path, graph, Markdown, environment and log checks."""

import hashlib
import time
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from docs_drift.cli import app
from docs_drift.config import Config
from docs_drift.docs.defaults import extract_defaults
from docs_drift.docs.discover import discover_docs
from docs_drift.docs.extract_blocks import project_command_names
from docs_drift.docs.extract_inline import extract_claims
from docs_drift.docs.sections import parse_document
from docs_drift.index.base import source_files
from docs_drift.match.commands import _project_bins
from docs_drift.models import DocFile, DocFormat
from docs_drift.report.markdown import escape
from docs_drift.safe_files import metadata_text
from docs_drift.web.git import git_environment
from docs_drift.workspaces import _data


@given(st.text(alphabet="`[]{}*_-#()abc01/ ", min_size=0, max_size=8000))
@example("`" * 8000)
@example("[" * 4000 + "]" * 4000)
@example("*_[a](" * 1000)
@settings(max_examples=60, deadline=None)
def test_extractor_fuzz(text: str) -> None:
    doc = DocFile(path="README.md", content_hash=hashlib.sha256(text.encode()).hexdigest(), format=DocFormat.markdown)
    # Each extractor gets its own 100 ms CPU budget; a combined wall deadline
    # would also count the other two calls and scheduler contention in CI.
    start = time.process_time()
    try:
        parsed = parse_document(doc, text)
    except ValueError as exc:
        assert "parser" in str(exc)
        assert time.process_time() - start < 0.1
        return
    assert time.process_time() - start < 0.1
    start = time.process_time()
    claims = extract_claims(parsed).claims
    assert time.process_time() - start < 0.1
    start = time.process_time()
    extract_defaults(parsed, claims)
    assert time.process_time() - start < 0.1


def test_symlink_escape_and_env_exclusion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text('SECRET="never read"')
    try:
        (repo / "link.py").symlink_to(outside)
        (repo / "docs").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable; same test runs on Linux CI.")
    (repo / ".env").write_text("SECRET=never_read")
    assert source_files(repo, []) == []
    assert not discover_docs(repo, Config())


def test_mentions_images_and_secret_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    escaped = escape("@everyone ![tracking](https://evil.test/pixel) <img src=x>")
    assert "@everyone" not in escaped and "![" not in escaped and "<img" not in escaped
    monkeypatch.setenv("OPENROUTER_API_KEY", "TOP_SECRET_TOKEN")
    assert "OPENROUTER_API_KEY" not in git_environment(tmp_path)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError('password="HIDDEN_VALUE"')

    monkeypatch.setattr("docs_drift.scanning.run_scan", fail)
    output = CliRunner().invoke(app, ["scan", "--repo", str(tmp_path)])
    assert "HIDDEN_VALUE" not in output.output
    assert "HIDDEN_VALUE" not in (tmp_path / ".docs-drift/last-error.log").read_text()
    assert "TOP_SECRET_TOKEN" not in (tmp_path / ".docs-drift/last-error.log").read_text()


def test_deep_configuration_is_not_indexed(tmp_path: Path) -> None:
    (tmp_path / "deep.json").write_text("[" * 10000 + "]" * 10000)
    (tmp_path / ".env").write_text("password=secret")
    assert source_files(tmp_path, []) == []


def test_metadata_symlinks_are_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"name":"DO_NOT_READ","bin":"secret.py"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        (repo / "package.json").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    assert _data(repo / "package.json") == {}
    assert "DO_NOT_READ" not in project_command_names(repo)[0]
    assert "DO_NOT_READ" not in _project_bins(repo)
    with pytest.raises(OSError, match="symbolic link"):
        metadata_text(repo / "package.json")
