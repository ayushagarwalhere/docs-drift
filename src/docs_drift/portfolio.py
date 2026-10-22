"""Sequential self-hosted scans of explicit local checkouts, with a static dashboard."""

import json
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict, Field, model_validator

from docs_drift.baseline import FindingBaseline, load_baseline
from docs_drift.config import load_config
from docs_drift.llm.privacy import mask_secrets
from docs_drift.policy import GatePolicy, GateSummary, evaluate_gate
from docs_drift.report import render
from docs_drift.safe_files import metadata_text
from docs_drift.scanning import run_scan


class RepositorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
    path: str = Field(min_length=1)
    baseline: str | None = None
    require_git: bool = True
    min_confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    max_findings: int = Field(default=0, ge=0, strict=True)


class PortfolioManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    repositories: list[RepositorySpec] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_names(self) -> "PortfolioManifest":
        if len({repo.name for repo in self.repositories}) != len(self.repositories):
            raise ValueError("Repository names must be unique.")
        return self


class RepositoryOutcome(BaseModel):
    name: str
    status: Literal["passed", "failed", "report_only", "error"]
    gate: GateSummary | None = None
    report: str | None = None
    error: str | None = None


class PortfolioResult(BaseModel):
    schema_version: Literal[1] = 1
    generated_at: str
    run_id: str
    repositories: list[RepositoryOutcome]

    @property
    def exit_code(self) -> int:
        if any(repo.status == "error" for repo in self.repositories):
            return 2
        return int(any(repo.status == "failed" for repo in self.repositories))


def _contained(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if Path(value).is_absolute() or not path.is_relative_to(root):
        raise ValueError("Repository and baseline paths must be relative and stay inside their selected root.")
    return path


def scan_portfolio(workspace: Path, manifest_path: Path, output: Path, *, report_only: bool = False) -> PortfolioResult:
    """No cloning, target execution, hosted storage or model invocation.

    Validate the whole manifest before work starts. A scan error remains an error
    in the dashboard while the other repositories continue. Reports are written
    under a new run ID, so a previous run cannot impersonate a failed scan.
    """
    workspace, output = workspace.resolve(), output.resolve()
    # Windows PowerShell commonly saves UTF-8 files with a BOM.
    manifest = PortfolioManifest.model_validate(tomllib.loads(metadata_text(manifest_path).removeprefix("\ufeff")))
    selected: list[tuple[RepositorySpec, Path, FindingBaseline | None]] = []
    seen: set[Path] = set()
    for spec in manifest.repositories:
        repo = _contained(workspace, spec.path)
        if not repo.is_dir():
            raise ValueError(f"Repository {spec.name} is not an existing local directory.")
        if spec.require_git and not (repo / ".git").exists():
            raise ValueError(
                f"Repository {spec.name} must point to a checkout root with a .git marker. "
                "For an intentional source snapshot, explicitly set require_git=false."
            )
        if repo in seen:
            raise ValueError("The same checkout cannot appear twice in a portfolio.")
        if output.is_relative_to(repo):
            raise ValueError("The portfolio report directory must be outside every scanned checkout.")
        seen.add(repo)
        baseline = load_baseline(_contained(repo, spec.baseline)) if spec.baseline is not None else None
        selected.append((spec, repo, baseline))
    run_id = "run-" + uuid.uuid4().hex
    run_dir = output / run_id
    run_dir.mkdir(parents=True)
    outcomes: list[RepositoryOutcome] = []
    for spec, repo, baseline in selected:
        try:
            config = load_config(repo)
            policy = GatePolicy(
                min_confidence=config.min_confidence if spec.min_confidence is None else spec.min_confidence,
                max_findings=spec.max_findings,
                fail_on_drift=not report_only,
            )
            scan = run_scan(repo, config, no_cache=True)
            evaluation = evaluate_gate(scan, policy, baseline)
            directory = run_dir / ("repo-" + spec.name)
            directory.mkdir()
            for format, suffix in [("html", "html"), ("json", "json"), ("sarif", "sarif")]:
                (directory / ("scan." + suffix)).write_text(render(scan, format), encoding="utf-8")
            (directory / "gate.json").write_text(evaluation.summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
            outcomes.append(
                RepositoryOutcome(
                    name=spec.name,
                    status=evaluation.summary.status,
                    gate=evaluation.summary,
                    report=f"{run_id}/repo-{spec.name}/scan.html",
                )
            )
        except Exception as exc:  # A single repository must not prevent the rest of the organization's scans.
            outcomes.append(RepositoryOutcome(name=spec.name, status="error", error=mask_secrets(str(exc))))
    result = PortfolioResult(generated_at=datetime.now(UTC).isoformat(), run_id=run_id, repositories=outcomes)
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "report"), autoescape=select_autoescape(["html"])
    )
    html = environment.get_template("portfolio.html").render(result=result)
    payload = json.dumps(result.model_dump(mode="json"), indent=2) + "\n"
    # Keep immutable per-run summaries, then replace the latest dashboard atomically.
    for directory in (run_dir, output):
        for name, content in (("index.html", html), ("portfolio.json", payload)):
            rendered_content = content
            # The archived index is one directory closer to its report targets.
            if directory == run_dir and name == "index.html":
                rendered_content = environment.get_template("portfolio.html").render(result=result, archived=True)
            temporary = directory / (name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(rendered_content, encoding="utf-8")
            temporary.replace(directory / name)
    return result
