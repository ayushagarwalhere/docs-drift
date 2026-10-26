"""pytest plugin: ``pytest --docs-drift`` adds one test per documentation file.

The plugin is registered through the ``pytest11`` entry point and is inert until
``--docs-drift`` (or the ``docs_drift`` ini option) enables it. One scan per
session feeds every item; its result is cached under pytest's cache directory,
keyed by every scan input, so unchanged repositories are not rescanned. Cache
writes are atomic, collection is deterministic and each pytest-xdist worker
reuses the shared cache, so the plugin is safe to run with ``-n``.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from _pytest._code.code import ExceptionInfo, TerminalRepr
    from _pytest.cacheprovider import Cache
    from _pytest.terminal import TerminalReporter

    from docs_drift.api import CheckResult

ITEM_PREFIX = "docs-drift"


class DocsDriftFailure(AssertionError):
    """Drift findings for one documentation file; rendered without a traceback."""


class DocsDriftScanError(RuntimeError):
    """The repository could not be scanned; rendered without a traceback."""


@dataclass
class _State:
    repo: Path
    baseline: Path | None
    history: bool
    min_confidence: float | None
    strict: bool
    cache_file: Path | None
    result: CheckResult | None = None
    error: str | None = None
    cache_hit: bool = False
    docs: list[str] = field(default_factory=list)

    def check(self) -> CheckResult:
        """Run (or load) the session's single scan on first use."""
        if self.result is None and self.error is None:
            from docs_drift.errors import DocsDriftError  # noqa: PLC0415 - plugin import stays light.
            from docs_drift.testing import cached_check  # noqa: PLC0415

            try:
                self.result, self.cache_hit = cached_check(
                    self.repo,
                    cache_file=self.cache_file,
                    min_confidence=self.min_confidence,
                    history=self.history,
                    baseline=self.baseline,
                    strict=self.strict,
                )
            except (DocsDriftError, OSError, ValueError) as exc:
                hint = getattr(exc, "hint", None)
                self.error = f"docs-drift could not scan {self.repo}: {exc}" + (f" {hint}" if hint else "")
        if self.result is None:
            raise DocsDriftScanError(self.error)
        return self.result


_STATE = pytest.StashKey[_State]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("docs-drift", "documentation drift checks")
    group.addoption(
        "--docs-drift",
        action="store_true",
        default=False,
        help="Add a test per documentation file that fails on documentation drift.",
    )
    group.addoption(
        "--docs-drift-strict",
        action="store_true",
        default=False,
        help="Like --docs-drift, and also fail on unverifiable claims.",
    )
    parser.addini("docs_drift", "Enable docs-drift tests (same as --docs-drift).", type="bool", default=False)
    parser.addini("docs_drift_strict", "Also fail on unverifiable claims.", type="bool", default=False)
    parser.addini("docs_drift_repo", "Repository to check, relative to the rootdir.", type="string", default="")
    parser.addini(
        "docs_drift_baseline",
        "Baseline JSON relative to the repository (default: .docs-drift-baseline.json when present).",
        type="string",
        default="",
    )
    parser.addini("docs_drift_history", "Inspect Git history (attribution, renames).", type="bool", default=True)
    parser.addini(
        "docs_drift_min_confidence",
        "Override min_confidence from the repository configuration.",
        type="string",
        default="",
    )


def _state(config: pytest.Config) -> _State:
    from docs_drift.baseline import DEFAULT_BASELINE  # noqa: PLC0415 - plugin import stays light.

    strict = bool(config.getoption("docs_drift_strict")) or bool(config.getini("docs_drift_strict"))
    repo_option = str(config.getini("docs_drift_repo")).strip()
    repo = (config.rootpath / repo_option).resolve() if repo_option else config.rootpath
    baseline_option = str(config.getini("docs_drift_baseline")).strip()
    baseline: Path | None = None
    if baseline_option:
        baseline = Path(baseline_option)
        baseline = baseline if baseline.is_absolute() else repo / baseline
    elif (repo / DEFAULT_BASELINE).is_file():
        baseline = repo / DEFAULT_BASELINE
    threshold_option = str(config.getini("docs_drift_min_confidence")).strip()
    try:
        threshold = float(threshold_option) if threshold_option else None
    except ValueError:
        raise pytest.UsageError(f"docs_drift_min_confidence must be a number, not {threshold_option!r}.") from None
    if threshold is not None and not 0 <= threshold <= 1:
        raise pytest.UsageError("docs_drift_min_confidence must be between 0 and 1.")
    cache: Cache | None = getattr(config, "cache", None)
    return _State(
        repo=repo,
        baseline=baseline,
        history=bool(config.getini("docs_drift_history")),
        min_confidence=threshold,
        strict=strict,
        cache_file=cache.mkdir("docs-drift") / "check.json" if cache is not None else None,
    )


