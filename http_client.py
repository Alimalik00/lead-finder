"""Shared HTTP layer: rate limiting, retries and error normalisation.

Every outbound request in the application goes through :class:`HttpClient`, so
retry policy, back-off, timeouts and the user-agent are defined exactly once.

Design notes
------------
* Retries are hand-rolled rather than delegated to ``urllib3.Retry`` so that we
  can honour a ``Retry-After`` header, apply jitter, and log each attempt.
* :class:`RateLimiter` is a simple thread-safe minimum-interval gate. It is
  intentionally simpler than a token bucket: the APIs we call publish
  per-second quotas, and spacing requests evenly is the friendliest way to stay
  under them while several worker threads are running.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, Mapping, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

from logging_setup import get_logger

logger = get_logger("http")

#: Status codes worth trying again.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})


class RateLimiter:
    """Thread-safe gate that enforces a minimum interval between calls."""

    def __init__(self, requests_per_second: float) -> None:
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        """Block until the caller is allowed to issue its request."""
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class HttpError(Exception):
    """Raised when a request fails after exhausting all retries."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        url: str = "",
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body


class SSLVerificationError(HttpError):
    """Raised when a TLS handshake or certificate check fails.

    Kept distinct from :class:`HttpError` because a certificate problem is a
    *finding* for the audit (an expired or mismatched cert is a real, sellable
    issue), not merely a failed request.
    """


class HttpClient:
    """A small ``requests`` wrapper with retries, back-off and rate limiting.

    Args:
        timeout: Per-request timeout in seconds.
        max_retries: Number of *additional* attempts after the first failure.
        backoff_factor: Base for the exponential back-off (``factor * 2**n``).
        backoff_max: Ceiling for any single sleep, in seconds.
        user_agent: Value sent as the ``User-Agent`` header.
        rate_limiter: Optional shared :class:`RateLimiter`.
        verify_ssl: Whether certificates are verified by default.
        pool_size: urllib3 connection-pool size (raise it for wide fan-out).
    """

    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_factor: float = 0.8,
        backoff_max: float = 30.0,
        user_agent: str = "LeadFinderBot/1.0",
        rate_limiter: Optional[RateLimiter] = None,
        verify_ssl: bool = True,
        pool_size: int = 20,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.backoff_max = backoff_max
        self.verify_ssl = verify_ssl
        self.rate_limiter = rate_limiter

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    # -- internals ----------------------------------------------------------

    def _sleep_for(self, attempt: int, response: Optional[requests.Response]) -> float:
        """Compute the back-off delay, honouring ``Retry-After`` when present."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), self.backoff_max)
                except ValueError:
                    pass  # Date-formatted Retry-After: fall through to back-off.
        delay = self.backoff_factor * (2 ** attempt)
        delay += random.uniform(0, self.backoff_factor)  # jitter, avoids lockstep
        return min(delay, self.backoff_max)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        data: Optional[Any] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        allow_redirects: bool = True,
        verify: Optional[bool] = None,
        stream: bool = False,
        retry_on_status: bool = True,
    ) -> requests.Response:
        """Issue a request, retrying transient failures.

        Raises:
            HttpError: If every attempt fails, or the final response has a
                retryable status code.
        """
        attempts = self.max_retries + 1
        last_error: Optional[str] = None
        last_status: Optional[int] = None
        last_body = ""

        for attempt in range(attempts):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()

            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=dict(headers) if headers else None,
                    timeout=timeout or self.timeout,
                    allow_redirects=allow_redirects,
                    verify=self.verify_ssl if verify is None else verify,
                    stream=stream,
                )
            except requests.exceptions.SSLError as exc:
                # Surfaced as a distinct type so website_audit can flag an SSL
                # problem instead of merely recording "unreachable".
                raise SSLVerificationError(f"SSL error: {exc}", url=url) from exc
            except requests.exceptions.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts - 1:
                    delay = self._sleep_for(attempt, None)
                    logger.debug(
                        "%s %s failed (%s); retrying in %.1fs [%d/%d]",
                        method.upper(), url, last_error, delay, attempt + 1, self.max_retries,
                    )
                    time.sleep(delay)
                    continue
                break

            if retry_on_status and response.status_code in RETRYABLE_STATUS:
                last_status = response.status_code
                last_body = response.text[:500]
                last_error = f"HTTP {response.status_code}"
                if attempt < attempts - 1:
                    delay = self._sleep_for(attempt, response)
                    logger.debug(
                        "%s %s returned %d; retrying in %.1fs [%d/%d]",
                        method.upper(), url, response.status_code, delay,
                        attempt + 1, self.max_retries,
                    )
                    time.sleep(delay)
                    continue
                break

            return response

        raise HttpError(
            f"{method.upper()} {url} failed after {attempts} attempt(s): {last_error}",
            status_code=last_status,
            url=url,
            body=last_body,
        )

    # -- convenience wrappers ------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("retry_on_status", False)
        return self.request("HEAD", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Dict[str, Any]:
        """GET a URL and parse the body as JSON.

        Raises:
            HttpError: On transport failure, a non-2xx status, or invalid JSON.
        """
        response = self.get(url, **kwargs)
        return self._parse_json(response)

    def post_json(self, url: str, **kwargs: Any) -> Dict[str, Any]:
        """POST to a URL and parse the body as JSON."""
        response = self.post(url, **kwargs)
        return self._parse_json(response)

    @staticmethod
    def _parse_json(response: requests.Response) -> Dict[str, Any]:
        if response.status_code >= 400:
            raise HttpError(
                f"HTTP {response.status_code} for {response.url}",
                status_code=response.status_code,
                url=response.url,
                body=response.text[:1000],
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise HttpError(
                f"Response from {response.url} was not valid JSON: {exc}",
                status_code=response.status_code,
                url=response.url,
                body=response.text[:500],
            ) from exc
        if not isinstance(payload, dict):
            raise HttpError(
                f"Expected a JSON object from {response.url}, got {type(payload).__name__}",
                status_code=response.status_code,
                url=response.url,
            )
        return payload

    def probe(self, url: str, timeout: Optional[float] = None) -> Tuple[Optional[int], str]:
        """Cheaply check whether *url* resolves to something that exists.

        Tries ``HEAD`` first and falls back to a ranged ``GET`` for the many
        servers that answer ``HEAD`` with 405/501 while serving ``GET`` fine.

        Returns:
            ``(status_code, error)``. ``status_code`` is ``None`` when the
            request could not be completed at all.
        """
        try:
            response = self.head(url, timeout=timeout, allow_redirects=True)
            if response.status_code in (403, 405, 501) or response.status_code >= 500:
                response = self.get(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    stream=True,
                    headers={"Range": "bytes=0-2047"},
                    retry_on_status=False,
                )
                response.close()
            return response.status_code, ""
        except HttpError as exc:
            return exc.status_code, str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return None, f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def build_client(
    http_settings: Any,
    requests_per_second: Optional[float] = None,
    pool_size: int = 20,
) -> HttpClient:
    """Construct an :class:`HttpClient` from a :class:`config.HttpSettings`."""
    limiter = RateLimiter(requests_per_second) if requests_per_second else None
    return HttpClient(
        timeout=http_settings.timeout_seconds,
        max_retries=http_settings.max_retries,
        backoff_factor=http_settings.backoff_factor,
        backoff_max=http_settings.backoff_max_seconds,
        user_agent=http_settings.user_agent,
        rate_limiter=limiter,
        verify_ssl=http_settings.verify_ssl,
        pool_size=pool_size,
    )
