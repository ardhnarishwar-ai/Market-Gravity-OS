"""Optional Angel One SmartAPI data-only WebSocket adapter."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from datetime import datetime, timezone

from analytics.cas_iep_engine import calculate_cas_iep_if_in_window
from smartapi_connector.candle_pipeline import (
    NiftyFuturesCandlePipeline, fetch_historical_5m, resolve_active_nifty_future,
)

log = logging.getLogger(__name__)

MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

INDEX = {
    "NIFTY": ("99926000", 1, 50),
    "SENSEX": ("99919000", 3, 100),
}


def env_value(*names):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def credentials():
    return {
        "api_key": env_value(
            "ANGEL_ONE_API_KEY",
            "ANGEL_API_KEY",
        ),
        "client_code": env_value(
            "ANGEL_ONE_CLIENT_CODE",
            "ANGEL_CLIENT_CODE",
        ),
        "pin": env_value(
            "ANGEL_ONE_PIN",
            "ANGEL_PIN",
        ),
        "totp_key": env_value(
            "ANGEL_TOTP_KEY",
            "ANGEL_ONE_TOTP_KEY",
            "ANGEL_TOTP_SECRET",
        ),
    }


def credentials_present():
    return all(credentials().values())


def load_scrip_master(requests_module):
    """Load today's instrument master from disk; download only when stale/missing.

    If refresh fails, a previous cache is still usable. With no cache, return an
    empty list so the known index spot token can still start the WebSocket.
    """
    cache_path = Path(__file__).resolve().parents[1] / "scrip_master.json"
    today = datetime.now().astimezone().date()

    if cache_path.is_file():
        try:
            cached_day = datetime.fromtimestamp(
                cache_path.stat().st_mtime
            ).date()
            if cached_day == today:
                with cache_path.open("r", encoding="utf-8") as handle:
                    cached = json.load(handle)
                if isinstance(cached, list) and cached:
                    log.info("Using today's cached scrip master: %s", cache_path)
                    return cached
                log.warning("Today's scrip master cache is empty/invalid; refreshing")
        except Exception as exc:
            log.warning("Could not read scrip master cache: %s", exc)

    temp_path = cache_path.with_suffix(".json.tmp")
    try:
        log.info("Refreshing scrip master cache from Angel One")
        with requests_module.get(
            MASTER_URL,
            timeout=(5, 15),
            stream=True,
        ) as response:
            response.raise_for_status()
            with temp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)

        # Validate before replacing a known-good cache.
        with temp_path.open("r", encoding="utf-8") as handle:
            downloaded = json.load(handle)
        if not isinstance(downloaded, list) or not downloaded:
            raise ValueError("Downloaded scrip master is empty or invalid")

        temp_path.replace(cache_path)
        log.info("Scrip master cached locally: %s", cache_path)
        return downloaded
    except Exception as exc:
        log.warning("Scrip master refresh failed: %s", exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

        # A stale cache is preferable to blocking startup on a mobile network.
        if cache_path.is_file():
            try:
                with cache_path.open("r", encoding="utf-8") as handle:
                    cached = json.load(handle)
                if isinstance(cached, list) and cached:
                    log.warning("Using stale scrip master cache")
                    return cached
            except Exception as cache_exc:
                log.warning("Stale scrip master cache is unreadable: %s", cache_exc)

        log.warning(
            "No scrip master available; starting index-spot WebSocket only "
            "with known token %s",
            INDEX.get("NIFTY", ("99926000",))[0],
        )
        return []


class AngelLiveFeed:
    def __init__(self, index="NIFTY"):
        self.index = index.upper()

        if self.index not in INDEX:
            raise ValueError("MGO_INDEX must be NIFTY or SENSEX")

        self.lock = threading.RLock()
        self.status = "starting"
        self.error = None
        self.spot = {}
        self.options = {}
        self.contracts = {}
        self.atm = None
        self.oi_previous = {}
        self.oi_accel = 0.0
        self.ws = None
        self.thread = None
        self.candles_5m = []
        self.futures_contract = None
        self.candle_pipeline = NiftyFuturesCandlePipeline() if self.index == "NIFTY" else None
        self._cas_orderbook = None  # CAS depth is unavailable until a provider supplies it.
        self._footprint_levels = []  # Populated only by a classified trade/depth adapter.

    def update_footprint_levels(self, levels):
        """Store classified per-price bid/ask volume from an external feed adapter."""
        clean = []
        for row in levels or []:
            if isinstance(row, dict):
                clean.append(dict(row))
        with self.lock:
            self._footprint_levels = clean[-5000:]

    def update_cas_orderbook(self, bids, asks, received_at=None):
        """Accept externally aggregated CAS depth; LTP ticks are not order-book depth."""
        with self.lock:
            self._cas_orderbook = {
                "bids": list(bids or []),
                "asks": list(asks or []),
                "received_at": received_at or datetime.now(timezone.utc).isoformat(),
            }

    def start(self):
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="angel-smartapi-feed",
            )
            self.thread.start()

    def snapshot(self):
        with self.lock:
            return {
                "index": self.index,
                "status": self.status,
                "connected": (
                    self.status == "connected"
                    and bool(self.spot.get("ltp"))
                ),
                "spot": dict(self.spot),
                "options": {
                    k: dict(v) for k, v in self.options.items()
                },
                "atm_strike": self.atm,
                "oi_acceleration": self.oi_accel,
                "candles_5m": self.candle_pipeline.snapshot() if self.candle_pipeline else list(self.candles_5m),
                "current_forming_candle": self.candle_pipeline.forming_snapshot() if self.candle_pipeline else None,
                "cas_reference": self.candle_pipeline.cas_reference_snapshot() if self.candle_pipeline else None,
                "cas_orderbook": dict(self._cas_orderbook) if self._cas_orderbook else None,
                "footprint": [dict(x) for x in self._footprint_levels],
                "cas_iep": calculate_cas_iep_if_in_window(
                    (self._cas_orderbook or {}).get("bids"),
                    (self._cas_orderbook or {}).get("asks"),
                    (self.candle_pipeline.cas_reference_snapshot().get("vwap_reference_price") if self.candle_pipeline else None),
                ) if self.candle_pipeline and self._cas_orderbook else {
                    "status": "UNAVAILABLE",
                    "reason": "No aggregated CAS order-book depth source connected",
                    "indicative_equilibrium_price": None,
                },
                "cas_reference": self.candle_pipeline.cas_reference_snapshot(ltp=self.candle_pipeline.forming_snapshot().get("close") if self.candle_pipeline and self.candle_pipeline.forming_snapshot() else None) if self.candle_pipeline else None,
                "futures_contract": dict(self.futures_contract) if self.futures_contract else None,
                "error": self.error,
            }

    def _run(self):
        try:
            import requests
            import pyotp
            from SmartApi import SmartConnect
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2

            creds = credentials()

            if not all(creds.values()):
                raise RuntimeError(
                    "Missing Angel One environment credentials"
                )

            smart = SmartConnect(api_key=creds["api_key"])

            auth = smart.generateSession(
                creds["client_code"],
                creds["pin"],
                pyotp.TOTP(creds["totp_key"]).now(),
            )

            if not isinstance(auth, dict) or not auth.get("status"):
                self.status = "auth_failed"
                self.error = (
                    auth.get("message", "Authentication failed")
                    if isinstance(auth, dict)
                    else "Authentication error"
                )
                return

            data = auth.get("data") or {}
            jwt = data.get("jwtToken")
            feed = data.get("feedToken") or smart.getfeedToken()

            if not jwt or not feed:
                raise RuntimeError(
                    "Login response lacks feed tokens"
                )

            # Connect the stable index token first. Instrument-master loading,
            # JSON parsing and historical REST work run independently and cannot
            # hold the first spot subscription hostage.
            token, exchange, _ = INDEX[self.index]
            self.ws = SmartWebSocketV2(
                jwt,
                creds["api_key"],
                creds["client_code"],
                feed,
            )
            self._ws_open_event = threading.Event()
            self.ws.on_open = lambda ws: self._opened(ws, token, exchange)
            self.ws.on_data = self._tick
            self.ws.on_error = self._error
            self.ws.on_close = self._closed

            with self.lock:
                self.status = "connecting"

            threading.Thread(
                target=self._load_instruments_background,
                args=(requests, smart),
                daemon=True,
                name="angel-scrip-master-loader",
            ).start()

            log.info(
                "Starting SmartAPI WebSocket immediately with %s spot token",
                token,
            )
            self.ws.connect()

        except Exception as exc:
            log.exception("SmartAPI live feed startup failed")
            with self.lock:
                self.status = "error"
                self.error = f"{type(exc).__name__}: {exc}"

    def _opened(self, ws, token, exchange, futures_token=None):
        with self.lock:
            self.status = "connected"
            self.error = None
        if hasattr(self, "_ws_open_event"):
            self._ws_open_event.set()

        # Do not wait for scrip-master download: subscribe to index spot now.
        ws.subscribe(
            "mgo-index-spot",
            3,
            [{"exchangeType": exchange, "tokens": [str(token)]}],
        )

    def _load_instruments_background(self, requests_module, smart):
        """Load contracts after spot WebSocket startup, then add optional streams."""
        try:
            master = load_scrip_master(requests_module)
            today = datetime.now().date()

            if self.index == "NIFTY" and master:
                try:
                    self.futures_contract = resolve_active_nifty_future(master)
                except Exception as exc:
                    log.warning("NIFTY futures contract unavailable: %s", exc)

                if self.futures_contract:
                    history = []
                    try:
                        history = fetch_historical_5m(
                            smart,
                            self.futures_contract["token"],
                        )
                        log.info("Historical candles loaded: %d", len(history))
                    except Exception as exc:
                        log.warning("Historical candle fetch skipped: %s", exc)
                    self.candle_pipeline.seed_history(history)
                    self.candle_pipeline.seed_today_volume_baseline(history)

            rows = []
            for row in master if isinstance(master, list) else []:
                if not isinstance(row, dict):
                    continue
                if (
                    row.get("exch_seg") not in ("NFO", "BFO")
                    or row.get("instrumenttype") != "OPTIDX"
                    or str(row.get("name", "")).upper() != self.index
                ):
                    continue
                try:
                    expiry = datetime.strptime(row["expiry"], "%d%b%Y").date()
                    strike = float(row["strike"]) / 100
                except (KeyError, TypeError, ValueError):
                    continue
                if expiry >= today:
                    rows.append({**row, "_expiry": expiry, "_strike": strike})

            if rows:
                expiry = min(x["_expiry"] for x in rows)
                with self.lock:
                    self.contracts = {
                        str(x["token"]): {
                            "symbol": str(x.get("symbol", "")),
                            "strike": x["_strike"],
                            "type": str(x.get("symbol", ""))[-2:],
                            "expiry": x["_expiry"].isoformat(),
                            "exchange": 2 if x.get("exch_seg") == "NFO" else 4,
                        }
                        for x in rows if x["_expiry"] == expiry
                    }
            else:
                log.warning("No option contracts loaded; spot stream remains active")

            # Wait briefly for on_open before adding optional futures/options.
            if not self._ws_open_event.wait(timeout=30):
                log.warning("WebSocket did not open; instrument loader will not subscribe")
                return

            if self.futures_contract and self.ws:
                try:
                    self.ws.subscribe(
                        "mgo-index-futures",
                        3,
                        [{"exchangeType": 2, "tokens": [str(self.futures_contract["token"])]}],
                    )
                except Exception as exc:
                    log.warning("NIFTY futures subscription unavailable: %s", exc)

            with self.lock:
                atm = self.atm
            if atm is not None and self.ws:
                self._subscribe_atm(self.ws, atm)

        except Exception:
            log.exception("Background scrip-master load failed; spot stream remains independent")

    def _tick(self, ws, msg):
        try:
            if isinstance(msg, str):
                msg = json.loads(msg)

            if not isinstance(msg, dict):
                return

            token = str(
                msg.get("token")
                or msg.get("symbolToken")
                or ""
            )

            raw = msg.get(
                "last_traded_price",
                msg.get("ltp"),
            )

            if not token or raw is None:
                return

            ltp = float(raw) / 100
            stamp = datetime.now(
                timezone.utc
            ).isoformat(timespec="milliseconds")

            with self.lock:
                if self.candle_pipeline and self.futures_contract and token == str(self.futures_contract["token"]):
                    exchange_ts = msg.get("exchange_timestamp")
                    day_volume = msg.get("volume_trade_for_the_day")
                    if exchange_ts is not None and day_volume is not None:
                        self.candle_pipeline.ingest_tick(exchange_ts, raw, day_volume)
                    return

                if token == INDEX[self.index][0]:
                    self.spot = {
                        "token": token,
                        "ltp": ltp,
                        "received_at": stamp,
                    }

                    step = INDEX[self.index][2]
                    atm = int(round(ltp / step) * step)

                    if atm != self.atm:
                        self.atm = atm
                        self._subscribe_atm(ws, atm)

                    return

                contract = self.contracts.get(token)

                if not contract:
                    return

                key = (
                    f"{contract['strike']:.0f}"
                    f"{contract['type']}"
                )

                raw_oi = msg.get(
                    "open_interest",
                    msg.get("opnInterest"),
                )

                oi = float(raw_oi) if raw_oi is not None else None
                previous = self.oi_previous.get(token)

                if oi is not None and previous is not None:
                    self.oi_accel += oi - previous

                if oi is not None:
                    self.oi_previous[token] = oi

                self.options[key] = {
                    "symbol": contract["symbol"],
                    "strike": contract["strike"],
                    "option_type": contract["type"],
                    "ltp": ltp,
                    "open_interest": oi,
                    "iv": msg.get("implied_volatility", msg.get("iv")),
                    "expiry": contract.get("expiry"),
                    "received_at": stamp,
                }

        except Exception:
            log.exception("Failed to parse SmartAPI tick")

    def _subscribe_atm(self, ws, atm):
        step = INDEX[self.index][2]
        groups = {}

        for token, contract in self.contracts.items():
            if abs(contract["strike"] - atm) <= step + 0.001:
                groups.setdefault(
                    contract["exchange"], []
                ).append(token)

        for exchange, tokens in groups.items():
            try:
                ws.subscribe(
                    f"mgo-atm-{atm}",
                    3,
                    [{
                        "exchangeType": exchange,
                        "tokens": tokens,
                    }],
                )
            except Exception as exc:
                with self.lock:
                    self.error = (
                        f"Option subscription failed: "
                        f"{type(exc).__name__}"
                    )

    def _error(self, ws, error):
        with self.lock:
            self.status = "error"
            self.error = str(error)[:240]

    def _closed(self, ws, *args):
        with self.lock:
            if self.status != "error":
                self.status = "disconnected"
