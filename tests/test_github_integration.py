"""GitHub publishing core, Action wiring and CLI against an offline GitHub fake and real fixture history."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from docs_drift.cli import app
from docs_drift.config import Config
from docs_drift.github import manifest, render
from docs_drift.github.bundle import BUNDLE_NAME, ReviewBundle, build_bundle, load_bundle
from docs_drift.github.client import GitHubClient, GitHubError, RetryPolicy
from docs_drift.github.context import actions_context
from docs_drift.github.diff import changed_lines, commentable_lines, diff_map
from docs_drift.github.publish import (
    PublishOptions,
    PullRequestRef,
    RepositoryRef,
    publish_issue,
    publish_pull_request,
)
from docs_drift.policy import GatePolicy
from scripts import action_publish, action_runner
from tests.fixtures.build_fixtures import build_fixtures
from tests.github_fake import HUMAN, FakeGitHub, git, pr_files

API = "https://api.github.com"


@pytest.fixture(scope="module")
def pr_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    """py-cli history (README `--port` vs code `--listen`) plus a PR commit that edits README line 4."""
    root = tmp_path_factory.mktemp("github-pr")
    build_fixtures(root)
    repo = root / "py-cli"
    base = git(repo, "rev-parse", "HEAD~4").strip()  # Documentation matched the code here.
    readme = (repo / "README.md").read_text(encoding="utf-8")
    edited = readme.replace("(default 8000).", "(default 8000) for the HTTP listener.")
    (repo / "README.md").write_bytes(edited.encode())
    git(repo, "-c", "user.name=PR Author", "-c", "user.email=pr@example.invalid", "commit", "-qam", "Clarify serve")
    return repo, base, git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture(scope="module")
def bundle(pr_repo: tuple[Path, str, str]) -> ReviewBundle:
    repo, base, _ = pr_repo
    return build_bundle(repo, Config(), GatePolicy(), base=base)


def client_for(fake: FakeGitHub, **kwargs: Any) -> GitHubClient:
    return GitHubClient(token="test-token", transport=fake.transport(), sleep=lambda _: None, **kwargs)


def ref(head: str) -> PullRequestRef:
    return PullRequestRef("acme", "py-cli", 7, head)


# --- client -------------------------------------------------------------------------------------


def test_client_honours_secondary_rate_limit_retry_after_and_api_headers() -> None:
    fake = FakeGitHub()
    fake.forced = [
        httpx.Response(
            403, json={"message": "You have exceeded a secondary rate limit."}, headers={"retry-after": "7"}
        ),
        httpx.Response(
            429, json={"message": "slow down"}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}
        ),
    ]
    slept: list[float] = []
    client = GitHubClient(token="test-token", transport=fake.transport(), sleep=slept.append, clock=lambda: 1000.0)
    assert client.get("/repos/acme/py-cli/pulls/1/files") == []
    assert slept == [7.0, 30.0]
    request = fake.requests[0]
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.headers["user-agent"].startswith("docs-drift/")


def test_client_retries_5xx_and_transport_errors_then_fails_bounded_without_leaking_token() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(502, json={"message": "bad gateway"})

    slept: list[float] = []
    client = GitHubClient(
        token="super-secret-token",
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
        retry=RetryPolicy(attempts=3, base_delay=1, jitter=0),
    )
    with pytest.raises(GitHubError) as raised:
        client.get("/repos/acme/py-cli")
    assert calls == 3 and slept == [1.0, 2.0]
    assert raised.value.status == 502
    assert "super-secret-token" not in str(raised.value) + repr(client)
    with pytest.raises(ValueError, match="relative"):
        client.get("https://evil.example/steal")


def test_client_paginates_link_headers_and_refuses_foreign_hosts() -> None:
    fake = FakeGitHub(files=[{"filename": f"f{i}.md", "patch": "@@ -1 +1 @@\n+x"} for i in range(5)], page_size=2)
    client = client_for(fake)
    assert [f["filename"] for f in client.paginate("/repos/acme/py-cli/pulls/1/files")] == [
        f"f{i}.md" for i in range(5)
    ]
    assert len(fake.requests) == 3

    def foreign(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{}], headers={"link": '<https://evil.example/next?page=2>; rel="next"'})

    with pytest.raises(ValueError, match="another host"):
        list(GitHubClient(token="t", transport=httpx.MockTransport(foreign)).paginate("/repos/a/b/issues"))


def test_client_does_not_retry_validation_errors() -> None:
    fake = FakeGitHub()
    fake.forced = [httpx.Response(422, json={"message": "Validation Failed", "errors": [{"message": "bad line"}]})]
    with pytest.raises(GitHubError, match="bad line") as raised:
        client_for(fake).post("/repos/a/b/issues", {})
    assert raised.value.status == 422 and len(fake.requests) == 1


# --- diff mapping -------------------------------------------------------------------------------


def test_commentable_lines_follow_hunk_arithmetic() -> None:
    patch = (
        "@@ -1,3 +1,4 @@\n context\n-old\n+new\n+added\n context2\n"
        "\\ No newline at end of file\n@@ -10 +11,2 @@\n+x\n y"
    )
    assert commentable_lines(patch) == {1, 2, 3, 4, 11, 12}
    assert diff_map([{"filename": "a.md", "status": "removed", "patch": "@@ -1 +0,0 @@\n-x"}]) == {}
    assert diff_map([{"filename": "big.md", "status": "modified"}]) == {"big.md": set()}


def test_changed_lines_reads_the_local_pr_diff(pr_repo: tuple[Path, str, str]) -> None:
    repo, base, _ = pr_repo
    lines = changed_lines(repo, base)
    assert lines["README.md"] == {4}
    assert lines["cli.py"]


# --- bundle -------------------------------------------------------------------------------------


def test_bundle_selects_only_drift_introduced_since_base_with_certain_line_suggestions(bundle: ReviewBundle) -> None:
    values = {f.claim.value for f in bundle.findings}
    assert {"--port", "migrate", "--workers"} <= values
    assert bundle.gate.status == "failed" and bundle.base_sha
    port = next(s for s in bundle.suggestions if s.line == 4)
    assert port.path == "README.md"
    assert port.replacement == "- `serve` accepts `--listen` (default 8000) for the HTTP listener."
    assert all(f.confidence >= 0.6 for f in bundle.findings)


def test_unchanged_history_renames_are_not_reported_as_new_drift(pr_repo: tuple[Path, str, str]) -> None:
    repo, _, _ = pr_repo
    previous = git(repo, "rev-parse", "HEAD~1").strip()
    result = build_bundle(repo, Config(), GatePolicy(), base=previous)
    # Base snapshots have no history: missing-at-base vs renamed-at-head is pre-existing drift.
    assert result.findings == []
    assert [f.claim.value for f in result.touched] == ["--port"]
    assert result.gate.status == "passed"


# --- PR publishing ------------------------------------------------------------------------------


def test_sticky_summary_and_suggestions_only_on_diff_lines(
    pr_repo: tuple[Path, str, str], bundle: ReviewBundle
) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    report = publish_pull_request(client_for(fake), ref(head), bundle, PublishOptions(artifact_url="https://ci/run/1"))
    assert report.skipped is None
    assert len(fake.reviews) == 1 and fake.reviews[0]["commit_id"] == head
    inline = fake.review_comments
    # Line 4 was edited; line 6 is hunk context (commentable on GitHub); line 8 is outside the diff.
    assert [(c["path"], c["line"]) for c in inline] == [("README.md", 4), ("README.md", 6)]
    assert "```suggestion\n- `serve` accepts `--listen` (default 8000) for the HTTP listener.\n```" in inline[0]["body"]
    (summary,) = fake.issue_comments[7]
    assert summary["body"].startswith(render.SUMMARY_MARKER)
    assert "💬 inline" in summary["body"] and "README.md:8" in summary["body"]  # migrate falls back to the summary.
    assert f"/blob/{head}/README.md#L8" in summary["body"]
    assert "[Full report and artifacts](https://ci/run/1)" in summary["body"]


def test_rerun_dedupes_inline_comments_and_updates_sticky_in_place(
    pr_repo: tuple[Path, str, str], bundle: ReviewBundle
) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    client = client_for(fake)
    publish_pull_request(client, ref(head), bundle, PublishOptions())
    first_writes = len(fake.writes())
    second = publish_pull_request(client, ref(head), bundle, PublishOptions())
    assert len(fake.writes()) == first_writes  # Identical rerun: nothing posted or edited.
    assert [a.kind for a in second.actions] == ["summary-unchanged"]
    changed = bundle.model_copy(update={"findings": bundle.findings[1:]})
    publish_pull_request(client, ref(head), changed, PublishOptions())
    assert len(fake.issue_comments[7]) == 1  # Updated in place, never a second sticky comment.
    assert ("PATCH", f"/repos/acme/py-cli/issues/comments/{fake.issue_comments[7][0]['id']}") in fake.writes()
    assert len(fake.reviews) == 1


def test_changed_finding_posts_a_new_comment_and_marks_the_old_one_outdated(
    pr_repo: tuple[Path, str, str], bundle: ReviewBundle
) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    client = client_for(fake)
    publish_pull_request(client, ref(head), bundle, PublishOptions())
    before = len(fake.review_comments)
    port = next(f for f in bundle.findings if f.claim.line == 4)
    changed = port.model_copy(update={"confidence": 0.99})
    updated = bundle.model_copy(update={"findings": [changed if f is port else f for f in bundle.findings]})
    publish_pull_request(client, ref(head), updated, PublishOptions())
    assert len(fake.reviews) == 2 and len(fake.review_comments) == before + 1
    old = next(c for c in fake.review_comments if c["line"] == 4)
    assert "state=resolved" in old["body"] and "Outdated" in old["body"]
    assert "confidence 0.99" in fake.review_comments[-1]["body"]


def test_resolved_findings_mark_old_comments_and_clean_state_collapses_summary(
    pr_repo: tuple[Path, str, str], bundle: ReviewBundle
) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    client = client_for(fake)
    publish_pull_request(client, ref(head), bundle, PublishOptions())
    clean = bundle.model_copy(update={"findings": [], "touched": [], "suggestions": []})
    report = publish_pull_request(client, ref(head), clean, PublishOptions())
    assert "resolve-comment" in [a.kind for a in report.actions]
    assert (
        "state=resolved" in fake.review_comments[0]["body"] and "no longer reports" in fake.review_comments[0]["body"]
    )
    assert "✅ docs-drift: no new documentation drift" in fake.issue_comments[7][0]["body"]
    # A clean PR that never had a summary gets no comment at all.
    quiet = FakeGitHub(pr_files(repo, base, head))
    publish_pull_request(client_for(quiet), ref(head), clean, PublishOptions())
    assert quiet.writes() == []


def test_rejected_inline_review_falls_back_to_summary(pr_repo: tuple[Path, str, str], bundle: ReviewBundle) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))  # Line 4 is commentable for our planner...
    original = fake.handle

    def reject(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews"):  # ...but the head moved on GitHub's side.
            fake.requests.append(request)
            return httpx.Response(
                422, json={"message": "Unprocessable Entity", "errors": ["pull_request_review_thread.line"]}
            )
        return original(request)

    client = GitHubClient(token="t", transport=httpx.MockTransport(reject), sleep=lambda _: None)
    report = publish_pull_request(client, ref(head), bundle, PublishOptions())
    kinds = [a.kind for a in report.actions]
    assert "inline-fallback" in kinds and "create-summary" in kinds
    assert "💬 inline" not in fake.issue_comments[7][0]["body"]


def test_marker_on_a_human_comment_does_not_block_our_sticky(
    pr_repo: tuple[Path, str, str], bundle: ReviewBundle
) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    fake.issue_comments[7] = [{"id": 1, "body": render.SUMMARY_MARKER + " copied", "user": HUMAN}]
    publish_pull_request(client_for(fake), ref(head), bundle, PublishOptions(inline=False))
    assert len(fake.issue_comments[7]) == 2 and fake.issue_comments[7][1]["user"]["type"] == "Bot"


@pytest.mark.parametrize(
    ("status", "conclusion"), [("failed", "failure"), ("passed", "success"), ("report_only", "neutral")]
)
def test_check_run_conclusion_follows_the_gate(bundle: ReviewBundle, status: str, conclusion: str) -> None:
    fake = FakeGitHub()
    gated = bundle.model_copy(update={"gate": bundle.gate.model_copy(update={"status": status})})
    publish_pull_request(client_for(fake), ref(bundle.head_sha), gated, PublishOptions(check_run=True, inline=False))
    (run,) = fake.check_runs
    assert run["conclusion"] == conclusion and run["status"] == "completed"
    assert {a["path"] for a in run["output"]["annotations"]} == {"README.md"}


def test_check_run_annotations_are_batched_by_fifty(bundle: ReviewBundle) -> None:
    fake = FakeGitHub()
    many = bundle.model_copy(update={"findings": bundle.findings * 40})
    publish_pull_request(client_for(fake), ref(bundle.head_sha), many, PublishOptions(check_run=True, inline=False))
    assert len(fake.check_runs[0]["output"]["annotations"]) == len(many.findings)
    assert sum(method == "PATCH" and "/check-runs/" in path for method, path in fake.writes()) >= 2


def test_read_only_token_skips_publishing_without_failing(pr_repo: tuple[Path, str, str], bundle: ReviewBundle) -> None:
    repo, base, head = pr_repo
    fake = FakeGitHub(pr_files(repo, base, head))
    fake.read_only = True
    report = publish_pull_request(client_for(fake), ref(head), bundle, PublishOptions())
    assert report.skipped and "read-only" in report.skipped
    assert fake.issue_comments.get(7, []) == []


# --- rendering ----------------------------------------------------------------------------------


def test_comment_truncation_respects_github_limit_and_keeps_marker(bundle: ReviewBundle) -> None:
    huge = render.truncate(render.SUMMARY_MARKER + "\n" + ("x" * 100 + "\n") * 2000)
    assert len(huge) <= render.GITHUB_COMMENT_LIMIT and huge.startswith(render.SUMMARY_MARKER)
    assert "truncated" in huge
    long_reason = bundle.findings[0].model_copy(update={"suggestion": "y" * 5000})
    many = bundle.model_copy(update={"findings": [long_reason] * 400})
    body = render.summary_comment(many, slug="acme/py-cli")
    assert len(body) <= render.GITHUB_COMMENT_LIMIT and "more findings in the full report" in body
    issue = render.issue_body(many, slug="acme/py-cli")
    assert len(issue) <= render.GITHUB_COMMENT_LIMIT


def test_repository_text_is_escaped_everywhere(bundle: ReviewBundle) -> None:
    hostile = bundle.findings[0].model_copy(
        update={
            "claim": bundle.findings[0].claim.model_copy(update={"value": "@octocat ![x](https://evil.example/t.png)"}),
            "suggestion": "</details><img src=x onerror=alert(1)> | `code`",
        }
    )
    evil = bundle.model_copy(update={"findings": [hostile]})
    for body in (
        render.summary_comment(evil, slug="acme/py-cli"),
        render.issue_body(evil, slug="acme/py-cli"),
        render.inline_comment([hostile], None).body,
    ):
        assert "@octocat" not in body and "![x]" not in body and "<img" not in body and "</details><" not in body
    fenced = render.inline_comment([hostile], bundle.suggestions[0].model_copy(update={"replacement": "```x```"}))
    assert "````suggestion\n```x```\n````" in fenced.body
    assert render.blob_url("https://github.com", "acme/../x", "a" * 40, "R.md", 1) is None
    assert render.blob_url("javascript:alert(1)", "acme/py-cli", "a" * 40, "R.md", 1) is None


# --- tracking issue -----------------------------------------------------------------------------


def test_issue_lifecycle_create_update_close_reopen_without_spam(bundle: ReviewBundle) -> None:
    fake = FakeGitHub()
    client = client_for(fake)
    repo = RepositoryRef("acme", "py-cli")
    options = PublishOptions(issue_label="docs-drift")
    publish_issue(client, repo, bundle, options)
    assert len(fake.issues) == 1 and fake.labels == {"docs-drift"}
    issue = fake.issues[0]
    assert issue["labels"] == ["docs-drift"] and render.ISSUE_MARKER in issue["body"]
    assert f"/blob/{bundle.head_sha}/README.md#L4" in issue["body"]
    assert [a.kind for a in publish_issue(client, repo, bundle, options).actions] == ["issue-unchanged"]
    fewer = bundle.model_copy(update={"findings": bundle.findings[:1]})
    publish_issue(client, repo, fewer, options)
    assert len(fake.issues) == 1 and fake.issues[0]["title"] == render.issue_title(1)
    clean = bundle.model_copy(update={"findings": [], "touched": []})
    publish_issue(client, repo, clean, options)
    assert fake.issues[0]["state"] == "closed" and fake.issues[0]["state_reason"] == "completed"
    assert "no documentation drift" in fake.issue_comments[1][-1]["body"]
    assert [a.kind for a in publish_issue(client, repo, clean, options).actions] == ["issue-unchanged"]
    publish_issue(client, repo, bundle, options)
    assert len(fake.issues) == 1 and fake.issues[0]["state"] == "open"
    assert "Reopening" in fake.issue_comments[1][-1]["body"]


# --- Actions context, CLI and Action wiring -----------------------------------------------------


def event_file(tmp_path: Path, *, fork: bool) -> Path:
    head_repo = {"full_name": "someone/py-cli" if fork else "acme/py-cli"}
    payload = {
        "pull_request": {
            "number": 7,
            "head": {"sha": "a" * 40, "repo": head_repo},
            "base": {"sha": "b" * 40, "repo": {"full_name": "acme/py-cli"}},
        }
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_actions_context_detects_forks_and_never_reprs_the_token(tmp_path: Path) -> None:
    env = {
        "GITHUB_EVENT_PATH": str(event_file(tmp_path, fork=True)),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": "acme/py-cli",
        "GITHUB_RUN_ID": "99",
        "GITHUB_TOKEN": "secret-value",
    }
    context = actions_context(env)
    assert context.read_only_fork and context.pr_number == 7 and context.head_sha == "a" * 40
    assert context.run_url == "https://github.com/acme/py-cli/actions/runs/99"
    assert "secret-value" not in repr(context)
    assert not actions_context({**env, "GITHUB_EVENT_PATH": str(event_file(tmp_path, fork=False))}).fork
    assert actions_context({"GITHUB_REPOSITORY": "../evil"}).repository is None


def test_fork_pr_cli_skips_commenting_with_clear_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file(tmp_path, fork=True)))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/py-cli")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only")
    result = CliRunner().invoke(app, ["github", "pr-review", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "fork pull request detected" in result.output and "gate still applies" in result.output


def test_dry_run_prints_bodies_and_sends_nothing(
    pr_repo: tuple[Path, str, str], bundle: ReviewBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / BUNDLE_NAME
    path.write_text(bundle.model_dump_json(), encoding="utf-8")
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_EVENT_PATH"):
        monkeypatch.delenv(name, raising=False)
    sent: list[httpx.Request] = []
    monkeypatch.setattr(httpx.Client, "send", lambda self, request, **kw: sent.append(request))
    args = [
        "github",
        "pr-review",
        "--bundle",
        str(path),
        "--pr",
        "7",
        "--github-repository",
        "acme/py-cli",
        "--dry-run",
    ]
    result = CliRunner().invoke(app, [*args, "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "dry run: nothing was posted" in result.output
    assert "=== create-review: POST /repos/acme/py-cli/pulls/7/reviews" in result.output
    assert "```suggestion" in result.output and render.SUMMARY_MARKER in result.output
    issue = CliRunner().invoke(
        app,
        [
            "github",
            "issue",
            "--bundle",
            str(path),
            "--github-repository",
            "acme/py-cli",
            "--dry-run",
            "--repo",
            str(tmp_path),
        ],
    )
    assert issue.exit_code == 0 and "=== create-issue" in issue.output
    assert sent == []
    missing = CliRunner().invoke(
        app, ["github", "pr-review", "--bundle", str(path), "--pr", "7", "--repo", str(tmp_path)]
    )
    assert missing.exit_code != 0


def test_missing_github_extra_is_an_actionable_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util  # noqa: PLC0415 - patched only for this test.

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "httpx" else real(name, *a))
    result = CliRunner().invoke(app, ["github", "issue", "--dry-run", "--repo", str(tmp_path)])
    assert result.exit_code == 2 and "docs-drift[github]" in result.output


def patch_transport(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub) -> None:
    original = httpx.Client.__init__

    def init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = fake.transport()
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),
        (
            {"DOCS_DRIFT_COMMENT": "true", "GITHUB_EVENT_NAME": "pull_request"},
            ["github", "pr-review", "--bundle", "b.json"],
        ),
        (
            {"DOCS_DRIFT_COMMENT": "true", "GITHUB_EVENT_NAME": "schedule"},
            ["github", "issue", "--bundle", "b.json", "--label", "docs-drift"],
        ),
        ({"DOCS_DRIFT_COMMENT": "true", "GITHUB_EVENT_NAME": "push"}, None),
        (
            {"DOCS_DRIFT_MODE": "pr-review", "DOCS_DRIFT_CHECK_RUN": "true", "DOCS_DRIFT_SUGGESTIONS": "false"},
            ["github", "pr-review", "--bundle", "b.json", "--check-run", "--no-suggestions"],
        ),
        (
            {"DOCS_DRIFT_MODE": "issue", "DOCS_DRIFT_ISSUE_LABEL": "docs"},
            ["github", "issue", "--bundle", "b.json", "--label", "docs"],
        ),
    ],
)
def test_action_inputs_map_to_cli_arguments(env: dict[str, str], expected: list[str] | None) -> None:
    assert action_publish.command({"DOCS_DRIFT_BUNDLE": "b.json", **env}) == expected


@pytest.mark.parametrize(
    "env", [{"DOCS_DRIFT_MODE": "comment"}, {"DOCS_DRIFT_COMMENT": "yes"}, {"DOCS_DRIFT_MODE": "issue"}]
)
def test_action_publish_rejects_invalid_inputs(env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert action_publish.main(env) == 1
    assert "::error" in capsys.readouterr().out


def test_action_scan_then_publish_step_posts_review(
    pr_repo: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = pr_repo
    for key, value in {
        "GITHUB_WORKSPACE": str(repo),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "SCAN_BASE_REF": base,
        "FAIL_ON_DRIFT": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    action_runner.main()  # The scan step: no token in its environment.
    outputs = dict(line.split("=", 1) for line in (tmp_path / "output.txt").read_text().splitlines())
    bundle_path = Path(outputs["bundle-path"])
    assert load_bundle(bundle_path).base_sha == base
    fake = FakeGitHub(pr_files(repo, base, head))
    patch_transport(monkeypatch, fake)
    monkeypatch.chdir(repo)
    publish_env = {
        "DOCS_DRIFT_MODE": "pr-review",
        "DOCS_DRIFT_BUNDLE": str(bundle_path),
        "GITHUB_EVENT_PATH": str(event_file(tmp_path, fork=False)),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": "acme/py-cli",
    }
    for key, value in {**publish_env, "GITHUB_TOKEN": "publish-token"}.items():
        monkeypatch.setenv(key, value)
    assert action_publish.main(publish_env) == 0
    assert len(fake.issue_comments[7]) == 1
    assert all(r.headers["authorization"] == "Bearer publish-token" for r in fake.requests)
    assert "publish-token" not in capsys.readouterr().out
    fake.read_only = True
    fake.issue_comments.clear()
    assert action_publish.main(publish_env) == 0  # Publishing problems never fail the step.


def test_action_publish_reports_failures_as_warnings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    env = {"DOCS_DRIFT_MODE": "issue", "DOCS_DRIFT_BUNDLE": str(tmp_path / "missing.json")}
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/py-cli")
    assert action_publish.main(env) == 0
    assert "::warning" in capsys.readouterr().out


# --- App manifest helper ------------------------------------------------------------------------


def test_manifest_file_matches_generator_and_requests_least_privilege() -> None:
    shipped = json.loads((Path(__file__).parents[1] / "deploy/github-app-manifest.json").read_text(encoding="utf-8"))
    assert shipped == manifest.build_manifest("https://13-211-179-95.sslip.io")
    assert shipped["default_permissions"] == {
        "checks": "write",
        "pull_requests": "write",
        "issues": "write",
        "contents": "read",
        "metadata": "read",
    }
    assert shipped["hook_attributes"]["url"].endswith("/github/webhook")
    with pytest.raises(ValueError):
        manifest.build_manifest("http://insecure.example")
    page = manifest.registration_page(shipped, "st<ate", org="acme")
    assert "organizations/acme/settings/apps/new?state=st&lt;ate" in page and "<ate" not in page
    with pytest.raises(ValueError):
        manifest.registration_page(shipped, "s", org="../x")


def test_manifest_code_exchange_prints_secrets_commands_only(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub()
    credentials = manifest.exchange_code("abc123", client=client_for(fake))
    assert fake.requests[0].url.path == "/app-manifests/abc123/conversions"
    text = manifest.secrets_commands(credentials)
    assert "append these three lines to /etc/docs-drift.env" in text
    assert "\nDOCS_DRIFT_GITHUB_APP_ID=42\n" in text and "deploy/ec2/redeploy.sh" in text
    assert "fly " not in text
    assert "DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY=" in text and "BEGIN RSA" not in text  # base64, one shell word
    assert "returned no webhook secret" in text
    with pytest.raises(ValueError):
        manifest.exchange_code("../../user")
    patch_transport(monkeypatch, fake)
    result = CliRunner().invoke(app, ["github", "app-exchange", "abc123"])
    assert result.exit_code == 0 and "DOCS_DRIFT_GITHUB_WEBHOOK_SECRET=" in result.output
    shown = CliRunner().invoke(app, ["github", "app-manifest", "--url", "https://drift.example"])
    assert json.loads(shown.output)["hook_attributes"]["url"] == "https://drift.example/github/webhook"
