from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import logging
import math
import os
import sys
import time

from flask import Flask, jsonify, render_template

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

from core.engine import (
    EngineInput,
    evaluate_signal,
    load_weights,
    weighted_basket_flow,
)
from smartapi_connector.dynamic_regime import regime_engine
from smartapi_connector.live_feed import (
    AngelLiveFeed,
    credentials_present,
)
from analytics.mtf_delta_engine import calculate_mtf_delta

app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parent / "templates"),
)

IST = ZoneInfo("Asia/Kolkata")
WEIGHTS_PATH = ROOT / "config" / "weights.json"
WEIGHTS = load_weights(WEIGHTS_PATH)

LIVE_FEED = None

if credentials_present():
    try:
        LIVE_FEED = AngelLiveFeed(
            os.getenv("MGO_INDEX", "NIFTY").upper()
        )
        LIVE_FEED.start()
    except Exception:
        logging.getLogger(__name__).exception(
            "Live feed initialization failed"
        )

SETTINGS = {
    "market": {
        "zscore_base_threshold": 4.2,
        "iv_percentile_low": 20,
        "iv_percentile_high": 80,
        "minimum_downward_displacement_atr": 0.15,
        "expiry_fade_lockout_time": "13:30",
    },
    "execution": {
        "enabled": False,
        "mode": "simulation",
    },
    "risk": {
        "max_trades_per_day": 2,
        "max_risk_per_trade_inr": 1500,
        "max_daily_drawdown_inr": 3000,
        "max_consecutive_stopouts": 2,
    },
}


def _mtf_payload(snapshot):
    candles = (snapshot or {}).get("candles_5m") or []
    try:
        return calculate_mtf_delta(candles, current_forming_candle=(snapshot or {}).get("current_forming_candle"))
    except Exception:
        logging.getLogger(__name__).exception(
            "MTF calculation failed"
        )
        return {
            "status": "WAITING",
            "ready": False,
            "reason": "MTF calculation error; inspect server logs",
            "bias": "NEUTRAL",
            "score_pct": 0,
        }


