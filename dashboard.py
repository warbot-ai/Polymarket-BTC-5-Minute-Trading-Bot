#!/usr/bin/env python3
"""
Polymarket BTC 5m Bot — Dashboard Server
Drop in bot dir alongside bot.py, then: python dashboard.py
Open: http://localhost:8080
"""
import asyncio, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
import httpx

BOT_DIR    = Path(__file__).parent
PAPER_FILE = BOT_DIR / "paper_trades_5m.json"
MARKET_INT = 300
WIN_START  = 210
WIN_END    = 255
TREND_UP   = 0.62
TREND_DOWN = 0.38

# ── Redis (optional) ──────────────────────────────────────────────────────────
try:
    import redis as _r
    _rc = _r.Redis(host='localhost', port=6379, db=2,
                   decode_responses=True, socket_connect_timeout=2)
    _rc.ping(); REDIS = _rc
except Exception: REDIS = None

# ── Bot process ───────────────────────────────────────────────────────────────
bot_proc  = None
LOG_BUF   = []   # [{ts, msg, cat}]
MAX_LOG   = 600

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mK]')
JUNK    = set('⠀⣿⣾⣶⣤⠿⣴⣧⣦⡄⣠⠸⠠⠋⠃⡀⢀⣸⠹⣻⣟⠻⣡⠁⠈⠉⠙⠚⠛')

# ── Structured live state ─────────────────────────────────────────────────────
STATE = {
    "mode":            "simulation",
    "test_mode":       False,
    "status":          "stopped",
    "markets_loaded":  0,
    "current_market":  "",
    "market_ends":     "",
    # Signal readings parsed from logs
    "signals": {},        # source → {direction, score, conf}
    "fused":   None,      # {direction, score, conf}
    # Trade window event
    "window_hit": None,   # {ts, price, bid, ask, trend_strong, market}
    # Decision
    "last_decision": None,  # {ts, direction, reason}
    # Context
    "ctx": {},            # deviation, momentum, volatility, spot, fear_greed
    # Risk
    "risk_block":  None,
    "liq_warn":    None,
    # Live order events
    "last_fill":    None,
    "last_denied":  None,
    "last_rejected": None,
}

SIG_NAMES = ["OrderBookImbalance","TickVelocity","PriceDivergence",
             "SpikeDetection","SentimentAnalysis","DeribitPCR"]

def _cat(msg):
    m = msg.lower()
    if '[dashboard]'    in m: return 'SYS'
    if re.search(r'error|failed|exception|traceback', m):            return 'ERROR'
    if re.search(r'warn|⚠',                          m):            return 'WARN'
    if re.search(r'\[sim\]|real order|order filled',  m):            return 'TRADE'
    if re.search(r'fused:|score=.*conf=',             msg):          return 'SIGNAL'
    if re.search(r'neutral|skip|dead zone|no signal|not enough', m): return 'SKIP'
    if 'trade window hit' in m:                                      return 'WINDOW'
    return 'INFO'

