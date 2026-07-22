from __future__ import annotations

"""smartapi_connector.config

Configuration module for SmartAPI Connector.

This module defines a typed ConnectorConfig using pydantic.BaseSettings, which loads
configuration from environment variables and an optional .env file. It centralizes all
connector configuration keys and provides a single source-of-truth for other connector
components.

NOTE: This module implements configuration only. It does not perform network calls or
instantiate other connector components.
"""

from typing import Any, Dict, Optional
from pathlib import Path
import json

from pydantic import BaseSettings, Field, HttpUrl, validator


class ConnectorConfig(BaseSettings):
    # Provider endpoints
    smartapi_base_url: HttpUrl = Field(..., env="SMARTAPI_BASE_URL")
    smartapi_ws_url: HttpUrl = Field(..., env="SMARTAPI_WS_URL")

    # Credentials / authentication
    smartapi_client_id: Optional[str] = Field(None, env="SMARTAPI_CLIENT_ID")
    smartapi_client_secret: Optional[str] = Field(None, env="SMARTAPI_CLIENT_SECRET")
    smartapi_api_key: Optional[str] = Field(None, env="SMARTAPI_API_KEY")
    smartapi_username: Optional[str] = Field(None, env="SMARTAPI_USERNAME")
    smartapi_password: Optional[str] = Field(None, env="SMARTAPI_PASSWORD")
    smartapi_feed_token: Optional[str] = Field(None, env="SMARTAPI_FEED_TOKEN")

    # Connector identity and versioning
    connector_id: str = Field("smartapi_adapter", env="CONNECTOR_ID")
    connector_version: str = Field("0.1.0", env="CONNECTOR_VERSION")

    # Storage & paths
    storage_base_path: Path = Field(Path("./data/smartapi"), env="STORAGE_BASE_PATH")

    # Subscription / runtime tuning
    subscription_chunk_size: int = Field(50, env="SUBSCRIPTION_CHUNK_SIZE")
    heartbeat_interval_ms: int = Field(30_000, env="HEARTBEAT_INTERVAL_MS")
    reconnect_base_delay_ms: int = Field(1_000, env="RECONNECT_BASE_DELAY_MS")
    max_reconnect_attempts: int = Field(10, env="MAX_RECONNECT_ATTEMPTS")

    # Rate limiting (JSON string or empty)
    rate_limit_overrides_json: Optional[str] = Field(None, env="RATE_LIMIT_OVERRIDES_JSON")

    # Diagnostics & logging
    log_level: str = Field("INFO", env="LOG_LEVEL")
    diagnostics_enable: bool = Field(True, env="DIAGNOSTICS_ENABLE")

    # Feature flags
    greeks_fetch_enabled: bool = Field(True, env="GREEKS_FETCH_ENABLED")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def rate_limit_overrides(self) -> Dict[str, Any]:
        """Return rate limit overrides as a dictionary.

        The RATE_LIMIT_OVERRIDES_JSON environment variable may be set to a JSON string
        representing per-endpoint rate limit overrides. This property parses that value
        and returns a typed dictionary. If not set or invalid, an empty dict is returned.
        """
        raw = self.rate_limit_overrides_json
        if not raw:
            return {}

        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
            return {}
        except Exception:
            # Do not raise during config load; instead, return empty and leave diagnostics to flag the issue.
            return {}

    @validator("subscription_chunk_size")
    def validate_chunk_size(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("subscription_chunk_size must be > 0")
        return v

    @validator("storage_base_path", pre=True)
    def expand_storage_path(cls, v: Any) -> Path:
        # Accept strings or Paths and expand user home
        p = Path(v)
        return p.expanduser().resolve()


# Singleton config instance
_config: Optional[ConnectorConfig] = None


def get_config() -> ConnectorConfig:
    """Return a cached ConnectorConfig instance (lazy-loaded).

    Callers should import get_config() and use the returned instance. The first call will
    load environment variables and .env file according to pydantic behavior.
    """
    global _config
    if _config is None:
        _config = ConnectorConfig()
    return _config


def config_to_dict(cfg: Optional[ConnectorConfig] = None) -> Dict[str, Any]:
    """Return a dict representation suitable for diagnostics (redacts sensitive fields).

    Sensitive keys (client_secret, password, api_key, feed_token) are redacted.
    """
    c = cfg or get_config()
    raw = c.dict()

    # Redact sensitive fields
    for k in ("smartapi_client_secret", "smartapi_password", "smartapi_api_key", "smartapi_feed_token"):
        if k in raw and raw[k]:
            raw[k] = "<REDACTED>"

    # Represent storage path as string for JSON serialization
    raw["storage_base_path"] = str(raw.get("storage_base_path"))

    return raw
