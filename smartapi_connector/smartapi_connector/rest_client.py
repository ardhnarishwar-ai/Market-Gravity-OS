"""smartapi_connector.rest_client

Provider-agnostic REST client foundation.

Responsibilities:
- Build request URLs from ConnectorConfig
- Inject auth headers via an injected callable (auth_header_provider)
- Use a pluggable http_client (requests-like) to send requests
- Integrate with rate limiter and retry policy
- Emit structured diagnostics hooks (pluggable)
- Return raw provider responses (no mapping or storage performed here)

This module contains no SmartAPI-specific logic and is safe for unit testing with
mock http_client and auth header providers.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin
import logging

from .config import get_config, ConnectorConfig
from .errors import RateLimitError, TransientError

logger = logging.getLogger(__name__)


class RESTClient:
    def __init__(
        self,
        config: Optional[ConnectorConfig] = None,
        http_client: Optional[Any] = None,
        auth_header_provider: Optional[Callable[[], Dict[str, str]]] = None,
        rate_limiter: Optional[Any] = None,
        retry_policy: Optional[Any] = None,
        diagnostics: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self._config = config or get_config()
        self._http = http_client
        self._auth_header_provider = auth_header_provider
        self._rate_limiter = rate_limiter
        self._retry_policy = retry_policy
        self._diagnostics = diagnostics

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        require_auth: bool = True,
        endpoint_name: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self._http:
            raise RuntimeError("http_client not configured")

        if not path.startswith("/"):
            path = "/" + path
        url = urljoin(str(self._config.smartapi_base_url), path)

        hdrs = dict(headers or {})
        if require_auth and self._auth_header_provider:
            try:
                hdrs.update(self._auth_header_provider())
            except Exception as e:
                raise TransientError(f"auth header provider failed: {e}")

        # Rate limiting
        if self._rate_limiter and endpoint_name:
            allowed = self._rate_limiter.acquire(endpoint_name)
            if not allowed:
                raise RateLimitError(f"rate limit exceeded for {endpoint_name}")

        # Prepare request call
        def do_call() -> Dict[str, Any]:
            resp = self._http.request(method=method, url=url, params=params, json=json, headers=hdrs, timeout=timeout)
            # Expect http_client to raise on network errors; otherwise return raw dict/parsed JSON
            return resp

        if self._retry_policy:
            return self._retry_policy.execute(do_call)
        else:
            return do_call()