def pytest_configure(config: pytest.Config) -> None:
    enabled = (
        bool(config.getoption("docs_drift"))
        or bool(config.getoption("docs_drift_strict"))
        or bool(config.getini("docs_drift"))
    )
    if enabled:
        config.stash[_STATE] = _state(config)


def pytest_report_header(config: pytest.Config) -> str | None:
    state = config.stash.get(_STATE, None)
    if state is None:
        return None
    baseline = state.baseline.as_posix() if state.baseline else "none"
    return (
        f"docs-drift: {state.repo.as_posix()} (history {'on' if state.history else 'off'}, "
        f"strict {'on' if state.strict else 'off'}, baseline {baseline})"
    )


def _discover(state: _State) -> list[str]:
    from docs_drift.config import load_config  # noqa: PLC0415 - plugin import stays light.
    from docs_drift.docs.discover import discover_docs  # noqa: PLC0415

    with warnings.catch_warnings():
        # The scan reports discovery warnings; collection only needs the file list.
        warnings.simplefilter("ignore")
        return sorted(doc.path for doc in discover_docs(state.repo, load_config(state.repo)))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    """Append documentation items before -k/-m selection so they can be selected and deselected."""
    state = config.stash.get(_STATE, None)
    if state is None:
        return
    try:
        state.docs = _discover(state)
    except (OSError, ValueError) as exc:
        state.error = f"docs-drift could not discover documentation in {state.repo}: {exc}"
        state.docs = []
    collector = DocsDriftCollector.from_parent(session, name=ITEM_PREFIX, path=state.repo, nodeid=ITEM_PREFIX)
    # Go through the collection hooks so reporters count the items and show collection errors.
    session.ihook.pytest_collectstart(collector=collector)
    report = session.ihook.pytest_make_collect_report(collector=collector)
    session.ihook.pytest_collectreport(report=report)
    new_items = [node for node in report.result if isinstance(node, pytest.Item)]
    if config.pluginmanager.hasplugin("xdist"):
        # With --dist loadgroup every documentation item runs on one worker and one scan.
        for item in new_items:
            item.add_marker(pytest.mark.xdist_group(ITEM_PREFIX))
    items.extend(new_items)


def pytest_terminal_summary(terminalreporter: TerminalReporter, exitstatus: int, config: pytest.Config) -> None:
    state = config.stash.get(_STATE, None)
    if state is None or state.result is None:
        return
    summary = state.result.summary
    terminalreporter.write_sep("-", "docs-drift")
    terminalreporter.write_line(
        f"{len(summary.docs_scanned)} docs, {summary.claims} claims, {summary.failing} failing, "
        f"{summary.baselined} baselined, {summary.below_threshold} below threshold, "
        f"{summary.unverifiable} unverifiable{' (cached)' if state.cache_hit else ''}"
    )
    for warning in summary.warnings:
        terminalreporter.write_line(f"warning: {warning}")


class DocsDriftCollector(pytest.Collector):
    """Session-level collector for the documentation items."""

    def collect(self) -> list[pytest.Item]:
        state = self.config.stash[_STATE]
        if state.error is not None or not state.docs:
            name = "scan" if state.error is not None else "no-documentation"
            return [self._item(name, None, state.repo)]
        return [self._item(doc, doc, state.repo / doc) for doc in state.docs]

    def _item(self, name: str, doc_path: str | None, path: Path) -> DocsDriftItem:
        # Node IDs read like "docs/guide.md::docs-drift", the convention of file-based linters.
        try:
            location = path.relative_to(self.config.rootpath).as_posix()
        except ValueError:
            location = path.as_posix()
        if doc_path is None:
            location = f"{location.rstrip('/')}/[{name}]" if location != "." else f"[{name}]"
        item = DocsDriftItem.from_parent(
            self, name=name, doc_path=doc_path, path=path, nodeid=f"{location}::{ITEM_PREFIX}"
        )
        item.extra_keyword_matches.add(ITEM_PREFIX)
        if doc_path is None and self.config.stash[_STATE].error is None:
            item.add_marker(pytest.mark.skip(reason="docs-drift found no documentation files matching the globs."))
        return item


class DocsDriftItem(pytest.Item):
    """Fails when the documentation file has drift findings that fail the check."""

    def __init__(self, *, doc_path: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.doc_path = doc_path

    def runtest(self) -> None:
        result = self.config.stash[_STATE].check()
        if self.doc_path is None:
            return
        if result.for_doc(self.doc_path):
            raise DocsDriftFailure(result.report(self.doc_path))

    def repr_failure(
        self,
        excinfo: ExceptionInfo[BaseException],
        style: Any = None,
    ) -> str | TerminalRepr:
        if isinstance(excinfo.value, DocsDriftFailure | DocsDriftScanError):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style=style)

    def reportinfo(self) -> tuple[Path, int, str]:
        # Line 0 (shown as line 1): skip reports need a concrete location.
        return self.path, 0, f"{ITEM_PREFIX}: {self.doc_path or self.name}"
