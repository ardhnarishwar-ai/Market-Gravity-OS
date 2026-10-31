from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from math import isfinite
from typing import Any

TIMEFRAMES = (5, 15, 30, 60)
CANDLE_SECONDS = 300
IST = ZoneInfo("Asia/Kolkata")
MAX_5M_AGE_SECONDS = 600


def _number(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("Invalid candle timestamp")

    if dt.tzinfo is None:
        raise ValueError("Candle timestamp must include timezone")

    return dt.astimezone(timezone.utc)


def _safe_wait(reason):
    return {
        "status": "WAITING",
        "ready": False,
        "reason": reason,
        "bias": "NEUTRAL",
        "score_pct": 0,
        "delta_5m": None,
        "delta_15m": None,
        "delta_30m": None,
        "delta_60m": None,
        "absorption": "UNAVAILABLE",
        "trend_60m": "UNAVAILABLE",
        "macd_5m": "UNAVAILABLE",
        "atr_14": None,
        "targets": {"upper": None, "lower": None},
        "bars": {"5m": 0, "15m": 0, "30m": 0, "60m": 0},
    }


def normalize_candles(rows):
    result = []

    for row in rows or []:
        if not isinstance(row, dict):
            continue

        try:
            ts = _timestamp(
                row.get("timestamp", row.get("time"))
            )
        except (ValueError, TypeError, OverflowError):
            continue

        o = _number(row.get("open"))
        h = _number(row.get("high"))
        low = _number(row.get("low"))
        c = _number(row.get("close"))
        v = _number(row.get("volume"))

        if None in (o, h, low, c, v):
            continue

        if v < 0 or h < low or h < max(o, c) or low > min(o, c):
            continue

        epoch = int(ts.timestamp())
        if epoch % CANDLE_SECONDS != 0:
            continue

        result.append({
            "timestamp": ts,
            "open": o,
            "high": h,
            "low": low,
            "close": c,
            "volume": v,
        })

    result.sort(key=lambda x: x["timestamp"])

    deduped = []
    seen = set()

    for candle in result:
        key = candle["timestamp"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candle)

    return deduped


def _resample(candles, minutes):
    """Aggregate complete bars on the NSE 09:15 IST session grid."""
    count = minutes // 5
    buckets = {}
    for candle in candles:
        local = candle["timestamp"].astimezone(IST)
        if local.time() < time(9, 15) or local.time() >= time(15, 30):
            continue
        offset = local.hour * 60 + local.minute - 555
        start_local = datetime.combine(local.date(), time(9, 15), IST)
        start_local += timedelta(minutes=(offset // minutes) * minutes)
        start = start_local.astimezone(timezone.utc)
        buckets.setdefault(start, []).append(candle)
    output = []
    for start, group in sorted(buckets.items()):
        group.sort(key=lambda x: x["timestamp"])
        if len(group) != count:
            continue
        expected = [start + timedelta(seconds=i * CANDLE_SECONDS) for i in range(count)]
        if [x["timestamp"] for x in group] != expected:
            continue
        output.append({"timestamp": start, "open": group[0]["open"],
                       "high": max(x["high"] for x in group),
                       "low": min(x["low"] for x in group),
                       "close": group[-1]["close"],
                       "volume": sum(x["volume"] for x in group)})
    return output

def _delta(candle):
    spread = candle["high"] - candle["low"]

    if spread <= 0 or candle["volume"] <= 0:
        return 0.0

    buy_proxy = candle["volume"] * (
        (candle["close"] - candle["low"]) / spread
    )
    sell_proxy = candle["volume"] * (
        (candle["high"] - candle["close"]) / spread
    )

    return buy_proxy - sell_proxy


def _ema(values, period):
    if len(values) < period:
        return None

    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period

    for current in values[period:]:
        value = alpha * current + (1 - alpha) * value

    return value


def _atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    tr_values = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]

        tr_values.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))

    if len(tr_values) < period:
        return None

    value = sum(tr_values[:period]) / period

    for tr in tr_values[period:]:
        value = ((value * (period - 1)) + tr) / period

    return value


