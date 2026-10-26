"""Evaluate optional LLM triage against hand-labelled rule findings and known true positives.

Inputs are local: a sweep directory with ``<owner>__<repo>/repo`` clones and round-1
``scan.json`` outputs, the labels JSON, and optionally a rich-cli clone at the
reviewed commit. Replay mode never touches the network; record mode needs
OPENROUTER_API_KEY and writes recordings to ``--recordings``.

Example (replay of committed or local recordings):
    uv run python -m benchmark.triage_eval --sweep <dir> --labels <file> --mode replay
"""

import argparse
import asyncio
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from docs_drift.config import Config
from docs_drift.llm.client import ClientError, RecordedClient
from docs_drift.llm.pricing import TRIAGE_DEFAULT_MODEL
from docs_drift.llm.triage import PROMPT_VERSION, triage_findings
from docs_drift.llm.types import ModelRequest, TriageResponse
from docs_drift.llm.verify import Budget
from docs_drift.models import Finding, ScanResult, SymbolKind

ROOT = Path(__file__).resolve().parents[1]
RICH_CLI_TPS = {("README.md", 61), ("README.md", 180)}
FIXTURE_REPOS = ("py-cli", "fastapi-svc", "node-tool")
# Categories where the doc statement is not about the scanned project at all. Triage is
# designed to catch these. Every other category is a real project claim the static
# index failed to resolve, which triage must keep by design.
NOT_PROJECT = {
    "intentional_example",
    "third_party_command",
    "third_party_schema_in_example",
    "third_party_config",
    "third_party_env_var",
    "third_party_symbol",
    "third_party_api",
    "third_party_api_identifier",
    "third_party_config_key",
    "third_party_plugin_command",
    "shell_completion_placeholder",
    "placeholder_flag",
    "python_builtin",
    "builtin_symbol",
    "builtin_in_traceback",
    "stdlib_or_os_api",
    "stdlib_symbol",
    "language_keyword",
    "historical_version_note",
    "historical_changelog",
    "user_file_not_repo_file",
    "user_home_path",
    "user_config_file",
    "runtime_user_path",
    "ide_macro_path",
    "external_url_as_endpoint",
    "system_path_as_endpoint",
    "command_output",
    "json_output_key",
    "negated_example_flag",
    "negative_number_as_flag",
    "prose_word_as_flag",
    "prose_word_as_env_var",
    "prose_in_code_block",
    "prose_concept_not_path",
    "generic_concept_filename",
    "generic_filename",
    "mime_type_as_path",
    "data_value_as_symbol",
    "literal_value_as_key",
    "literal_value_as_config_key",
    "python_literal_as_config_key",
    "protocol_term_as_symbol",
    "env_var_read_by_dependency",
    # Round-2 label names.
    "user_project_example_path",
    "illustrative-example-path",
    "placeholder-template-path",
    "github_repo_slug_as_path",
    "quoted-option-value-parsed-as-flags",
    "intentional-error-example / remainder-args-after-app",
    "dynamic-map-key-as-config-key",
}


class CapturingClient:
    """Wrap a client to keep raw answers and per-request latency for analysis."""

    def __init__(self, inner: RecordedClient, replay: RecordedClient | None = None) -> None:
        self.inner = inner
        self.replay = replay
        self.calls: list[tuple[list[dict[str, Any]], TriageResponse, float]] = []

    async def complete_triage(self, request: ModelRequest) -> TriageResponse:
        started = time.perf_counter()
        response = None
        if self.replay is not None:
            # Record mode reuses existing recordings so a rerun never pays twice.
            try:
                response = await self.replay.complete_triage(request)
            except ClientError:
                response = None
        if response is None:
            try:
                response = await self.inner.complete_triage(request)
            except ClientError as exc:
                if self.inner.mode != "replay":
                    raise
                # A batch that failed while recording has nothing to replay; count it as kept.
                raise ClientError("No recording for this batch; its findings were kept.") from exc
        block = request.context.split('<data name="findings">\n', 1)[1].rsplit("\n</data>", 1)[0]
        payloads = json.loads(block)
        self.calls.append((payloads, response, time.perf_counter() - started))
        return response


