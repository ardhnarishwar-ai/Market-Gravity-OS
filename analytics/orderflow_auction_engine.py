"""Auction/order-flow research metrics for NIFTY futures.

True footprint/absorption requires classified trade prints or exchange depth.
OHLCV-derived volume-at-price and close-location delta are explicitly labelled
proxies and are never presented as true bid/ask footprint data.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _num(v):
    try:
        x = float(v)
        return x if isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _dt(v):
    try:
        if isinstance(v, datetime):
            d = v
        elif isinstance(v, (int, float)):
            d = datetime.fromtimestamp(v, IST)
        else:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            return None
        return d.astimezone(IST)
    except (TypeError, ValueError, OverflowError):
        return None


def _session_key(d):
    return d.date() if d and time(9, 15) <= d.time() < time(15, 30) else None


def _clean_candles(candles):
    out = []
    for row in candles or []:
        d = _dt(row.get("timestamp", row.get("time")))
        o, h, l, c, v = (_num(row.get(k)) for k in ("open", "high", "low", "close", "volume"))
        if d and _session_key(d) and None not in (o, h, l, c, v) and v >= 0 and h >= max(o, c, l) and l <= min(o, c):
            out.append({"dt": d, "open": o, "high": h, "low": l, "close": c, "volume": v})
    return sorted(out, key=lambda x: x["dt"])


def _volume_profile(candles, bin_size):
    bins = defaultdict(float)
    for c in candles:
        # OHLCV has no true volume-at-price. Distribute bar volume evenly over
        # price bins intersecting the range; use typical-price bin for flat bars.
        low = int(c["low"] / bin_size)
        high = int(c["high"] / bin_size)
        if high < low:
            continue
        count = high - low + 1
        allocation = c["volume"] / count if count else 0.0
        for idx in range(low, high + 1):
            bins[idx] += allocation
    if not bins:
        return {"status": "WAITING", "source": "UNAVAILABLE", "poc": None, "hvn": [], "lvn": [], "bins": []}
    values = list(bins.values())
    average = sum(values) / len(values)
    maximum = max(values)
    poc_idx = max(bins, key=bins.get)
    ordered = sorted(bins.items())
    return {
        "status": "PROXY",
        "source": "OHLCV_RANGE_DISTRIBUTION_PROXY_NOT_TRUE_VAP",
        "bin_size": bin_size,
        "poc": round((poc_idx + 0.5) * bin_size, 2),
        "poc_volume_proxy": round(bins[poc_idx], 2),
        "hvn": [{"price": round((i + 0.5) * bin_size, 2), "volume_proxy": round(v, 2)} for i, v in ordered if v >= max(average * 1.5, maximum * 0.65)][:12],
        "lvn": [{"price": round((i + 0.5) * bin_size, 2), "volume_proxy": round(v, 2)} for i, v in ordered if v <= average * 0.35][:20],
        "bins": [{"price": round((i + 0.5) * bin_size, 2), "volume_proxy": round(v, 2)} for i, v in ordered],
    }


def _stacked_imbalance(footprint, ratio=3.0, min_volume=1.0):
    if not isinstance(footprint, list) or not footprint:
        return {"status": "UNAVAILABLE", "source": "NO_CLASSIFIED_BID_ASK_FOOTPRINT", "buy_stacks": [], "sell_stacks": []}
    levels = []
    for row in footprint:
        price, bid, ask = (_num(row.get(k)) for k in ("price", "bid_volume", "ask_volume"))
        if None in (price, bid, ask) or bid < 0 or ask < 0:
            continue
        buy = ask >= max(bid * ratio, min_volume)
        sell = bid >= max(ask * ratio, min_volume)
        levels.append({"price": price, "buy": buy, "sell": sell, "bid": bid, "ask": ask})
    levels.sort(key=lambda x: x["price"])
    positive_gaps = [levels[i]["price"] - levels[i - 1]["price"] for i in range(1, len(levels)) if levels[i]["price"] > levels[i - 1]["price"]]
    tick_size = min(positive_gaps) if positive_gaps else 0.05
    def stacks(flag):
        result, current = [], []
        previous = None
        for level in levels:
            adjacent = previous is None or level["price"] - previous <= tick_size * 1.5
            if level[flag] and adjacent:
                current.append(level["price"])
            else:
                if len(current) >= 3:
                    result.append(current)
                current = [level["price"]] if level[flag] else []
            previous = level["price"]
        if len(current) >= 3:
            result.append(current)
        return result
    return {"status": "CALCULATED", "source": "CLASSIFIED_BID_ASK_VOLUME", "ratio": ratio, "tick_size_inferred": tick_size, "buy_stacks": stacks("buy"), "sell_stacks": stacks("sell")}


def _absorption(footprint):
    if not isinstance(footprint, list) or not footprint:
        return {"status": "UNAVAILABLE", "source": "NO_CLASSIFIED_TRADE_FLOW", "signal": "UNAVAILABLE"}
    # Requires price response and classified aggressive volume at the same level.
    rows = []
    for r in footprint:
        price, bid, ask, delta_price = (_num(r.get(k)) for k in ("price", "bid_volume", "ask_volume", "price_change"))
        if None not in (price, bid, ask, delta_price):
            rows.append((price, bid, ask, delta_price))
    if not rows:
        return {"status": "UNAVAILABLE", "source": "INCOMPLETE_FOOTPRINT", "signal": "UNAVAILABLE"}
    buy_absorb = sum(ask for _, _, ask, dp in rows if ask > 0 and dp <= 0)
    sell_absorb = sum(bid for _, bid, _, dp in rows if bid > 0 and dp >= 0)
    signal = "BUYER_ABSORPTION" if buy_absorb > sell_absorb * 1.5 else "SELLER_ABSORPTION" if sell_absorb > buy_absorb * 1.5 else "BALANCED"
    return {"status": "CALCULATED", "source": "CLASSIFIED_BID_ASK_WITH_PRICE_RESPONSE", "signal": signal, "footprint_delta": round(sum(ask - bid for _, bid, ask, _ in rows), 2), "aggressive_buy_absorbed": round(buy_absorb, 2), "aggressive_sell_absorbed": round(sell_absorb, 2)}


def calculate_auction_metrics(candles, footprint=None, now=None, bin_size=5.0):
    rows = _clean_candles(candles)
    now = _dt(now) if now is not None else datetime.now(IST)
    sessions = defaultdict(list)
    for c in rows:
        sessions[c["dt"].date()].append(c)
    dates = sorted(sessions)
    prior = sessions[dates[-2]] if len(dates) >= 2 else []
    current = sessions[dates[-1]] if dates else []
    prior_high = max((x["high"] for x in prior), default=None)
    prior_low = min((x["low"] for x in prior), default=None)
    halfback = (prior_high + prior_low) / 2 if prior_high is not None and prior_low is not None else None
    profile = _volume_profile(rows, max(float(bin_size), 0.05))
    lvn = profile.get("lvn", [])
    corridors = []
    for a, b in zip(lvn, lvn[1:]):
        if b["price"] - a["price"] <= bin_size * 1.6:
            corridors.append({"lower": a["price"] - bin_size / 2, "upper": b["price"] + bin_size / 2, "classification": "LVN_VACUUM_CANDIDATE"})
    footprint_metrics = _stacked_imbalance(footprint)
    return {
        "status": "PROXY" if rows else "WAITING",
        "instrument": "NIFTY_FUTURES",
        "overnight_halfback": {
            "status": "CALCULATED" if halfback is not None else "WAITING",
            "previous_session_date": dates[-2].isoformat() if len(dates) >= 2 else None,
            "previous_session_high": round(prior_high, 2) if prior_high is not None else None,
            "previous_session_low": round(prior_low, 2) if prior_low is not None else None,
            "midpoint": round(halfback, 2) if halfback is not None else None,
            "formula": "(PREVIOUS_SESSION_HIGH + PREVIOUS_SESSION_LOW) / 2",
        },
        "stacked_imbalance": footprint_metrics,
        "footprint_delta_absorption": _absorption(footprint),
        "continuous_volume_profile": profile,
        "lvn_vacuum_corridors": {"status": "PROXY" if corridors else "WAITING", "source": profile.get("source"), "corridors": corridors},
        "data_quality": {
            "candles": len(rows),
            "sessions": len(dates),
            "footprint_prints": len(footprint or []),
            "note": "OHLCV profile is a range-distribution proxy. True stacked imbalance and absorption require classified bid/ask trade volume or exchange depth.",
        },
    }
