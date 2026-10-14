"""Exploratory real-OHLCV Solana historical PAPER trading, NOT live execution.

A fixed, explicitly different research entry rule creates hypothetical BUY/SELL
trades using historical GeckoTerminal pool candles. No orders, no wallet keys.
Current trending-pool selection has survivorship/selection bias. Bar data have
no historical pool-depth or order-routing proof. Never claim profitable live edge.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import time

# Capture only public HTTP JSON so a forward-independent researcher can rerun
# exactly the same hypothetical trades. No wallet keys or private data.
RAW_HTTP_SNAPSHOTS = {}

BASE = "https://api.geckoterminal.com/api/v2"
COST = {"trade_fee_pct": 0.003, "slippage_pct": 0.0075, "network_fee_usd_each_side": 0.25,
        "capital_usd": 100.0, "per_trade_usd": 20.0, "max_positions": 3,
        "maximum_previous_bar_volume_fraction": 0.002}
MIN_BARS = 85

def get_json(path, params=None, fetch=None):
    url = BASE + path + ("?" + urlencode(params) if params else "")
    if fetch is not None:
        result = fetch(url)
        if not isinstance(result,dict):
            raise ValueError("Unexpected public API payload")
        RAW_HTTP_SNAPSHOTS[url] = result
        return result
    if not url.startswith(BASE + "/"):
        raise ValueError("Only GeckoTerminal public read API is allowed")
    last = None
    for attempt in range(3):
        try:
            req = Request(url, headers={"Accept":"application/json;version=20230203",
                                        "User-Agent":"Godlike-Paper-Research/1.0"})
            with urlopen(req, timeout=20) as r:
                data = r.read(5_000_001)
                if len(data) > 5_000_000:
                    raise ValueError("Oversized market data")
                payload = json.loads(data)
                if not isinstance(payload,dict):
                    raise ValueError("Unexpected public API payload")
                RAW_HTTP_SNAPSHOTS[url] = payload
                return payload
        except (HTTPError,URLError,TimeoutError) as e:
            last=e
            if attempt < 2 and (not isinstance(e,HTTPError) or e.code in (429,500,502,503,504)):
                # GitHub runners share public IPs. 429 is a rate limit, not an
                # empty market. Never silently treat it as bad token data.
                retry = 0
                if isinstance(e,HTTPError) and e.code == 429:
                    try: retry = int(e.headers.get("Retry-After","0"))
                    except (TypeError,ValueError): retry = 0
                time.sleep(min(75, max(retry, 30 if isinstance(e,HTTPError) and e.code==429 else 3*(attempt+1))))
                continue
            break
    raise RuntimeError(f"Public OHLCV unavailable: {type(last).__name__}: {last}")

def finite_positive(v):
    try:
        x=float(v)
        return math.isfinite(x) and x>0
    except (TypeError,ValueError,OverflowError):
        return False

def discover_pools(fetch=None,max_pools=8):
    """Select today's active Solana pools once; NEVER label this historical unbiased."""
    response=get_json("/networks/solana/trending_pools",{"include":"base_token","page":1},fetch)
    items=response.get("data",[])
    if not isinstance(items,list): raise ValueError("Unexpected pool listing")
    selected,seen=[],set()
    for p in items:
        if not isinstance(p,dict): continue
        attr=p.get("attributes") or {}
        address=attr.get("address")
        if not address and isinstance(p.get("id"),str) and p["id"].startswith("solana_"):
            address=p["id"][7:]
        if not isinstance(address,str) or not (32<=len(address)<=60) or address in seen:
            continue
        reserve=attr.get("reserve_in_usd")
        volume=(attr.get("volume_usd") or {}).get("h24")
        if not (finite_positive(reserve) and finite_positive(volume)):continue
        if float(reserve)<150_000 or float(volume)<50_000:continue
        name=attr.get("name") if isinstance(attr.get("name"),str) else address[:10]
        selected.append({"pool":address,"name":name[:100],
                         "CURRENT_reserve_usd":str(reserve),"CURRENT_volume24_usd":str(volume)})
        seen.add(address)
        if len(selected)>=max_pools:break
    return selected

