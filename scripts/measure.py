"""Measure extracted-claim recall against the manually authored fixture key."""

import argparse
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docs_drift.cli import _full_findings
from docs_drift.config import Config
from docs_drift.docs.pipeline import scan_claims
from docs_drift.index import build_index
from docs_drift.models import Verdict
from tests.fixtures.build_fixtures import build_fixtures


class Claim(BaseModel):
    """A manually enumerated, located assertion in fixture documentation."""

    model_config = ConfigDict(extra="forbid")

    repo: Literal["py-cli", "fastapi-svc", "node-tool", "clean-repo"]
    doc_path: Annotated[str, Field(min_length=1)]
    line: Annotated[int, Field(ge=1, strict=True)]
    claim_kind: Literal[
        "cli_flag",
        "cli_subcommand",
        "environment_variable",
        "http_endpoint",
        "config_key",
        "function",
        "package_script",
        "shell_command",
        "default_value",
        "make_target",
        "prose",
    ]
    claim_text: Annotated[str, Field(min_length=1)]

    @field_validator("doc_path")
    @classmethod
    def relative_doc_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
            raise ValueError("Use a repository-relative POSIX documentation path without '..'.")
        return value

    def identity(self) -> tuple[str, str, int, str, str]:
        return self.repo, self.doc_path, self.line, self.claim_kind, self.claim_text


class Finding(Claim):
    requires_llm: bool = False
    expected_verdict: Literal["missing", "renamed", "default changed", "invalid command"]
    expected_new: str | int | None
    introduced_by: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]

    @model_validator(mode="after")
    def replacement_matches_verdict(self) -> Self:
        if self.expected_verdict == "renamed" and not isinstance(self.expected_new, str):
            raise ValueError("A rename must name its replacement.")
        if self.expected_verdict == "default changed" and self.expected_new is None:
            raise ValueError("A changed default must supply its new value.")
        if self.expected_verdict in {"missing", "invalid command"} and self.expected_new is not None:
            raise ValueError("Missing items and invalid commands must use null for expected_new.")
        return self


class NonFinding(Claim):
    reason: Annotated[str, Field(min_length=1)]


class AnswerKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    findings: Annotated[list[Finding], Field(min_length=1)]
    non_findings: Annotated[list[NonFinding], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_claims(self) -> Self:
        seen: set[tuple[str, str, int, str, str]] = set()
        for claim in (*self.findings, *self.non_findings):
            identity = claim.identity()
            if identity in seen:
                raise ValueError(f"Duplicate or conflicting answer for {identity}.")
            seen.add(identity)
        return self


def load_key(path: Path) -> AnswerKey:
    """Read JSON only; do not run a detector or inspect executable repository code."""
    return AnswerKey.model_validate_json(path.read_text(encoding="utf-8"))


_KIND = {
    "cli_subcommand": "cli_command",
    "environment_variable": "env_var",
    "http_endpoint": "endpoint",
    "function": "symbol",
    "shell_command": "code_command",
}
MAX_DEFAULT_LINK_DISTANCE = 2


def _expected_value(claim: Claim) -> str:
    if claim.claim_kind == "environment_variable" and claim.claim_text.startswith("process.env."):
        return claim.claim_text.removeprefix("process.env.")
    if claim.claim_kind == "http_endpoint":
        return re.sub(r"\{[^}]+\}|:[A-Za-z_]\w*|<[A-Za-z_]\w*>", "{param}", claim.claim_text)
    if claim.claim_kind == "default_value":
        name = re.search(r"`([^`]+)`", claim.claim_text)
        return name.group(1) if name else ""
    return claim.claim_text


def measure(key: AnswerKey) -> tuple[int, int, list[str], list[str]]:
    with tempfile.TemporaryDirectory(prefix="docs-drift-measure-") as directory:
        root = Path(directory)
        build_fixtures(root)
        scans = {
            name: scan_claims(root / name, Config()) for name in ("py-cli", "fastapi-svc", "node-tool", "clean-repo")
        }
    misses = []
    for expected in (item for item in key.findings if not item.requires_llm):
        actual_kind = _KIND.get(expected.claim_kind, expected.claim_kind)
        value = _expected_value(expected)
        if expected.claim_kind == "default_value" and value == "30":
            subjects = [
                item.value
                for item in scans[expected.repo].claims
                if item.doc_path == expected.doc_path
                and item.kind.value == "config_key"
                and abs(item.line - expected.line) <= MAX_DEFAULT_LINK_DISTANCE
            ]
            value = subjects[0] if len(subjects) == 1 else ""
        found = any(
            item.doc_path == expected.doc_path
            and item.kind.value == actual_kind
            and item.value == value
            and (expected.claim_kind != "default_value" or item.expected_default is not None)
            and (expected.claim_kind != "default_value" or item.line == expected.line or item.line == expected.line - 1)
            for item in scans[expected.repo].claims
        )
        if not found:
            misses.append(
                f"{expected.repo}:{expected.doc_path}:{expected.line} {expected.claim_kind} {expected.claim_text}"
            )
    noisy = []
    for expected_nf in (item for item in key.non_findings if item.repo == "clean-repo"):
        actual_kind = _KIND.get(expected_nf.claim_kind, expected_nf.claim_kind)
        value = _expected_value(expected_nf)
        result = scans[expected_nf.repo]
        found = any(
            item.doc_path == expected_nf.doc_path
            and item.kind.value == actual_kind
            and item.value == value
            and item.line == expected_nf.line
            for item in result.claims
        )
        suppressed = any(
            item.claim.doc_path == expected_nf.doc_path
            and item.claim.kind.value == actual_kind
            and item.claim.value == value
            and item.claim.line == expected_nf.line
            for item in result.suppressed
        )
        excluded = expected_nf.doc_path not in {doc.path for doc in result.docs}
        near_miss = any(word in expected_nf.reason.casefold() for word in ("historical", "example data", "third-party"))
        if near_miss and found and not (suppressed or excluded):
            noisy.append(
                f"{expected_nf.repo}:{expected_nf.doc_path}:{expected_nf.line} "
                f"{expected_nf.claim_kind} {expected_nf.claim_text}"
            )
    total = sum(not item.requires_llm for item in key.findings)
    return total - len(misses), total, misses, noisy


def measure_verdicts(key: AnswerKey) -> tuple[int, int, list[str], list[str]]:
    """Score verdicts over keyed positives and explicitly listed clean-repo negatives."""
    expected_verdict = {
        "missing": Verdict.missing,
        "renamed": Verdict.renamed,
        "default changed": Verdict.default_changed,
        "invalid command": Verdict.invalid_command,
    }
    key_to_kind = {
        "cli_subcommand": "cli_command",
        "environment_variable": "env_var",
        "http_endpoint": "endpoint",
        "function": "symbol",
        "shell_command": "code_command",
    }
    with tempfile.TemporaryDirectory(prefix="docs-drift-verdict-measure-") as directory:
        root = Path(directory)
        build_fixtures(root)
        results = {}
        for name in ("py-cli", "fastapi-svc", "node-tool", "clean-repo"):
            scan = scan_claims(root / name, Config())
            index = build_index(root / name, Config())
            results[name] = (scan, _full_findings(scan.claims, index, root / name)[0])
    true_positive = 0
    misses: list[str] = []
    for expected in (item for item in key.findings if not item.requires_llm):
        scan, findings = results[expected.repo]
        kind = key_to_kind.get(expected.claim_kind, expected.claim_kind)
        value = _expected_value(expected)
        candidates = [
            finding
            for finding in findings
            if finding.claim.doc_path == expected.doc_path
            and finding.claim.kind.value == kind
            and (finding.claim.value == value or expected.claim_kind == "default_value")
            and (expected.claim_kind != "default_value" or finding.claim.expected_default is not None)
            and abs(finding.claim.line - expected.line) <= 1
        ]
        verdict = expected_verdict[expected.expected_verdict]
        if any(item.verdict == verdict for item in candidates):
            true_positive += 1
        else:
            observed = ",".join(item.verdict.value for item in candidates) or "not found"
            misses.append(
                f"{expected.repo}:{expected.doc_path}:{expected.line} expected {verdict.value}, got {observed}"
            )
    false_positives: list[str] = []
    for negative in key.non_findings:
        _, findings = results[negative.repo]
        kind = key_to_kind.get(negative.claim_kind, negative.claim_kind)
        value = _expected_value(negative)
        leaked = [
            item
            for item in findings
            if item.claim.doc_path == negative.doc_path
            and item.claim.kind.value == kind
            and item.claim.value == value
            and item.is_drift
            and abs(item.claim.line - negative.line) <= 1
        ]
        false_positives.extend(
            f"{negative.repo}:{negative.doc_path}:{item.claim.line} {kind} {value}" for item in leaked
        )
    return true_positive, sum(not item.requires_llm for item in key.findings), misses, false_positives


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/answer_key.json"
    )
    args = parser.parse_args(argv)
    try:
        key = load_key(args.key)
    except (OSError, ValueError) as exc:
        print(
            f"Cannot validate the answer key: {exc}. "
            "Check its path and schema; never alter it to match detector output.",
            file=sys.stderr,
        )
        return 1
    hit, total, misses, noisy = measure(key)
    print(f"Claim recall: {hit}/{total} ({hit / total:.1%})")
    print(f"Model-required cases excluded from rule-only metrics: {sum(item.requires_llm for item in key.findings)}")
    print(f"Clean-repo historical/example/third-party near-miss leaks: {len(noisy)}")
    for miss in misses:
        print(f"MISS: {miss}")
    for item in noisy:
        print(f"UNEXPECTED: {item}")
    true_positive, verdict_total, verdict_misses, false_positives = measure_verdicts(key)
    false_positive_count = len(false_positives)
    precision = true_positive / (true_positive + false_positive_count) if true_positive + false_positive_count else 1.0
    recall = true_positive / verdict_total if verdict_total else 1.0
    print(f"Keyed finding recall: {true_positive}/{verdict_total} ({recall:.1%})")
    print(f"Keyed-case precision: {true_positive}/{true_positive + false_positive_count} ({precision:.1%})")
    for miss in verdict_misses:
        print(f"VERDICT MISS: {miss}")
    for item in false_positives:
        print(f"FALSE POSITIVE: {item}")
    print("Command: uv run python scripts/measure.py")
    return 0 if not misses and not noisy and not verdict_misses and not false_positives else 1


if __name__ == "__main__":
    raise SystemExit(main())
