"""
Dynamic Volatility & Adaptive Regime Module for Market-Gravity-OS.
Dynamically scales statistical thresholds based on rolling ATR and implied volatility regimes.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Optional

@dataclass
class RegimeState:
    regime_name: str
    vix_level: float
    atr_norm_ratio: float
    dynamic_z_threshold: float
    spread_tolerance_pts: float
    description: str

class DynamicRegimeEngine:
    def __init__(
        self,
        base_z_threshold: float = 4.20,
        baseline_atr: float = 120.0,
        baseline_vix: float = 14.5
    ):
        self.base_z_threshold = base_z_threshold
        self.baseline_atr = baseline_atr
        self.baseline_vix = baseline_vix

    def compute_threshold(
        self,
        current_vix: Optional[float] = None,
        rolling_5d_atr: Optional[float] = None
    ) -> RegimeState:
        vix = current_vix if (current_vix and current_vix > 0) else self.baseline_vix
        atr = rolling_5d_atr if (rolling_5d_atr and rolling_5d_atr > 0) else self.baseline_atr

        vix_ratio = vix / self.baseline_vix
        atr_ratio = atr / self.baseline_atr
        vol_factor = (0.6 * vix_ratio) + (0.4 * atr_ratio)

        if vol_factor < 0.88:
            regime = "COMPRESSED_LOW_VOL"
            z_thresh = round(max(3.0, self.base_z_threshold * math.sqrt(vol_factor)), 2)
            spread_tol = 2.5
            desc = "Range-bound intraday dynamics. Lower sigma triggers active."
        elif vol_factor > 1.25:
            regime = "EXPANDED_TRENDING"
            z_thresh = round(min(5.5, self.base_z_threshold * (vol_factor ** 0.85)), 2)
            spread_tol = 6.0
            desc = "High directional velocity. Strict threshold to prevent false counter-trend entries."
        else:
            regime = "EQUILIBRIUM"
            z_thresh = round(self.base_z_threshold, 2)
            spread_tol = 4.0
            desc = "Standard institutional parameters operational."

        return RegimeState(
            regime_name=regime,
            vix_level=round(vix, 2),
            atr_norm_ratio=round(vol_factor, 3),
            dynamic_z_threshold=z_thresh,
            spread_tolerance_pts=spread_tol,
            description=desc
        )

regime_engine = DynamicRegimeEngine()
