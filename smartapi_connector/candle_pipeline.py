"""NIFTY futures 5-minute OHLCV pipeline for SmartAPI ticks."""
from __future__ import annotations
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo
import threading

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

def _utc(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, UTC)
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(UTC)

def resolve_active_nifty_future(master_rows, today=None):
    today = today or datetime.now(IST).date()
    found = []
    for row in master_rows if isinstance(master_rows, list) else []:
        if not isinstance(row, dict) or row.get("exch_seg") != "NFO" or row.get("instrumenttype") != "FUTIDX" or str(row.get("name", "")).upper() != "NIFTY":
            continue
        try:
            expiry = datetime.strptime(str(row["expiry"]), "%d%b%Y").date()
            token, symbol = str(row["token"]), str(row.get("symbol", ""))
        except (KeyError, TypeError, ValueError):
            continue
        if expiry >= today and token and symbol:
            found.append((expiry, token, symbol))
    if not found:
        raise RuntimeError("No unexpired NIFTY FUTIDX contract in instrument master")
    expiry, token, symbol = min(found)
    return {"name":"NIFTY","exchange":"NFO","exchange_type":2,"token":token,"symbol":symbol,"expiry":expiry.isoformat()}

def fetch_historical_5m(smart_api, token, days=95):
    end = datetime.now(IST)
    start = end - timedelta(days=min(max(int(days), 1), 95))
    response = smart_api.getCandleData({
        "exchange":"NFO","symboltoken":str(token),"interval":"FIVE_MINUTE",
        "fromdate":start.strftime("%Y-%m-%d %H:%M"),"todate":end.strftime("%Y-%m-%d %H:%M")
    })
    if not isinstance(response, dict) or not response.get("status"):
        message = response.get("message", "Historical candle request failed") if isinstance(response, dict) else "Invalid historical response"
        raise RuntimeError(message)
    now = datetime.now(UTC)
    by_time = {}
    for row in response.get("data") or []:
        try:
            ts = _utc(row[0])
            local = ts.astimezone(IST)
            if local.time() < time(9,15) or local.time() >= time(15,30) or ts + timedelta(minutes=5) > now:
                continue
            offset = local.hour*60 + local.minute - 555
            bucket = datetime.combine(local.date(), time(9,15), IST) + timedelta(minutes=(offset//5)*5)
            bucket = bucket.astimezone(UTC)
            if ts != bucket:
                continue
            o,h,l,c,v = map(float, row[1:6])
            if v < 0 or h < max(o,c,l) or l > min(o,c):
                continue
            by_time[bucket.isoformat()] = {"timestamp":bucket.isoformat(),"open":o,"high":h,"low":l,"close":c,"volume":v,"complete":True}
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    return [by_time[k] for k in sorted(by_time)]

class NiftyFuturesCandlePipeline:
    def __init__(self, max_bars=3000):
        self.max_bars=max_bars
        self._bars={}
        self._current=None
        self._last_cumulative=None
        self._today=None
        self._lock=threading.RLock()
        self.gap_detected=False
        self._cas_date=None
        self._cas_price_volume=0.0
        self._cas_volume=0.0
        self._cas_last_second=None
        self._cas_last_ltp=None

    def seed_history(self, rows):
        with self._lock:
            for row in rows or []:
                key=row.get("timestamp")
                if key:
                    self._bars[key]=dict(row)
            for key in sorted(self._bars)[:-self.max_bars]:
                self._bars.pop(key,None)

    def seed_today_volume_baseline(self, rows, today=None):
        today=today or datetime.now(IST).date()
        total=0.0
        found_today=False
        for row in rows or []:
            try:
                if _utc(row["timestamp"]).astimezone(IST).date()==today:
                    total += float(row["volume"])
                    found_today=True
            except (KeyError,TypeError,ValueError):
                continue
        with self._lock:
            self._today=today
            # If history has no current-session bars, the first live packet
            # establishes the cumulative-volume baseline instead of adding
            # the entire day's volume to its first candle.
            self._last_cumulative=total if found_today else None

    def ingest_tick(self, exchange_timestamp, ltp_paise, cumulative_day_volume):
        try:
            ts=_utc(exchange_timestamp)
            local=ts.astimezone(IST)
            if local.time()<time(9,15) or local.time()>=time(15,30):
                return
            offset=local.hour*60+local.minute-555
            start=datetime.combine(local.date(),time(9,15),IST)+timedelta(minutes=(offset//5)*5)
            key=start.astimezone(UTC).isoformat()
            price=float(ltp_paise)/100.0
            cumulative=float(cumulative_day_volume)
            if price<=0 or cumulative<0:
                return
        except (TypeError,ValueError,OverflowError):
            return
        with self._lock:
            if self._today != local.date():
                self._today=local.date()
                self._last_cumulative=None
                self._current=None
            if self._cas_date != local.date():
                self._cas_date=local.date()
                self._cas_price_volume=0.0
                self._cas_volume=0.0
                self._cas_last_second=None
                self._cas_last_ltp=None
            if self._last_cumulative is None:
                self._last_cumulative=cumulative
                volume=0.0
            else:
                volume=cumulative-self._last_cumulative
                self._last_cumulative=cumulative
                if volume<0:
                    volume=0.0
                    self.gap_detected=True
            self._cas_last_ltp = price

            # Capture the final pre-CAS second separately from the CAS VWAP.
            # Cumulative day volume deltas approximate traded volume between ticks.
            if time(14,59,59) <= local.time() < time(15,0):
                self._cas_last_second = {
                    "timestamp": ts.isoformat(),
                    "ltp": price,
                    "volume_delta": volume,
                    "running_delta": self._forming_delta(self._current),
                    "absorption": self._forming_absorption(self._current),
                }
                self._cas_last_ltp = price

            # CAS reference VWAP: sum(LTP * incremental traded volume) / sum(volume).
            if time(15,0) <= local.time() < time(15,15) and volume > 0:
                self._cas_price_volume += price * volume
                self._cas_volume += volume
                self._cas_last_ltp = price

            if self._current and self._current["timestamp"]!=key:
                self._current["complete"]=True
                self._bars[self._current["timestamp"]]=dict(self._current)
                self._current=None
            if self._current is None:
                self._current={"timestamp":key,"open":price,"high":price,"low":price,"close":price,"volume":volume,"complete":False}
            else:
                self._current["high"]=max(self._current["high"],price)
                self._current["low"]=min(self._current["low"],price)
                self._current["close"]=price
                self._current["volume"]+=volume
            for old in sorted(self._bars)[:-self.max_bars]:
                self._bars.pop(old,None)

    @staticmethod
    def _forming_delta(candle):
        if not candle:
            return None
        spread = candle["high"] - candle["low"]
        volume = candle["volume"]
        if spread <= 0 or volume <= 0:
            return 0.0
        return round(volume * ((candle["close"] - candle["low"]) / spread - (candle["high"] - candle["close"]) / spread), 2)

    @classmethod
    def _forming_absorption(cls, candle):
        if not candle:
            return "UNAVAILABLE"
        delta = cls._forming_delta(candle)
        return "TRAPPED_BUYERS_FORMING" if candle["close"] > candle["open"] and delta is not None and delta < 0 and candle["volume"] > 0 else "NONE"

    def cas_reference_snapshot(self, ltp=None, now=None):
        now = now or datetime.now(IST)
        local = now.astimezone(IST)
        with self._lock:
            traded_volume = self._cas_volume
            has_vwap = traded_volume > 0
            fallback = self._cas_last_ltp or ltp
            value = self._cas_price_volume / traded_volume if has_vwap else fallback
            return {
                "formula": "SUM(LTP_i * DELTA_VOLUME_i) / SUM(DELTA_VOLUME_i)",
                "window_start": "15:00:00",
                "window_end_exclusive": "15:15:00",
                "vwap_reference_price": round(value, 2) if value is not None else None,
                "traded_volume": round(traded_volume, 2),
                "source": "CAS_TICK_VWAP" if has_vwap else ("DAY_LTP_FALLBACK" if fallback is not None else "UNAVAILABLE"),
                "status": "FORMING" if local.time() < time(15,15) else "CLOSED",
                "last_second_tick": dict(self._cas_last_second) if self._cas_last_second else None,
            }

    def forming_snapshot(self):
        """Return a copy of the active 5m candle and its running OHLCV delta proxy."""
        with self._lock:
            if not self._current:
                return None
            candle = dict(self._current)
        spread = candle["high"] - candle["low"]
        volume = candle["volume"]
        delta = (
            volume * (
                (candle["close"] - candle["low"]) / spread
                - (candle["high"] - candle["close"]) / spread
            )
            if spread > 0 and volume > 0 else 0.0
        )
        candle["running_delta"] = round(delta, 2)
        candle["complete"] = False
        candle["state"] = "FORMING"
        candle["price_positive"] = candle["close"] > candle["open"]
        candle["absorption"] = (
            "TRAPPED_BUYERS_FORMING"
            if candle["price_positive"] and delta < 0 and volume > 0
            else "NONE"
        )
        return candle

    def snapshot(self):
        with self._lock:
            return [dict(self._bars[k]) for k in sorted(self._bars)][-self.max_bars:]
