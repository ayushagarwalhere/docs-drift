"""GitHub App settings from the environment. Import-light: no httpx, PyJWT or FastAPI here."""

import base64
import binascii
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

APP_ENV = ("DOCS_DRIFT_GITHUB_APP_ID", "DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY", "DOCS_DRIFT_GITHUB_WEBHOOK_SECRET")
MIN_WEBHOOK_SECRET = 16


class AppConfigError(ValueError):
    """The App is partially or wrongly configured; messages never echo secret values."""


def load_private_key(value: str) -> str:
    """Accept a PEM string (literal ``\\n`` escapes allowed) or base64-encoded PEM."""
    text = value.strip()
    if "-----BEGIN" not in text:
        try:
            # Tools like `base64` wrap output at 76 columns; whitespace is never part of the encoding.
            text = base64.b64decode("".join(text.split()), validate=True).decode("ascii").strip()
        except (binascii.Error, UnicodeDecodeError):
            raise AppConfigError("The GitHub App private key is neither PEM nor base64-encoded PEM.") from None
    text = text.replace("\\n", "\n").strip()
    if "-----BEGIN" not in text or "PRIVATE KEY-----" not in text:
        raise AppConfigError("The GitHub App private key is not a PEM private key.")
    return text + "\n"


def process_environment() -> dict[str, str]:
    """Read every App variable by its literal name (greppable, and indexed by docs-drift itself)."""
    values = {
        "DOCS_DRIFT_GITHUB_APP_ID": os.environ.get("DOCS_DRIFT_GITHUB_APP_ID"),
        "DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY": os.environ.get("DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY"),
        "DOCS_DRIFT_GITHUB_WEBHOOK_SECRET": os.environ.get("DOCS_DRIFT_GITHUB_WEBHOOK_SECRET"),
        "DOCS_DRIFT_GITHUB_API_URL": os.environ.get("DOCS_DRIFT_GITHUB_API_URL"),
        "DOCS_DRIFT_GITHUB_QUEUE_SIZE": os.environ.get("DOCS_DRIFT_GITHUB_QUEUE_SIZE"),
        "DOCS_DRIFT_GITHUB_WORKERS": os.environ.get("DOCS_DRIFT_GITHUB_WORKERS"),
        "DOCS_DRIFT_GITHUB_HISTORY_DEPTH": os.environ.get("DOCS_DRIFT_GITHUB_HISTORY_DEPTH"),
    }
    return {name: value for name, value in values.items() if value is not None}


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    private_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    api_url: str = "https://api.github.com"
    max_pending: int = 32
    max_concurrent: int = 2
    history_depth: int = 200
    check_name: str = "docs-drift"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GitHubAppConfig | None":
        """Return None when the App is not configured; fail fast when it is only partly configured."""
        env = process_environment() if env is None else env
        values = {name: env.get(name, "").strip() for name in APP_ENV}
        if not any(values.values()):
            return None
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise AppConfigError("The GitHub App is partially configured; also set " + ", ".join(missing) + ".")
        app_id = values["DOCS_DRIFT_GITHUB_APP_ID"]
        if not re.fullmatch(r"\d{1,12}", app_id):
            raise AppConfigError("DOCS_DRIFT_GITHUB_APP_ID must be the numeric App ID.")
        secret = values["DOCS_DRIFT_GITHUB_WEBHOOK_SECRET"]
        if len(secret) < MIN_WEBHOOK_SECRET:
            raise AppConfigError("DOCS_DRIFT_GITHUB_WEBHOOK_SECRET must be at least 16 characters.")

        def bounded(name: str, default: int, maximum: int) -> int:
            raw = env.get(name, str(default))
            if not raw.isdigit() or not 1 <= int(raw) <= maximum:
                raise AppConfigError(f"{name} must be an integer between 1 and {maximum}.")
            return int(raw)

        api_url = env.get("DOCS_DRIFT_GITHUB_API_URL", "https://api.github.com").rstrip("/")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._/-]*)?", api_url):
            raise AppConfigError("DOCS_DRIFT_GITHUB_API_URL must be an https:// URL.")
        return cls(
            app_id=app_id,
            private_key=load_private_key(values["DOCS_DRIFT_GITHUB_APP_PRIVATE_KEY"]),
            webhook_secret=secret,
            api_url=api_url,
            max_pending=bounded("DOCS_DRIFT_GITHUB_QUEUE_SIZE", 32, 256),
            max_concurrent=bounded("DOCS_DRIFT_GITHUB_WORKERS", 2, 4),
            history_depth=bounded("DOCS_DRIFT_GITHUB_HISTORY_DEPTH", 200, 2000),
        )
