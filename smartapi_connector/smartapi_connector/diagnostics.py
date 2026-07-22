"""Diagnostics utilities for SmartAPI connector.

Provides structured logging, simple metrics counters, and a diagnostics sink interface
that other modules can call. Designed to be implementation-neutral and testable.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, Optional

from .config import get_config

logger = logging.getLogger("smartapi_connector.diagnostics")


class DiagnosticsSink(Protocol):
    def emit(self, record: Dict[str, Any]) -> None: ...


class ConsoleSink:
    def emit(self, record: Dict[str, Any]) -> None:
        print(json.dumps(record))


class Diagnostics:
    def __init__(self, sink: Optional[DiagnosticsSink] = None):
        cfg = get_config()
        self.sink = sink or ConsoleSink()
        self.enabled = bool(cfg.diagnostics_enable)

    def log_event(self, level: str, component: str, message: str, **kwargs: Any) -> None:
        if not self.enabled:
            return
        rec = {
            "ts": time.time(),
            "level": level,
            "component": component,
            "message": message,
            "meta": kwargs,
        }
        try:
            self.sink.emit(rec)
        except Exception:
            logger.exception("failed to emit diagnostic record")

    def incr_metric(self, name: str, value: int = 1, **tags: Any) -> None:
        if not self.enabled:
            return
        rec = {"ts": time.time(), "metric": name, "value": value, "tags": tags}
        try:
            self.sink.emit(rec)
        except Exception:
            logger.exception("failed to emit metric record")

    def gauge_metric(self, name: str, value: float, **tags: Any) -> None:
        if not self.enabled:
            return
        rec = {"ts": time.time(), "gauge": name, "value": value, "tags": tags}
        try:
            self.sink.emit(rec)
        except Exception:
            logger.exception("failed to emit metric record")
