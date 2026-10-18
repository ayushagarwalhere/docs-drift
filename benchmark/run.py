"""Freeze scanner inputs, run pinned repositories, and prepare exclusively human labeling sheets."""

import argparse
import hashlib
import json
import random
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

from docs_drift import __version__
from docs_drift.config import Config
from docs_drift.models import ScanResult

ROOT = Path(__file__).resolve().parents[1]
MIN_AUDIT_CHARACTERS = 20
DRIFT = {"missing", "renamed", "default_changed", "invalid_command"}


def source_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".scm"}:
            digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def freeze(run: Path, *, finding_seed: int = 20261026, recall_seed: int = 20261027) -> dict[str, Any]:
    if (run / "stamp.json").exists():
        stamp: dict[str, Any] = json.loads((run / "stamp.json").read_text(encoding="utf-8"))
        if stamp["source_sha256"] != source_digest():
            raise ValueError(
                "Scanner changed: create a new dated run and a different sample seed; old results are preserved."
            )
        if stamp["protocol_sha256"] != hashlib.sha256((ROOT / "benchmark/PROTOCOL.md").read_bytes()).hexdigest():
            raise ValueError("Protocol changed; use a new run.")
        return stamp
    run.mkdir(parents=True)
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True)
    stamp = {
        "run_id": run.name,
        "date": datetime.now(UTC).date().isoformat(),
        "version": __version__,
        "scanner_commit": git.stdout.strip(),
        "dirty": bool(dirty.stdout),
        "source_sha256": source_digest(),
        "protocol_sha256": hashlib.sha256((ROOT / "benchmark/PROTOCOL.md").read_bytes()).hexdigest(),
        "labeler": None,
        "finding_seed": finding_seed,
        "recall_seed": recall_seed,
        "config": Config().model_dump(mode="json"),
    }
    (run / "stamp.json").write_text(json.dumps(stamp, indent=2) + "\n")
    (run / "PROTOCOL.md").write_bytes((ROOT / "benchmark/PROTOCOL.md").read_bytes())
    with ZipFile(run / "scanner-source.zip", "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "src").rglob("*")):
            if path.is_file() and path.suffix in {".py", ".scm"}:
                archive.write(path, path.relative_to(ROOT).as_posix())
        archive.write(ROOT / "pyproject.toml", "pyproject.toml")
        archive.write(ROOT / "uv.lock", "uv.lock")
        archive.write(ROOT / "README.md", "README.md")
        archive.write(ROOT / "LICENSE", "LICENSE")
    return stamp


def scan_all(
    run: Path,
    *,
    sweep: bool = False,
    finding_seed: int = 20261026,
    recall_seed: int = 20261027,
    timeout_seconds: int = 300,
) -> None:
    if not sweep:
        freeze(run, finding_seed=finding_seed, recall_seed=recall_seed)
    run.mkdir(parents=True, exist_ok=True)
    manifest = ROOT / ("benchmark/sweep-repos.json" if sweep else "benchmark/repos.json")
    repos = json.loads(manifest.read_text(encoding="utf-8"))
    (run / "repos.json").write_text(json.dumps(repos, indent=2) + "\n")
    statuses = json.loads((run / "status.json").read_text(encoding="utf-8")) if (run / "status.json").exists() else {}
    for repo in repos:
        name = repo["name"]
        if statuses.get(name, {}).get("status") == "done":
            continue
        previous = statuses.get(name)
        if not sweep:
            freeze(run)
        slug = name.replace("/", "--")
        out = run / f"{slug}.json"
        command = [sys.executable, "-m", "benchmark.probe", str(ROOT / repo["local_path"]), "--out", str(out)]
        if sweep:
            command.append("--no-history")
        print("scanning", name, flush=True)
        if repo.get("acquisition_status") not in {"done", "existing"}:
            statuses[name] = {"status": "acquisition_failed", "failure": repo.get("failure", "not acquired")}
        else:
            with (run / f"{slug}.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                try:
                    process.wait(timeout=timeout_seconds)
                    statuses[name] = {
                        "status": "done" if process.returncode == 0 else "scan_crash",
                        "exit_code": process.returncode,
                        "command": command,
                    }
                except subprocess.TimeoutExpired:
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False
                        )
                    else:
                        process.kill()
                    process.wait()
                    statuses[name] = {"status": "timeout", "command": command}
        statuses[name]["timeout_seconds"] = timeout_seconds
        if previous:
            statuses[name]["previous_attempt"] = previous
        (run / "status.json").write_text(json.dumps(statuses, indent=2) + "\n")
        print(name, statuses[name]["status"], flush=True)
    measurements = {
        repo["name"]: json.loads(
            (run / (repo["name"].replace("/", "--") + ".measurement.json")).read_text(encoding="utf-8")
        )
        for repo in repos
        if statuses.get(repo["name"], {}).get("status") == "done"
    }
    (run / "measurements.json").write_text(json.dumps(measurements, indent=2) + "\n")


def rng(seed: int, repo: str) -> random.Random:
    return random.Random(seed + int(hashlib.sha256(repo.encode()).hexdigest()[:16], 16))


def sample(items: list[Any], count: int, seed: int, name: str) -> list[Any]:
    return rng(seed, name).sample(items, min(count, len(items)))


def link(repo: dict[str, Any], path: str, line: int) -> str:
    return f"{repo['repo_url']}/blob/{repo['sha']}/{quote(path, safe='/')}#L{line}"


def prepare_labels(run: Path) -> None:  # noqa: PLR0912 - fixed sampling strata.
    stamp = freeze(run)
    rows: list[dict[str, Any]] = []
    populations: dict[str, Any] = {}
    repos = json.loads((run / "repos.json").read_text(encoding="utf-8"))
    statuses = json.loads((run / "status.json").read_text(encoding="utf-8"))
    for repo in repos:
        name = repo["name"]
        if statuses.get(name, {}).get("status") != "done":
            continue
        result = ScanResult.model_validate_json((run / (name.replace("/", "--") + ".json")).read_text(encoding="utf-8"))
        eligible = sorted(
            [f for f in result.findings if f.verdict in DRIFT and f.confidence >= Config().min_confidence],
            key=lambda f: f.claim.claim_id,
        )
        precision = sample(eligible, 25, stamp["finding_seed"], name)
        populations[name] = {"precision": len(eligible), "precision_sample": len(precision), "recall": {}}
        for finding in precision:
            claim = finding.claim
            rows.append(
                {
                    "repo": name,
                    "sample": "precision",
                    "stratum": "drift",
                    "id": claim.claim_id,
                    "doc_path": claim.doc_path,
                    "line": claim.line,
                    "statement": finding.doc_text or claim.raw_text,
                    "doc_link": link(repo, claim.doc_path, claim.line),
                    "tool_verdict": finding.verdict.value,
                    "suggestion": finding.suggestion or "",
                    "code_links": [link(repo, e.file, e.line) for e in finding.evidence if e.file != claim.doc_path],
                    "confidence": finding.confidence,
                }
            )
        extracted = {(claim.doc_path, location.line) for claim in result.claims for location in claim.locations}
        extracted.update((claim.doc_path, claim.line) for claim in result.claims)
        ok: list[dict[str, Any]] = [
            {
                "id": f.claim.claim_id,
                "doc_path": f.claim.doc_path,
                "line": f.claim.line,
                "statement": f.doc_text or f.claim.raw_text,
                "tool_verdict": "ok",
                "code_links": [link(repo, e.file, e.line) for e in f.evidence if e.file != f.claim.doc_path],
            }
            for f in result.findings
            if f.verdict == "ok"
        ]
        unextracted: list[dict[str, Any]] = []
        for doc in result.docs:
            path = ROOT / repo["local_path"] / doc.path
            for line, text in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if (
                    (doc.path, line) not in extracted
                    and len(text.strip()) >= MIN_AUDIT_CHARACTERS
                    and not text.lstrip().startswith(("#", "---", "===", "```"))
                ):
                    unextracted.append(
                        {
                            "id": f"{doc.path}:{line}",
                            "doc_path": doc.path,
                            "line": line,
                            "statement": text,
                            "tool_verdict": "not extracted",
                            "code_links": [],
                        }
                    )
        pools = {"ok": sorted(ok, key=lambda x: x["id"]), "unextracted": sorted(unextracted, key=lambda x: x["id"])}
        counts = {"ok": min(15, len(ok)), "unextracted": min(15, len(unextracted))}
        for stratum in ("ok", "unextracted"):
            other = "unextracted" if stratum == "ok" else "ok"
            counts[stratum] = min(len(pools[stratum]), 30 - counts[other])
        for stratum, pool in pools.items():
            chosen = sample(pool, counts[stratum], stamp["recall_seed"], name + ":" + stratum)
            populations[name]["recall"][stratum] = {"population": len(pool), "selected": len(chosen)}
            for candidate in chosen:
                rows.append(
                    {
                        **candidate,
                        "repo": name,
                        "sample": "recall",
                        "stratum": stratum,
                        "doc_link": link(repo, candidate["doc_path"], candidate["line"]),
                        "suggestion": "",
                    }
                )
    for row in rows:
        row["row_id"] = hashlib.sha256((run.name + row["repo"] + row["sample"] + row["id"]).encode()).hexdigest()[:20]
    (run / "samples.json").write_text(json.dumps({"rows": rows, "populations": populations}, indent=2) + "\n")
    lines = [
        f"# Human labeling sheet — {run.name}",
        "",
        "Labeler: <!-- enter your human name -->",
        "",
        "Precision verdicts: true / false. Recall verdicts: stale / current / not-a-claim. Leave no AI verdicts here.",
        "False-positive note prefixes: third-party:, historical:, example:, index-miss:, dynamic:, other:.",
        "",
        "| ID | Repo / sample | Exact documentation | Statement | Code evidence | "
        "Tool verdict | Suggestion | Human verdict | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def cell(text: Any) -> str:
        return str(text).replace("|", "&#124;").replace("\n", " ").replace("`", "&#96;").replace("<", "&lt;")

    source_trees = {repo["name"]: f"{repo['repo_url']}/tree/{repo['sha']}" for repo in repos}
    for row in rows:
        values = [
            row["row_id"],
            row["repo"] + " / " + row["sample"] + ":" + row["stratum"],
            f"[line {row['line']}]({row['doc_link']})",
            cell(row["statement"]),
            " ".join(f"[code {i + 1}]({url})" for i, url in enumerate(row["code_links"]))
            or f"[No declaration evidence; pinned source tree]({source_trees[row['repo']]})",
            row["tool_verdict"],
            cell(row["suggestion"]),
            "",
            "",
        ]
        lines.append("| " + " | ".join(values) + " |")
    text = "\n".join(lines) + "\n"
    target = run / "labels_todo.md"
    if target.exists() and target.read_text(encoding="utf-8") != text:
        raise ValueError("Existing labeling sheet differs; refusing to overwrite human work.")
    target.write_text(text, encoding="utf-8")
    root_target = ROOT / "benchmark/labels_todo.md"
    if not root_target.exists():
        root_target.write_text(text, encoding="utf-8")
    print(f"Prepared {len(rows)} blank human rows for {len(populations)} completed repos.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["freeze", "scan", "labels", "sweep"])
    parser.add_argument("--run", default="2026-10-18-r4")
    parser.add_argument("--finding-seed", type=int, default=20261026)
    parser.add_argument("--recall-seed", type=int, default=20261027)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    run = ROOT / "benchmark/runs" / args.run
    if args.command == "freeze":
        print(freeze(run, finding_seed=args.finding_seed, recall_seed=args.recall_seed))
    elif args.command == "labels":
        prepare_labels(run)
    else:
        scan_all(
            run,
            sweep=args.command == "sweep",
            finding_seed=args.finding_seed,
            recall_seed=args.recall_seed,
            timeout_seconds=args.timeout_seconds,
        )


if __name__ == "__main__":
    main()
