"""
smartapi_connector.auth

Defines an authentication interface (AuthProvider) and a SmartAPI-specific
implementation scaffold. The module is provider-agnostic: production code
consumes AuthProvider instances without depending on provider-specific details.

The SmartAPIAuth class is a thin adapter that delegates HTTP calls to a provided
HTTP client. Network behavior is intentionally pluggable to allow testing without
real network calls.
"""

from __future__ import annotations

from typing import Protocol, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import ConnectorConfig, get_config
from . import errors


class AuthProvider(Protocol):
    """Protocol for authentication providers.

    Implementations MUST provide login() and refresh() methods that return a
    dictionary containing at least the access token and expiry information.
    The exact keys are provider-specific; callers should document expected keys
    or wrap providers with adapters that normalize the token dictionary.
    """

    def login(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        ...

    def refresh(self, token: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        ...

    def logout(self) -> None:
        ...


@dataclass
class TokenInfo:
    access_token: str
    expires_at: float  # epoch seconds
    raw: Dict[str, Any]


class SmartAPIAuth:
    """SmartAPI authentication adapter.

    This class provides a thin adapter that invokes an HTTP client to perform
    provider-specific authentication operations. The HTTP client is pluggable
    (for tests or later network implementation).

    NOTE: SmartAPIAuth only orchestrates the auth flow via the provided http_client.
    It does not hard-code network calls; the http_client must implement the
    concrete request semantics used by SmartAPI (configured externally).
    """

    def __init__(self, config: Optional[ConnectorConfig] = None, http_client: Optional[Any] = None):
        self._config = config or get_config()
        self._http = http_client

    def login(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Perform login and return the raw provider response.

        The http_client is expected to expose a method 'post' that accepts an
        endpoint path and JSON payload. This method does not parse provider
        semantics; it returns the raw dictionary as returned by the provider.
        """
        if not self._http:
            raise errors.NotImplementedProviderError("HTTP client not configured for SmartAPIAuth")

        body = payload or {}
        # The actual endpoint path and request shape are provided by the http_client wrapper.
        resp = self._http.post("/login", json=body)
        return resp

    def refresh(self, token: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Attempt to refresh a session token.

        The http_client should implement provider-specific refresh semantics.
        Returns raw provider response dict.
        """
        if not self._http:
            raise errors.NotImplementedProviderError("HTTP client not configured for SmartAPIAuth")

        body = payload or {"token": token}
        resp = self._http.post("/refresh", json=body)
        return resp

    def logout(self) -> None:
        if self._http:
            try:
                self._http.post("/logout", json={})
            except Exception:
                # Best-effort; do not raise on logout
                pass


# A small helper for creating TokenInfo from provider response; callers may wrap as needed.
def parse_token_response(data: Dict[str, Any]) -> TokenInfo:
    """Extract token and expiry information from provider response data.

    This helper attempts to find common keys. Implementations or callers may
    normalize provider responses to the expected shape before using this helper.
    """
    if not isinstance(data, dict):
        raise errors.AuthError("Invalid token response format")

    # Common fields used by different providers
    token = None
    expires_in = None

    # Try a few common locations
    token = data.get("access_token") or data.get("token") or data.get("session_token")
    if token is None:
        # Some providers wrap data in 'data' key
        inner = data.get("data")
        if isinstance(inner, dict):
            token = inner.get("access_token") or inner.get("token") or inner.get("session_token")
            expires_in = inner.get("expires_in") or inner.get("expiry")

    if expires_in is None:
        expires_in = data.get("expires_in") or data.get("expiry_seconds") or data.get("expiry")

    if token is None:
        raise errors.AuthError("No access token present in auth response")

    now = datetime.now(timezone.utc).timestamp()
    if expires_in is None:
        # If provider does not disclose expiry, set a conservative default (1 hour)
        expires_at = now + 3600
    else:
        try:
            expires_at = now + float(expires_in)
        except Exception:
            expires_at = now + 3600

    return TokenInfo(access_token=str(token), expires_at=expires_at, raw=data)
