"""Compare static CLI symbols with flags explicitly documented in public project docs."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from docs_drift.config import Config  # noqa: E402
from docs_drift.index import build_index  # noqa: E402
from docs_drift.models import SymbolKind  # noqa: E402

PROJECTS = {
    "httpie-cli": "https://github.com/httpie/cli.git",
    "pallets-click": "https://github.com/pallets/click.git",
    "vercel-serve": "https://github.com/vercel/serve.git",
}
FLAG = re.compile(r"(?<![\w-])--[A-Za-z][\w-]*")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="docs-drift-spotcheck-") as temporary:
        for name, url in PROJECTS.items():
            repo = Path(temporary) / name
            command = ["git", "clone", "--depth", "1", url, str(repo)]
            print("Command: " + " ".join(command))
            subprocess.run(command, check=True, capture_output=True, text=True)
            index = build_index(repo, Config(), no_cache=True)
            indexed = {symbol.name for symbol in index.symbols if symbol.kind == SymbolKind.cli_flag}
            docs = [path for path in repo.rglob("*.md") if path.name.casefold().startswith(("readme", "usage"))]
            documented = {
                flag for path in docs for flag in FLAG.findall(path.read_text(encoding="utf-8", errors="replace"))
            }
            common = indexed & documented
            print(
                f"{name}: indexed flags={len(indexed)}; README/docs flags={len(documented)}; "
                f"matched={len(common)}/{len(documented)}; frameworks="
                f"{sorted({symbol.framework for symbol in index.symbols if symbol.framework})}"
            )
            if documented - indexed:
                print("  documented but not indexed: " + ", ".join(sorted(documented - indexed)[:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
