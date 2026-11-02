"""NIFTY-only VWAP/option-chain expiry probability research engine.

Probabilities estimate expiry intrinsic-value outcomes, not guaranteed premium
decay or executable trading signals. Inputs must be timestamped and validated.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from math import erf, isfinite, log, sqrt
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _num(value):
    try:
        x = float(value)
        return x if isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _cdf(z):
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def _aware(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(IST)


def _expiry_close(expiry):
    dt = _aware(expiry)
    if dt is None:
        return None
    if dt.time() == time(0, 0):
        return datetime.combine(dt.date(), time(15, 30), IST)
    return dt


def _vwap_30m(candles):
    rows = []
    for row in candles or []:
        close = _num(row.get("close"))
        high = _num(row.get("high"))
        low = _num(row.get("low"))
        volume = _num(row.get("volume"))
        stamp = _aware(row.get("timestamp", row.get("time")))
        if None in (close, high, low, volume) or stamp is None or volume <= 0:
            continue
        typical = (high + low + close) / 3.0
        rows.append((stamp, typical, volume))
    rows.sort(key=lambda x: x[0])
    if not rows:
        return None
    cutoff = rows[-1][0] - timedelta(minutes=30)
    rows = [x for x in rows if x[0] > cutoff]
    total_volume = sum(x[2] for x in rows)
    if total_volume <= 0:
        return None
    return sum(price * volume for _, price, volume in rows) / total_volume


def _realized_bar_volatility(candles):
    closes = [_num(x.get("close")) for x in (candles or [])]
    closes = [x for x in closes if x is not None and x > 0]
    returns = [log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(returns) < 20:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
    return sqrt(variance)


def calculate_nifty_strike_expiry_probabilities(
    spot, candles_5m, options, expiry, now=None, iv_by_strike=None
):
    """Return strike-wise P(expiry intrinsic value == 0) for NIFTY CE/PE."""
    now = _aware(now) if now is not None else datetime.now(IST)
    expiry_at = _expiry_close(expiry)
    spot = _num(spot)
    if spot is None or spot <= 0:
        return {"status": "WAITING", "reason": "Valid NIFTY spot price required", "index": "NIFTY"}
    if expiry_at is None or expiry_at <= now:
        return {"status": "WAITING", "reason": "Valid future option expiry required", "index": "NIFTY"}

    vwap = _vwap_30m(candles_5m)
    if vwap is None:
        return {"status": "WAITING", "reason": "Insufficient timestamped 30-minute OHLCV for VWAP", "index": "NIFTY"}

    rows = list(candles_5m or [])
    closes = [_num(x.get("close")) for x in rows]
    closes = [x for x in closes if x is not None and x > 0]
    if len(closes) >= 12:
        recent = closes[-6:]
        prior = closes[-12:-6]
        slope = (sum(recent) / len(recent)) - (sum(prior) / len(prior))
    else:
        slope = 0.0

    # A deliberately bounded VWAP/slope drift adjustment; this is a research
    # prior, not a calibrated forecast. It cannot move the mean by >0.25%.
    displacement = (spot - vwap) / vwap
    drift_adjustment = max(-0.0025, min(0.0025, 0.35 * displacement + 0.15 * slope / spot))
    expected_expiry_spot = spot * (1.0 + drift_adjustment)

    minutes_to_expiry = max(0.0, (expiry_at - now).total_seconds() / 60.0)
    periods = max(minutes_to_expiry / 5.0, 1.0)
    realized_sigma_5m = _realized_bar_volatility(rows)
    iv_map = iv_by_strike or {}
    results = []
    missing_vol = 0

    for option in options or []:
        if not isinstance(option, dict):
            continue
        strike = _num(option.get("strike"))
        kind = str(option.get("option_type", option.get("type", ""))).upper()
        if strike is None or strike <= 0 or kind not in ("CE", "PE"):
            continue
        key = f"{strike:.2f}:{kind}"
        iv = _num(option.get("iv", iv_map.get(key)))
        if iv is not None and iv > 3:
            iv /= 100.0
        if iv is not None and not 0 < iv < 3:
            iv = None

        if iv is not None:
            sigma = iv * sqrt(max(minutes_to_expiry, 1.0) / (365.0 * 24.0 * 60.0))
            vol_source = "OPTION_IV"
        elif realized_sigma_5m is not None:
            sigma = realized_sigma_5m * sqrt(periods)
            vol_source = "REALIZED_5M_PROXY"
        else:
            missing_vol += 1
            results.append({
                "strike": strike, "option_type": kind,
                "ltp": _num(option.get("ltp")),
                "expiry_zero_intrinsic_probability_pct": None,
                "status": "WAITING_FOR_IV_OR_REALIZED_VOL",
                "volatility_source": "UNAVAILABLE",
            })
            continue

        sigma_price = max(expected_expiry_spot * sigma, 1e-9)
        z = (strike - expected_expiry_spot) / sigma_price
        probability = _cdf(z) if kind == "CE" else 1.0 - _cdf(z)
        moneyness = "OTM" if (kind == "CE" and expected_expiry_spot <= strike) or (kind == "PE" and expected_expiry_spot >= strike) else "ITM"
        results.append({
            "strike": strike,
            "option_type": kind,
            "ltp": _num(option.get("ltp")),
            "expiry_zero_intrinsic_probability_pct": round(probability * 100.0, 2),
            "expected_expiry_spot": round(expected_expiry_spot, 2),
            "moneyness_at_expected_price": moneyness,
            "volatility_source": vol_source,
            "status": "ESTIMATE",
        })

    return {
        "status": "ESTIMATE" if any(x.get("expiry_zero_intrinsic_probability_pct") is not None for x in results) else "WAITING",
        "index": "NIFTY",
        "model": "VWAP_DRIFT_BOUNDED_NORMAL_EXPIRY_DISTRIBUTION",
        "spot": round(spot, 2),
        "vwap_30m": round(vwap, 2),
        "spot_vwap_displacement_pct": round(displacement * 100.0, 4),
        "vwap_slope_points": round(slope, 4),
        "expected_expiry_spot": round(expected_expiry_spot, 2),
        "expiry": expiry_at.isoformat(),
        "minutes_to_expiry": round(minutes_to_expiry, 1),
        "volatility_source": "MIXED_OR_PER_STRIKE",
        "missing_volatility_count": missing_vol,
        "interpretation": "Probability of zero intrinsic value at expiry; not a guarantee that the option premium will be zero before expiry.",
        "calibration": "UNVALIDATED_RESEARCH_ESTIMATE",
        "strikes": results,
    }
