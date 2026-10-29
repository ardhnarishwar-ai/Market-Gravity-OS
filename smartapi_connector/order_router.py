"""
Order Router & Risk Guardian Engine for Market-Gravity-OS.
Handles Best-Bid Limit Chaser execution and Non-Negotiable Capital Circuit Breaker.
"""
from __future__ import annotations
import time
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any

logger = logging.getLogger("OrderRouter")

@dataclass
class CircuitBreakerConfig:
    max_daily_loss: float = -3000.0
    max_trades_per_day: int = 2
    max_consecutive_stopouts: int = 2
    risk_per_trade: float = 1500.0

class RiskGuardian:
    def __init__(self, config: Optional[CircuitBreakerConfig] = None):
        self.config = config or CircuitBreakerConfig()
        self.daily_realized_pnl: float = 0.0
        self.daily_trades_count: int = 0
        self.consecutive_stopouts: int = 0
        self.is_locked: bool = False
        self.lock_reason: Optional[str] = None

    def record_trade_result(self, pnl: float):
        self.daily_trades_count += 1
        self.daily_realized_pnl += pnl

        if pnl < 0:
            self.consecutive_stopouts += 1
        else:
            self.consecutive_stopouts = 0

        self.check_circuit_breaker()

    def check_circuit_breaker(self) -> bool:
        if self.daily_realized_pnl <= self.config.max_daily_loss:
            self.is_locked = True
            self.lock_reason = f"MAX_DAILY_LOSS_BREACHED ({self.daily_realized_pnl} <= {self.config.max_daily_loss})"
            logger.critical(f"CIRCUIT BREAKER TRIGGERED: {self.lock_reason}")
            return False

        if self.daily_trades_count >= self.config.max_trades_per_day:
            self.is_locked = True
            self.lock_reason = f"MAX_TRADES_EXCEEDED ({self.daily_trades_count}/{self.config.max_trades_per_day})"
            return False

        if self.consecutive_stopouts >= self.config.max_consecutive_stopouts:
            self.is_locked = True
            self.lock_reason = f"CONSECUTIVE_STOPOUTS_HIT ({self.consecutive_stopouts})"
            return False

        return True

    def can_trade(self) -> tuple[bool, str]:
        if self.is_locked:
            return False, f"LOCKED: {self.lock_reason}"
        if not self.check_circuit_breaker():
            return False, f"LOCKED: {self.lock_reason}"
        return True, "ARMED_AND_ACTIVE"


class BestBidLimitChaser:
    def __init__(self, smart_connect_instance=None):
        self.client = smart_connect_instance

    def calculate_chaser_price(self, best_bid: float, tick_size: float = 0.05) -> float:
        """Place limit at Best Bid + 1 tick to capture edge without crossing spread."""
        return round(best_bid + tick_size, 2)

    def route_order(
        self,
        symbol_token: str,
        trading_symbol: str,
        best_bid: float,
        quantity: int,
        transaction_type: str = "BUY",
        guardian: Optional[RiskGuardian] = None
    ) -> Dict[str, Any]:
        if guardian:
            allowed, reason = guardian.can_trade()
            if not allowed:
                logger.warning(f"Order rejected by Risk Guardian: {reason}")
                return {"status": "REJECTED_BY_CIRCUIT_BREAKER", "reason": reason}

        limit_price = self.calculate_chaser_price(best_bid)

        order_payload = {
            "variety": "NORMAL",
            "tradingsymbol": trading_symbol,
            "symboltoken": str(symbol_token),
            "transactiontype": transaction_type,
            "exchange": "NFO",
            "ordertype": "LIMIT",
            "producttype": "INTRADAY",
            "duration": "IOC",
            "price": str(limit_price),
            "quantity": str(quantity)
        }

        logger.info(f"Dispatching Best-Bid Chaser Order: {order_payload}")

        if not self.client:
            return {
                "status": "SIMULATED_FILLED",
                "price": limit_price,
                "payload": order_payload,
                "note": "Simulator execution pass; no live broker instance."
            }

        try:
            order_id = self.client.placeOrder(order_payload)
            return {"status": "SUBMITTED", "order_id": order_id, "limit_price": limit_price}
        except Exception as e:
            logger.error(f"API Execution Failure: {e}")
            return {"status": "EXECUTION_ERROR", "error": str(e)}

guardian = RiskGuardian()
chaser = BestBidLimitChaser()