def _parse(msg):
    """Parse every known log pattern into STATE."""
    # Individual signal: [OrderBookImbalance] BULLISH: score=82.3, conf=0.74
    m = re.search(r'\[(\w+)\]\s+(BULLISH|BEARISH):\s+score=([\d.]+),\s+conf=([\d.]+)', msg, re.I)
    if m:
        STATE["signals"][m.group(1)] = {
            "direction": m.group(2).lower(), "score": float(m.group(3)), "conf": float(m.group(4))}

    # Fused: FUSED: BULLISH (score=76.6, conf=76.50%)
    m = re.search(r'FUSED:\s+(BULLISH|BEARISH)\s+\(score=([\d.]+),\s*conf=([\d.]+)', msg, re.I)
    if m:
        STATE["fused"] = {"direction": m.group(1).lower(),
                          "score": float(m.group(2)), "conf": float(m.group(3))/100}

    # Context: dev=+1.23%, mom=-0.45%, vol=0.0031
    m = re.search(r'dev=([\-+\d.]+)%.*?mom=([\-+\d.]+)%.*?vol=([\d.]+)', msg)
    if m:
        STATE["ctx"].update({"deviation": float(m.group(1)),
                             "momentum":  float(m.group(2)),
                             "volatility":float(m.group(3))})

    # Coinbase spot
    m = re.search(r'Coinbase spot.*?\$([\d,]+\.?\d*)', msg)
    if m: STATE["ctx"]["spot"] = float(m.group(1).replace(',',''))

    # Fear & Greed
    m = re.search(r'Fear.*?Greed:\s*([\d.]+)', msg, re.I)
    if m: STATE["ctx"]["fear_greed"] = float(m.group(1))

    # Trade window hit
    if 'TRADE WINDOW HIT' in msg:
        STATE["window_hit"] = {"ts": datetime.now(timezone.utc).isoformat()}
    m = re.search(r'Market:\s*(btc-updown-5m-\d+)', msg)
    if m and STATE["window_hit"]: STATE["window_hit"]["market"] = m.group(1)
    m = re.search(r'Price:\s*\$([\d,]+\.?\d*).*?Bid:\s*\$([\d,]+\.?\d*).*?Ask:\s*\$([\d,]+\.?\d*)', msg)
    if m and STATE["window_hit"]:
        STATE["window_hit"].update({
            "price": float(m.group(1).replace(',','')),
            "bid":   float(m.group(2).replace(',','')),
            "ask":   float(m.group(3).replace(',',''))})
    m = re.search(r'Trend:\s+(STRONG|WEAK)', msg)
    if m and STATE["window_hit"]: STATE["window_hit"]["trend_strong"] = (m.group(1)=="STRONG")

    # Trend decision
    m = re.search(r'TREND (UP|DOWN)\s*\(([\d.]+)%\)\s*→\s*(YES|NO)', msg)
    if m:
        STATE["last_decision"] = {"ts": datetime.now(timezone.utc).isoformat(),
                                   "direction": "LONG" if m.group(1)=="UP" else "SHORT",
                                   "price_pct": float(m.group(2)), "token": m.group(3)}
    if 'dead zone' in msg.lower():
        STATE["last_decision"] = {"ts": datetime.now(timezone.utc).isoformat(),
                                   "direction": "SKIP", "reason": "neutral dead zone"}

    # Risk / liquidity
    m = re.search(r'Risk blocked:\s*(.+)', msg)
    if m: STATE["risk_block"] = m.group(1).strip()
    if 'No ask liquidity' in msg or 'No bid liquidity' in msg:
        STATE["liq_warn"] = msg.strip()

    # Market / markets loaded
    m = re.search(r'Loading (\d+) BTC 5-min slugs', msg)
    if m: STATE["markets_loaded"] = int(m.group(1))
    m = re.search(r'CURRENT MARKET:\s*(btc-updown-5m-\d+)', msg)
    if m: STATE["current_market"] = m.group(1)
    m = re.search(r'Ends at:\s*(\d+:\d+:\d+)', msg)
    if m: STATE["market_ends"] = m.group(1)

    # Mode
    if 'TEST MODE ACTIVE' in msg:          STATE["mode"] = "test"; STATE["test_mode"] = True
    elif 'SIMULATION MODE' in msg:         STATE["mode"] = "simulation"
    elif 'LIVE TRADING' in msg and 'REAL' in msg: STATE["mode"] = "live"

    # Order events (live mode)
    m = re.search(r'ORDER FILLED:\s*(\S+)\s*@\s*\$([\d.]+)', msg)
    if m: STATE["last_fill"] = {"ts": datetime.now(timezone.utc).isoformat(),
                                 "order_id": m.group(1), "price": float(m.group(2))}
    m = re.search(r'ORDER DENIED:\s*(\S+)\s*—\s*(.+)', msg)
    if m: STATE["last_denied"] = {"ts": datetime.now(timezone.utc).isoformat(),
                                   "order_id": m.group(1), "reason": m.group(2)}
    m = re.search(r'Order rejected:\s*(.+)', msg)
    if m: STATE["last_rejected"] = {"ts": datetime.now(timezone.utc).isoformat(),
                                     "reason": m.group(1)}

def _clean(line):
    line = ANSI_RE.sub('', line).strip()
    if not line: return None
    if sum(1 for c in line if c in JUNK) > 2: return None
    return line

async def _tail():
    global bot_proc
    while True:
        if bot_proc and bot_proc.stdout:
            try:
                raw = bot_proc.stdout.readline()
                if raw:
                    msg = _clean(raw)
                    if msg:
                        cat = _cat(msg)
                        _parse(msg)
                        LOG_BUF.append({"ts": datetime.now(timezone.utc).isoformat(),
                                        "msg": msg, "cat": cat})
                        if len(LOG_BUF) > MAX_LOG: del LOG_BUF[:-MAX_LOG]
                    continue
            except Exception: pass
        await asyncio.sleep(0.04)

