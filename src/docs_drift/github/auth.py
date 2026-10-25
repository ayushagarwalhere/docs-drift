"""GitHub App authentication: RS256 app JWTs and cached installation tokens.

Requires the ``github-app`` extra (PyJWT + cryptography). Private keys are held
in memory only; nothing here logs or persists key material or tokens.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from docs_drift.github.app_config import AppConfigError, load_private_key
from docs_drift.github.client import GitHubClient

__all__ = ["AppAuthError", "InstallationTokens", "app_jwt", "load_private_key"]

JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540  # GitHub rejects exp more than 10 minutes ahead; stay inside clock skew.
TOKEN_REFRESH_MARGIN = 300


class AppAuthError(AppConfigError):
    """App credentials are missing or malformed; the message never echoes key material."""


def app_jwt(app_id: str, private_key: str, now: float | None = None) -> str:
    """Create a short-lived app JWT: ``iat`` backdated 60s, ``exp`` under 10 minutes, ``iss`` = App ID."""
    import jwt  # noqa: PLC0415 - optional github-app extra.

    issued = int(time.time() if now is None else now) - JWT_BACKDATE_SECONDS
    claims = {"iat": issued, "exp": issued + JWT_BACKDATE_SECONDS + JWT_LIFETIME_SECONDS, "iss": str(app_id)}
    try:
        return jwt.encode(claims, private_key, algorithm="RS256")
    except (ValueError, TypeError, jwt.PyJWTError):
        raise AppAuthError("The GitHub App private key could not sign a JWT; check the PEM value.") from None


@dataclass
class _Cached:
    token: str
    expires_at: float


@dataclass
class InstallationTokens:
    """Mint and cache installation tokens per installation, refreshing 5 minutes before expiry."""

    app_id: str
    private_key: str
    client: GitHubClient
    clock: Callable[[], float] = time.time
    _cache: dict[int, _Cached] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __repr__(self) -> str:
        return f"InstallationTokens(app_id={self.app_id!r}, cached={len(self._cache)})"

    def jwt(self) -> str:
        return app_jwt(self.app_id, self.private_key, self.clock())

    def token(self, installation_id: int) -> str:
        with self._lock:
            cached = self._cache.get(installation_id)
            if cached and cached.expires_at - TOKEN_REFRESH_MARGIN > self.clock():
                return cached.token
            data: dict[str, Any] = self.client.post(
                f"/app/installations/{int(installation_id)}/access_tokens", {}, auth=self.jwt(), idempotent=True
            )
            token = str(data["token"])
            try:
                expires = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                expires = self.clock() + 3600
            self._cache[installation_id] = _Cached(token, expires)
            return token

    def invalidate(self, installation_id: int) -> None:
        with self._lock:
            self._cache.pop(installation_id, None)

    def provider(self, installation_id: int) -> Callable[[], str]:
        return lambda: self.token(installation_id)
