"""
Angel One SmartAPI Live Order Placement Engine.
Integrates authenticated session with Risk Guardian & Best-Bid Limit Chaser.
"""
from __future__ import annotations
import os
from pathlib import Path
import pyotp
import logging
from typing import Optional, Dict, Any
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

from smartapi_connector.order_router import RiskGuardian, BestBidLimitChaser, guardian

logger = logging.getLogger("LiveOrderClient")

try:
    from SmartApi import SmartConnect
except ImportError:
    SmartConnect = None


class SmartApiLiveExecutor:
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.api_key = os.getenv("ANGEL_API_KEY")
        self.client_code = os.getenv("ANGEL_CLIENT_CODE")
        self.password = os.getenv("ANGEL_PIN")
        self.totp_token = os.getenv("ANGEL_TOTP_KEY") or os.getenv("ANGEL_TOTP_SECRET")
        self.smart_api: Optional[SmartConnect] = None
        self.refresh_token: Optional[str] = None
        self.is_authenticated: bool = False
        self.chaser = BestBidLimitChaser(None)
        self.balance: float = 0.0

    def authenticate(self) -> bool:
        if not SmartConnect:
            return False

        if not all([self.api_key, self.client_code, self.password, self.totp_token]):
            return False

        try:
            self.smart_api = SmartConnect(api_key=self.api_key)
            totp = pyotp.TOTP(self.totp_token).now()
            data = self.smart_api.generateSession(self.client_code, self.password, totp)

            if data and data.get("status"):
                self.is_authenticated = True
                self.refresh_token = data.get("data", {}).get("refreshToken")
                self.chaser.client = self.smart_api if not self.dry_run else None
                try:
                    rms = self.smart_api.rmsLimit()
                    self.balance = float(rms.get("data", {}).get("net", 0.0))
                except Exception:
                    pass
                return True
            return False
        except Exception as e:
            logger.error(f"Auth Exception: {e}")
            return False

    def get_status(self) -> Dict[str, Any]:
        return {
            "authenticated": self.is_authenticated,
            "dry_run": self.dry_run,
            "client_code": self.client_code if self.is_authenticated else None,
            "balance": self.balance,
            "mode": "PAPER_ROUTING" if self.dry_run else "LIVE_EXCHANGE"
        }

    def execute_limit_chaser(
        self,
        symbol_token: str,
        trading_symbol: str,
        best_bid: float,
        quantity: int,
        transaction_type: str = "BUY"
    ) -> Dict[str, Any]:
        if not self.is_authenticated and not self.dry_run:
            return {"status": "AUTH_REQUIRED", "error": "Session not active."}

        return self.chaser.route_order(
            symbol_token=symbol_token,
            trading_symbol=trading_symbol,
            best_bid=best_bid,
            quantity=quantity,
            transaction_type=transaction_type,
            guardian=guardian
        )

live_executor = SmartApiLiveExecutor(dry_run=True)