def _running():
    global bot_proc
    if bot_proc is None: return False
    if bot_proc.poll() is None: return True
    bot_proc = None; STATE["status"] = "stopped"; return False

def _start(mode, test=False):
    global bot_proc
    if _running(): return False, "already running"
    args = [sys.executable, str(BOT_DIR/"bot.py"), "--no-grafana"]
    if mode == "live": args.append("--live")
    if test:           args.append("--test-mode")
    bot_proc = subprocess.Popen(args, cwd=str(BOT_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace")
    STATE.update({"status":"running","mode":"test" if test else mode,
                  "test_mode":test,"signals":{},"fused":None,
                  "risk_block":None,"liq_warn":None,"last_decision":None})
    LOG_BUF.append({"ts":datetime.now(timezone.utc).isoformat(),
                    "msg":f"[DASHBOARD] Bot started — {'TEST MODE' if test else mode.upper()}",
                    "cat":"SYS"})
    return True, "ok"

def _stop():
    global bot_proc
    if bot_proc:
        bot_proc.terminate()
        try: bot_proc.wait(timeout=6)
        except subprocess.TimeoutExpired: bot_proc.kill()
        bot_proc = None
    STATE["status"] = "stopped"
    LOG_BUF.append({"ts":datetime.now(timezone.utc).isoformat(),
                    "msg":"[DASHBOARD] Bot stopped","cat":"SYS"})
    return True

def _getmode():
    if REDIS:
        try:
            v = REDIS.get("btc_trading:simulation_mode")
            if v is not None: return "live" if v=="0" else STATE.get("mode","simulation")
        except: pass
    return STATE.get("mode","simulation")

def _setmode(mode):
    STATE["mode"] = mode
    if REDIS:
        try: REDIS.set("btc_trading:simulation_mode","0" if mode=="live" else "1")
        except: pass
    LOG_BUF.append({"ts":datetime.now(timezone.utc).isoformat(),
                    "msg":f"[DASHBOARD] Mode → {mode.upper()}","cat":"SYS"})

# ── Market data ───────────────────────────────────────────────────────────────
_mc = {}; _mts = 0

async def _market():
    global _mc, _mts
    now  = datetime.now(timezone.utc)
    base = (int(now.timestamp()) // MARKET_INT) * MARKET_INT
    elapsed = now.timestamp() - base
    if time.time() - _mts < 3 and _mc:
        _mc.update({"elapsed":round(elapsed,1),
                    "in_window": WIN_START<=elapsed<=WIN_END})
        return _mc
    slug = f"btc-updown-5m-{base}"
    out  = dict(slug=slug, elapsed=round(elapsed,1),
                in_window=WIN_START<=elapsed<=WIN_END,
                end_time=datetime.fromtimestamp(base+MARKET_INT,tz=timezone.utc).strftime("%H:%M:%S UTC"),
                yes_price=None,no_price=None,trend="NEUTRAL",
                liquidity=0,question="",btc_price=None,queue=_queue(now,base))
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r=await c.get("https://gamma-api.polymarket.com/markets",params={"slug":slug})
            d=r.json()
            if d:
                m=d[0]; out["question"]=m.get("question","")
                out["liquidity"]=float(m.get("liquidityNum",0))
                ids=m.get("clobTokenIds",[])
                if isinstance(ids,str): ids=json.loads(ids)
                if ids:
                    mr=await c.get("https://clob.polymarket.com/midpoint",params={"token_id":ids[0]})
                    mid=float(mr.json().get("mid",0.5))
                    out["yes_price"]=round(mid,4); out["no_price"]=round(1-mid,4)
                    out["trend"]="UP" if mid>TREND_UP else "DOWN" if mid<TREND_DOWN else "NEUTRAL"
            sr=await c.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            out["btc_price"]=float(sr.json().get("price",0))
    except Exception: pass
    _mc=out; _mts=time.time()
    return out

def _queue(now,base):
    return [{"start":datetime.fromtimestamp(base+i*MARKET_INT,tz=timezone.utc).strftime("%H:%M"),
             "end":  datetime.fromtimestamp(base+(i+1)*MARKET_INT,tz=timezone.utc).strftime("%H:%M"),
             "active":i==0,"past":i<0}
            for i in range(-2,10)]

def _trades():
    try: raw=json.loads(PAPER_FILE.read_text()) if PAPER_FILE.exists() else []
    except: raw=[]
    wins=losses=0; pnl=0.0; run=0.0; peak=0.0; max_dd=0.0
    cur_s=best_s=0; wp=[]; lp=[]
    for t in raw:
        p=t.get("price",0.5) or 0.5; sz=t.get("size_usd",1)
        out=t.get("outcome","PENDING")
        if out=="WIN":
            g=sz*(1/p-1); pnl+=g; run+=g; wins+=1; cur_s+=1
            best_s=max(best_s,cur_s); wp.append(g)
        elif out=="LOSS":
            pnl-=sz; run-=sz; losses+=1; cur_s=0; lp.append(sz)
        else: cur_s=0
        if run>peak: peak=run
        dd=(peak-run)/peak if peak>0 else 0
        if dd>max_dd: max_dd=dd
    settled=wins+losses
    return raw, dict(
        total=len(raw),wins=wins,losses=losses,
        pending=len(raw)-settled,
        win_rate=round(wins/settled*100,1) if settled else 0,
        total_pnl=round(pnl,2),best_streak=best_s,
        max_drawdown=round(max_dd*100,1),
        avg_win=round(sum(wp)/len(wp),3) if wp else 0,
        avg_loss=round(sum(lp)/len(lp),3) if lp else 0,
        profit_factor=round(sum(wp)/sum(lp),2) if lp and sum(lp)>0 else None,
        roi_per_trade=round(pnl/len(raw)*100,2) if raw else 0)

# ── HTTP ──────────────────────────────────────────────────────────────────────
HTML = BOT_DIR/"dashboard.html"

def _j(d):
    b=json.dumps(d,default=str).encode()
    return (f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Access-Control-Allow-Origin:*\r\nContent-Length:{len(b)}\r\nConnection:close\r\n\r\n"
           ).encode()+b

async def _handle(reader,writer):
    try:
        raw=await asyncio.wait_for(reader.read(16384),timeout=10)
        txt=raw.decode("utf-8",errors="replace")
        fl=txt.split("\r\n")[0].split(" ")
        if len(fl)<2: return
        method,path=fl[0],fl[1].split("?")[0]
        body={}
        if "\r\n\r\n" in txt:
            chunk=txt.split("\r\n\r\n",1)[1].strip()
            if chunk:
                try: body=json.loads(chunk)
                except: pass

        if path in ("/","/index.html"):
            h=HTML.read_bytes()
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type:text/html\r\n"
                          f"Content-Length:{len(h)}\r\nConnection:close\r\n\r\n").encode()+h)

        elif path=="/api/status":
            writer.write(_j({"running":_running(),"mode":_getmode(),
                             "redis":REDIS is not None,"state":STATE}))
        elif path=="/api/market":
            writer.write(_j(await _market()))
        elif path=="/api/trades":
            t,s=_trades(); writer.write(_j({"trades":t[-200:],"stats":s}))
        elif path=="/api/logs":
            writer.write(_j({"logs":LOG_BUF[-300:]}))
        elif path=="/api/bot/start" and method=="POST":
            m=body.get("mode","simulation"); test=(m=="test")
            amode="live" if m=="live" else "simulation"
            ok,msg=_start(amode,test); writer.write(_j({"ok":ok,"msg":msg}))
        elif path=="/api/bot/stop" and method=="POST":
            writer.write(_j({"ok":_stop()}))
        elif path=="/api/mode" and method=="POST":
            _setmode(body.get("mode","simulation")); writer.write(_j({"ok":True}))
        elif path=="/api/clear_trades" and method=="POST":
            try: PAPER_FILE.write_text("[]")
            except: pass
            writer.write(_j({"ok":True}))
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length:9\r\nConnection:close\r\n\r\nNot Found")

        await writer.drain()
    except Exception: pass
    finally:
        try: writer.close()
        except: pass

async def _main():
    print("\n"+"═"*50)
    print("  ₿  Polymarket 5m Bot  ·  Dashboard")
    print("     http://localhost:8080")
    print(f"     Redis: {'✓ connected' if REDIS else '✗ not available'}")
    print("═"*50+"\n")
    srv=await asyncio.start_server(_handle,"0.0.0.0",8080)
    asyncio.create_task(_tail())
    async with srv: await srv.serve_forever()

if __name__=="__main__": asyncio.run(_main())