def build_demo_telemetry():
    now_epoch = time.time()
    phase = now_epoch / 45.0

    regime_res = regime_engine.compute_threshold(
        current_vix=16.5 + 7.5 * math.sin(phase / 4.0),
        rolling_5d_atr=120.0 + 30.0 * math.cos(phase / 6.0),
    )

    target_z = 3.25 + 0.8 * math.sin(phase)
    iv_percentile = 50.0 + 38.0 * math.sin(phase / 3.0)

    history = [-3, -2, -1, 0, 1, 2, 3]
    mean = sum(history) / len(history)
    hist_sigma = (
        sum((v - mean) ** 2 for v in history)
        / (len(history) - 1)
    ) ** 0.5

    stretch = target_z * hist_sigma

    simulated_returns = {
        symbol: 0.0015 * math.sin(phase + offset * 0.47)
        for offset, symbol in enumerate((
            "RELIANCE", "HDFCBANK", "ICICIBANK",
            "INFY", "TCS", "BHARTIARTL", "SBIN"
        ))
    }

    basket_flow = weighted_basket_flow(
        "NIFTY", simulated_returns, WEIGHTS
    )

    cvd_base = -1000.0 * math.sin(phase)
    cvd_series = [
        cvd_base, cvd_base - 900, cvd_base - 1800
    ]

    oi_acceleration = 400 + 250 * math.sin(phase / 4)
    net_gamma = -1.0 if math.sin(phase / 5) < 0 else 1.0

    now_local = datetime.now(IST)

    data = EngineInput(
        stretch=stretch,
        stretch_history=history,
        iv_percentile=iv_percentile,
        basket_flow=basket_flow,
        cvd_series=cvd_series,
        downward_displacement_atr=0.22 + 0.08 * abs(math.sin(phase)),
        oi_acceleration=oi_acceleration,
        net_gamma=net_gamma,
        now_local=now_local,
        is_expiry_day=False,
        data_fresh=True,
        news_lockout=False,
    )

    demo_settings = dict(SETTINGS)
    demo_settings["market"] = dict(SETTINGS["market"])
    demo_settings["market"]["zscore_base_threshold"] = (
        regime_res.dynamic_z_threshold
    )

    signal = evaluate_signal(data, demo_settings)

    labels = [
        ("1_dynamic_stretch", "Dynamic ATR Z-score"),
        ("2_weighted_capital_flow", "Free-float weighted basket flow"),
        ("3_cvd_displacement", "CVD + downward displacement"),
        ("4_moneyness_oi_acceleration", "ATM ±1 OI acceleration"),
        ("5_gamma_regime", "Gamma regime / expiry lockout"),
        ("6_execution_filter", "Spread / limit-chaser filter"),
        ("7_risk_guardian", "Hard circuit breaker"),
    ]

    layers = []

    for key, label in labels:
        layer = signal.layers[key]

        if key == "6_execution_filter":
            status = "SIMULATION ONLY"
        elif key == "7_risk_guardian":
            status = "ARMED · DEMO"
        else:
            status = "PASS" if layer.get("pass") else "WAIT"

        layers.append({
            "id": key,
            "label": label,
            "status": status,
            "passed": bool(layer.get("pass", False)),
            "details": layer.get("note") or "",
        })

    z_layer = signal.layers["1_dynamic_stretch"]
    cvd_layer = signal.layers["3_cvd_displacement"]

    return {
        "mode": "SIMULATED",
        "live_data_connected": False,
        "execution_enabled": False,
        "timestamp": now_local.isoformat(timespec="seconds"),
        "index": "NIFTY · DEMO",
        "signal": {
            "allowed": signal.allowed,
            "score_pct": round(signal.score * 100, 1),
            "label": (
                "DEMO SIGNAL" if signal.allowed
                else "NO ALIGNED SIGNAL"
            ),
        },
        "metrics": {
            "z_score": round(z_layer["z_score"], 2),
            "dynamic_threshold": round(
                regime_res.dynamic_z_threshold, 2
            ),
            "regime_name": regime_res.regime_name,
            "iv_percentile": round(iv_percentile, 1),
            "basket_flow_pct": round(basket_flow * 100, 3),
            "cvd_velocity": round(cvd_layer["cvd_velocity"]),
            "net_gamma": (
                "NEGATIVE (DEMO)" if net_gamma < 0
                else "POSITIVE (DEMO)"
            ),
            "oi_acceleration": round(oi_acceleration),
        },
        "mtf_matrix": _mtf_payload({}),
        "risk": SETTINGS["risk"],
        "layers": layers,
        "reasons": signal.reasons,
        "warning": (
            "Synthetic demo values only. Not exchange ticks, "
            "a trading signal, or a live broker connection."
        ),
    }