def parse_candles(payload):
    rows=payload.get("data",{}).get("attributes",{}).get("ohlcv_list",[])
    if not isinstance(rows,list):raise ValueError("Missing ohlcv_list")
    candles=[]
    for item in rows:
        if not isinstance(item,list) or len(item)<6:continue
        try:
            ts=int(item[0]);o,h,l,c,v=map(float,item[1:6])
        except (ValueError,TypeError,OverflowError):continue
        if (ts<=0 or not all(math.isfinite(x) for x in (o,h,l,c,v))
            or min(o,h,l,c)<=0 or v<0 or l>min(o,c) or h<max(o,c) or l>h):
            continue
        candles.append({"ts":ts,"o":o,"h":h,"l":l,"c":c,"v":v})
    candles.sort(key=lambda x:x["ts"])
    if any(b["ts"]<=a["ts"] for a,b in zip(candles,candles[1:])):
        raise ValueError("Duplicate/nonmonotonic bars")
    return candles

def next_open_entry(asset,idx):
    """Signal only uses CLOSED bars up to idx-1; entry is at bar idx open."""
    bars=asset["bars"]
    if idx<26 or idx>=len(bars):return False
    recent=bars[idx-1]
    if bars[idx]["ts"]-recent["ts"] != 3600:return False
    if any(bars[j+1]["ts"]-bars[j]["ts"] != 3600 for j in range(idx-26,idx-1)):
        return False
    close=recent["c"]
    six=bars[idx-7]["c"]
    momentum=close/six-1
    sma20=sum(x["c"] for x in bars[idx-20:idx])/20
    vol6=sum(x["v"] for x in bars[idx-6:idx])
    # Prespecified demonstration rule; NOT the existing runtime meme scanner.
    return 0.01<=momentum<=0.14 and close>sma20 and vol6>=10_000 and recent["v"]>=1000

def simulate_exit(gross,qty,cost=COST):
    """Conservative bar-level gross sale then two fixed modeled exit costs."""
    return max(0.0,gross*qty*(1-cost["trade_fee_pct"])*(1-cost["slippage_pct"])
               -cost["network_fee_usd_each_side"])

