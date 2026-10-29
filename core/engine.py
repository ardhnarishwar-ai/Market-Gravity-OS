"""Market-Gravity-OS: deterministic, testable seven-layer research prototype.

No broker integration or live order placement is implemented here.
Inputs must be timestamped and validated by a separate data adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Mapping, Sequence
import json
import math
import os


def mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def stddev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    return math.sqrt(sum((x - mu) ** 2 for x in values) / (len(values) - 1))


def z_score(value: float, history: Sequence[float]) -> float:
    """Sample z-score; returns 0 for insufficient/constant history."""
    sigma = stddev(history)
    return 0.0 if sigma <= 1e-12 else (value - mean(history)) / sigma


def percentile_rank(value: float, history: Sequence[float]) -> float:
    """Empirical percentile in [0, 100]; no extrapolation or distribution assumption."""
    if not history:
        return 50.0
    return 100.0 * sum(1 for x in history if x <= value) / len(history)


def dynamic_z_threshold(base: float, iv_percentile: float,
                        low_percentile: float = 20.0,
                        high_percentile: float = 80.0) -> float:
    """Volatility-regime threshold: relax slightly in low IV, tighten in high IV.

    This is a tunable research heuristic, not a statistically validated optimum.
    Clamp to prevent an extreme percentile from making the gate meaningless.
    """
    p = max(0.0, min(100.0, iv_percentile))
    if p <= low_percentile:
        return max(2.5, base - 0.7)
    if p >= high_percentile:
        return min(6.0, base + 0.8)
    return base


def load_weights(path: str | Path) -> dict[str, dict[str, float]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    result: dict[str, dict[str, float]] = {}
    for index in ("NIFTY", "SENSEX"):
        basket = payload.get(index, {})
        cleaned = {str(k): max(0.0, float(v)) for k, v in basket.items()
                   if isinstance(v, (int, float)) and math.isfinite(float(v))}
        total = sum(cleaned.values())
        result[index] = ({k: v / total for k, v in cleaned.items()}
                         if total > 0 else {})
    return result


def weighted_basket_flow(index: str, constituent_returns: Mapping[str, float],
                         weights: Mapping[str, Mapping[str, float]]) -> float:
    basket = weights.get(index, {})
    usable = {name: weight for name, weight in basket.items()
              if name in constituent_returns and math.isfinite(constituent_returns[name])}
    total = sum(usable.values())
    if total <= 0:
        return 0.0
    return sum(usable[name] * constituent_returns[name] for name in usable) / total


def cvd_velocity(cvd_series: Sequence[float]) -> float:
    """Change per observation across the supplied rolling window."""
    if len(cvd_series) < 2:
        return 0.0
    return (cvd_series[-1] - cvd_series[0]) / (len(cvd_series) - 1)


def oi_acceleration_weighted(strikes: Sequence[Mapping[str, float]],
                             atm_strike: float,
                             strike_step: float) -> float:
    """Weighted OI acceleration for ATM and adjacent strikes.

    Each row may include strike, oi_change_now, oi_change_prev. Moneyness weights
    are 1.0 at ATM and 0.5 at +/- one strike. Missing values are ignored.
    """
    if strike_step <= 0:
        return 0.0
    weighted, total = 0.0, 0.0
    for row in strikes:
        try:
            distance = abs(float(row["strike"]) - atm_strike) / strike_step
            if distance > 1.000001:
                continue
            weight = 1.0 if distance < 0.000001 else 0.5
            acceleration = float(row["oi_change_now"]) - float(row["oi_change_prev"])
            if not math.isfinite(acceleration):
                continue
            weighted += weight * acceleration
            total += weight
        except (KeyError, TypeError, ValueError):
            continue
    return weighted / total if total else 0.0


def spread_ticks(best_bid: float, best_ask: float, tick_size: float) -> float:
    if tick_size <= 0 or best_bid <= 0 or best_ask < best_bid:
        return math.inf
    return (best_ask - best_bid) / tick_size


def limit_chaser_plan(best_bid: float, best_ask: float, tick_size: float,
                      max_spread_ticks: float, offset_ticks: int = 1) -> dict[str, Any]:
    """Return a simulation plan only; never submits/cancels a real order."""
    width = spread_ticks(best_bid, best_ask, tick_size)
    if not math.isfinite(width) or width > max_spread_ticks:
        return {"allowed": False, "reason": "spread_or_quote_invalid", "limit_price": None}
    price = round(best_bid + max(0, offset_ticks) * tick_size, 8)
    if price > best_ask:
        return {"allowed": False, "reason": "would_cross_ask", "limit_price": None}
    return {"allowed": True, "reason": "simulation_only", "limit_price": price,
            "timeout_ms": 1500, "cancel_on_timeout": True}


@dataclass
class RiskGuardian:
    max_trades_per_day: int = 2
    max_risk_per_trade: float = 1500.0
    max_daily_drawdown: float = 3000.0
    max_consecutive_stopouts: int = 2
    trades_today: int = 0
    realized_pnl: float = 0.0
    consecutive_stopouts: int = 0
    locked: bool = False
    lock_reason: str | None = None
    state_path: str | None = None

    def __post_init__(self) -> None:
        self.load_state()

    def load_state(self) -> None:
        if not self.state_path:
            return
        try:
            data = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
            for key in ("trades_today", "realized_pnl", "consecutive_stopouts",
                        "locked", "lock_reason"):
                if key in data:
                    setattr(self, key, data[key])
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            # Fail closed if persistent state is corrupt.
            self.locked, self.lock_reason = True, "risk_state_unreadable"

    def save_state(self) -> None:
        if not self.state_path:
            return
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")
        os.replace(temp, path)

    def snapshot(self) -> dict[str, Any]:
        return {
            "trades_today": self.trades_today,
            "realized_pnl": self.realized_pnl,
            "consecutive_stopouts": self.consecutive_stopouts,
            "locked": self.locked,
            "lock_reason": self.lock_reason,
        }

    def trip(self, reason: str) -> None:
        self.locked, self.lock_reason = True, reason
        self.save_state()

    def allow_trade(self, proposed_risk: float) -> bool:
        if self.locked:
            return False
        if proposed_risk <= 0 or proposed_risk > self.max_risk_per_trade:
            return False
        if self.trades_today >= self.max_trades_per_day:
            self.trip("max_trades_per_day")
            return False
        if self.realized_pnl <= -abs(self.max_daily_drawdown):
            self.trip("max_daily_drawdown")
            return False
        if self.consecutive_stopouts >= self.max_consecutive_stopouts:
            self.trip("consecutive_stopouts")
            return False
        return True

    def record_trade(self, realized_pnl: float, was_stopout: bool) -> None:
        self.trades_today += 1
        self.realized_pnl += realized_pnl
        self.consecutive_stopouts = self.consecutive_stopouts + 1 if was_stopout else 0
        if self.realized_pnl <= -abs(self.max_daily_drawdown):
            self.trip("max_daily_drawdown")
        elif self.consecutive_stopouts >= self.max_consecutive_stopouts:
            self.trip("consecutive_stopouts")
        elif self.trades_today >= self.max_trades_per_day:
            self.trip("max_trades_per_day")
        else:
            self.save_state()


@dataclass
class EngineInput:
    stretch: float
    stretch_history: Sequence[float]
    iv_percentile: float
    basket_flow: float
    cvd_series: Sequence[float]
    downward_displacement_atr: float
    oi_acceleration: float
    net_gamma: float
    now_local: datetime
    is_expiry_day: bool = False
    data_fresh: bool = True
    news_lockout: bool = False


@dataclass
class SignalResult:
    allowed: bool
    score: float
    layers: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def evaluate_signal(data: EngineInput, settings: Mapping[str, Any]) -> SignalResult:
    """Evaluate the seven layers; returns research signal, never a live order."""
    market = settings.get("market", {})
    execution = settings.get("execution", {})
    layers: dict[str, Any] = {}
    reasons: list[str] = []

    threshold = dynamic_z_threshold(
        float(market.get("zscore_base_threshold", 4.2)),
        data.iv_percentile,
        float(market.get("iv_percentile_low", 20)),
        float(market.get("iv_percentile_high", 80)),
    )
    stretch_z = z_score(data.stretch, data.stretch_history)
    layers["1_dynamic_stretch"] = {
        "z_score": stretch_z, "threshold": threshold,
        "pass": abs(stretch_z) >= threshold,
    }

    layers["2_weighted_capital_flow"] = {
        "flow": data.basket_flow, "pass": data.basket_flow < 0,
    }
    velocity = cvd_velocity(data.cvd_series)
    min_displacement = float(market.get("minimum_downward_displacement_atr", 0.15))
    layers["3_cvd_displacement"] = {
        "cvd_velocity": velocity,
        "downward_displacement_atr": data.downward_displacement_atr,
        "pass": velocity < 0 and data.downward_displacement_atr >= min_displacement,
    }
    layers["4_moneyness_oi_acceleration"] = {
        "weighted_oi_acceleration": data.oi_acceleration,
        "pass": data.oi_acceleration > 0,
    }
    lockout = time.fromisoformat(str(market.get("expiry_fade_lockout_time", "13:30")))
    time_lockout = data.is_expiry_day and data.now_local.time() >= lockout
    gamma_pass = data.net_gamma < 0 and not time_lockout
    layers["5_gamma_regime"] = {
        "net_gamma": data.net_gamma, "expiry_time_lockout": time_lockout,
        "pass": gamma_pass,
    }

    freshness_pass = data.data_fresh and not data.news_lockout
    layers["6_execution_filter"] = {
        "enabled": bool(execution.get("enabled", False)),
        "data_fresh": data.data_fresh,
        "news_lockout": data.news_lockout,
        "pass": freshness_pass,
        "note": "Order routing is not implemented; use limit_chaser_plan for simulation.",
    }

    # A signal requires every core layer and fresh, non-event data.
    core_keys = ("1_dynamic_stretch", "2_weighted_capital_flow",
                 "3_cvd_displacement", "4_moneyness_oi_acceleration",
                 "5_gamma_regime")
    core_pass = all(layers[key]["pass"] for key in core_keys)
    allowed = core_pass and freshness_pass
    if not data.data_fresh:
        reasons.append("stale_market_data")
    if data.news_lockout:
        reasons.append("news_event_lockout")
    if time_lockout:
        reasons.append("expiry_fade_time_lockout")
    if not core_pass:
        reasons.append("signal_layers_not_aligned")

    layers["7_risk_guardian"] = {
        "configured_limits": settings.get("risk", {}),
        "note": "Call RiskGuardian.allow_trade() before any downstream execution adapter.",
    }
    score = sum(1 for key in core_keys if layers[key]["pass"]) / len(core_keys)
    return SignalResult(allowed=allowed, score=score, layers=layers, reasons=reasons)
