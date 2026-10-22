"""Exercise a deployed demo without executing any scanned repository code."""

import argparse
import json
import time
import urllib.request
from typing import Any

HEADERS = ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options")


def request(base: str, path: str, payload: dict[str, str] | None = None) -> tuple[bytes, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json", "Origin": base})
    with urllib.request.urlopen(req, timeout=30) as response:
        for header in HEADERS:
            assert response.headers.get(header), f"Missing {header} on {path}"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        return response.read(), response.headers


def smoke(base: str) -> None:
    landing, _ = request(base, "/")
    assert b"Inspect the real case" in landing
    print("PASS landing and security headers", flush=True)
    examples = json.loads(request(base, "/api/examples")[0])
    ident = next(e["scan_id"] for e in examples if e["scan_id"] == "example-py-cli")
    verify_report(base, ident)
    print("PASS cached py-cli fixture", flush=True)
    submitted = json.loads(request(base, "/api/scans", {"repo_url": "https://github.com/textualize/rich-cli"})[0])
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        data = json.loads(request(base, "/api/scans/" + submitted["scan_id"])[0])
        if data["status"] == "failed":
            raise AssertionError(data["error"])
        if data["status"] == "done":
            verify_report(base, submitted["scan_id"])
            print("PASS live public scan, HTML and schema-v1 JSON", flush=True)
            return
        time.sleep(1)
    raise TimeoutError("Live scan did not finish")


def verify_report(base: str, ident: str) -> None:
    html, _ = request(base, f"/api/scans/{ident}/report.html")
    assert b"<!doctype html>" in html.lower() and b"docs-drift" in html
    result = json.loads(request(base, f"/api/scans/{ident}/result.json")[0])
    assert result["schema_version"] == 1 and isinstance(result["findings"], list)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    smoke(parser.parse_args().base_url.rstrip("/"))
