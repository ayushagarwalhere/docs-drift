"""Bake fixture reports and optionally scan public repositories through the production worker."""

import argparse
import asyncio
import json
from pathlib import Path

from docs_drift.config import Config
from docs_drift.models import ScanResult
from docs_drift.scanning import run_scan
from docs_drift.web.jobs import LocalQueue, Store
from docs_drift.web.settings import Settings
from docs_drift.web.validation import validate_repository
from docs_drift.web.worker import public_result

EXAMPLES = Path("src/docs_drift/web/examples")


async def real_repos(urls: list[str]) -> None:
    settings = Settings(data_dir=Path(".docs-drift/real-scans"))
    queue = LocalQueue(settings, Store(settings))
    jobs = [queue.submit(validate_repository(url)) for url in urls]
    while queue.tasks:
        print([(j.repo_url.rsplit("/", 1)[-1], j.status) for j in jobs], flush=True)
        await asyncio.sleep(2)
    manifest_path = EXAMPLES / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for job in jobs:
        print(job.repo_url, job.status, job.error, job.metrics, flush=True)
        if job.result:
            name = job.repo_url.rsplit("/", 1)[-1]
            (EXAMPLES / f"{name}.json").write_text(job.result.model_dump_json(indent=2), encoding="utf-8")
            manifest = [m for m in manifest if m["scan_id"] != "example-" + name]
            manifest.append(
                {
                    "scan_id": "example-" + name,
                    "name": name,
                    "repo_url": job.repo_url,
                    "file": name + ".json",
                    "kind": "public",
                    "sha": job.result.head_sha,
                    "verified": False,
                    "note": "Static findings awaiting manual evidence review.",
                }
            )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    apply_reviews()


def apply_reviews() -> None:
    manifest_path = EXAMPLES / "manifest.json"
    reviews = json.loads((EXAMPLES / "verification.json").read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest:
        for review in reviews:
            if item["repo_url"] == review["repo_url"] and item.get("sha") == review["sha"]:
                item.update(
                    verified=True, name=review["name"], note=review["note"], verified_values=review["verified_values"]
                )
                path = EXAMPLES / item["file"]
                result = ScanResult.model_validate_json(path.read_text(encoding="utf-8"))
                if review["note"] not in result.warnings:
                    result.warnings.append(review["note"])
                path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", nargs="*")
    args = parser.parse_args()
    EXAMPLES.mkdir(parents=True, exist_ok=True)
    manifest_path = EXAMPLES / "manifest.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    manifest = [item for item in previous if item.get("kind") == "public" and (EXAMPLES / item["file"]).exists()]
    for name in ("py-cli", "fastapi-svc", "node-tool", "clean-repo"):
        repo = Path("tests/fixtures/repos") / name
        url = f"https://github.com/docs-drift/{name}-demo"
        result = public_result(run_scan(repo, Config()), url)
        (EXAMPLES / f"{name}.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        manifest.append(
            {
                "scan_id": "example-" + name,
                "name": name + " demo",
                "repo_url": url,
                "file": name + ".json",
                "kind": "fixture",
                "verified": True,
                "note": "Synthetic fixture with a deterministic Git history.",
            }
        )
    (EXAMPLES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.real:
        asyncio.run(real_repos(args.real))


if __name__ == "__main__":
    main()
