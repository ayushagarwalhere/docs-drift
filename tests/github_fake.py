"""In-memory GitHub REST fake on httpx.MockTransport; validates what real GitHub would reject."""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

from docs_drift.github.diff import diff_map

BOT = {"login": "docs-drift[bot]", "type": "Bot"}
HUMAN = {"login": "octocat", "type": "User"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def pr_files(repo: Path, base: str, head: str) -> list[dict[str, Any]]:
    """Build `GET /pulls/{n}/files` entries from a real local diff, as GitHub would."""
    files = []  # GitHub's PR diff is three-dot: from the merge base, not the moving base tip.
    for name in git(repo, "diff", "--name-only", "-z", f"{base}...{head}").split("\0"):
        if not name:
            continue
        patch = git(repo, "diff", "--no-ext-diff", f"{base}...{head}", "--", f":(literal){name}")
        files.append({"filename": name, "status": "modified", "patch": patch[patch.index("@@") :]})
    return files


class FakeGitHub:
    def __init__(self, files: list[dict[str, Any]] | None = None, page_size: int = 100):
        self.files = files or []
        self.page_size = page_size
        self.requests: list[httpx.Request] = []
        self.issue_comments: dict[int, list[dict[str, Any]]] = {}
        self.review_comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.check_runs: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.labels: set[str] = set()
        self.pulls: dict[int, dict[str, Any]] = {}
        self.forced: list[httpx.Response] = []
        self.read_only = False
        self.checks_forbidden = False  # Like a PAT: only GitHub Apps may write checks.
        self.tokens_issued = 0
        self.token_expiry = "2099-01-01T00:00:00Z"
        self.next_id = 1000

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def writes(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests if r.method != "GET"]

    def _page(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        per_page = min(int(request.url.params.get("per_page", "30")), self.page_size)
        page = int(request.url.params.get("page", "1"))
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {}
        if page * per_page < len(items):
            params = dict(request.url.params)
            params["page"] = str(page + 1)
            headers["link"] = f'<{request.url.copy_with(params=params)}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911, PLR0912, PLR0915 - route table.
        self.requests.append(request)
        if self.forced:
            return self.forced.pop(0)
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else {}
        if self.read_only and method != "GET" and "/access_tokens" not in path:
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if path == "/app":
            assert request.headers["authorization"].startswith("Bearer ey")
            return httpx.Response(200, json={"slug": "docs-drift", "id": 12345})
        if self.checks_forbidden and "/check-runs" in path:
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        if match := re.fullmatch(r"/app/installations/(\d+)/access_tokens", path):
            assert request.headers["authorization"].startswith("Bearer ey")
            self.tokens_issued += 1
            return httpx.Response(
                201, json={"token": f"inst-token-{self.tokens_issued}", "expires_at": self.token_expiry}
            )
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)/files", path):
            return self._page(request, self.files)
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)/comments", path):
            return self._page(request, self.review_comments)
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)/reviews", path):
            allowed = diff_map(self.files)
            for comment in body["comments"]:
                if comment["line"] not in allowed.get(comment["path"], set()) or comment.get("side") != "RIGHT":
                    return httpx.Response(422, json={"message": "Unprocessable Entity", "errors": ["line not in diff"]})
            review = {"id": self._id(), "body": body["body"], "commit_id": body["commit_id"], "event": body["event"]}
            self.reviews.append(review)
            for comment in body["comments"]:
                self.review_comments.append({"id": self._id(), "user": BOT, **comment})
            return httpx.Response(200, json=review)
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/comments/(\d+)", path):
            comment = next(c for c in self.review_comments if c["id"] == int(match.group(1)))
            if comment["user"] != BOT:
                return httpx.Response(403, json={"message": "Must have admin rights"})
            comment["body"] = body["body"]
            return httpx.Response(200, json=comment)
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)", path):
            return httpx.Response(200, json=self.pulls[int(match.group(1))])
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)/comments", path):
            comments = self.issue_comments.setdefault(int(match.group(1)), [])
            if method == "GET":
                return self._page(request, comments)
            comment = {"id": self._id(), "body": body["body"], "user": BOT}
            comments.append(comment)
            return httpx.Response(201, json=comment)
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/issues/comments/(\d+)", path):
            for comments in self.issue_comments.values():
                for comment in comments:
                    if comment["id"] == int(match.group(1)):
                        if comment["user"] != BOT:
                            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
                        comment["body"] = body["body"]
                        return httpx.Response(200, json=comment)
            return httpx.Response(404, json={"message": "Not Found"})
        if re.fullmatch(r"/repos/[^/]+/[^/]+/issues", path):
            if method == "GET":
                label = request.url.params.get("labels")
                rows = [i for i in self.issues if label in i["labels"]]
                return self._page(request, sorted(rows, key=lambda i: -i["number"]))
            issue = {"number": len(self.issues) + 1, "state": "open", "user": BOT, **body}
            self.issues.append(issue)
            return httpx.Response(201, json=issue)
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)", path):
            issue = next(i for i in self.issues if i["number"] == int(match.group(1)))
            issue.update(body)
            return httpx.Response(200, json=issue)
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/labels/(.+)", path):
            if match.group(1) in self.labels:
                return httpx.Response(200, json={"name": match.group(1)})
            return httpx.Response(404, json={"message": "Not Found"})
        if re.fullmatch(r"/repos/[^/]+/[^/]+/labels", path):
            self.labels.add(body["name"])
            return httpx.Response(201, json=body)
        if re.fullmatch(r"/repos/[^/]+/[^/]+/check-runs", path):
            run = {"id": self._id(), **body}
            self.check_runs.append(run)
            return httpx.Response(201, json=run)
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/check-runs/(\d+)", path):
            run = next(r for r in self.check_runs if r["id"] == int(match.group(1)))
            annotations = run.get("output", {}).get("annotations", [])
            run.update({k: v for k, v in body.items() if k != "output"})
            if "output" in body:
                run["output"] = {**body["output"], "annotations": annotations + body["output"].get("annotations", [])}
            return httpx.Response(200, json=run)
        if re.fullmatch(r"/app-manifests/[^/]+/conversions", path):
            return httpx.Response(
                201,
                json={
                    "id": 42,
                    "slug": "docs-drift-test",
                    "pem": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n",
                    "webhook_secret": None,
                    "html_url": "https://github.com/apps/docs-drift-test",
                },
            )
        return httpx.Response(404, json={"message": f"fake: no route {method} {path}"})