def build_live_telemetry(snapshot):
    spot = snapshot.get("spot") or {}
    options = snapshot.get("options") or {}

    spot_price = spot.get("ltp")
    atm = snapshot.get("atm_strike")

    fresh = bool(
        snapshot.get("connected")
        and spot_price
        and spot.get("received_at")
    )

    regime_res = regime_engine.compute_threshold(
        current_vix=None,
        rolling_5d_atr=None,
    )

    engine_input = EngineInput(
        stretch=0.0,
        stretch_history=[],
        iv_percentile=50.0,
        basket_flow=0.0,
        cvd_series=[],
        downward_displacement_atr=0.0,
        oi_acceleration=float(
            snapshot.get("oi_acceleration") or 0.0
        ),
        net_gamma=0.0,
        now_local=datetime.now(IST),
        data_fresh=False,
        news_lockout=False,
        spot_price=(
            float(spot_price) if spot_price is not None else None
        ),
        atm_strike=float(atm) if atm is not None else None,
        atm_options=options,
    )

    signal = evaluate_signal(engine_input, SETTINGS)

    labels = [
        ("1_dynamic_stretch", "Dynamic ATR Z-score"),
        ("2_weighted_capital_flow", "Free-float weighted basket flow"),
        ("3_cvd_displacement", "CVD + downward displacement"),
        ("4_moneyness_oi_acceleration", "ATM ±1 OI acceleration"),
        ("5_gamma_regime", "Gamma regime / expiry lockout"),
        ("6_execution_filter", "Spread / limit-chaser filter"),
        ("7_risk_guardian", "Hard circuit breaker"),
    ]

    layers = []

    for key, label in labels:
        if key == "4_moneyness_oi_acceleration" and options:
            status = "LIVE TICKS"
            detail = (
                "Raw option ticks; OI delta is not a validated signal."
            )
        elif key == "6_execution_filter":
            status = "DISABLED"
            detail = "Order routing is disabled."
        else:
            status = "UNAVAILABLE"
            detail = (
                "Required validated input stream is not connected."
            )

        layers.append({
            "id": key,
            "label": label,
            "status": status,
            "passed": False,
            "details": detail,
        })

    return {
        "mode": "LIVE PARTIAL" if fresh else "LIVE CONNECTING",
        "live_data_connected": fresh,
        "execution_enabled": False,
        "timestamp": datetime.now(IST).isoformat(timespec="seconds"),
        "index": f"{snapshot.get('index', 'NIFTY')} · ANGEL ONE",
        "signal": {
            "allowed": False,
            "score_pct": 0,
            "label": "SIGNAL DISABLED · INCOMPLETE FEED",
        },
        "metrics": {
            "z_score": None,
            "dynamic_threshold": round(
                regime_res.dynamic_z_threshold, 2
            ),
            "iv_percentile": None,
            "basket_flow_pct": None,
            "cvd_velocity": None,
            "net_gamma": "UNAVAILABLE",
            "oi_acceleration": snapshot.get("oi_acceleration"),
            "spot": spot_price,
            "spot_received_at": spot.get("received_at"),
            "atm_strike": atm,
            "atm_options": options,
        },
        "mtf_matrix": _mtf_payload(snapshot),
        "chart": {
            "candles_5m": snapshot.get("candles_5m") or [],
            "futures_contract": snapshot.get("futures_contract"),
        },
        "risk": SETTINGS["risk"],
        "layers": layers,
        "reasons": [
            "live_feed_partial",
            "required_cvd_iv_gamma_and_constituent_flow_not_connected",
        ],
        "warning": (
            "Angel One spot/near-ATM option ticks only. "
            "MTF is unavailable until timestamped 5m OHLCV candles "
            "are connected. Signal generation and order routing "
            "remain disabled."
        ),
    }


@app.get("/")
def dashboard():
    return render_template("index.html")


@app.get("/api/telemetry")
def telemetry():
    snapshot = LIVE_FEED.snapshot() if LIVE_FEED else None

    if (
        snapshot
        and snapshot.get("status") in ("connected", "connecting")
        and (snapshot.get("spot") or snapshot.get("status") == "connecting")
    ):
        payload = build_live_telemetry(snapshot)
    else:
        payload = build_demo_telemetry()

        if snapshot and snapshot.get("status") == "error":
            payload["warning"] = (
                "Angel One feed unavailable; simulated fallback active."
            )

    payload["execution_enabled"] = False

    response = jsonify(payload)
    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )
    response.headers["Pragma"] = "no-cache"

    return response


@app.get("/health")
def health():
    snapshot = LIVE_FEED.snapshot() if LIVE_FEED else None

    return jsonify({
        "status": "ok",
        "mode": snapshot.get("status") if snapshot else "simulated",
        "live_data_connected": bool(
            snapshot and snapshot.get("connected")
        ),
        "execution_enabled": False,
    })


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=8000,
        debug=False,
        threaded=True,
    )
