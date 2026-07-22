"""WebSocket connection manager (provider-agnostic)

Responsibilities:
- Connect / Disconnect
- Auto reconnect with backoff
- Heartbeat (ping/pong) using scheduler
- Subscribe / Unsubscribe (logical; actual subscribe payloads are provider-specific and
  should be provided by the caller via send_raw or a provider adapter)
- Expose connection state and lifecycle hooks

Design constraints:
- No business logic, event mapping, or storage performed here.
- Provider-specific WebSocket implementation must be injected as `ws_impl`.
- Uses threads for receiver and timers for heartbeat/reconnect; keeps implementation
  simple and testable.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .config import get_config


class ConnectionState(Enum):
    CONNECTING = "connecting"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    ERROR = "error"
    RECONNECTING = "reconnecting"


@dataclass
class WSMessage:
    raw: Any
    received_at: float


class WebSocketClient:
    def __init__(
        self,
        url: str,
        ws_impl: Any,
        headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
        config: Optional[Any] = None,
    ):
        """Create a WebSocketClient.

        - url: WebSocket endpoint URL
        - ws_impl: provider-agnostic implementation with method connect(url, headers)
                   that returns a socket-like object with send(), recv(), close(), ping()/pong() as available.
        - headers_provider: callable returning headers dict (for auth); may be None.
        - config: optional ConnectorConfig (defaults to get_config())
        """
        self.url = url
        self.ws_impl = ws_impl
        self.headers_provider = headers_provider
        self._config = config or get_config()

        self._state = ConnectionState.CLOSED
        self._socket = None
        self._rx_thread: Optional[threading.Thread] = None
        self._rx_stop = threading.Event()
        self._heartbeat_timer: Optional[threading.Timer] = None
        self._reconnect_lock = threading.Lock()
        self._reconnect_attempts = 0

        # Subscriptions tracked logically; payload formation is caller responsibility
        self._subscriptions: set = set()

        # Hooks
        self._on_message: List[Callable[[WSMessage], None]] = []
        self._on_open: List[Callable[[], None]] = []
        self._on_close: List[Callable[[], None]] = []
        self._on_error: List[Callable[[Exception], None]] = []
        self._on_reconnect: List[Callable[[int], None]] = []

        # Heartbeat
        self._heartbeat_interval = getattr(self._config, "heartbeat_interval_ms", 30000) / 1000.0
        self._reconnect_base = getattr(self._config, "reconnect_base_delay_ms", 1000) / 1000.0
        self._max_reconnect = int(getattr(self._config, "max_reconnect_attempts", 10))

    # Hook registration
    def register_on_message(self, fn: Callable[[WSMessage], None]) -> None:
        self._on_message.append(fn)

    def register_on_open(self, fn: Callable[[], None]) -> None:
        self._on_open.append(fn)

    def register_on_close(self, fn: Callable[[], None]) -> None:
        self._on_close.append(fn)

    def register_on_error(self, fn: Callable[[Exception], None]) -> None:
        self._on_error.append(fn)

    def register_on_reconnect(self, fn: Callable[[int], None]) -> None:
        self._on_reconnect.append(fn)

    # Connection management
    def connect(self, blocking: bool = True) -> None:
        if self._state in (ConnectionState.CONNECTING, ConnectionState.OPEN):
            return
        self._state = ConnectionState.CONNECTING
        try:
            headers = self.headers_provider() if self.headers_provider else {}
            self._socket = self.ws_impl.connect(self.url, headers=headers)
            self._state = ConnectionState.OPEN
            self._rx_stop.clear()
            self._start_receiver()
            self._start_heartbeat()
            self._reconnect_attempts = 0
            for fn in self._on_open:
                try:
                    fn()
                except Exception:
                    pass
        except Exception as e:
            self._state = ConnectionState.ERROR
            self._fire_error(e)
            # schedule reconnect
            self._schedule_reconnect()
            if blocking:
                raise

    def disconnect(self) -> None:
        if self._state in (ConnectionState.CLOSING, ConnectionState.CLOSED):
            return
        self._state = ConnectionState.CLOSING
        try:
            self._cancel_heartbeat()
            self._stop_receiver()
            if self._socket:
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None
            self._state = ConnectionState.CLOSED
            for fn in self._on_close:
                try:
                    fn()
                except Exception:
                    pass
        except Exception as e:
            self._state = ConnectionState.ERROR
            self._fire_error(e)
            raise

    def send_raw(self, payload: Any) -> None:
        """Send raw payload through underlying socket; payload formatting is caller responsibility."""
        if self._state != ConnectionState.OPEN:
            raise RuntimeError("WebSocket not open")
        try:
            # attempt to use send_json if provided by implementation, else send
            if hasattr(self._socket, "send_json"):
                self._socket.send_json(payload)
            else:
                self._socket.send(payload)
        except Exception as e:
            self._fire_error(e)
            # If socket is dead, schedule reconnect
            self._schedule_reconnect()
            raise

    # Subscription management (logical only)
    def subscribe(self, topic: str) -> None:
        self._subscriptions.add(topic)

    def unsubscribe(self, topic: str) -> None:
        self._subscriptions.discard(topic)

    def subscriptions(self) -> List[str]:
        return list(self._subscriptions)

    def state(self) -> ConnectionState:
        return self._state

    # Internal receiver thread
    def _start_receiver(self) -> None:
        if self._rx_thread and self._rx_thread.is_alive():
            return
        self._rx_thread = threading.Thread(target=self._receiver_loop, daemon=True)
        self._rx_thread.start()

    def _stop_receiver(self) -> None:
        self._rx_stop.set()
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=2.0)
        self._rx_thread = None

    def _receiver_loop(self) -> None:
        try:
            while not self._rx_stop.is_set() and self._socket:
                try:
                    msg = self._socket.recv()
                    if msg is None:
                        # socket closed or no message; treat as disconnect
                        break
                    m = WSMessage(raw=msg, received_at=time.time())
                    for fn in list(self._on_message):
                        try:
                            fn(m)
                        except Exception:
                            pass
                except Exception as e:
                    self._fire_error(e)
                    break
        finally:
            # Ensure we mark closed and try to reconnect
            if self._state == ConnectionState.OPEN:
                self._state = ConnectionState.CLOSED
            self._schedule_reconnect()

    # Heartbeat
    def _start_heartbeat(self) -> None:
        self._cancel_heartbeat()
        if self._heartbeat_interval and self._state == ConnectionState.OPEN:
            self._heartbeat_timer = threading.Timer(self._heartbeat_interval, self._heartbeat_tick)
            self._heartbeat_timer.daemon = True
            self._heartbeat_timer.start()

    def _cancel_heartbeat(self) -> None:
        if self._heartbeat_timer:
            try:
                self._heartbeat_timer.cancel()
            except Exception:
                pass
            self._heartbeat_timer = None

    def _heartbeat_tick(self) -> None:
        if self._state != ConnectionState.OPEN:
            return
        try:
            if hasattr(self._socket, "ping"):
                try:
                    self._socket.ping()
                except Exception:
                    # if ping not supported but ping method exists, may raise
                    pass
            else:
                # fallback: send a lightweight keepalive string if send is available
                try:
                    self._socket.send("")
                except Exception:
                    pass
        except Exception as e:
            self._fire_error(e)
            self._schedule_reconnect()
            return
        finally:
            # reschedule next heartbeat
            self._start_heartbeat()

    # Reconnect logic
    def _schedule_reconnect(self) -> None:
        with self._reconnect_lock:
            if self._reconnect_attempts >= self._max_reconnect:
                return
            self._reconnect_attempts += 1
            delay = min(60.0, self._reconnect_base * (2 ** (self._reconnect_attempts - 1)))
            self._state = ConnectionState.RECONNECTING
            for fn in self._on_reconnect:
                try:
                    fn(self._reconnect_attempts)
                except Exception:
                    pass
            t = threading.Timer(delay, self._do_reconnect)
            t.daemon = True
            t.start()

    def _do_reconnect(self) -> None:
        try:
            # attempt to close existing socket if present
            try:
                if self._socket:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                    self._socket = None
            except Exception:
                pass
            # Call connect non-blocking; let connect schedule further reconnects on failure
            self.connect(blocking=False)
        except Exception:
            # connect already schedules next reconnect on its own failures
            pass

    # Error handling
    def _fire_error(self, exc: Exception) -> None:
        for fn in list(self._on_error):
            try:
                fn(exc)
            except Exception:
                pass

