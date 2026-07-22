from __future__ import annotations

"""Session manager for SmartAPI connector.

Manages authentication lifecycle via an injected AuthProvider. It schedules token
refreshes using a pluggable scheduler. Exposes a read-only SessionContext and
hook registration. Does not perform REST or WebSocket operations.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime, timezone

from .auth import AuthProvider, parse_token_response, TokenInfo
from .config import get_config, ConnectorConfig
from .errors import AuthError, TransientError


class SessionState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass(frozen=True)
class SessionContext:
    is_authenticated: bool
    created_at: str
    expires_at: Optional[str]
    seconds_until_expiry: Optional[int]
    token_ttl_seconds: Optional[int]
    consecutive_failures: int
    last_error: Optional[str]
    state: SessionState


class SessionManager:
    def __init__(self, auth_provider: AuthProvider, config: Optional[ConnectorConfig] = None, scheduler: Optional[Any] = None):
        self._auth = auth_provider
        self._config = config or get_config()
        self._scheduler = scheduler
        self._state = SessionState.STOPPED
        self._token_info: Optional[TokenInfo] = None
        self._created_at: Optional[float] = None
        self._consecutive_failures = 0
        self._last_error: Optional[str] = None
        self._hooks: Dict[str, List[Callable[[SessionContext], None]]] = {}
        self._refresh_handle = None
        self._refresh_margin = 60  # seconds before expiry to refresh

    # Hook management
    def register_hook(self, name: str, fn: Callable[[SessionContext], None]) -> None:
        self._hooks.setdefault(name, []).append(fn)

    def _fire_hook(self, name: str) -> None:
        ctx = self.get_session_context()
        for fn in self._hooks.get(name, []):
            try:
                fn(ctx)
            except Exception:
                pass

    # Lifecycle
    def start(self) -> None:
        if self._state in (SessionState.STARTING, SessionState.RUNNING):
            return
        self._state = SessionState.STARTING
        try:
            raw = self._auth.login()
            self._token_info = parse_token_response(raw)
            self._created_at = datetime.now(timezone.utc).timestamp()
            self._consecutive_failures = 0
            self._last_error = None
            self._state = SessionState.RUNNING
            self._schedule_refresh()
            self._fire_hook("on_authenticated")
        except Exception as e:
            self._last_error = str(e)
            self._consecutive_failures += 1
            self._state = SessionState.ERROR
            self._fire_hook("on_session_error")
            raise

    def stop(self) -> None:
        self._state = SessionState.STOPPING
        try:
            try:
                self._auth.logout()
            except Exception:
                pass
            self._cancel_refresh()
            self._state = SessionState.STOPPED
            self._fire_hook("on_session_stopped")
        except Exception as e:
            self._last_error = str(e)
            self._state = SessionState.ERROR
            self._fire_hook("on_session_error")
            raise

    def is_authenticated(self) -> bool:
        return self._token_info is not None and datetime.now(timezone.utc).timestamp() < self._token_info.expires_at

    def get_session_context(self) -> SessionContext:
        now_ts = datetime.now(timezone.utc).timestamp()
        if self._token_info:
            seconds_until = int(max(0, self._token_info.expires_at - now_ts))
            ttl = int(max(0, self._token_info.expires_at - (self._created_at or now_ts)))
            expires_iso = datetime.fromtimestamp(self._token_info.expires_at, tz=timezone.utc).isoformat()
        else:
            seconds_until = None
            ttl = None
            expires_iso = None
        created_iso = datetime.fromtimestamp(self._created_at, tz=timezone.utc).isoformat() if self._created_at else ""
        return SessionContext(
            is_authenticated=self.is_authenticated(),
            created_at=created_iso,
            expires_at=expires_iso,
            seconds_until_expiry=seconds_until,
            token_ttl_seconds=ttl,
            consecutive_failures=self._consecutive_failures,
            last_error=self._last_error,
            state=self._state,
        )

    # Internal scheduling
    def _schedule_refresh(self) -> None:
        self._cancel_refresh()
        if not self._token_info or not self._scheduler:
            return
        delay = max(0, (self._token_info.expires_at - datetime.now(timezone.utc).timestamp()) - self._refresh_margin)
        try:
            self._refresh_handle = self._scheduler.schedule(delay, self._refresh_callback)
        except Exception:
            self._refresh_handle = None

    def _cancel_refresh(self) -> None:
        if self._refresh_handle and self._scheduler:
            try:
                self._scheduler.cancel(self._refresh_handle)
            except Exception:
                pass
            finally:
                self._refresh_handle = None

    def _refresh_callback(self) -> None:
        try:
            if not self._token_info:
                raise AuthError("No token to refresh")
            raw = self._auth.refresh(self._token_info.access_token)
            self._token_info = parse_token_response(raw)
            self._consecutive_failures = 0
            self._last_error = None
            self._schedule_refresh()
            self._fire_hook("on_token_refreshed")
        except TransientError as te:
            self._consecutive_failures += 1
            self._last_error = str(te)
            if self._consecutive_failures >= (self._config.max_reconnect_attempts or 3):
                self._state = SessionState.ERROR
                self._fire_hook("on_session_error")
            else:
                # Simple retry via scheduler with backoff
                backoff = min(60, 2 ** self._consecutive_failures)
                if self._scheduler:
                    self._refresh_handle = self._scheduler.schedule(backoff, self._refresh_callback)
        except Exception as e:
            self._consecutive_failures += 1
            self._last_error = str(e)
            self._state = SessionState.ERROR
            self._fire_hook("on_session_error")

    # Manual refresh
    def trigger_manual_refresh(self) -> None:
        # perform refresh synchronously
        self._refresh_callback()

    # Health summary for diagnostics
    def health_summary(self) -> Dict[str, Any]:
        ctx = self.get_session_context()
        return {
            "state": ctx.state.value,
            "is_authenticated": ctx.is_authenticated,
            "created_at": ctx.created_at,
            "expires_at": ctx.expires_at,
            "seconds_until_expiry": ctx.seconds_until_expiry,
            "consecutive_failures": ctx.consecutive_failures,
            "last_error": ctx.last_error,
        }
