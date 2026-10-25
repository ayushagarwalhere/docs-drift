"""GitHub App manifest flow: register the App in two clicks without writing secrets to disk.

https://docs.github.com/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
"""

import base64
import html
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from docs_drift.github.client import DEFAULT_API_URL, GitHubClient

PERMISSIONS = {
    "checks": "write",
    "pull_requests": "write",
    "issues": "write",
    "contents": "read",
    "metadata": "read",
}
EVENTS = ["pull_request", "issue_comment"]
_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d{2,5})?(?:/[A-Za-z0-9._~/-]*)?$")
_CODE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
DEFAULT_ENV_FILE = "/etc/docs-drift.env"
DEFAULT_URL = "https://13-211-179-95.sslip.io"
_ORG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def build_manifest(url: str, *, name: str = "docs-drift", redirect_url: str | None = None) -> dict[str, Any]:
    """The manifest GitHub reads during registration; it contains no secrets."""
    base = url.rstrip("/")
    if not _URL.fullmatch(base):
        raise ValueError("Use the public https:// base URL of your deployment, without query or fragment.")
    manifest: dict[str, Any] = {
        "name": name,
        "url": "https://github.com/ayushagarwalhere/docs-drift",
        "description": "Flags documentation statements the code no longer supports, with suggested fixes.",
        "hook_attributes": {"url": f"{base}/github/webhook", "active": True},
        "public": False,
        "default_permissions": dict(PERMISSIONS),
        "default_events": list(EVENTS),
    }
    if redirect_url:
        manifest["redirect_url"] = redirect_url
    return manifest


def registration_page(manifest: dict[str, Any], state: str, org: str | None = None) -> str:
    if org is not None and not _ORG.fullmatch(org):
        raise ValueError("Use a valid GitHub organization login.")
    target = (
        f"https://github.com/organizations/{org}/settings/apps/new" if org else "https://github.com/settings/apps/new"
    )
    return (
        "<!doctype html><meta charset=utf-8><title>Register docs-drift</title>"
        "<body style='font-family:system-ui;max-width:40rem;margin:4rem auto'>"
        "<h1>Register the docs-drift GitHub App</h1>"
        "<p>GitHub will show the requested permissions before anything is created.</p>"
        f"<form method=post action='{html.escape(target)}?state={html.escape(state)}'>"
        f"<input type=hidden name=manifest value='{html.escape(json.dumps(manifest), quote=True)}'>"
        "<button type=submit style='font-size:1.2rem;padding:.6rem 1.2rem'>Create GitHub App</button></form>"
    )


def exchange_code(code: str, *, client: GitHubClient | None = None) -> dict[str, Any]:
    """POST /app-manifests/{code}/conversions; the code is single-use and expires after one hour."""
    if not _CODE.fullmatch(code):
        raise ValueError("The manifest code has an unexpected format; copy it from the redirect URL.")
    owned = client is None
    client = client or GitHubClient(api_url=DEFAULT_API_URL)
    try:
        data: dict[str, Any] = client.post(f"/app-manifests/{code}/conversions", {})
    finally:
        if owned:
            client.close()
    if not {"id", "pem"} <= data.keys():
        raise ValueError("GitHub did not return App credentials; the code may be used or expired.")
    return data


def secrets_commands(credentials: dict[str, Any], env_file: str = DEFAULT_ENV_FILE) -> str:
    """Render one-time server instructions. Values go to the terminal only, never to a file."""
    key = base64.b64encode(str(credentials["pem"]).encode()).decode()
    webhook = credentials.get("webhook_secret")
    generated = not webhook
    webhook = webhook or secrets.token_hex(32)
    if not re.fullmatch(r"/[A-Za-z0-9._/-]{1,200}", env_file):
        env_file = DEFAULT_ENV_FILE
    lines = [
        f"# Created GitHub App {credentials.get('slug', '')} (id {int(credentials['id'])}).",
        "# These values are secrets: they are shown once and were not saved anywhere. Clear your scrollback after use.",
    ]
    if generated:
        lines.append(
            "# GitHub returned no webhook secret: set the generated value below under App settings > Webhook secret."
        )
    lines += [
        f"# 1. On the server, append these three lines to {env_file} (root-owned, chmod 600):",
        f"DOCS_DRIFT_GITHUB_APP_ID={int(credentials['id'])}",
        f"DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY={key}",
        f"DOCS_DRIFT_GITHUB_WEBHOOK_SECRET={webhook}",
        "# 2. Recreate the container so it reads them: DEPLOY_SHA=origin/main ./deploy/ec2/redeploy.sh",
    ]
    if credentials.get("html_url"):
        lines.append(f"# 3. Install the App: {credentials['html_url']}/installations/new")
    return "\n".join(lines)


def serve_manifest_flow(manifest: dict[str, Any], *, org: str | None, port: int) -> None:  # pragma: no cover - UI.
    """Serve the form on 127.0.0.1, receive GitHub's redirect, exchange the code, print the commands."""
    state = secrets.token_urlsafe(24)
    page = registration_page(manifest, state, org).encode()
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:  # Never log query strings (they carry the code).
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                self._reply(200, page)
            elif parsed.path == "/callback" and secrets.compare_digest(query.get("state", [""])[0], state):
                try:
                    print(secrets_commands(exchange_code(query.get("code", [""])[0])), flush=True)
                    self._reply(200, b"<p>Done. Return to your terminal for the next steps.</p>")
                except ValueError as exc:
                    self._reply(400, html.escape(str(exc)).encode())
                done.set()
            else:
                self._reply(404, b"Not found")

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Open http://127.0.0.1:{port}/ and click 'Create GitHub App'. Waiting for GitHub...", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        done.wait()
    finally:
        server.shutdown()
