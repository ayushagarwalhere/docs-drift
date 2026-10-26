"""docs-drift as a dependency: Python API, assert helper, pytest plugin, pre-commit hooks and ``python -m``."""

import json
import re
import shlex
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import docs_drift
from docs_drift import pytest_plugin, testing
from docs_drift.api import CheckResult, evaluate, format_finding
from docs_drift.baseline import DEFAULT_BASELINE, save_baseline
from docs_drift.cli import app
from docs_drift.config import Config, ConfigError
from docs_drift.docs.discover import DOC_FORMATS
from docs_drift.fix.rules import MIN_RENAME_FIX_CONFIDENCE, rule_edit
from docs_drift.index.base import NAMED_LANGUAGES, SUFFIX_LANGUAGES, language_for
from docs_drift.models import Finding, Verdict
from docs_drift.scanning import run_scan
from docs_drift.testing import assert_no_drift, cached_check, fingerprint, read_cached, write_cached
from tests.fixtures.build_fixtures import build_fixtures

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def repos(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Read-only fixture repositories shared by tests that do not modify them."""
    root = tmp_path_factory.mktemp("dependency-repos")
    build_fixtures(root)
    return root


@pytest.fixture(scope="module")
def py_cli_result(repos: Path) -> CheckResult:
    return docs_drift.check(repos / "py-cli")


def _values(result: CheckResult) -> list[str]:
    return [finding.claim.value for finding in result.findings]


# Python API ---------------------------------------------------------------------------------


def test_public_api_is_exported_lazily() -> None:
    assert docs_drift.__all__ == ["CheckResult", "CheckSummary", "__version__", "check"]
    assert {"check", "CheckResult", "CheckSummary"} <= set(dir(docs_drift))
    assert docs_drift.CheckResult is CheckResult
    with pytest.raises(AttributeError, match="no attribute 'missing_name'"):
        _ = docs_drift.missing_name
    # Importing the package (as the pytest plugin does at startup) must not load the scanner.
    code = "import sys, docs_drift; assert 'docs_drift.scanning' not in sys.modules; print(docs_drift.check.__name__)"
    output = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60)
    assert output.stdout.strip() == "check"


def test_check_reports_failing_drift_sorted_with_summary(py_cli_result: CheckResult) -> None:
    result = py_cli_result
    assert not result.passed
    assert _values(result) == ["--port", "--workers", "migrate"]
    assert [f.claim.line for f in result.findings] == [4, 6, 8]
    summary = result.summary
    assert summary.docs_scanned == ["README.md"]
    assert (summary.drift, summary.failing, summary.below_threshold, summary.baselined) == (4, 3, 1, 0)
    assert summary.unverifiable == len(result.unverifiable) == 1
    assert summary.min_confidence == 0.6
    assert summary.history and not summary.strict
    assert summary.by_verdict["renamed"] == 2
    assert all(finding.introduced_by is not None for finding in result.findings)
    assert result.failures == result.findings


def test_check_result_round_trips_through_json(py_cli_result: CheckResult) -> None:
    restored = CheckResult.model_validate_json(py_cli_result.model_dump_json())
    assert restored == py_cli_result


def test_clean_repository_passes(repos: Path) -> None:
    result = docs_drift.check(str(repos / "clean-repo"))
    assert result.passed
    assert result.findings == []
    assert result.report() == "docs-drift: no drift in 1 documentation file.\n"


def test_history_and_threshold_options(repos: Path) -> None:
    static = docs_drift.check(repos / "py-cli", history=False)
    # Without history, renames are only "missing" at low confidence and fall below the threshold.
    assert _values(static) == ["--workers"]
    assert not static.summary.history
    assert all(finding.introduced_by is None for finding in static.findings)
    permissive = docs_drift.check(repos / "py-cli", history=False, min_confidence=0.4)
    assert set(_values(permissive)) == {"--port", "APP_DEBUG", "--workers", "migrate"}
    assert permissive.summary.min_confidence == 0.4


def test_docs_override_and_validation(repos: Path) -> None:
    result = docs_drift.check(repos / "py-cli", docs="nothing/**/*.md", history=False)
    assert result.passed
    assert result.summary.docs_scanned == []
    assert docs_drift.check(repos / "py-cli", docs=["README.md"], history=False).summary.docs_scanned == ["README.md"]
    with pytest.raises(ValueError, match="at least one documentation glob"):
        docs_drift.check(repos / "py-cli", docs=[])
    with pytest.raises(ValueError, match="between 0 and 1"):
        docs_drift.check(repos / "py-cli", min_confidence=1.5)
    with pytest.raises(ValueError, match="does not exist"):
        docs_drift.check(repos / "absent")


def test_explicit_config_path(repos: Path, tmp_path: Path) -> None:
    config = tmp_path / "strictish.toml"
    config.write_text("min_confidence = 0.93\n", encoding="utf-8")
    result = docs_drift.check(repos / "py-cli", config=config)
    assert _values(result) == ["--workers"]
    broken = tmp_path / "broken.toml"
    broken.write_text("min_confidence = [", encoding="utf-8")
    with pytest.raises(ConfigError):
        docs_drift.check(repos / "py-cli", config=broken)


def test_baseline_and_strict(tmp_path: Path) -> None:
    build_fixtures(tmp_path)
    repo = tmp_path / "py-cli"
    scan = run_scan(repo, Config())
    save_baseline(repo / ".docs-drift-baseline.json", scan.findings)
    accepted = evaluate(scan, baseline=".docs-drift-baseline.json")
    assert accepted.passed
    assert accepted.summary.baselined == 4
    strict = evaluate(scan, baseline=repo / ".docs-drift-baseline.json", strict=True)
    # The baseline only holds drift; an unverifiable claim still fails a strict check.
    assert not strict.passed
    assert [(f.claim.value, f.verdict.value) for f in strict.failures] == [("--port", "unverifiable")]
    assert strict.summary.failing == 1
    with pytest.raises(ValueError, match="between 0 and 1"):
        evaluate(scan, min_confidence=-0.1)
    (repo / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid baseline"):
        evaluate(scan, baseline="bad.json")


def test_report_is_diff_style(py_cli_result: CheckResult) -> None:
    report = py_cli_result.report("README.md")
    assert report.startswith("docs-drift: 3 findings in README.md\n")
    assert "README.md:4: renamed cli_flag `--port` (confidence 0.92)" in report
    assert "  - - `serve` accepts `--port` (default 8000)." in report
    assert "  + - `serve` accepts `--listen` (default 8000)." in report
    assert "  + - The `--workers` default is `8`." in report
    assert "introduced by: " in report and "Rename port to listen (2026-10-02)" in report
    assert "evidence: README.md:4 --port" in report
    assert "ada@" not in report and "Priya" not in report  # no author identities in test output
    assert py_cli_result.report("docs/other.md") == "docs-drift: no drift in docs/other.md.\n"
    assert py_cli_result.report().startswith("docs-drift: 3 findings in 1 documentation file")


def test_format_finding_without_safe_edit(repos: Path) -> None:
    result = docs_drift.check(repos / "py-cli", history=False, min_confidence=0)
    missing = next(f for f in result.findings if f.claim.value == "APP_DEBUG")
    lines = format_finding(missing)
    assert lines[0] == "README.md:7: missing env_var `APP_DEBUG` (confidence 0.50)"
    assert lines[1] == "    - Set `APP_DEBUG` to enable debug logging."
    assert lines[2] == "    closest match: APP_LOG_LEVEL"
    assert not any(line.startswith("  + ") for line in lines)
    unanchored = missing.model_copy(update={"doc_text": None, "suggestion": None})
    assert format_finding(unanchored)[1] == "    APP_DEBUG"
    default = next(f for f in result.findings if f.verdict == "default_changed")
    moved = default.model_copy(update={"doc_text": "The default moved elsewhere."})
    assert not any(line.startswith("  + ") for line in format_finding(moved))


# assert_no_drift ----------------------------------------------------------------------------


def test_assert_no_drift_raises_readable_assertion(repos: Path) -> None:
    with pytest.raises(AssertionError) as raised:
        assert_no_drift(repos / "py-cli", history=False)
    message = str(raised.value)
    assert message.startswith("docs-drift: 1 finding in 1 documentation file")
    assert "README.md:6: default_changed default_value `--workers`" in message
    assert "docs-drift explain <claim-id>" in message


def test_assert_no_drift_returns_result_when_clean(repos: Path) -> None:
    result = assert_no_drift(repos / "clean-repo")
    assert result.passed
    assert result.summary.claims == 4


# Result cache -------------------------------------------------------------------------------


def test_cached_check_hits_and_invalidates_on_source_change(tmp_path: Path) -> None:
    build_fixtures(tmp_path / "repos")
    repo = tmp_path / "repos" / "py-cli"
    cache = tmp_path / "cache" / "check.json"
    first, hit = cached_check(repo, cache_file=cache, history=False)
    assert not hit and cache.is_file()
    second, hit = cached_check(repo, cache_file=cache, history=False)
    assert hit and second == first
    _, hit = cached_check(repo, cache_file=cache, history=False, min_confidence=0.4)
    assert not hit  # options are part of the key
    source = repo / "cli.py"
    source.write_text(source.read_text(encoding="utf-8").replace('"--workers"', '"--threads"'), encoding="utf-8")
    changed, hit = cached_check(repo, cache_file=cache, history=False, min_confidence=0.4)
    assert not hit
    assert "--workers" in _values(changed)
    _, hit = cached_check(repo, cache_file=None, history=False)
    assert not hit
    assert list(cache.parent.glob("*.tmp")) == []


def test_fingerprint_covers_baseline_and_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_fixtures(tmp_path)
    repo = tmp_path / "py-cli"

    def key(baseline: Path | None = None, docs: list[str] | None = None, config: Path | None = None) -> str | None:
        return fingerprint(
            repo, docs=docs, min_confidence=None, history=True, baseline=baseline, config=config, strict=False
        )

    absent = key(baseline=Path("base.json"))
    (repo / "base.json").write_text('{"findings": []}', encoding="utf-8")
    present = key(baseline=repo / "base.json")
    assert absent is not None and present is not None and absent != present
    assert key(docs=["README.md"]) != key()
    broken = tmp_path / "broken.toml"
    broken.write_text("docs = [", encoding="utf-8")
    assert key(config=broken) is None
    monkeypatch.setattr(testing, "MAX_FINGERPRINT_BYTES", 10)
    assert key() is None
    monkeypatch.setattr(testing, "MAX_FINGERPRINT_FILES", 0)
    assert key() is None


def test_cache_reads_reject_stale_or_corrupt_entries(py_cli_result: CheckResult, tmp_path: Path) -> None:
    path = tmp_path / "check.json"
    assert read_cached(path, "key") is None
    write_cached(path, "key", py_cli_result)
    assert read_cached(path, "key") == py_cli_result
    assert read_cached(path, "other") is None
    path.write_text("{not json", encoding="utf-8")
    assert read_cached(path, "key") is None
    path.write_text(json.dumps({"key": "key", "result": {"passed": True}}), encoding="utf-8")
    assert read_cached(path, "key") is None
    path.write_text("[]", encoding="utf-8")
    assert read_cached(path, "key") is None


def test_cache_writes_are_atomic_under_concurrency(py_cli_result: CheckResult, tmp_path: Path) -> None:
    """Simulate pytest-xdist workers sharing one cache file: readers never see a partial entry."""
    path = tmp_path / "shared" / "check.json"
    write_cached(path, "key", py_cli_result)
    errors: list[str] = []

    def writer() -> None:
        for _ in range(15):
            write_cached(path, "key", py_cli_result)

    def reader() -> None:
        for _ in range(30):
            value = read_cached(path, "key")
            if value is not None and value != py_cli_result:
                errors.append("partial or different entry")

    threads = [threading.Thread(target=writer) for _ in range(3)] + [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert read_cached(path, "key") == py_cli_result
    assert list(path.parent.glob("*.tmp")) == []


def test_unwritable_cache_only_costs_speed(py_cli_result: CheckResult, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    directory = tmp_path / "dir"
    directory.mkdir()
    (directory / "check.json").mkdir()  # replacing a directory fails on every platform
    write_cached(directory / "check.json", "key", py_cli_result)
    assert list(directory.glob("*.tmp")) == []


# pytest plugin ------------------------------------------------------------------------------


@pytest.fixture
def plugin_repos(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    build_fixtures(pytester.path)
    return pytester


def test_plugin_is_inert_without_flag(plugin_repos: pytest.Pytester) -> None:
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = py-cli\n")
    result = plugin_repos.runpytest()
    assert result.ret == pytest.ExitCode.NO_TESTS_COLLECTED
    assert "docs-drift:" not in result.stdout.str()


def test_plugin_fails_per_document_with_readable_message(plugin_repos: pytest.Pytester) -> None:
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = py-cli\n")
    result = plugin_repos.runpytest("--docs-drift", "-v")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "docs-drift: */py-cli (history on, strict off, baseline none)",
            "*collected 1 item",
            "py-cli/README.md::docs-drift FAILED*",
            "docs-drift: 3 findings in README.md",
            "README.md:4: renamed cli_flag `--port` (confidence 0.92)",
            "  - - `serve` accepts `--port` (default 8000).",
            "  + - `serve` accepts `--listen` (default 8000).",
            "*1 docs, 10 claims, 3 failing, 0 baselined, 1 below threshold, 1 unverifiable",
        ]
    )
    assert "Traceback" not in result.stdout.str()
    cached = plugin_repos.runpytest("--docs-drift")
    cached.assert_outcomes(failed=1)
    cached.stdout.fnmatch_lines(["*unverifiable (cached)"])


def test_plugin_passes_clean_repository_from_ini(plugin_repos: pytest.Pytester) -> None:
    plugin_repos.makeini("[pytest]\ndocs_drift = true\ndocs_drift_repo = clean-repo\n")
    result = plugin_repos.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_plugin_respects_baseline_and_strict(plugin_repos: pytest.Pytester) -> None:
    repo = plugin_repos.path / "py-cli"
    save_baseline(repo / ".docs-drift-baseline.json", run_scan(repo, Config()).findings)
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = py-cli\n")
    result = plugin_repos.runpytest("--docs-drift")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*baseline */py-cli/.docs-drift-baseline.json)", "*0 failing, 4 baselined*"])
    strict = plugin_repos.runpytest("--docs-drift-strict")
    strict.assert_outcomes(failed=1)
    strict.stdout.fnmatch_lines(["README.md:4: unverifiable default_value `--port` (confidence 0.30)"])


def test_plugin_options_from_ini(plugin_repos: pytest.Pytester) -> None:
    plugin_repos.makeini(
        "[pytest]\ndocs_drift_repo = py-cli\ndocs_drift_history = false\n"
        "docs_drift_min_confidence = 0.4\ndocs_drift_baseline = missing.json\ndocs_drift_strict = false\n"
    )
    result = plugin_repos.runpytest("--docs-drift", "-p", "no:cacheprovider")
    # The configured baseline file does not exist, which is a readable failure rather than a crash.
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*history off*", "docs-drift could not scan *Cannot read baseline*"])


def test_plugin_threshold_override(plugin_repos: pytest.Pytester) -> None:
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = py-cli\ndocs_drift_min_confidence = 0.93\n")
    result = plugin_repos.runpytest("--docs-drift")
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "docs-drift: 1 finding in README.md" in output
    assert "`--workers`" in output and "`migrate`" not in output


@pytest.mark.parametrize("value", ["high", "2"])
def test_plugin_rejects_invalid_threshold(plugin_repos: pytest.Pytester, value: str) -> None:
    plugin_repos.makeini(f"[pytest]\ndocs_drift_repo = py-cli\ndocs_drift_min_confidence = {value}\n")
    result = plugin_repos.runpytest("--docs-drift")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "docs_drift_min_confidence" in result.stderr.str()


def test_plugin_items_can_be_deselected(plugin_repos: pytest.Pytester) -> None:
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = py-cli\n")
    plugin_repos.makepyfile(test_unit="def test_unit():\n    assert True\n")
    result = plugin_repos.runpytest("--docs-drift", "-k", "not docs-drift", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, deselected=1)
    selected = plugin_repos.runpytest("--docs-drift", "-k", "README", "-p", "no:cacheprovider", "--co", "-q")
    selected.stdout.fnmatch_lines(["py-cli/README.md::docs-drift", "1/2 tests collected*"])


def test_plugin_without_documentation_skips(plugin_repos: pytest.Pytester) -> None:
    (plugin_repos.path / "clean-repo" / ".docs-drift.toml").write_text('docs = ["nothing/*.md"]\n', encoding="utf-8")
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = clean-repo\n")
    result = plugin_repos.runpytest("--docs-drift", "-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["SKIPPED * clean-repo*no documentation files matching*"])
    listed = plugin_repos.runpytest("--docs-drift", "--co", "-q", "-p", "no:cacheprovider")
    listed.stdout.fnmatch_lines(["clean-repo/[[]no-documentation[]]::docs-drift"])


def test_plugin_reports_discovery_errors(plugin_repos: pytest.Pytester) -> None:
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = absent\n")
    result = plugin_repos.runpytest("--docs-drift", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*could not discover documentation*"])
    assert "Traceback" not in result.stdout.str()


def test_plugin_marks_items_for_xdist_loadgroup(plugin_repos: pytest.Pytester) -> None:
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = clean-repo\n")
    plugin_repos.makeconftest(
        """
        def pytest_configure(config):
            config.pluginmanager.register(object(), "xdist")
            config.addinivalue_line("markers", "xdist_group(name): fake xdist marker")

        def pytest_collection_finish(session):
            for item in session.items:
                print("GROUP", item.nodeid, item.get_closest_marker("xdist_group").args)
        """
    )
    result = plugin_repos.runpytest("--docs-drift", "-s", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["GROUP clean-repo/README.md::docs-drift ('docs-drift',)"])


def test_plugin_failure_rendering_falls_back_for_other_errors(plugin_repos: pytest.Pytester) -> None:
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = clean-repo\n")
    plugin_repos.makeconftest(
        """
        import docs_drift.pytest_plugin as plugin

        def pytest_runtest_setup(item):
            if isinstance(item, plugin.DocsDriftItem):
                item.runtest = lambda: 1 / 0
        """
    )
    result = plugin_repos.runpytest("--docs-drift", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*ZeroDivisionError*"])


def test_plugin_is_registered_through_entry_point() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["entry-points"]["pytest11"] == {"docs_drift": "docs_drift.pytest_plugin"}
    assert pytest_plugin.ITEM_PREFIX == "docs-drift"


# pre-commit hooks ---------------------------------------------------------------------------


def _hooks() -> list[dict[str, object]]:
    data = yaml.safe_load((ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def test_pre_commit_manifest_is_valid() -> None:
    hooks = _hooks()
    assert [hook["id"] for hook in hooks] == ["docs-drift", "docs-drift-fast"]
    for hook in hooks:
        assert hook["language"] == "python"
        assert hook["pass_filenames"] is False
        assert hook["require_serial"] is True
    clientlib = pytest.importorskip("pre_commit.clientlib")
    clientlib.load_manifest(str(ROOT / ".pre-commit-hooks.yaml"))


def test_pre_commit_file_filter_selects_docs_and_source() -> None:
    pattern = re.compile(str(_hooks()[0]["files"]))
    for path in ["README.md", "docs/guide.rst", "src/pkg/cli.py", "web/app.tsx", "package.json", "Makefile"]:
        assert pattern.search(path), path
    assert pattern.search("config/.env.example")
    for path in ["logo.png", "README.md.orig", "dist/app.whl", ".env"]:
        assert not pattern.search(path), path


@pytest.mark.parametrize(("hook_id", "repo_name", "exit_code"), [
    ("docs-drift", "py-cli", 1),
    ("docs-drift", "clean-repo", 0),
    ("docs-drift-fast", "py-cli", 1),
    ("docs-drift-fast", "clean-repo", 0),
])  # fmt: skip
def test_pre_commit_entries_are_real_cli_invocations(repos: Path, hook_id: str, repo_name: str, exit_code: int) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry = shlex.split(str(next(hook for hook in _hooks() if hook["id"] == hook_id)["entry"]))
    assert entry[0] in project["project"]["scripts"]
    result = CliRunner().invoke(app, ["--repo", str(repos / repo_name), *entry[1:]])
    assert result.exit_code == exit_code, result.output
    if exit_code == 0:
        assert result.output == ""  # --quiet keeps passing hooks silent
    elif hook_id == "docs-drift-fast":
        assert "Introduced by" not in result.output
    else:
        assert "Introduced by" in result.output


def test_dependency_guide_snippets_match_shipped_hooks() -> None:
    guide = (ROOT / "docs" / "as-a-dependency.md").read_text(encoding="utf-8")
    blocks = [yaml.safe_load(block) for block in re.findall(r"```yaml\n(.*?)```", guide, re.S)]
    pre_commit = next(block for block in blocks if isinstance(block, dict) and "repos" in block)
    (repo,) = pre_commit["repos"]
    assert repo["repo"] == "https://github.com/ayushagarwalhere/docs-drift"
    assert repo["rev"] == "v0.1.1"
    assert {hook["id"] for hook in repo["hooks"]} <= {hook["id"] for hook in _hooks()}
    for option in re.findall(r"--docs-drift[a-z-]*", guide):
        assert option in {"--docs-drift", "--docs-drift-strict"}, option
    for ini in set(re.findall(r"\bdocs_drift(?:_[a-z_]+)?\b(?= =)", guide)):
        assert ini in {
            "docs_drift",
            "docs_drift_strict",
            "docs_drift_repo",
            "docs_drift_baseline",
            "docs_drift_history",
            "docs_drift_min_confidence",
        }, ini


# python -m docs_drift and the CLI flag ------------------------------------------------------


def test_python_module_entry_point() -> None:
    # A real interpreter, so this also holds for an installed wheel (runpy under pytest's import hook does not).
    run = subprocess.run(
        [sys.executable, "-m", "docs_drift", "--version"], capture_output=True, text=True, check=False, timeout=120
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == docs_drift.__version__


def test_check_no_history_skips_attribution(repos: Path) -> None:
    result = CliRunner().invoke(app, ["--repo", str(repos / "py-cli"), "--no-color", "check", "--no-history"])
    assert result.exit_code == 1, result.output
    assert "DEFAULT" in result.output
    assert "Introduced by" not in result.output


# Review regressions -------------------------------------------------------------------------


def _added_lines(finding: Finding) -> list[str]:
    return [line for line in format_finding(finding) if line.startswith("  + ")]


def test_proposed_line_uses_word_bounded_unique_rule_edit(py_cli_result: CheckResult) -> None:
    renamed = next(f for f in py_cli_result.findings if f.claim.value == "--port")
    # A prefix collision must not be rewritten: only the real `--port` changes.
    collision = renamed.model_copy(update={"doc_text": "Use `--port-file PATH` or `--port 80`."})
    assert _added_lines(collision) == ["  + Use `--port-file PATH` or `--listen 80`."]
    # Two occurrences are ambiguous for `docs-drift fix`, so no edit is proposed.
    twice = renamed.model_copy(update={"doc_text": "Pass `--port` or `--port=80`."})
    lines = format_finding(twice)
    assert not _added_lines(twice)
    assert "    closest match: --listen" in lines
    # Only a prefix collision and no exact word: nothing to propose.
    prefix_only = renamed.model_copy(update={"doc_text": "Use `--port-file PATH`."})
    assert not _added_lines(prefix_only)
    # Below the rename-fix confidence gate the fixer refuses, so the report does too.
    unsure = renamed.model_copy(update={"confidence": MIN_RENAME_FIX_CONFIDENCE - 0.01})
    assert not _added_lines(unsure)


def test_proposed_line_matches_fixer_for_defaults_with_units(py_cli_result: CheckResult) -> None:
    default = next(f for f in py_cli_result.findings if f.verdict == Verdict.default_changed)
    line = "- The `--timeout` default is `30s`."
    claim = default.claim.model_copy(
        update={"value": "--timeout", "raw_text": "default is `30s`", "expected_default": "30s"}
    )

    def with_code_default(value: str, doc_text: str = line) -> Finding:
        symbol = default.matched_symbol.model_copy(update={"default": value}) if default.matched_symbol else None
        update = {"claim": claim, "suggestion": value, "matched_symbol": symbol, "doc_text": doc_text}
        return default.model_copy(update=update)

    # A unitless code default cannot be placed next to a documented unit: the fixer
    # refuses, so the report must not propose "`60`" (dropping the unit) either.
    unitless = with_code_default("60")
    assert rule_edit(unitless, line) is None
    assert not _added_lines(unitless)
    assert "    closest match: 60" in format_finding(unitless)
    # A comparable duration keeps the documented unit, exactly as `docs-drift fix` writes it.
    minute = with_code_default("1m")
    edit = rule_edit(minute, line)
    assert edit is not None and edit.new_text == "60"
    assert _added_lines(minute) == ["  + - The `--timeout` default is `60s`."]
    # A line the fixer cannot place uniquely gets no proposed edit.
    assert not _added_lines(with_code_default("1m", "- `--timeout` default is `30s` (was `30s`)."))


def test_baselined_repository_passes_plugin_and_hook_entries(plugin_repos: pytest.Pytester) -> None:
    repo = plugin_repos.path / "py-cli"
    save_baseline(repo / DEFAULT_BASELINE, run_scan(repo, Config()).findings)
    plugin_repos.makeini("[pytest]\ndocs_drift_repo = py-cli\n")
    plugin_repos.runpytest("--docs-drift").assert_outcomes(passed=1)
    for hook in _hooks():
        entry = shlex.split(str(hook["entry"]))
        result = CliRunner().invoke(app, ["--repo", str(repo), *entry[1:]])
        assert result.exit_code == 0, (hook["id"], result.output)
        assert result.output == ""


def test_auto_baseline_is_inert_without_file_and_explicit_baseline_wins(tmp_path: Path) -> None:
    build_fixtures(tmp_path)
    repo = tmp_path / "py-cli"
    command = ["--repo", str(repo), "--no-color", "check", "--no-history", "--auto-baseline"]
    assert CliRunner().invoke(app, command).exit_code == 1  # no baseline file: same as a plain check
    save_baseline(repo / DEFAULT_BASELINE, run_scan(repo, Config(), history=False).findings)
    assert CliRunner().invoke(app, command).exit_code == 0
    save_baseline(repo / "empty.json", [])
    explicit = CliRunner().invoke(app, [*command, "--baseline", "empty.json"])
    assert explicit.exit_code == 1, explicit.output
    plain = CliRunner().invoke(app, command[:-1])
    assert plain.exit_code == 1  # without the flag the CLI stays opt-in


def test_pre_commit_file_filter_covers_everything_the_scanner_reads() -> None:
    for hook in _hooks():
        pattern = re.compile(str(hook["files"]))
        suffixes = [*SUFFIX_LANGUAGES, *DOC_FORMATS]
        for suffix in suffixes:
            for path in (f"a{suffix}", f"deep/dir/a{suffix.upper()}"):
                assert pattern.search(path), (hook["id"], path)
        for name in NAMED_LANGUAGES:
            assert pattern.search(name), (hook["id"], name)
            assert pattern.search(f"sub/{name}"), (hook["id"], name)
        for path in ["Dockerfile", "deploy/api.dockerfile", "GNUmakefile", "build.mk", "src/a.mts", "scripts/run.sh"]:
            assert pattern.search(path), path
        assert pattern.search("docs/x.txt") and pattern.search(".docs-drift.toml")
        for path in ["logo.png", "README.md.orig", "dist/app.whl", ".env", ".env.local"]:
            assert not pattern.search(path), path
        # Every path the indexer or doc discovery accepts must trigger the hook.
        for path in ["x/Taskfile.yml", "tox.ini", "a/.env.sample", "web/app.CTS", ".dockerfile"]:
            assert language_for(Path(path)) is not None
            assert pattern.search(path), path