def _key(finding: Finding) -> tuple[str, int, str]:
    return finding.claim.doc_path, finding.claim.line, finding.claim.value


def _label_key(finding: Finding) -> str:
    return f"{finding.claim.claim_id}:{finding.claim.line}"


def _normal(text: str) -> str:
    return " ".join(text.replace("`", "").replace("'", "").replace('"', "").split())


def _match(row: dict[str, Any], findings: list[Finding]) -> Finding | None:
    """Match a label by claim-id prefix, else by doc line and claim text (some ids are synthetic)."""
    path, line = row["doc"].rsplit(":", 1)
    here = [item for item in findings if item.is_drift and item.claim.doc_path == path and str(item.claim.line) == line]
    prefix = row.get("id", "").removeprefix("claim_")[:12] or "-"
    exact = [item for item in here if item.claim.claim_id[6:18] == prefix]
    if exact:
        return exact[0]
    claim = _normal(row["claim"])

    def score(item: Finding) -> int:
        value = _normal(item.claim.value)
        if claim.startswith(f"{item.claim.kind.value} {value}") or claim == value:
            return 3
        if claim.startswith(value) or f"`{item.claim.value}`" in row["claim"]:
            return 2
        return 1 if value and value in claim else 0

    ranked = sorted(here, key=score, reverse=True)
    if ranked and (score(ranked[0]) > 0 or len(ranked) == 1):
        return ranked[0]
    return None


