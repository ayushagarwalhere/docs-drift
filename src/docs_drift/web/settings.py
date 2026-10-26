"""Server-owned limits; repository configuration is never loaded by public jobs."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(".docs-drift/web-data")
    max_concurrent: int = 2
    allowed_origin: str = "http://127.0.0.1:8080"
    rate_limit: int = 5
    rate_window: int = 600
    ttl: int = 86400
    max_results: int = 50
    max_queue: int = 32
    max_bytes: int = 200_000_000
    max_files: int = 20_000
    clone_timeout: float = 60
    scan_timeout: float = 120
    history_days: int = 365
    history_commits: int = 2000
    live_enabled: bool = True
    build_sha: str = "development"
    project_url: str = "https://github.com/ayushagarwalhere/docs-drift"
    # /write re-checks dictated text ~600 ms after each burst, so it gets its own, larger budget.
    write_rate_limit: int = 60
    write_rate_window: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        def integer(name: str, default: int, maximum: int) -> int:
            value = int(os.environ.get(name, str(default)))
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}.")
            return value

        return cls(
            data_dir=Path(os.environ.get("DOCS_DRIFT_DATA_DIR", ".docs-drift/web-data")),
            max_concurrent=integer("DOCS_DRIFT_MAX_CONCURRENT", 2, 2),
            allowed_origin=os.environ.get(
                "DOCS_DRIFT_ALLOWED_ORIGIN", os.environ.get("RENDER_EXTERNAL_URL", "http://127.0.0.1:8080")
            ).rstrip("/"),
            rate_limit=integer("DOCS_DRIFT_RATE_LIMIT", 5, 100),
            clone_timeout=integer("DOCS_DRIFT_CLONE_TIMEOUT", 60, 60),
            scan_timeout=integer("DOCS_DRIFT_SCAN_TIMEOUT", 120, 120),
            history_days=integer("DOCS_DRIFT_HISTORY_DAYS", 365, 365),
            history_commits=integer("DOCS_DRIFT_HISTORY_COMMITS", 2000, 2000),
            live_enabled=os.environ.get("DOCS_DRIFT_LIVE_ENABLED", "1") == "1",
            build_sha=os.environ.get("DOCS_DRIFT_BUILD_SHA", os.environ.get("RENDER_GIT_COMMIT", "development"))[:40],
            project_url=os.environ.get("DOCS_DRIFT_PROJECT_URL", "https://github.com/ayushagarwalhere/docs-drift"),
            write_rate_limit=integer("DOCS_DRIFT_WRITE_RATE_LIMIT", 60, 600),
        )
