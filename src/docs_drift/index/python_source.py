"""Share immutable Python source snapshots across the static indexers in one build."""

import ast
import warnings
from functools import lru_cache
from pathlib import Path


def read_python(path: Path) -> tuple[str, ast.Module]:
    stat = path.stat()
    return _snapshot(path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=8192)
def _snapshot(path: Path, mtime_ns: int, size: int) -> tuple[str, ast.Module]:
    source = path.read_text(encoding="utf-8", errors="replace")
    with warnings.catch_warnings():
        # Invalid escapes in scanned code are the target project's concern, not scan output.
        warnings.simplefilter("ignore", SyntaxWarning)
        return source, ast.parse(source, filename=str(path))


def clear_python_cache() -> None:
    _snapshot.cache_clear()
