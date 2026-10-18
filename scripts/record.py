"""Explicitly record OpenRouter fixture requests; CI replays without credentials."""

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docs_drift.cli import _full_findings
from docs_drift.config import Config
from docs_drift.docs.pipeline import scan_claims
from docs_drift.index import build_index
from docs_drift.llm.candidates import select_candidates
from docs_drift.llm.client import ClientError, RecordedClient, Recording
from docs_drift.llm.pricing import DEFAULT_MODEL, estimate_request
from docs_drift.llm.prompts import PROMPT_VERSION, build_request
from docs_drift.llm.retrieve import retrieve
from docs_drift.llm.types import ModelRequest
from docs_drift.llm.verify import Budget
from tests.fixtures.build_fixtures import build_fixtures


def fixture_requests(root: Path, config: Config) -> list[ModelRequest]:
    build_fixtures(root)
    requests: dict[str, ModelRequest] = {}
    for name in ("py-cli", "fastapi-svc", "node-tool", "clean-repo"):
        repo = root / name
        result = scan_claims(repo, config)
        index = build_index(repo, config)
        findings, _ = _full_findings(result.claims, index, repo)
        result = result.model_copy(update={"symbols": index.symbols, "findings": findings})
        for candidate in select_candidates(result):
            snippets = retrieve(repo, candidate, index.symbols, config.llm.redact_paths)
            if snippets:
                request = build_request(config.llm.model or DEFAULT_MODEL, candidate.claim.raw_text, snippets)
                requests[request.request_hash()] = request
    return list(requests.values())


async def record_requests(requests: list[ModelRequest], config: Config, output: Path) -> None:
    client = RecordedClient(config.llm, mode="record", recordings=output)
    budget = Budget(config.llm.budget)
    semaphore = asyncio.Semaphore(config.llm.concurrency)

    async def record(request: ModelRequest) -> None:
        async with semaphore:
            reserve = estimate_request(request)
            if not await budget.reserve(reserve):
                raise ClientError("Recording budget reached. Increase --budget explicitly to continue.")
            response = await client.complete(request)
            await budget.settle(reserve, response.usage.cost)
            print(f"Recorded {request.request_hash()} (${response.usage.cost:.4f})")

    await asyncio.gather(*(record(request) for request in requests))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tests/recordings"))
    parser.add_argument("--budget", type=float, default=0.50)
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="List hashes without sending any repository text.")
    args = parser.parse_args()
    config = Config.model_validate({"llm": {"enabled": True, "budget": args.budget}})
    with tempfile.TemporaryDirectory(prefix="docs-drift-record-") as directory:
        requests = fixture_requests(Path(directory), config)
    if args.dry_run:
        for request in requests:
            print(request.request_hash())
        return 0
    print("Sending redacted fixture sentences/snippets through OpenRouter; see docs/SECURITY.md.")
    try:
        asyncio.run(record_requests(requests, config, args.output))
    except (ClientError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.prune:
        active = {f"{request.request_hash()}.json" for request in requests}
        for path in args.output.glob("*.json"):
            if path.name not in active:
                try:
                    recording = Recording.model_validate_json(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    print(f"Kept unrecognized recording file: {path.name}")
                    continue
                # Preserve hand-authored attack fixtures and recordings owned by the rewrite path.
                if recording.provenance == "live" and recording.prompt_version == PROMPT_VERSION:
                    path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