def _compatible(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop what newer scanners add that triage never reads (the symbol index, new symbol kinds)."""
    kinds = {kind.value for kind in SymbolKind}
    raw["symbols"] = []
    for finding in raw.get("findings", []):
        symbol = finding.get("matched_symbol")
        if symbol and symbol.get("kind") not in kinds:
            finding["matched_symbol"] = None
    return raw


def _label_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Flatten round-1 rows or round-2 per-repository findings into rows by repository."""
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in json.loads(path.read_text(encoding="utf-8"))["rows"]:
        if "findings" not in row:
            by_repo[row["repo"]].append(row)
            continue
        for item in row["findings"]:
            if item.get("label") in {"true_positive", "false_positive"}:
                by_repo[row["repo"]].append({**item, "repo": row["repo"], "holdout": row.get("holdout")})
    return by_repo


def load_cases(args: argparse.Namespace, config: Config) -> list[tuple[str, ScanResult, dict[str, dict[str, Any]]]]:
    """Return (case name, scan restricted to labelled findings, labels by claim id).

    Round-1 labels are flat rows; round-2 labels nest findings per repository and are
    read against fresh scans in ``--scans`` (one ``<owner>__<repo>.json`` per repository).
    """
    cases = []
    for repo, rows in sorted(_label_rows(Path(args.labels)).items()):
        folder = Path(args.sweep) / repo.replace("/", "__")
        scan_file = (
            Path(args.scans) / f"{repo.replace('/', '__')}.json"
            if getattr(args, "scans", None)
            else folder / "scan.json"
        )
        raw = _compatible(json.loads(scan_file.read_text(encoding="utf-8")))
        raw["config_used"] = config.model_dump(mode="json")
        scan = ScanResult.model_validate(raw).model_copy(
            update={"repo_path": folder / "repo", "remote_url": f"https://github.com/{repo}"}
        )
        chosen: dict[str, dict[str, Any]] = {}
        findings = []
        for row in rows:
            finding = _match(row, scan.findings)
            if finding is not None and _label_key(finding) not in chosen:
                chosen[_label_key(finding)] = row
                findings.append(finding)
        missing = len(rows) - len(chosen)
        if missing:
            unmatched = [row["doc"] for row in rows if row not in chosen.values()]
            print(f"warning: {repo}: {missing} labelled rows have no drift finding in the scan: {unmatched}")
        cases.append((repo, scan.model_copy(update={"findings": findings}), chosen))
    if args.rich_cli:
        raw = json.loads((ROOT / "src/docs_drift/web/examples/rich-cli-reviewed.json").read_text(encoding="utf-8"))
        raw["config_used"] = config.model_dump(mode="json")
        scan = ScanResult.model_validate(raw).model_copy(
            update={"repo_path": Path(args.rich_cli), "remote_url": "https://github.com/Textualize/rich-cli"}
        )
        findings = [
            item for item in scan.findings if item.is_drift and (item.claim.doc_path, item.claim.line) in RICH_CLI_TPS
        ]
        rich_rows: dict[str, dict[str, Any]] = {
            _label_key(item): {"label": "true_positive", "claim": item.claim.value, "repo": "Textualize/rich-cli"}
            for item in findings
        }
        cases.append(("Textualize/rich-cli", scan.model_copy(update={"findings": findings}), rich_rows))
    if args.fixtures:
        from docs_drift.scanning import run_scan  # noqa: PLC0415 - heavy import only when requested.
        from tests.fixtures.build_fixtures import build_fixtures  # noqa: PLC0415

        out = Path(args.out) / "fixtures"
        if not out.exists():
            build_fixtures(out)
        key = json.loads((ROOT / "tests/fixtures/answer_key.json").read_text(encoding="utf-8"))["findings"]
        expected = {(row["repo"], row["doc_path"], row["line"]) for row in key}
        for name in FIXTURE_REPOS:
            scan = run_scan(out / name, config)
            findings = [item for item in scan.findings if item.is_drift]
            fixture_rows: dict[str, dict[str, Any]] = {
                _label_key(item): {
                    "label": "true_positive"
                    if (name, item.claim.doc_path, item.claim.line) in expected
                    else "unlabeled",
                    "claim": item.claim.value,
                    "repo": f"fixture/{name}",
                }
                for item in findings
            }
            cases.append((f"fixture/{name}", scan.model_copy(update={"findings": findings}), fixture_rows))
    return cases


async def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    llm = {
        "triage": True,
        "triage_model": args.model,
        "triage_batch_size": args.batch_size,
        "budget": args.budget,
        "concurrency": args.concurrency,
        "timeout": 600,
    }
    config = Config.model_validate({"llm": llm})
    system = Path(args.prompt).read_text(encoding="utf-8") if args.prompt else None
    prompt_version = Path(args.prompt).name if args.prompt else PROMPT_VERSION
    recordings = Path(args.recordings)
    replay = RecordedClient(config.llm, mode="replay", recordings=recordings) if args.mode == "record" else None
    client = CapturingClient(RecordedClient(config.llm, mode=args.mode, recordings=recordings), replay)
    budget = Budget(args.budget)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    started = time.perf_counter()
    for name, scan, labels in load_cases(args, config):
        if args.only and name not in args.only:
            continue
        if args.dry_run:
            print(f"{name}: {len(scan.findings)} findings")
            continue
        outcome = await triage_findings(scan, client, budget=budget, prompt_version=prompt_version, system=system)
        warnings.extend(f"{name}: {item}" for item in outcome.warnings)
        raw = {
            (p["doc"], p["line"], p["name"]): item
            for payloads, response, _ in client.calls
            for p in payloads
            for item in response.result.results
            if item.id == p["id"]
        }
        for before, after in zip(scan.findings, outcome.findings, strict=True):
            label = labels[_label_key(before)]
            answer = raw.get(_key(before))
            rows.append(
                {
                    "repo": name,
                    "doc": f"{before.claim.doc_path}:{before.claim.line}",
                    "claim": f"{before.claim.kind.value} {before.claim.value}",
                    "label": label["label"],
                    "fp_category": label.get("fp_category"),
                    "not_project_category": label.get("fp_category") in NOT_PROJECT,
                    "downgraded": after.triage is not None,
                    "model_decision": answer.decision if answer else None,
                    "model_category": answer.category if answer else None,
                    "quote": answer.quote if answer else None,
                    "rationale": answer.rationale if answer else None,
                }
            )
    latencies = [seconds for _, _, seconds in client.calls]
    cost = sum(response.usage.cost for _, response, _ in client.calls)
    return {
        "model": args.model,
        "prompt_version": prompt_version,
        "mode": args.mode,
        "rows": rows,
        "warnings": warnings,
        "requests": len(client.calls),
        "input_tokens": sum(response.usage.input_tokens for _, response, _ in client.calls),
        "output_tokens": sum(response.usage.output_tokens for _, response, _ in client.calls),
        "cost": cost,
        "budget_spent": budget.spent,
        "wall_seconds": time.perf_counter() - started,
        "latency_mean": sum(latencies) / len(latencies) if latencies else 0,
        "latency_max": max(latencies, default=0),
    }


def summarize(report: dict[str, Any]) -> str:
    rows = report["rows"]
    tps = [row for row in rows if row["label"] == "true_positive"]
    fps = [row for row in rows if row["label"] == "false_positive"]
    lost = [row for row in tps if row["downgraded"]]
    removed = [row for row in fps if row["downgraded"]]
    said = Counter(row["model_decision"] for row in rows)
    invalid = [row for row in rows if row["model_decision"] == "not_project_claim" and not row["downgraded"]]
    lines = [
        f"model {report['model']} · prompt {report['prompt_version']} · mode {report['mode']}",
        f"TPs kept {len(tps) - len(lost)}/{len(tps)}"
        + (f" LOST: {[(r['repo'], r['doc'], r['claim']) for r in lost]}" if lost else ""),
        f"FPs removed {len(removed)}/{len(fps)}",
        f"  not-project categories: {sum(r['downgraded'] for r in fps if r['not_project_category'])}/"
        f"{sum(r['not_project_category'] for r in fps)}",
        f"  index-gap categories:   {sum(r['downgraded'] for r in fps if not r['not_project_category'])}/"
        f"{sum(not r['not_project_category'] for r in fps)}",
        f"model decisions {dict(said)}; not_project_claim rejected by quote validation: {len(invalid)}",
        f"unlabeled fixture drift downgraded: {sum(r['downgraded'] for r in rows if r['label'] == 'unlabeled')}/"
        f"{sum(r['label'] == 'unlabeled' for r in rows)}",
        f"requests {report['requests']} · tokens in {report['input_tokens']} out {report['output_tokens']} · "
        f"cost ${report['cost']:.4f} · wall {report['wall_seconds']:.1f}s · latency mean {report['latency_mean']:.1f}s "
        f"max {report['latency_max']:.1f}s",
    ]
    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in fps:
        by_category[row["fp_category"] or "unknown"][1] += 1
        by_category[row["fp_category"] or "unknown"][0] += row["downgraded"]
    for category, (hit, total) in sorted(by_category.items(), key=lambda item: (-item[1][1], item[0])):
        marker = "*" if category in NOT_PROJECT else " "
        lines.append(f"  {marker} {category}: {hit}/{total}")
    lines.extend(f"warning: {item}" for item in report["warnings"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--scans", help="directory of fresh scans for round-2 style labels")
    parser.add_argument("--rich-cli", help="rich-cli clone at the reviewed commit")
    parser.add_argument("--fixtures", action="store_true", help="include synthetic fixture drift")
    parser.add_argument("--out", default=".pytest-tmp/triage-eval")
    parser.add_argument("--recordings", default=".pytest-tmp/triage-eval/recordings")
    parser.add_argument("--mode", choices=["replay", "record", "live"], default="replay")
    parser.add_argument("--model", default=TRIAGE_DEFAULT_MODEL)
    parser.add_argument("--prompt", help="alternative system prompt file for variant runs")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--budget", type=float, default=0.05)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", action="append", help="evaluate only these case names")
    parser.add_argument("--report", help="write the full JSON report here")
    args = parser.parse_args(argv)
    report = asyncio.run(evaluate(args))
    if args.dry_run:
        return 0
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(summarize(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
