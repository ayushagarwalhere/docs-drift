"""Check README file targets and HTTP links, recording failures without claiming pending URLs work."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from markdown_it import MarkdownIt


def targets(readme: str) -> list[str]:
    """Extract Markdown links and images, including badges nested in links."""
    result = set()
    for block in MarkdownIt().parse(readme):
        for token in block.children or []:
            target = token.attrGet("href") if token.type == "link_open" else token.attrGet("src")
            if isinstance(target, str) and target:
                result.add(target)
    return sorted(result)


def check(target: str, root: Path) -> dict[str, object]:
    """Follow HTTP redirects; distinguish a failed check from an accessible resource."""
    parts = urlsplit(target)
    if parts.scheme in {"https", "http"}:
        try:
            response = httpx.get(target, follow_redirects=True, timeout=20)
            return {"target": target, "status": response.status_code, "ok": response.is_success}
        except httpx.HTTPError as error:
            return {"target": target, "status": type(error).__name__, "ok": False}
    exists = not parts.path or (root / unquote(parts.path)).exists()
    return {"target": target, "status": "local target exists" if exists else "missing local target", "ok": exists}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".docs-drift/readme-links.json"))
    args = parser.parse_args()
    root = Path.cwd()
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(
            pool.map(lambda target: check(target, root), targets(Path("README.md").read_text(encoding="utf-8")))
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checked": len(rows), "failed": [row for row in rows if not row["ok"]]}))
    raise SystemExit(any(not row["ok"] for row in rows))


if __name__ == "__main__":
    main()
