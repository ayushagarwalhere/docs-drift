"""Disposable scan process. Only JSON status messages are written to stdout."""

import json
import sys
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

from docs_drift.config import Config, HistoryConfig
from docs_drift.history_cache import CACHE_DISABLED
from docs_drift.limits import MAX_FILE_BYTES, MAX_LINE_CHARS, MAX_NESTING, MAX_RESULT_BYTES
from docs_drift.llm.privacy import mask_secrets
from docs_drift.models import ScanResult
from docs_drift.web.git import JobError, checkout, disk_bytes, resolve
from docs_drift.web.settings import Settings
from docs_drift.web.validation import validate_repository


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


def public_result(result: ScanResult, url: str) -> ScanResult:
    data = scrub(result.model_dump(mode="json"))
    data["repo_path"] = url.rsplit("/", 1)[-1]
    data["docs_repo_path"] = None
    data["remote_url"] = url
    for finding in data["findings"]:
        if finding["introduced_by"]:
            finding["introduced_by"]["author_name"] = "Author hidden"
            finding["introduced_by"]["author_email"] = None
    return ScanResult.model_validate(data)


def execute(repo: Path, settings: Settings, emit: Any) -> ScanResult:
    from docs_drift.scanning import run_scan  # noqa: PLC0415 - worker-only scanner import.

    # Reject pathological parser input before any extractor or JSON/TOML parser sees it.
    for path in repo.rglob("*"):
        if not path.is_file() or ".git" in path.relative_to(repo).parts or path.is_symlink():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            raise JobError(
                "too_large",
                "A source or documentation file exceeds the 1 MB demo limit.",
                "Scan this repository locally or choose a smaller example.",
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(len(line) > MAX_LINE_CHARS for line in text.splitlines()):
            raise JobError(
                "input_limit",
                "A file contains an excessively long line.",
                "Remove generated/minified content or scan locally.",
            )
        if path.suffix in {".json", ".yaml", ".yml", ".toml"}:
            depth = 0
            for character in text:
                depth += (character in "[{") - (character in "]}")
                if depth > MAX_NESTING:
                    raise JobError(
                        "input_limit",
                        "A configuration file is nested too deeply.",
                        "Scan locally after reviewing the input.",
                    )
    token = CACHE_DISABLED.set(True)
    try:
        result = run_scan(
            repo,
            Config(history=HistoryConfig(max_commits=settings.history_commits)),
            stage_callback=emit,
            no_cache=True,
        )
    finally:
        CACHE_DISABLED.reset(token)
    if not result.docs:
        raise JobError(
            "no_docs", "No documentation files were found.", "Try a repository with a README or a supported docs site."
        )
    result.warnings.append(
        f"Public demo history uses a {settings.history_days}-day window or a {settings.history_commits}-commit "
        "fallback for inactive projects; lookups are capped and blobs download on demand."
    )
    return result


def main() -> None:
    request = json.loads(sys.stdin.readline())
    root = Path(request["workspace"]).resolve()
    values = {k: v for k, v in request["settings"].items() if k in {f.name for f in fields(Settings)}}
    values["data_dir"] = Path(values["data_dir"])
    settings = Settings(**values)
    repository = validate_repository(request["repo_url"], request.get("ref"))
    start = time.monotonic()

    def emit(stage: str, counts: dict[str, int] | None = None) -> None:
        print(json.dumps({"status": stage, "counts": counts or {}}), flush=True)

    try:
        if sys.platform != "win32":
            import resource  # noqa: PLC0415 - Unix-only resource limits.

            resource.setrlimit(resource.RLIMIT_AS, (536_870_912, 536_870_912))
            resource.setrlimit(resource.RLIMIT_CPU, (115, 120))
        emit("cloning")
        deadline = start + settings.clone_timeout
        sha = resolve(repository, root, settings, deadline)
        print(json.dumps({"resolved_sha": sha}), flush=True)
        if sys.stdin.readline().strip() != "continue":
            return
        repo = checkout(repository, sha, root, settings, deadline)
        clone_seconds, downloaded = time.monotonic() - start, disk_bytes(root)
        result = public_result(execute(repo, settings, emit), repository.url)
        data = result.model_dump_json()
        if len(data.encode()) > MAX_RESULT_BYTES:
            raise JobError(
                "too_large",
                "The report exceeds the demo output limit.",
                "Scan a smaller repository or run the CLI locally.",
            )
        (root / "result.json").write_text(data, encoding="utf-8")
        print(json.dumps({"status": "done", "clone_seconds": clone_seconds, "download_bytes": downloaded}), flush=True)
    except JobError as exc:
        print(
            json.dumps({"status": "failed", "error": {"kind": exc.kind, "message": str(exc), "hint": exc.hint}}),
            flush=True,
        )
    except Exception:
        # Never send repository content, environment values or tracebacks to public logs.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {
                        "kind": "scan",
                        "message": "This repository could not be scanned safely.",
                        "hint": "Try an example or run the CLI locally for more detail.",
                    },
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