def _macd(values):
    if len(values) < 35:
        return None

    ema12 = _ema(values, 12)
    ema26 = _ema(values, 26)

    if ema12 is None or ema26 is None:
        return None

    fast = []
    alpha12 = 2 / 13
    alpha26 = 2 / 27

    f = sum(values[:12]) / 12
    s = sum(values[:26]) / 26

    for i, value in enumerate(values):
        if i >= 12:
            f = alpha12 * value + (1 - alpha12) * f
        if i >= 26:
            s = alpha26 * value + (1 - alpha26) * s

        if i >= 25:
            fast.append(f - s)

    signal = _ema(fast, 9)

    if signal is None:
        return None

    line = fast[-1]
    return {
        "line": line,
        "signal": signal,
        "histogram": line - signal,
        "direction": "BULLISH" if line > signal else "BEARISH",
    }


def calculate_mtf_delta(candles_5m, now=None, current_forming_candle=None):
    forming = None
    if isinstance(current_forming_candle, dict):
        try:
            fo = _number(current_forming_candle.get("open"))
            fh = _number(current_forming_candle.get("high"))
            fl = _number(current_forming_candle.get("low"))
            fc = _number(current_forming_candle.get("close"))
            fv = _number(current_forming_candle.get("volume"))
            fd = _number(current_forming_candle.get("running_delta"))
            if None not in (fo, fh, fl, fc, fv) and fv >= 0:
                spread = fh - fl
                if fd is None:
                    fd = (fv * ((fc - fl) / spread - (fh - fc) / spread) if spread > 0 and fv > 0 else 0.0)
                live_absorption = "TRAPPED_BUYERS_FORMING" if fc > fo and fd < 0 and fv > 0 else "NONE"
                forming = {"state": "FORMING", "delta": round(fd, 2), "absorption": live_absorption, "price_positive": fc > fo, "volume": fv, "timestamp": current_forming_candle.get("timestamp")}
        except (TypeError, ValueError):
            forming = None

    def waiting(reason):
        result = _safe_wait(reason)
        result.update({"delta_5m_running": forming.get("delta") if forming else None, "candle_5m_state": "FORMING" if forming else "CLOSED", "absorption_live": forming.get("absorption", "NONE") if forming else "NONE", "current_forming_candle": forming})
        return result

    candles = normalize_candles(candles_5m)
    if not candles:
        return waiting("No valid 5-minute OHLCV candles")

    now = now or datetime.now(timezone.utc)

    if now.tzinfo is None:
        return waiting("Current time must be timezone-aware")

    now = now.astimezone(timezone.utc)

    candles = [
        c for c in candles
        if c["timestamp"] + timedelta(minutes=5) <= now
    ]

    if not candles:
        return waiting("Waiting for a completed 5-minute candle")

    last_close = candles[-1]["timestamp"] + timedelta(minutes=5)

    if (now - last_close).total_seconds() > MAX_5M_AGE_SECONDS:
        return waiting("5-minute OHLCV feed is stale")

    for previous, current in zip(candles[-60:-1], candles[-59:]):
        a = previous["timestamp"].astimezone(IST)
        b = current["timestamp"].astimezone(IST)
        if a.date() == b.date() and a.time() < time(15, 30) and (current["timestamp"] - previous["timestamp"]).total_seconds() != 300:
            return waiting("Gap detected in recent 5-minute candles")

    bars15 = _resample(candles, 15)
    bars30 = _resample(candles, 30)
    bars60 = _resample(candles, 60)

    if len(candles) < 35:
        return waiting("Need at least 35 completed 5-minute candles")

    if len(bars15) < 3 or len(bars30) < 2:
        return waiting("Waiting for complete 15m and 30m bars")

    if len(bars60) < 200:
        return waiting("Waiting for 200 complete 60m bars for EMA200")

    def is_closed(bar, minutes):
        return bar["timestamp"] + timedelta(minutes=minutes) <= now

    bars15 = [b for b in bars15 if is_closed(b, 15)]
    bars30 = [b for b in bars30 if is_closed(b, 30)]
    bars60 = [b for b in bars60 if is_closed(b, 60)]

    if len(bars15) < 3 or len(bars30) < 2 or len(bars60) < 200:
        return waiting("Waiting for completed higher-timeframe bars")

    delta5 = _delta(candles[-1])
    delta15 = _delta(bars15[-1])
    delta30 = _delta(bars30[-1])
    delta60 = _delta(bars60[-1])

    last30 = bars30[-1]

    if last30["close"] > last30["open"] and delta30 < 0:
        absorption = "TRAPPED_BUYERS"
    elif last30["close"] < last30["open"] and delta30 > 0:
        absorption = "TRAPPED_SELLERS"
    else:
        absorption = "NONE"

    closes60 = [b["close"] for b in bars60]
    ema200 = _ema(closes60, 200)

    if ema200 is None:
        return waiting("60m EMA200 unavailable")

    trend = (
        "BULLISH" if bars60[-1]["close"] > ema200
        else "BEARISH"
    )

    closes5 = [b["close"] for b in candles]
    macd = _macd(closes5)
    atr = _atr(candles, 14)

    if macd is None or atr is None:
        return waiting("MACD or ATR history insufficient")

    deltas = [delta5, delta15, delta30, delta60]
    positive = sum(x > 0 for x in deltas)
    negative = sum(x < 0 for x in deltas)

    direction = "BULLISH" if positive >= 3 else (
        "BEARISH" if negative >= 3 else "NEUTRAL"
    )

    if direction == "NEUTRAL":
        return {
            **_safe_wait("Multi-timeframe delta is not aligned"),
            "status": "READY",
            "ready": True,
            "delta_5m_running": forming.get("delta") if forming else None,
            "candle_5m_state": "FORMING" if forming else "CLOSED",
            "absorption_live": forming.get("absorption", "NONE") if forming else "NONE",
            "current_forming_candle": forming,
            "delta_5m": round(delta5, 2),
            "delta_15m": round(delta15, 2),
            "delta_30m": round(delta30, 2),
            "delta_60m": round(delta60, 2),
            "absorption": absorption,
            "trend_60m": trend,
            "macd_5m": macd,
            "atr_14": round(atr, 2),
            "bars": {
                "5m": len(candles),
                "15m": len(bars15),
                "30m": len(bars30),
                "60m": len(bars60),
            },
        }

    aligned = (
        (direction == "BULLISH" and trend == "BULLISH"
         and macd["direction"] == "BULLISH")
        or
        (direction == "BEARISH" and trend == "BEARISH"
         and macd["direction"] == "BEARISH")
    )

    agreement = max(positive, negative) / 4
    score = 10 + agreement * 60

    if aligned:
        score *= 1.20

    if (
        direction == "BULLISH" and absorption == "TRAPPED_SELLERS"
    ) or (
        direction == "BEARISH" and absorption == "TRAPPED_BUYERS"
    ):
        score *= 1.15

    score = max(10, min(95, score))

    latest = candles[-1]

    return {
        "status": "READY",
        "ready": True,
        "delta_5m_running": forming.get("delta") if forming else None,
        "candle_5m_state": "FORMING" if forming else "CLOSED",
        "absorption_live": forming.get("absorption", "NONE") if forming else "NONE",
        "current_forming_candle": forming,
        "reason": None,
        "bias": direction,
        "score_pct": round(score, 1),
        "delta_5m": round(delta5, 2),
        "delta_15m": round(delta15, 2),
        "delta_30m": round(delta30, 2),
        "delta_60m": round(delta60, 2),
        "absorption": absorption,
        "trend_60m": trend,
        "ema200_60m": round(ema200, 2),
        "macd_5m": macd,
        "atr_14": round(atr, 2),
        "targets": {
            "upper": round(latest["high"] + atr, 2),
            "lower": round(latest["low"] - atr, 2),
        },
        "bars": {
            "5m": len(candles),
            "15m": len(bars15),
            "30m": len(bars30),
            "60m": len(bars60),
        },
        "method": "OHLCV_CLOSE_LOCATION_PROXY_NOT_TRUE_AGGRESSOR_DELTA",
    }