def run_strategy(pools, *, start=None,end=None,cost=COST):
    """One $100 shared PAPER wallet; at most 3 concurrent $20 positions."""
    if not pools: raise ValueError("No actual market histories")
    for asset in pools:
        if "pool" not in asset or "bars" not in asset:
            raise ValueError("Pool history missing")
    bars_at={}
    index_of={}
    all_times=set()
    for asset in pools:
        per={b["ts"]:i for i,b in enumerate(asset["bars"])}
        index_of[asset["pool"]]=per
        all_times.update(per)
    times=sorted(t for t in all_times if (start is None or t>=start) and (end is None or t<end))
    if len(times)<20:raise ValueError("Insufficient timestamps")
    cash=float(cost["capital_usd"])
    open_positions={}
    trades=[]
    max_drawdown=0.0
    peak=cash
    observed_opportunities=0
    for ts in times:
        # Settle old trades using the NEXT observed bar's OHLC.
        for asset in pools:
            pool=asset["pool"]
            if pool not in open_positions: continue
            idx=index_of[pool].get(ts)
            if idx is None or idx<=open_positions[pool]["entry_idx"]:continue
            bar=asset["bars"][idx]
            pos=open_positions[pool]
            held_hours=(ts-pos["entry_ts"])/3600
            stop=pos["entry_price"]*.86
            take=pos["entry_price"]*1.30
            # Gaps worse than stop are honored; when stop and TP both hit,
            # stops take precedence. TP cannot fill above its threshold.
            if bar["o"]<=stop:exit_price=bar["o"];reason="STOP_GAP"
            elif bar["l"]<=stop:exit_price=stop;reason="STOP"
            elif bar["o"]>=take:exit_price=take;reason="TAKE_PROFIT"
            elif bar["h"]>=take:exit_price=take;reason="TAKE_PROFIT"
            elif held_hours>=24:exit_price=bar["o"];reason="MAX_HOLD"
            else:continue
            received=simulate_exit(exit_price,pos["quantity"],cost)
            cash+=received
            trades.append({"pool":pool,"name":pos["name"],"entry_ts":pos["entry_ts"],
                           "exit_ts":ts,"entry_usd":pos["spent"],"exit_usd":round(received,6),
                           "net_pnl_usd":round(received-pos["spent"],6),
                           "reason":reason,"hours_held":round(held_hours,2)})
            del open_positions[pool]

        # Entry at OPEN of the new bar; ALL signals depend on prior closed bars.
        new_entry_pools=[]
        if cash>=cost["per_trade_usd"] and len(open_positions)<cost["max_positions"]:
            eligible=[]
            for asset in pools:
                pool=asset["pool"]
                if pool in open_positions:continue
                idx=index_of[pool].get(ts)
                if idx is None or not next_open_entry(asset,idx):continue
                observed_opportunities+=1
                prior=asset["bars"][idx-1]
                if cost["per_trade_usd"]>prior["v"]*cost["maximum_previous_bar_volume_fraction"]:
                    continue
                eligible.append((asset["bars"][idx-1]["v"],pool,asset,idx))
            for _,pool,asset,idx in sorted(eligible,key=lambda q:(-q[0],q[1])):
                if cash<cost["per_trade_usd"] or len(open_positions)>=cost["max_positions"]:break
                bar=asset["bars"][idx]
                spent=cost["per_trade_usd"]
                # Fee and slippage are taken before calculating acquired quantity.
                qty=(spent-cost["network_fee_usd_each_side"])*(1-cost["trade_fee_pct"])*(1-cost["slippage_pct"])/bar["o"]
                if qty<=0:continue
                cash-=spent
                open_positions[pool]={"quantity":qty,"spent":spent,"entry_price":bar["o"],
                                      "entry_ts":ts,"entry_idx":idx,"name":asset["name"]}
                new_entry_pools.append((pool,bar))
        # Entries occur at the opening print. A same-hour low can then hit
        # the stop BEFORE the next hour. Never pretend the position survived
        # that bar. Process all OPEN buys first: intrabar exits may not fund
        # additional entries at the same historical opening price.
        for pool,bar in new_entry_pools:
            position=open_positions.get(pool)
            if position is None:continue
            stop=position["entry_price"]*.86
            take=position["entry_price"]*1.30
            if bar["l"]<=stop:
                px,why=stop,"ENTRY_BAR_STOP"
            elif bar["h"]>=take:
                px,why=take,"ENTRY_BAR_TAKE_PROFIT"
            else:
                continue
            received=simulate_exit(px,position["quantity"],cost)
            cash+=received
            trades.append({"pool":pool,"name":position["name"],"entry_ts":ts,
                           "exit_ts":ts,"entry_usd":position["spent"],"exit_usd":round(received,6),
                           "net_pnl_usd":round(received-position["spent"],6),
                           "reason":why,"hours_held":0.0})
            del open_positions[pool]
        liquidation=cash
        for pool,pos in open_positions.items():
            asset=next(a for a in pools if a["pool"]==pool)
            idx=index_of[pool].get(ts)
            if idx is not None:
                price=asset["bars"][idx]["o"]
            else:
                prior=[i for t,i in index_of[pool].items() if t<ts]
                price=asset["bars"][max(prior)]["c"] if prior else pos["entry_price"]
            liquidation+=simulate_exit(price,pos["quantity"],cost)
        peak=max(peak,liquidation)
        if peak>0:max_drawdown=max(max_drawdown, (peak-liquidation)/peak*100)

    # Do not fabricate a fill at an unavailable price. Open positions are
    # transparently marked to conservative liquidation, NOT counted as closed.
    remaining_value=cash
    for pool,pos in open_positions.items():
        asset=next(a for a in pools if a["pool"]==pool)
        valid=[b for b in asset["bars"] if b["ts"]<=times[-1]]
        mark=valid[-1]["c"] if valid else pos["entry_price"]
        remaining_value+=simulate_exit(mark,pos["quantity"],cost)
    pnl=remaining_value-cost["capital_usd"]
    closed_pnl=sum(t["net_pnl_usd"] for t in trades)
    return {"start":datetime.fromtimestamp(times[0],timezone.utc).isoformat(),
            "end":datetime.fromtimestamp(times[-1],timezone.utc).isoformat(),
            "observed_bars":len(times),"entry_signals":observed_opportunities,
            "closed_trades":len(trades),"open_positions":len(open_positions),
            "winning_closed":sum(t["net_pnl_usd"]>0 for t in trades),
            "closed_net_pnl_usd":round(closed_pnl,4),
            "end_equity_liquidation_model_usd":round(remaining_value,4),
            "total_net_pnl_usd":round(pnl,4),
            "net_return_pct":round(pnl/cost["capital_usd"]*100,4),
            "max_drawdown_pct":round(max_drawdown,4),
            "trades":trades}

