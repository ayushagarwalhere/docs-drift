"""Small synchronous GitHub REST client with bounded, jittered retries.

Tokens are only ever placed in the ``Authorization`` header. Errors, logs and
reprs carry the method, path and status but never headers or request bodies.
"""

import logging
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx

from docs_drift import __version__
from docs_drift.llm.privacy import mask_secrets

LOG = logging.getLogger("docs_drift.github")
API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://api.github.com"
USER_AGENT = f"docs-drift/{__version__} (+https://github.com/ayushagarwalhere/docs-drift)"
RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_PAGES = 30
SECONDARY_RATE_LIMIT_WAIT = 60.0
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')

TokenProvider = Callable[[], str]


class GitHubError(Exception):
    """A GitHub API call failed; the message never includes credentials."""

    def __init__(self, method: str, path: str, status: int, message: str):
        self.method, self.path, self.status = method, path, status
        self.api_message = mask_secrets(message)[:300]
        super().__init__(f"GitHub API {method} {path} returned {status}: {self.api_message}")

    @property
    def forbidden(self) -> bool:
        """Read-only tokens (fork PRs, Dependabot) surface as 403 'Resource not accessible'."""
        return self.status in {401, 403, 404} and "rate limit" not in self.api_message.lower()


@dataclass
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 90.0
    jitter: float = 0.25


@dataclass
class GitHubClient:
    """Use ``token`` for a fixed credential or ``token_provider`` for refreshed App tokens."""

    token: str | None = None
    token_provider: TokenProvider | None = None
    api_url: str = DEFAULT_API_URL
    timeout: float = 20.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    _http: httpx.Client | None = field(default=None, init=False, repr=False)

    def __repr__(self) -> str:  # Never include a token in tracebacks or logs.
        return f"GitHubClient(api_url={self.api_url!r}, authenticated={self.authenticated})"

    @property
    def authenticated(self) -> bool:
        return bool(self.token or self.token_provider)

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.api_url.rstrip("/"),
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                transport=self.transport,
                follow_redirects=False,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                    "User-Agent": USER_AGENT,
                },
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _headers(self, auth: str | None) -> dict[str, str]:
        token = auth or (self.token_provider() if self.token_provider else self.token)
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _delay(self, response: httpx.Response | None, attempt: int) -> float:
        """Honour Retry-After / x-ratelimit-reset, else back off; may exceed max_delay (caller fails fast)."""
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    try:
                        return max(0.0, parsedate_to_datetime(retry_after).timestamp() - self.clock())
                    except (TypeError, ValueError):
                        pass
            if response.headers.get("x-ratelimit-remaining") == "0" and response.headers.get("x-ratelimit-reset"):
                try:
                    return max(1.0, float(response.headers["x-ratelimit-reset"]) - self.clock())
                except ValueError:
                    pass
            if self._rate_limited(response):
                # Secondary limits without Retry-After: GitHub asks clients to wait at least a minute.
                return SECONDARY_RATE_LIMIT_WAIT
        backoff = min(self.retry.max_delay, self.retry.base_delay * 2.0**attempt)
        return backoff * (1 + random.uniform(0, self.retry.jitter))

    @staticmethod
    def _rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:  # noqa: PLR2004 - HTTP status.
            return True
        if response.status_code != 403:  # noqa: PLR2004 - HTTP status.
            return False
        if response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers:
            return True
        return "rate limit" in response.text[:2000].lower()

    def request(  # noqa: PLR0913 - explicit request options.
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        auth: str | None = None,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        """Send one logical request with bounded retries.

        Non-idempotent requests (POST by default) are retried only when GitHub cannot have
        processed them: connection failures and rate-limit rejections. A 5xx or read timeout
        after a POST may already have created a comment, so it is surfaced instead of repeated;
        marker-based reconciliation on the next run repairs any gap without duplicates.
        """
        if not path.startswith("/") or "://" in path or ".." in path:
            raise ValueError("GitHub API paths must be relative, starting with '/'.")
        safe = method in {"GET", "PATCH", "PUT", "DELETE"} if idempotent is None else idempotent
        last: Exception | None = None
        for attempt in range(self.retry.attempts):
            response: httpx.Response | None = None
            try:
                response = self.http.request(method, path, json=json, params=params, headers=self._headers(auth))
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
                LOG.warning("GitHub %s %s connection failed (%s); retrying", method, path, type(exc).__name__)
            except httpx.TransportError as exc:
                if not safe:
                    raise GitHubError(method, path, 0, f"no response ({type(exc).__name__}); not retried") from None
                last = exc
                LOG.warning("GitHub %s %s transport error (%s); retrying", method, path, type(exc).__name__)
            else:
                if response.status_code < 400:  # noqa: PLR2004 - HTTP status.
                    return response
                limited = self._rate_limited(response)
                retryable = limited or (safe and response.status_code in RETRYABLE_STATUS)
                if not retryable or attempt + 1 == self.retry.attempts:
                    raise GitHubError(method, path, response.status_code, _message(response))
                LOG.warning("GitHub %s %s returned %s; retrying", method, path, response.status_code)
            if attempt + 1 < self.retry.attempts:
                delay = self._delay(response, attempt)
                if delay > self.retry.max_delay:
                    status = response.status_code if response is not None else 0
                    raise GitHubError(method, path, status, f"rate limited; retry after {delay:.0f}s")
                self.sleep(delay)
        raise GitHubError(method, path, 0, f"network failure after retries: {type(last).__name__}")

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None).json()

    def post(self, path: str, body: dict[str, Any], *, auth: str | None = None, idempotent: bool = False) -> Any:
        response = self.request("POST", path, json=body, auth=auth, idempotent=idempotent)
        return response.json() if response.content else {}

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        response = self.request("PATCH", path, json=body)
        return response.json() if response.content else {}

    def delete(self, path: str) -> None:
        self.request("DELETE", path)

    def paginate(self, path: str, *, per_page: int = 100, max_pages: int = MAX_PAGES, **params: Any) -> Iterator[Any]:
        """Follow ``Link: rel="next"`` on the same API host, at most ``max_pages`` pages."""
        response = self.request("GET", path, params={**params, "per_page": per_page})
        for _ in range(max_pages):
            data = response.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            yield from cast(list[Any], items)
            match = _NEXT.search(response.headers.get("link", ""))
            if not match:
                return
            target = httpx.URL(match.group(1))
            if target.host != self.http.base_url.host:
                raise ValueError("Refusing to follow a pagination link to another host.")
            next_path = target.raw_path.decode("ascii")
            base_path = self.http.base_url.raw_path.decode("ascii").rstrip("/")
            response = self.request("GET", next_path.removeprefix(base_path) or "/")


def _message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    if isinstance(data, dict):
        text = str(data.get("message", "error"))
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            text += " " + "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors[:3])
        return text
    return "error"
