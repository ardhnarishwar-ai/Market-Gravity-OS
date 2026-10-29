"""
Angel One SmartAPI Instrument Master Downloader & Token Resolver.
Parses exact chronological expiries for NIFTY Spot, Futures, and Options.
"""
from __future__ import annotations
import json
import logging
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TokenMapper")

CACHE_DIR = Path.home() / "Market-Gravity-OS" / "data"
CACHE_FILE = CACHE_DIR / "scrip_master.json"
MAP_FILE = CACHE_DIR / "token_map.json"

def parse_expiry_date(d_str: str) -> datetime:
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d_str.strip().upper(), fmt)
        except ValueError:
            continue
    return datetime.max

def resolve_nifty_tokens(spot_reference: float = 24500.0) -> Dict[str, Any]:
    if not CACHE_FILE.exists():
        logger.error("Scrip master cache not found. Please download first.")
        return {}

    with open(CACHE_FILE, "r") as f:
        instruments = json.load(f)

    token_map = {"spot": {}, "futures": {}, "options": {}}

    # 1. Spot Token
    for item in instruments:
        if item.get("exch_seg") == "NSE" and item.get("symbol") in ("Nifty 50", "NIFTY"):
            token_map["spot"] = {
                "symbol": item.get("symbol"),
                "token": item.get("token"),
                "exch_seg": "NSE"
            }
            break
    if not token_map["spot"]:
        token_map["spot"] = {"symbol": "Nifty 50", "token": "99926000", "exch_seg": "NSE"}

    # 2. NFO Nifty contracts
    nfo_nifty = [
        item for item in instruments 
        if item.get("exch_seg") == "NFO" and item.get("name") == "NIFTY"
    ]

    now = datetime.now()
    unique_expiries = list(set(item.get("expiry", "") for item in nfo_nifty if item.get("expiry")))
    
    # Filter expiries >= today and sort chronologically
    valid_expiries = [exp for exp in unique_expiries if parse_expiry_date(exp) >= now]
    valid_expiries.sort(key=parse_expiry_date)

    current_expiry = valid_expiries[0] if valid_expiries else (unique_expiries[0] if unique_expiries else None)
    logger.info(f"Target Nearest Expiry: {current_expiry}")

    # 3. Match Futures & ATM Options
    atm_base = round(spot_reference / 50.0) * 50
    target_strikes = [float(atm_base + step * 50) for step in range(-5, 6)]

    for item in nfo_nifty:
        sym = item.get("symbol", "")
        # Futures (often marked with expiry in symbol or instrumenttype)
        if item.get("instrumenttype") in ("FUTIDX", "FUTIVX") or sym.endswith("-FUT") or ("FUT" in sym and not sym.endswith(("CE", "PE"))):
            if not token_map["futures"]:
                token_map["futures"] = {
                    "symbol": item.get("symbol"),
                    "token": item.get("token"),
                    "expiry": item.get("expiry"),
                    "lotsize": item.get("lotsize")
                }
        
        # Options
        if item.get("expiry") == current_expiry and item.get("instrumenttype") == "OPTIDX":
            try:
                raw_strike = float(item.get("strike", 0.0))
                strike_val = raw_strike / 100.0 if raw_strike > 100000 else raw_strike
                if strike_val in target_strikes:
                    opt_type = "CE" if sym.endswith("CE") else "PE"
                    key = f"{int(strike_val)}_{opt_type}"
                    token_map["options"][key] = {
                        "symbol": sym,
                        "token": item.get("token"),
                        "strike": strike_val,
                        "option_type": opt_type,
                        "expiry": item.get("expiry"),
                        "lotsize": item.get("lotsize")
                    }
            except (ValueError, TypeError):
                continue

    with open(MAP_FILE, "w") as f:
        json.dump(token_map, f, indent=2)

    logger.info(f"Mapped {len(token_map['options'])} ATM option strikes for expiry {current_expiry}.")
    return token_map

if __name__ == "__main__":
    res = resolve_nifty_tokens()
    print("\n=== UPDATED RESOLVED TOKENS ===")
    print(f"Spot: {res['spot']}")
    print(f"Nearest Futures: {res['futures']}")
    print(f"Nearest Expiry Active Options: {len(res['options'])} strikes mapped")
    for k in list(res['options'].keys())[:4]:
        print(f"  {k} -> Token {res['options'][k]['token']} ({res['options'][k]['symbol']})")
