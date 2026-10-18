"""Fast setup with detected defaults and explicit, non-destructive writes."""

from pathlib import Path

import tomli_w
import typer
import yaml

from docs_drift.baseline import save_baseline
from docs_drift.config import Config
from docs_drift.docs.discover import discover_docs
from docs_drift.errors import DocsDriftError
from docs_drift.index import build_index
from docs_drift.workspaces import docs_patterns

COMMENTS = {
    "docs": "Documentation paths or globs, relative to this repository.",
    "exclude": "Ignore generated, dependency and historical documentation paths.",
    "languages": "Detect source languages automatically, or list language names.",
    "ignore": "Suppress stable claim IDs or kind:value patterns.",
    "min_confidence": "Hide findings below this confidence (0 to 1).",
    "enabled": "Enable optional model assessment; static scans stay local by default.",
    "max_claims": "Maximum prose claims sent for optional model assessment.",
    "concurrency": "Maximum parallel model requests.",
    "timeout": "Model request timeout in seconds.",
    "budget": "Maximum model spending per scan in USD.",
    "redact_paths": "Source paths excluded from model context.",
    "max_commits": "Maximum Git commits searched for drift history.",
    "show_emails": "Include author email addresses in history metadata.",
}


def commented_config(config: Config) -> str:
    lines = ["# docs-drift configuration. Run docs-drift scan to check these docs."]
    for line in tomli_w.dumps(config.model_dump(exclude_none=True)).splitlines():
        key = line.split("=", 1)[0].strip()
        if key in COMMENTS:
            lines.append("# " + COMMENTS[key])
        lines.append(line)
    return "\n".join(lines) + "\n"


def initialize(repo: Path, *, yes: bool = False, quiet: bool = False) -> None:  # noqa: PLR0912 - setup choices.
    target = repo / ".docs-drift.toml"
    if target.exists():
        raise DocsDriftError(
            "A .docs-drift.toml already exists.", "Edit it directly, or move it aside before running init."
        )
    config = Config(docs=list(dict.fromkeys([*Config().docs, *docs_patterns(repo)])))
    docs = discover_docs(repo, config)
    index = build_index(repo, config)
    if not quiet:
        typer.echo("Detected languages: " + (", ".join(index.files_by_language) or "none"))
        typer.echo(
            "Detected frameworks: " + (", ".join(sorted({s.framework for s in index.symbols if s.framework})) or "none")
        )
        typer.echo("Documentation: " + (", ".join(d.path for d in docs) or "none yet"))
    if not yes:
        choices = {str(n): d.path for n, d in enumerate(docs, 1)}
        for number, path in choices.items():
            typer.echo(f"  {number}. {path} [selected]")
        answer = typer.prompt("Docs to check (all, or comma-separated numbers)", default="all")
        selected = [d.path for d in docs] if answer.strip() == "all" else []
        if answer.strip() != "all":
            for number in answer.split(","):
                if number.strip() not in choices:
                    raise DocsDriftError(
                        f"Unknown document selection {number!r}.", "Run init again and choose listed numbers."
                    )
                selected.append(choices[number.strip()])
        config = config.model_copy(update={"docs": selected})
    target.write_text(commented_config(config), encoding="utf-8", newline="\n")
    from docs_drift.scanning import run_scan  # noqa: PLC0415 - optional dependency or shared pipeline import.

    result = run_scan(repo, config)
    if any(f.is_drift for f in result.findings) and (
        yes or typer.confirm("Create a baseline for existing drift?", default=True)
    ):
        save_baseline(repo / ".docs-drift-baseline.json", result.findings)
    workflow = repo / ".github" / "workflows" / "docs-drift.yml"
    check_command = "docs-drift check"
    if (repo / ".docs-drift-baseline.json").exists():
        check_command += " --baseline .docs-drift-baseline.json"
    if not workflow.exists() and (yes or typer.confirm("Add a GitHub Actions check?", default=True)):
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(
            """name: Documentation drift
on: [push, pull_request]
permissions:
  contents: read
jobs:
  docs-drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install docs-drift
      - run: CHECK_COMMAND --format github
""".replace("CHECK_COMMAND", check_command),
            encoding="utf-8",
            newline="\n",
        )
    hook = repo / ".pre-commit-config.yaml"
    if yes or typer.confirm("Add a pre-commit hook?", default=True):
        data = yaml.safe_load(hook.read_text(encoding="utf-8")) if hook.exists() else {}
        data = data or {}
        repos = data.setdefault("repos", [])
        if not any(h.get("id") == "docs-drift" for r in repos for h in r.get("hooks", [])):
            repos.append(
                {
                    "repo": "local",
                    "hooks": [
                        {
                            "id": "docs-drift",
                            "name": "Check documentation drift",
                            "entry": check_command,
                            "language": "system",
                            "pass_filenames": False,
                            "always_run": True,
                        }
                    ],
                }
            )
            hook.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")
    if not quiet:
        typer.echo("Ready. Run docs-drift scan, then pre-commit install to enable the hook.")
