"""CAS Indicative Equilibrium Price (IEP) matching engine.

Consumes aggregated buy/sell limit-order levels. This module does not parse
broker packets; callers must supply a validated order-book snapshot.
"""
from __future__ import annotations

from datetime import datetime, time
from math import isfinite
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _levels(rows):
    result = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("price"))
            quantity = float(row.get("quantity", row.get("qty")))
        except (TypeError, ValueError):
            continue
        if isfinite(price) and isfinite(quantity) and price > 0 and quantity > 0:
            result.append((price, quantity))
    return result


def calculate_cas_iep(bids, asks, reference_price, band_pct=0.03, tick_size=0.05):
    """Apply ordered CAS rules: max executable volume, min imbalance, proximity.

    At candidate P:
      demand(P) = sum buy quantity with limit >= P
      supply(P) = sum sell quantity with limit <= P
      executable(P) = min(demand(P), supply(P))
      imbalance(P) = abs(demand(P) - supply(P))
    """
    try:
        reference = float(reference_price)
        band = float(band_pct)
        tick = float(tick_size)
    except (TypeError, ValueError):
        return {"status": "UNAVAILABLE", "reason": "Invalid reference/band/tick size"}
    if not isfinite(reference) or reference <= 0 or not 0 < band < 1 or tick <= 0:
        return {"status": "UNAVAILABLE", "reason": "Invalid reference/band/tick size"}

    buy = _levels(bids)
    sell = _levels(asks)
    if not buy or not sell:
        return {"status": "UNAVAILABLE", "reason": "Order-book bid and ask levels are required"}

    lower, upper = reference * (1 - band), reference * (1 + band)
    candidates = {lower, upper, reference}
    candidates.update(price for price, _ in buy if lower <= price <= upper)
    candidates.update(price for price, _ in sell if lower <= price <= upper)

    # Add exchange-tick-aligned prices in the band so flat matching plateaus
    # resolve deterministically to the closest reference-price tick.
    first = int(lower / tick)
    last = int(upper / tick)
    if last - first <= 50000:
        candidates.update(i * tick for i in range(first, last + 1))

    evaluated = []
    for price in candidates:
        if price < lower - 1e-9 or price > upper + 1e-9:
            continue
        demand = sum(qty for limit, qty in buy if limit >= price)
        supply = sum(qty for limit, qty in sell if limit <= price)
        executable = min(demand, supply)
        imbalance = abs(demand - supply)
        evaluated.append({
            "price": price,
            "buy_quantity": demand,
            "sell_quantity": supply,
            "executable_volume": executable,
            "imbalance_quantity": imbalance,
            "unmatched_surplus": max(demand, supply) - executable,
        })

    if not evaluated:
        return {"status": "UNAVAILABLE", "reason": "No candidate prices in permitted band"}

    # Ordered tie-breaks: maximum matched shares, then minimum imbalance,
    # then minimum distance to Phase-1 VWAP reference. Final deterministic
    # tie: lower price, to avoid unstable results from set iteration.
    best = min(
        evaluated,
        key=lambda x: (
            -x["executable_volume"],
            x["imbalance_quantity"],
            abs(x["price"] - reference),
            x["price"],
        ),
    )
    return {
        "status": "CALCULATED",
        "indicative_equilibrium_price": round(best["price"] / tick) * tick,
        "reference_price": reference,
        "price_band": {"lower": round(lower, 4), "upper": round(upper, 4), "pct": band * 100},
        "maximum_executable_volume": round(best["executable_volume"], 2),
        "minimum_imbalance_quantity": round(best["imbalance_quantity"], 2),
        "buy_quantity": round(best["buy_quantity"], 2),
        "sell_quantity": round(best["sell_quantity"], 2),
        "tie_break_order": ["MAX_EXECUTABLE_VOLUME", "MIN_IMBALANCE_QUANTITY", "PROXIMITY_TO_REFERENCE"],
        "candidate_count": len(evaluated),
        "source": "AGGREGATED_ORDER_BOOK",
    }


def calculate_cas_iep_if_in_window(bids, asks, reference_price, now=None, band_pct=0.03, tick_size=0.05):
    now = now or datetime.now(IST)
    local = now.astimezone(IST)
    if not time(15, 20) <= local.time() < time(15, 30):
        return {
            "status": "OUTSIDE_WINDOW",
            "window_start": "15:20:00",
            "window_end_exclusive": "15:30:00",
            "indicative_equilibrium_price": None,
        }
    result = calculate_cas_iep(bids, asks, reference_price, band_pct, tick_size)
    result.update({"window_start": "15:20:00", "window_end_exclusive": "15:30:00"})
    return result
