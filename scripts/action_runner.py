"""Static composite Action scan step: holds no token, never posts or calls a model.

Publishing to GitHub is a separate step (scripts/action_publish.py) that receives
the write token and only the bundle written here, never the scan environment.
"""

import os
import uuid
from pathlib import Path

from docs_drift.baseline import load_baseline
from docs_drift.collaboration import pr_comparison, write_pr_report
from docs_drift.config import load_config
from docs_drift.github.bundle import BUNDLE_NAME, bundle_from_evaluation, touched_findings
from docs_drift.github.diff import changed_lines
from docs_drift.history_cache import CACHE_DISABLED
from docs_drift.policy import GatePolicy, evaluate_gate
from docs_drift.report import RenderOptions, render
from docs_drift.review import load_reviews, suppressed_ids
from docs_drift.scanning import run_scan


def main() -> None:  # noqa: PLR0915 - one linear Action contract.
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    repo = (workspace / os.getenv("SCAN_REPO", ".")).resolve()
    if not repo.is_relative_to(workspace):
        raise ValueError("Repository input must stay inside the checked-out workspace.")
    config = load_config(repo)
    fail = os.getenv("FAIL_ON_DRIFT", "true")
    if fail not in {"true", "false"}:
        raise ValueError("fail-on-drift must be true or false.")
    policy = GatePolicy(
        min_confidence=float(os.getenv("SCAN_MIN_CONFIDENCE", "").strip() or config.min_confidence),
        max_findings=int(os.getenv("SCAN_MAX_FINDINGS", "0")),
        fail_on_drift=fail == "true",
    )
    baseline = None
    if selected := os.getenv("SCAN_BASELINE", "").strip():
        path = repo / selected
        if Path(selected).is_absolute() or not path.resolve().is_relative_to(repo):
            raise ValueError("Baseline input must be relative to the selected repository and stay inside it.")
        baseline = load_baseline(path)
    # Multiple scanner steps in one job must not overwrite each other's evidence.
    output = Path(os.environ["RUNNER_TEMP"]) / "docs-drift-reports" / uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=True)
    token = CACHE_DISABLED.set(True)
    try:
        result = run_scan(repo, config, no_cache=True)
    finally:
        CACHE_DISABLED.reset(token)
    gate_result = result
    book = None
    if selected := os.getenv("SCAN_REVIEWS", "").strip():
        path = repo / selected
        if Path(selected).is_absolute() or not path.resolve().is_relative_to(repo):
            raise ValueError("Reviews must stay inside the selected repository.")
        book = load_reviews(path)
    base_sha = None
    touched = []
    if base := os.getenv("SCAN_BASE_REF", "").strip():
        result, gate_result, comparison = pr_comparison(repo, config, base)
        write_pr_report(gate_result, comparison, output / "pr", book, full_result=result)
        base_sha = str(comparison["base_sha"])
        drift_docs = {f.claim.doc_path for f in result.findings if f.is_drift}
        touched = touched_findings(result, changed_lines(repo, base_sha, paths=drift_docs))
    suppressed = suppressed_ids(result, book) if book else set()
    for format, suffix in [("html", "html"), ("json", "json"), ("sarif", "sarif"), ("markdown", "md")]:
        (output / ("scan." + suffix)).write_text(render(result, format, RenderOptions(pr_mode=True)), encoding="utf-8")
    evaluation = evaluate_gate(gate_result, policy, baseline, suppressed)
    summary = evaluation.summary
    (output / "gate.json").write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
    new_result = result.model_copy(update={"findings": evaluation.new_findings, "baselined": summary.baseline_drift})
    for format, suffix in [("json", "json"), ("sarif", "sarif"), ("markdown", "md")]:
        (output / ("new." + suffix)).write_text(
            render(new_result, format, RenderOptions(pr_mode=True)), encoding="utf-8"
        )
    annotations = gate_result.model_copy(
        update={"findings": [f for f in evaluation.new_findings if f.claim.claim_id not in suppressed]}
    )
    print(render(annotations, "github"), end="")
    bundle = bundle_from_evaluation(result, evaluation, policy, suppressed, base_sha, touched)
    (output / BUNDLE_NAME).write_text(bundle.model_dump_json(), encoding="utf-8")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write("report-directory=" + output.as_posix() + "\n")
        stream.write("bundle-path=" + (output / BUNDLE_NAME).as_posix() + "\n")
        for name, value in {
            "gate-status": summary.status,
            "new-drift": summary.new_drift,
            "qualifying-new-drift": summary.qualifying_new_drift,
            "baseline-drift": summary.baseline_drift,
        }.items():
            stream.write(f"{name}={value}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(
            f"# Documentation gate: {summary.status}\n\n"
            f"New drift: **{summary.new_drift}**; qualifying: **{summary.qualifying_new_drift}**; "
            f"baseline accepted: **{summary.baseline_drift}**. "
            f"Allowed qualifying findings: **{policy.max_findings}**; confidence: **{policy.min_confidence:g}**.\n\n"
            "A passing gate is not proof that documentation is correct. Full findings, unverifiable claims "
            "and scan warnings remain in `scan.*`; the new-drift subset is in `new.*`.\n\n"
        )
        stream.write(render(new_result, "markdown"))
    if summary.status == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
