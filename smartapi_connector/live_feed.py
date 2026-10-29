"""Optional Angel One SmartAPI data-only WebSocket adapter. No order routing."""
from __future__ import annotations
import json, logging, os, threading
from datetime import datetime, timezone
from typing import Any
log=logging.getLogger(__name__)
MASTER_URL="https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
INDEX={"NIFTY":("99926000",1,50),"SENSEX":("99919000",3,100)}
def credentials_present():
    return all(os.getenv(k,"").strip() for k in ("ANGEL_API_KEY","ANGEL_CLIENT_CODE","ANGEL_PIN","ANGEL_TOTP_KEY"))
class AngelLiveFeed:
    def __init__(self,index="NIFTY"):
        self.index=index.upper()
        if self.index not in INDEX: raise ValueError("MGO_INDEX must be NIFTY or SENSEX")
        self.lock=threading.RLock(); self.status="starting"; self.error=None; self.spot={}; self.options={}
        self.contracts={}; self.atm=None; self.oi_previous={}; self.oi_accel=0.0; self.ws=None; self.thread=None
    def start(self):
        if not self.thread or not self.thread.is_alive():
            self.thread=threading.Thread(target=self._run,daemon=True,name="angel-smartapi-feed"); self.thread.start()
    def snapshot(self):
        with self.lock:
            return {"index":self.index,"status":self.status,"connected":self.status=="connected" and bool(self.spot.get("ltp")),
                "spot":dict(self.spot),"options":{k:dict(v) for k,v in self.options.items()},"atm_strike":self.atm,
                "oi_acceleration":self.oi_accel,"error":self.error}
    def _run(self):
        try:
            import requests, pyotp
            from SmartApi import SmartConnect
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2
            api=os.environ["ANGEL_API_KEY"].strip(); client=os.environ["ANGEL_CLIENT_CODE"].strip()
            smart=SmartConnect(api_key=api)
            auth=smart.generateSession(client,os.environ["ANGEL_PIN"].strip(),pyotp.TOTP(os.environ["ANGEL_TOTP_KEY"].strip()).now())
            if not isinstance(auth,dict) or not auth.get("status"): raise RuntimeError("Angel One login failed; check credentials/TOTP")
            data=auth.get("data") or {}; jwt=data.get("jwtToken"); feed=data.get("feedToken") or smart.getfeedToken()
            if not jwt or not feed: raise RuntimeError("Login response lacks feed tokens")
            res=requests.get(MASTER_URL,timeout=25); res.raise_for_status(); master=res.json()
            today=datetime.now().date(); rows=[]
            source_rows = master if isinstance(master, list) else []
            for row in source_rows:
                if row.get("exch_seg") not in ("NFO","BFO") or row.get("instrumenttype")!="OPTIDX" or row.get("name","").upper()!=self.index: continue
                try: expiry=datetime.strptime(row["expiry"],"%d%b%Y").date(); strike=float(row["strike"])/100
                except (KeyError,TypeError,ValueError): continue
                if expiry>=today: rows.append({**row,"_expiry":expiry,"_strike":strike})
            if not rows: raise RuntimeError("No current index options found in instrument master")
            expiry=min(x["_expiry"] for x in rows)
            self.contracts={str(x["token"]):{"symbol":str(x.get("symbol","")),"strike":x["_strike"],
                "type":str(x.get("symbol",""))[-2:],"exchange":2 if x.get("exch_seg")=="NFO" else 4}
                for x in rows if x["_expiry"]==expiry}
            token,exchange,_=INDEX[self.index]
            self.ws=SmartWebSocketV2(jwt,api,client,feed)
            self.ws.on_open=lambda ws:self._opened(ws,token,exchange)
            self.ws.on_data=self._tick; self.ws.on_error=self._error; self.ws.on_close=self._closed
            with self.lock:self.status="connecting"
            self.ws.connect()
        except Exception as exc:
            log.exception("SmartAPI live feed startup failed")
            with self.lock:self.status="error"; self.error=f"{type(exc).__name__}: {exc}"
    def _opened(self,ws,token,exchange):
        with self.lock:self.status="connected";self.error=None
        ws.subscribe("mgo-index-spot",3,[{"exchangeType":exchange,"tokens":[token]}])
    def _tick(self,ws,msg):
        try:
            if isinstance(msg,str):msg=json.loads(msg)
            if not isinstance(msg,dict):return
            token=str(msg.get("token") or msg.get("symbolToken") or "")
            raw=msg.get("last_traded_price",msg.get("ltp"))
            if not token or raw is None:return
            ltp=float(raw)/100; stamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            with self.lock:
                if token==INDEX[self.index][0]:
                    self.spot={"token":token,"ltp":ltp,"received_at":stamp}
                    step=INDEX[self.index][2]; atm=int(round(ltp/step)*step)
                    if atm!=self.atm:self.atm=atm;self._subscribe_atm(ws,atm)
                    return
                c=self.contracts.get(token)
                if not c:return
                key=f"{c['strike']:.0f}{c['type']}"; rawoi=msg.get("open_interest",msg.get("opnInterest"))
                oi=float(rawoi) if rawoi is not None else None; prev=self.oi_previous.get(token)
                if oi is not None and prev is not None:self.oi_accel+=oi-prev
                if oi is not None:self.oi_previous[token]=oi
                self.options[key]={"symbol":c["symbol"],"strike":c["strike"],"option_type":c["type"],"ltp":ltp,"open_interest":oi,"received_at":stamp}
        except Exception:log.exception("Failed to parse SmartAPI tick")
    def _subscribe_atm(self,ws,atm):
        step=INDEX[self.index][2]; groups={}
        for token,c in self.contracts.items():
            if abs(c["strike"]-atm)<=step+0.001:groups.setdefault(c["exchange"],[]).append(token)
        for exchange,tokens in groups.items():
            try:ws.subscribe(f"mgo-atm-{atm}",3,[{"exchangeType":exchange,"tokens":tokens}])
            except Exception as exc:
                with self.lock:self.error=f"Option subscription failed: {type(exc).__name__}"
    def _error(self,ws,error):
        with self.lock:self.status="error";self.error=str(error)[:240]
    def _closed(self,ws,*args):
        with self.lock:
            if self.status!="error":self.status="disconnected"
