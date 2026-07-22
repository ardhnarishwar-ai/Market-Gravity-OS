"""smartapi_connector.errors

Domain-specific exceptions for the SmartAPI connector.
"""

class ConnectorError(Exception):
    """Base class for connector errors."""


class AuthError(ConnectorError):
    """Authentication-related errors."""


class NotAuthenticated(AuthError):
    """Raised when an operation requires authentication but none exists."""


class TransientError(ConnectorError):
    """Transient errors suitable for retrying (network hiccups, temporary provider issues)."""


class RateLimitError(TransientError):
    """Raised when the provider indicates rate limit exceeded."""


class NotImplementedProviderError(ConnectorError):
    """Raised when a provider-specific operation is not implemented in a pluggable client."""
