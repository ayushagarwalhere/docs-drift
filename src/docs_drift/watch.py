"""In-memory incremental matching, independently testable without a watcher."""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock

from docs_drift.config import Config
from docs_drift.docs.pipeline import scan_claims
from docs_drift.index import build_index
from docs_drift.match.engine import _KINDS
from docs_drift.models import Claim, Finding, SymbolKind
from docs_drift.scanning import scoped_findings
from docs_drift.workspaces import detect_packages


def candidate_kinds(claim: Claim) -> tuple[SymbolKind, ...]:
    if claim.kind == "default_value":
        return {
            "cli_flag": (SymbolKind.cli_flag,),
            "env_var": (SymbolKind.env_var,),
            "config_key": (SymbolKind.config_key,),
        }.get(claim.qualifier or "", ())
    if claim.kind == "code_command":
        return (SymbolKind.cli_command, SymbolKind.cli_flag, SymbolKind.package_script, SymbolKind.make_target)
    return _KINDS.get(claim.kind, ())


class IncrementalScanner:
    def __init__(self, repo: Path, config: Config) -> None:
        self.repo, self.config = repo.resolve(), config
        self.index = build_index(self.repo, config)
        self.packages = detect_packages(self.repo)
        self.result = scan_claims(self.repo, config)
        self.result.findings, _ = scoped_findings(
            self.result.claims, self.index, self.repo, self.packages, history=False
        )
        self.result.symbols = self.index.symbols
        self.lock = RLock()
        self.generation = 0
        self.open_contents: dict[str, str] = {}
        self._adjacency()

    def _adjacency(self) -> None:
        self.adjacency: dict[str, set[str]] = {}
        for f in self.result.findings:
            # Candidate adjacency includes failed candidates, not just the winning match.
            kinds = candidate_kinds(f.claim)
            files = {s.file for k in kinds for s in self.index.by_kind.get(k, [])}
            files.update(e.file for e in f.evidence if e.file != f.claim.doc_path)
            if f.matched_symbol:
                files.add(f.matched_symbol.file)
            for file in files:
                self.adjacency.setdefault(file, set()).add(f.claim.claim_id)

    def update(
        self, paths: Sequence[str | Path], *, contents: dict[str, str] | None = None
    ) -> tuple[list[Finding], list[Finding]]:
        with self.lock:
            before = {f.claim.claim_id: f for f in self.result.findings if f.is_drift}
            changed = set()
            for changed_path in paths:
                path = Path(changed_path)
                absolute = path if path.is_absolute() else self.repo / path
                if absolute.resolve().is_relative_to(self.repo):
                    changed.add(absolute.resolve().relative_to(self.repo).as_posix())
            doc_paths = {p for p in changed if Path(p).suffix in {".md", ".markdown", ".mdx", ".rst", ".txt"}}
            if contents:
                self.open_contents.update(contents)
            if doc_paths:
                partial = scan_claims(
                    self.repo,
                    self.config,
                    doc_paths=doc_paths,
                    contents={p: t for p, t in self.open_contents.items() if p in doc_paths},
                )
                self.result.claims = [c for c in self.result.claims if c.doc_path not in doc_paths] + partial.claims
                self.result.docs = [d for d in self.result.docs if d.path not in doc_paths] + partial.docs
                self.result.sections = [
                    s for s in self.result.sections if s.doc_path not in doc_paths
                ] + partial.sections
            affected = {c.claim_id for c in self.result.claims if c.doc_path in doc_paths}
            sources = changed - doc_paths
            if sources:
                for file in sources:
                    affected.update(self.adjacency.get(file, set()))
                # The repository cache re-parses only changed files.
                old = self.index
                self.index = build_index(self.repo, self.config)
                new_kinds = {s.kind for s in self.index.symbols if s.file in sources}
                new_kinds.update(s.kind for s in old.symbols if s.file in sources)
                affected.update(c.claim_id for c in self.result.claims if set(candidate_kinds(c)) & new_kinds)
            changed_claims = [c for c in self.result.claims if c.claim_id in affected]
            items, _ = scoped_findings(changed_claims, self.index, self.repo, self.packages, history=False)
            valid = {c.claim_id for c in self.result.claims}
            self.result.findings = [
                f for f in self.result.findings if f.claim.claim_id in valid and f.claim.claim_id not in affected
            ] + items
            self.result.symbols = self.index.symbols
            self._adjacency()
            self.generation += 1
            after = {f.claim.claim_id: f for f in self.result.findings if f.is_drift}
            fixed = [f for key, f in before.items() if key not in after]
            new = [
                f
                for key, f in after.items()
                if key not in before or (f.verdict, f.suggestion) != (before[key].verdict, before[key].suggestion)
            ]
            return fixed, new

    def enrich(self, findings: list[Finding], generation: int) -> None:
        from docs_drift.cli import _full_findings  # noqa: PLC0415 - optional dependency or shared pipeline import.

        items, _ = _full_findings(
            [f.claim for f in findings], self.index, self.repo, initial=findings, history_config=self.config.history
        )
        with self.lock:
            if generation == self.generation:
                enriched = {f.claim.claim_id: f for f in items}
                self.result.findings = [enriched.get(f.claim.claim_id, f) for f in self.result.findings]


def watch_repository(repo: Path, config: Config, *, quiet: bool = False) -> None:
    import sys  # noqa: PLC0415 - optional dependency or shared pipeline import.

    import typer  # noqa: PLC0415 - optional dependency or shared pipeline import.

    try:
        from watchfiles import watch  # noqa: PLC0415 - optional dependency or shared pipeline import.
    except ImportError:
        from docs_drift.errors import DocsDriftError  # noqa: PLC0415 - optional dependency or shared pipeline import.

        raise DocsDriftError(
            "Watch support is not installed.", 'Install it with pip install "docs-drift[watch]".'
        ) from None
    scanner = IncrementalScanner(repo, config)
    if not quiet:
        typer.echo("Watching documentation and source files (300 ms debounce). Press Ctrl+C to stop.")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="docs-drift-history") as pool:
        for events in watch(repo, debounce=300, step=50):
            paths = [
                path
                for _, path in events
                if Path(path).resolve().is_relative_to(scanner.repo)
                and ".docs-drift" not in Path(path).resolve().relative_to(scanner.repo).parts
            ]
            if not paths:
                continue
            fixed, new = scanner.update(paths)
            if not quiet:
                for label, items in [("✓ fixed", fixed), ("✗ new", new)]:
                    for f in items:
                        typer.echo(f"{label}: {f.claim.doc_path}:{f.claim.line} {f.claim.value}")
                summary = f"{sum(f.is_drift for f in scanner.result.findings)} drift findings · watching"
                typer.echo(("\r\033[2K" if sys.stdout.isatty() else "") + summary)
            if new:
                pool.submit(scanner.enrich, new, scanner.generation)
