"""Optional Angel One SmartAPI data-only WebSocket adapter."""
from __future__ import annotations

import json
import logging
import os
import threading

from datetime import datetime, timezone

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

            response = requests.get(MASTER_URL, timeout=25)
            response.raise_for_status()
            master = response.json()

            if self.index == "NIFTY":
                self.futures_contract = resolve_active_nifty_future(master)
                history = fetch_historical_5m(smart, self.futures_contract["token"])
                self.candle_pipeline.seed_history(history)
                self.candle_pipeline.seed_today_volume_baseline(history)

            today = datetime.now().date()
            rows = []

            for row in master if isinstance(master, list) else []:
                if (
                    row.get("exch_seg") not in ("NFO", "BFO")
                    or row.get("instrumenttype") != "OPTIDX"
                    or row.get("name", "").upper() != self.index
                ):
                    continue

                try:
                    expiry = datetime.strptime(
                        row["expiry"], "%d%b%Y"
                    ).date()
                    strike = float(row["strike"]) / 100
                except (KeyError, TypeError, ValueError):
                    continue

                if expiry >= today:
                    rows.append({
                        **row,
                        "_expiry": expiry,
                        "_strike": strike,
                    })

            if not rows:
                raise RuntimeError(
                    "No current index options in instrument master"
                )

            expiry = min(x["_expiry"] for x in rows)

            self.contracts = {
                str(x["token"]): {
                    "symbol": str(x.get("symbol", "")),
                    "strike": x["_strike"],
                    "type": str(x.get("symbol", ""))[-2:],
                    "exchange": (
                        2 if x.get("exch_seg") == "NFO" else 4
                    ),
                }
                for x in rows
                if x["_expiry"] == expiry
            }

            token, exchange, _ = INDEX[self.index]

            self.ws = SmartWebSocketV2(
                jwt,
                creds["api_key"],
                creds["client_code"],
                feed,
            )

            futures_token = self.futures_contract["token"] if self.futures_contract else None
            self.ws.on_open = (
                lambda ws: self._opened(ws, token, exchange, futures_token)
            )
            self.ws.on_data = self._tick
            self.ws.on_error = self._error
            self.ws.on_close = self._closed

            with self.lock:
                self.status = "connecting"

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

        groups = [{"exchangeType": exchange, "tokens": [token]}]
        if futures_token:
            groups.append({"exchangeType": 2, "tokens": [str(futures_token)]})
        ws.subscribe("mgo-index-spot-and-futures", 3, groups)

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
