"""Polite HTTP client for the ingestion pipeline.

Design goals (from the reglamento + project conventions):

* Deterministic and respectful of official gov.co portals.
* At least ``base_delay`` seconds between consecutive requests.
* Progressive backoff on failures (HTTP errors / timeouts), capped, with a
  bounded number of retries.
* A realistic User-Agent and configurable timeout.
* An on-disk cache keyed by a hash of the URL so already-downloaded URLs are
  never re-fetched (raw-first ingestion).
* Graceful degradation: after exhausting retries it raises a typed
  :class:`FetchError` instead of crashing the whole run.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import requests

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FetchError(Exception):
    """Raised when a URL cannot be fetched after all retries.

    Carries the offending ``url`` so the pipeline can log a ``no_encontrado`` /
    ``error`` event and continue with the next document.
    """

    def __init__(self, url: str, message: str) -> None:
        super().__init__(f"{message} (url={url})")
        self.url = url
        self.message = message


@dataclass
class FetchResult:
    """Result of a fetch: the raw bytes, whether it came from cache, final URL."""

    content: bytes
    from_cache: bool
    final_url: str


class PoliteClient:
    """A rate-limited, backing-off, caching HTTP client."""

    def __init__(
        self,
        *,
        base_delay: float = 1.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        max_backoff: float = 60.0,
        cache_dir: str | Path = "data/raw/cache",
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
    ) -> None:
        self.base_delay = base_delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff
        self.cache_dir = Path(cache_dir)
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        # Timestamp of the last network request, used to enforce spacing.
        self._last_request_ts: float | None = None

    # -- cache helpers ---------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.bin"

    def cached(self, url: str) -> bool:
        """Return True if this URL is already present in the on-disk cache."""
        return self._cache_path(url).exists()

    # -- rate limiting ---------------------------------------------------

    def _respect_rate_limit(self) -> None:
        """Sleep so that at least ``base_delay`` seconds pass between requests."""
        if self._last_request_ts is not None:
            elapsed = time.monotonic() - self._last_request_ts
            wait = self.base_delay - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff, capped at ``max_backoff`` (attempt is 0-based)."""
        delay = self.base_delay * (self.backoff_factor**attempt)
        return min(delay, self.max_backoff)

    # -- public API ------------------------------------------------------

    def get(self, url: str, *, use_cache: bool = True) -> FetchResult:
        """Fetch ``url``, returning a :class:`FetchResult`.

        On repeated failure applies progressive backoff and, once retries are
        exhausted, raises :class:`FetchError`.
        """
        cache_path = self._cache_path(url)
        if use_cache and cache_path.exists():
            return FetchResult(
                content=cache_path.read_bytes(),
                from_cache=True,
                final_url=url,
            )

        last_error: str = "unknown error"
        for attempt in range(self.max_retries):
            if attempt > 0:
                time.sleep(self._backoff_delay(attempt))
            self._respect_rate_limit()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:  # timeouts, DNS, conn errors
                last_error = f"request exception: {exc}"
                continue
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
                # Client errors other than 429 are unlikely to recover on retry,
                # but we still let the loop apply backoff for transient 5xx/429.
                if resp.status_code not in (429,) and 400 <= resp.status_code < 500:
                    break
                continue

            content = resp.content
            if use_cache:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(content)
            return FetchResult(content=content, from_cache=False, final_url=resp.url)

        raise FetchError(url, f"failed after {self.max_retries} attempts: {last_error}")
