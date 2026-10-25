"""Polite asynchronous HTTP client wrapper for data collectors.

Boundary: Handles rate-limiting, explicit timeouts, bounded retries with exponential
backoff and jitter, and standard header configuration for public GET harvesting.
Strictly public GET queries only; never submits credentials or modifies external state.
"""

import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 15.0
DEFAULT_WRITE_TIMEOUT = 10.0
DEFAULT_POOL_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 0.5
DEFAULT_MAX_CONCURRENCY = 3
DEFAULT_USER_AGENT = (
    "AI-Job-Intelligence-Bot/1.0 (+https://github.com/TANMAY4806w/AI-Job-Intelligence)"
)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class PoliteHttpClient:
    """Polite HTTP client managing concurrency, bounded retries, and explicit timeouts."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        user_agent: str = DEFAULT_USER_AGENT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        """Initialize polite HTTP client.

        Args:
            client: Optional pre-configured httpx.AsyncClient (e.g., for test mocking).
            max_retries: Maximum retry attempts for transient server/transport errors.
            backoff_factor: Base factor in seconds for exponential backoff.
            max_concurrency: Maximum simultaneous in-flight requests.
            user_agent: Identifying User-Agent header.
            connect_timeout: Connection timeout in seconds.
            read_timeout: Response read timeout in seconds.
        """
        self._external_client = client is not None
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=DEFAULT_WRITE_TIMEOUT,
            pool=DEFAULT_POOL_TIMEOUT,
        )
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=headers)
        self.max_retries = max(0, max_retries)
        self.backoff_factor = max(0.0, backoff_factor)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    @property
    def client(self) -> httpx.AsyncClient:
        """Underlying httpx.AsyncClient instance."""
        return self._client

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute a polite public GET request with bounded retries and concurrency control.

        Args:
            url: Target URL.
            params: Optional query parameters.
            headers: Optional additional headers.

        Returns:
            httpx.Response: The HTTP response object.

        Raises:
            httpx.TransportError: On unrecoverable network or transport failures after retries.
            httpx.TimeoutException: On request timeouts after retries.
            httpx.HTTPStatusError: Never directly raised; non-2xx responses are returned for caller inspection.
        """
        last_exception: Exception | None = None

        for attempt in range(self.max_retries + 1):
            async with self._semaphore:
                try:
                    response = await self._client.get(url, params=params, headers=headers)
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last_exception = exc
                    if attempt < self.max_retries:
                        sleep_time = self._calculate_backoff(attempt)
                        logger.warning(
                            "Transient transport failure fetching %s (attempt %d/%d): %s. Retrying in %.2fs",
                            url,
                            attempt + 1,
                            self.max_retries,
                            exc,
                            sleep_time,
                        )
                        await asyncio.sleep(sleep_time)
                        continue
                    raise

                # Check for retryable HTTP status codes (429, 5xx)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    sleep_time = self._calculate_retry_after(response, attempt)
                    logger.warning(
                        "Retryable HTTP %d from %s (attempt %d/%d). Retrying in %.2fs",
                        response.status_code,
                        url,
                        attempt + 1,
                        self.max_retries,
                        sleep_time,
                    )
                    await asyncio.sleep(sleep_time)
                    continue

                return response

        # If loop exits without returning, re-raise the last exception if present
        if last_exception is not None:
            raise last_exception
        return response

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff with full jitter."""
        base = self.backoff_factor * (2**attempt)
        jitter = random.uniform(0, 0.1 * base)
        return base + jitter

    def _calculate_retry_after(self, response: httpx.Response, attempt: int) -> float:
        """Extract Retry-After header value if present, else fallback to exponential backoff."""
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                seconds = float(retry_after)
                if 0.0 <= seconds <= 30.0:
                    return seconds
            except (ValueError, TypeError):
                pass
        return self._calculate_backoff(attempt)

    async def aclose(self) -> None:
        """Close the underlying HTTP client if internally created."""
        if not self._external_client:
            await self._client.aclose()

    async def __aenter__(self) -> "PoliteHttpClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
