"""Bake fixture reports and optionally scan public repositories through the production worker."""

import argparse
import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

from docs_drift.config import Config
from docs_drift.fix.bundle import build_fix_bundle
from docs_drift.models import ScanResult
from docs_drift.scanning import run_scan
from docs_drift.web.git import checkout
from docs_drift.web.jobs import LocalQueue, Store
from docs_drift.web.settings import Settings
from docs_drift.web.validation import validate_repository
from docs_drift.web.worker import public_result
from scripts.build_judge_demo import build_demo
from tests.fixtures.build_fixtures import build_fixtures

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


FIXTURE_CHECKOUT = "uv run python -m tests.fixtures.build_fixtures && cd tests/fixtures/repos/{name}"
JUDGE_CHECKOUT = "uv run python scripts/build_judge_demo.py --output judge && cd judge/judge-fixture"


def _bake(item: dict[str, object], repo: Path) -> None:
    """Plan certain fixes for a saved example against a checkout of the exact scanned revision."""
    result = ScanResult.model_validate_json((EXAMPLES / str(item["file"])).read_text(encoding="utf-8"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    if head.stdout.strip() != result.head_sha:
        print(f"skip {item['scan_id']}: checkout is {head.stdout.strip()[:8]}, example is {result.head_sha[:8]}")
        return
    bundle = build_fix_bundle(result, repo)
    if not bundle.available:
        item.pop("fixes", None)
        print(f"{item['scan_id']}: no certain edits")
        return
    name = Path(str(item["file"])).stem + ".fixes.json"
    (EXAMPLES / name).write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    item["fixes"] = name
    print(f"{item['scan_id']}: {len(bundle.files)} file(s), {sum(f.edits for f in bundle.files)} edit(s)")


def bake_fixes(public: bool) -> None:
    """Attach fix bundles to saved examples; public repositories are fetched only with --public."""
    manifest_path = EXAMPLES / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        build_fixtures(root / "repos")
        build_demo(root / "judge")
        for item in manifest:
            stem = Path(item["file"]).stem
            if item["kind"] == "fixture" and stem == "judge-fixture":
                item["checkout"] = JUDGE_CHECKOUT
                _bake(item, root / "judge" / "judge-fixture")
            elif item["kind"] == "fixture" and (root / "repos" / stem).is_dir():
                item["checkout"] = FIXTURE_CHECKOUT.format(name=stem)
                _bake(item, root / "repos" / stem)
            elif public and item["kind"] == "public" and item.get("sha"):
                settings = Settings(data_dir=root / "data", history_days=3650)
                workspace = root / "public" / item["scan_id"]
                workspace.mkdir(parents=True)
                repository = validate_repository(item["repo_url"])
                deadline = time.monotonic() + settings.clone_timeout
                _bake(item, checkout(repository, item["sha"], workspace, settings, deadline, depth_only=True))
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", nargs="*")
    parser.add_argument("--fixes", action="store_true", help="Only attach fix bundles to the saved examples.")
    parser.add_argument("--public", action="store_true", help="With --fixes, also fetch pinned public examples.")
    args = parser.parse_args()
    if args.fixes:
        bake_fixes(args.public)
        return
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