def conduct_research(fetch=None,max_pools=8,bar_limit=500):
    meta=discover_pools(fetch,max_pools=max_pools)
    pools=[];failures=[]
    for position,m in enumerate(meta):
        # The free public feed has variable IP-based quotas. Space requests to
        # avoid dropping most of the universe on an HTTP 429 response.
        if fetch is None and position:
            time.sleep(9)
        try:
            raw=get_json("/networks/solana/pools/"+m["pool"]+"/ohlcv/hour",
                         {"aggregate":1,"limit":bar_limit,"currency":"usd","token":"base"},fetch)
            candles=parse_candles(raw)
            if len(candles)<MIN_BARS:
                failures.append({"pool":m["pool"],"reason":"insufficient historical bars"})
                continue
            pools.append(dict(m,bars=candles))
        except (ValueError,RuntimeError,KeyError) as exc:
            failures.append({"pool":m["pool"],"reason":f"{type(exc).__name__}: {exc}"[:180]})
    if not pools:
        raise RuntimeError("NO VALID HISTORICAL POOL DATA; no trade results can be claimed")
    # Chronological 60%/40% split over union of timestamps; rule is fixed
    # BEFORE both segments. Selection by CURRENT trending pools remains biased.
    timestamps=sorted(set(b["ts"] for p in pools for b in p["bars"]))
    if len(timestamps)<100:raise ValueError("Insufficient historical sample")
    cut=timestamps[int(len(timestamps)*.60)]
    insample=run_strategy(pools,end=cut)
    holdout=run_strategy(pools,start=cut)
    return {"status":"EXPLORATORY_NOT_LIVE_VALIDATED","live_approved":False,
            "profitability_validated":False,
            "data_source":"GeckoTerminal public onchain historical pool OHLCV (Solana)",
            "data_fetched_at_utc":datetime.now(timezone.utc).isoformat(),
            "source_universe":"CURRENT trending pools, NOT unbiased historical launch universe",
            "cost_model":COST,
            "independent_live_fills_verified":False,
            "assumptions_and_limitations":[
               "Current trending pool selection introduces survivorship/selection bias.",
               "Historical pool depth, buy/sell routes and transaction failures are unknown.",
               "Candle highs and lows have no intrabar ordering; conservative stop-first rule used.",
               "Fills at next bar open, fixed fees and slippage are approximations, not executions.",
               "No live capital, no live order API, no statistical evidence of generalizable alpha."],
            "selected_pools":[{k:v for k,v in p.items() if k!="bars"} |
                              {"history_bars":len(p["bars"]),
                               "ohlcv_sha256":hashlib.sha256(json.dumps(p["bars"],sort_keys=True).encode()).hexdigest()}
                              for p in pools],
            "unusable_pools":failures,
            "training":insample,"unmodified_holdout":holdout}

def main():
    p=argparse.ArgumentParser(description="Exploratory historical onchain OHLCV trading simulator; NEVER live.")
    p.add_argument("--max-pools",type=int,default=8)
    p.add_argument("--bars",type=int,default=500)
    p.add_argument("--output",default="historical_ohlcv_report.json")
    p.add_argument("--source-output",default="historical_ohlcv_source.json")
    p.add_argument("--source-input",help="Offline replay captured public JSON instead of refetching")
    args=p.parse_args()
    if not 1<=args.max_pools<=15 or not 100<=args.bars<=1000:
        p.error("max-pools 1..15; bars 100..1000")
    from pathlib import Path
    fetch=None
    if args.source_input:
        archived=json.loads(Path(args.source_input).read_text(encoding="utf-8"))
        if not isinstance(archived,dict) or not isinstance(archived.get("http_responses"),dict):
            p.error("Invalid offline public API archive")
        def fetch(url):
            if url not in archived["http_responses"]:
                raise ValueError(f"No archived public response for {url}")
            return archived["http_responses"][url]
    report=conduct_research(fetch=fetch,max_pools=args.max_pools,bar_limit=args.bars)
    if not args.source_input:
        Path(args.source_output).write_text(json.dumps({
            "source":"GeckoTerminal public read-only API",
            "fetched_at":report["data_fetched_at_utc"],
            "http_responses":RAW_HTTP_SNAPSHOTS},sort_keys=True,allow_nan=False),encoding="utf-8")
    Path(args.output).write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    for name in ("training","unmodified_holdout"):
        r=report[name]
        print(f"{name}: {r['closed_trades']} closed trades, {r['winning_closed']} winners, "
              f"NET $ {r['total_net_pnl_usd']}, return {r['net_return_pct']}%, "
              f"max DD {r['max_drawdown_pct']}%, open {r['open_positions']}")
    print(f"Pools: {len(report['selected_pools'])}; unreliable/unusable: {len(report['unusable_pools'])}; "
          f"full JSON: {args.output}; REAL MONEY USED: NO; PROFITABILITY VALIDATED: NO")
    if report["unmodified_holdout"]["closed_trades"] == 0:
        raise SystemExit(3)

if __name__=="__main__":
    main()
