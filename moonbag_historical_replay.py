"""Exploratory Solana OHLCV strategy replay using the ORIGINAL MOONBAG exits.

Never executes swaps or loads wallets. Uses the same fixed entry hypothesis and
same fee assumptions as historical_sol_paper, but TP1 +40% (40% original units),
TP2 +80% (35% original units), remaining 25% with a 12% trailing stop,
and an initial -14% stop. Hourly OHLCV has unknown intrabar ordering,
historical pool depth, route failures and quote latency; not a live backtest.

Existing 2026-10-14 historical snapshot has ALREADY been inspected. Reports
using it are retrospective diagnostics, not fresh, unbiased out-of-sample proof.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from historical_sol_paper import (
    COST, MIN_BARS, discover_pools, get_json, next_open_entry, parse_candles,
    run_strategy as old_single_take_profit_baseline,
)


def sell_proceeds(quantity, price, cost):
    if quantity <= 0 or price <= 0 or not all(math.isfinite(float(x)) for x in (quantity, price)):
        raise ValueError("Sale requires finite positive quantity and price")
    received = quantity * price * (1-cost["trade_fee_pct"]) * (1-cost["slippage_pct"])
    return max(0.0, received - cost["network_fee_usd_each_side"])


def run_moonbag(pools, *, start=None, end=None, cost=COST):
    """Fixed prior-bar entry hypothesis with original TP1/TP2/moonbag exits.

    Execute new entries at next OPEN before processing hourly intra-bar exits,
    so intrabar sale proceeds cannot be reused for imaginary same-open buys.
    Stop beats take-profit if the OHLCV candle could have done both; a new stop
    only applies on the NEXT observation. No historical AMM depth is inferred.
    """
    if not pools:
        raise ValueError("Missing historical public pool candles")
    if cost["per_trade_usd"] <= 0 or cost["capital_usd"] <= 0:
        raise ValueError("Invalid research wallet budget")
    indices = {}
    times = set()
    for asset in pools:
        if not isinstance(asset, dict) or not asset.get("pool") or not isinstance(asset.get("bars"), list):
            raise ValueError("Malformed pool history")
        if asset["pool"] in indices:
            raise ValueError("Duplicate pool history")
        indices[asset["pool"]] = {bar["ts"]: index for index, bar in enumerate(asset["bars"])}
        times.update(indices[asset["pool"]])
    times = sorted(ts for ts in times if (start is None or ts >= start) and (end is None or ts < end))
    if len(times) < 20:
        raise ValueError("Insufficient chronological history")
    cash = float(cost["capital_usd"])
    positions = {}
    closed = []
    partials = []
    peak_equity = cash
    max_drawdown = 0.0
    entry_signals = 0
    total_entries = 0
    current_marks = {}

    def sell(position, part, price, stamp, reason):
        nonlocal cash
        if not 0 < part <= position["remaining"] + 1e-12:
            raise ValueError("Cannot sell more than owned raw-equivalent quantity")
        received = sell_proceeds(part, price, cost)
        cash += received
        position["remaining"] = max(0.0, position["remaining"]-part)
        position["received"] += received
        partials.append({"pool":position["pool"], "entry_ts":position["entry_ts"],
                         "exit_ts":stamp,"event":reason,"quantity":round(part,12),
                         "usd_net":round(received,6)})
        if position["remaining"] <= position["qty"]*1e-10:
            pnl = position["received"]-position["spent"]
            closed.append({"pool":position["pool"],"name":position["name"],
                           "entry_ts":position["entry_ts"],"exit_ts":stamp,
                           "invested_usd":position["spent"],
                           "received_usd":round(position["received"],6),
                           "net_pnl_usd":round(pnl,6),
                           "exit_reason":reason,
                           "tp1_executed":position["tp1"],
                           "tp2_executed":position["tp2"]})
            del positions[position["pool"]]

    for ts in times:
        # Prior to any sale inside current bar: choose at this bar's opening.
        eligible = []
        for asset in pools:
            pool = asset["pool"]
            idx = indices[pool].get(ts)
            if idx is not None and idx > 0:
                current_marks[pool] = asset["bars"][idx-1]["c"]
            if idx is None or pool in positions or not next_open_entry(asset, idx):
                continue
            entry_signals += 1
            # A fraction of PREVIOUS bar USD volume; no future-volume lookahead.
            previous_volume = asset["bars"][idx-1]["v"]
            if cost["per_trade_usd"] > previous_volume * cost["maximum_previous_bar_volume_fraction"]:
                continue
            eligible.append((previous_volume,pool,asset,idx))
        for volume,pool,asset,idx in sorted(eligible,key=lambda x:(-x[0],x[1])):
            if len(positions) >= cost["max_positions"] or cash < cost["per_trade_usd"]:
                break
            open_price = asset["bars"][idx]["o"]
            invested = float(cost["per_trade_usd"])
            qty = ((invested-cost["network_fee_usd_each_side"]) *
                   (1-cost["trade_fee_pct"])*(1-cost["slippage_pct"]) / open_price)
            if qty <= 0:
                continue
            cash -= invested
            positions[pool] = {
                "pool":pool, "name":asset["name"], "entry_ts":ts,
                "entry_idx":idx, "price":open_price, "qty":qty, "remaining":qty,
                "spent":invested,"received":0.0,
                "stop":open_price*0.86,"peak":open_price,
                "tp1":False,"tp2":False,"trailing":False,
                "last_mark":open_price,
            }
            total_entries += 1

        # Exits occur after the bar opened. Intrabar proceeds are not available
        # to earlier entries at the same timestamp.
        for asset in pools:
            pool = asset["pool"]
            position = positions.get(pool)
            idx = indices[pool].get(ts)
            if position is None or idx is None:
                continue
            bar = asset["bars"][idx]
            position["last_mark"] = bar["c"]
            current_marks[pool] = bar["c"]
            if bar["o"] <= position["stop"] and ts > position["entry_ts"]:
                sell(position,position["remaining"],bar["o"],ts,"STOP_GAP")
                continue
            # A 24h hold limit is evaluated at the bar OPEN, before unknown
            # within-bar stop/take ordering. Moonbags must eventually exit.
            if ts-position["entry_ts"] >= 24*3600:
                sell(position,position["remaining"],bar["o"],ts,"MAX_HOLD")
                continue
            if bar["l"] <= position["stop"]:
                reason = ("MOONBAG_TRAILING_STOP" if position["trailing"]
                          else "POST_TP1_STOP" if position["tp1"]
                          else "STOP_LOSS")
                sell(position,position["remaining"],position["stop"],ts,reason)
                continue
            # TP1 then TP2 are separate observations, even if one candle
            # spans both thresholds. This avoids inventing an intrabar sequence.
            if not position["tp1"] and bar["h"] >= position["price"]*1.40:
                position["tp1"] = True
                sell(position,position["qty"]*0.40,position["price"]*1.40,ts,"TP1")
                position["stop"] = max(position["stop"],position["price"]*1.15)
            elif position["tp1"] and not position["tp2"] and bar["h"] >= position["price"]*1.80:
                position["tp2"] = True
                sell(position,position["qty"]*0.35,position["price"]*1.80,ts,"TP2")
                position["trailing"] = True
            if pool not in positions:
                continue
            position = positions[pool]
            # Newly observed high may ONLY raise the stop for subsequent bars.
            position["peak"] = max(position["peak"],bar["h"])
            if position["trailing"]:
                position["stop"] = max(position["stop"],position["peak"]*0.88)
            elif not position["tp1"] and bar["h"] >= position["price"]*1.15:
                position["stop"] = max(position["stop"],position["price"]*1.03)

        marked = cash
        for position in positions.values():
            # Last observed close only, never future candle data.
            marked += sell_proceeds(position["remaining"],position["last_mark"],cost)
        peak_equity = max(peak_equity,marked)
        if peak_equity > 0:
            max_drawdown = max(max_drawdown,(peak_equity-marked)/peak_equity*100)

    end_equity = cash+sum(
        sell_proceeds(p["remaining"],p["last_mark"],cost)
        for p in positions.values()
    )
    initial = float(cost["capital_usd"])
    return {
        "start":datetime.fromtimestamp(times[0],timezone.utc).isoformat(),
        "end":datetime.fromtimestamp(times[-1],timezone.utc).isoformat(),
        "strategy":"FIXED_MOONBAG_40_35_25_12_RESEARCH",
        "observed_bars":len(times),
        "entry_signals":entry_signals,
        "entries":total_entries,
        "closed_trades":len(closed),
        "winning_closed":sum(p["net_pnl_usd"]>0 for p in closed),
        "open_positions":len(positions),
        "tp1_partial_sells":sum(p["event"]=="TP1" for p in partials),
        "tp2_partial_sells":sum(p["event"]=="TP2" for p in partials),
        "moonbag_trailing_stops":sum(p["event"]=="MOONBAG_TRAILING_STOP" for p in partials),
        "net_closed_trade_pnl_usd":round(sum(p["net_pnl_usd"] for p in closed),4),
        "end_equity_after_modelled_liquidation_usd":round(end_equity,4),
        "net_pnl_usd":round(end_equity-initial,4),
        "net_return_pct":round((end_equity/initial-1)*100,4),
        "max_drawdown_pct":round(max_drawdown,4),
        "closed_positions":closed,
        "partial_sells":partials,
    }


def research(*, archived=None, max_pools=8, bar_limit=500):
    if archived is not None:
        if not isinstance(archived,dict) or not isinstance(archived.get("http_responses"),dict):
            raise ValueError("Invalid archived public market snapshots")
        responses=archived["http_responses"]
        def fetch(url):
            if url not in responses:
                raise ValueError(f"Required archived response missing: {url}")
            return responses[url]
    else:
        fetch=None
    meta=discover_pools(fetch,max_pools=max_pools)
    histories=[]
    rejected=[]
    for item in meta:
        try:
            payload=get_json(f"/networks/solana/pools/{item['pool']}/ohlcv/hour",
                             {"aggregate":1,"limit":bar_limit,
                              "currency":"usd","token":"base"},fetch)
            bars=parse_candles(payload)
            if len(bars)<MIN_BARS:
                rejected.append({"pool":item["pool"],"reason":"insufficient history"})
                continue
            histories.append({**item,"bars":bars})
        except (ValueError,TypeError,KeyError,RuntimeError) as exc:
            rejected.append({"pool":item["pool"],"reason":f"{type(exc).__name__}: {exc}"[:220]})
    if not histories:
        raise RuntimeError("No real historical pool candles; no results")
    timestamps=sorted(set(bar["ts"] for asset in histories for bar in asset["bars"]))
    if len(timestamps)<100:
        raise ValueError("Insufficient historical bars")
    cut=timestamps[int(len(timestamps)*0.6)]
    periods={}
    for name, start,end in (("training",None,cut),("previously_seen_holdout",cut,None)):
        moonbag=run_moonbag(histories,start=start,end=end)
        baseline=old_single_take_profit_baseline(histories,start=start,end=end)
        periods[name]={"moonbag":moonbag,
                       "fixed_prior_single_exit_baseline":{
                           key:value for key,value in baseline.items() if key!="trades"}}
    return {
        "mode":"HISTORICAL_OHLCV_EXPLORATION_ONLY",
        "live_approved":False,"profitability_validated":False,
        "using_previously_inspected_data":archived is not None,
        "lookahead_warning":"Current trending pool selection causes survivorship bias; true forward holdout not yet collected.",
        "fill_warning":"Hourly OHLCV offers no intrabar order sequencing, historical depth, sell-route availability or measured slippage.",
        "cost_model":COST,
        "data_source":"GeckoTerminal public pool OHLCV",
        "pools_used":[{"pool":p["pool"],"name":p["name"],"bars":len(p["bars"])} for p in histories],
        "pools_excluded":rejected,
        "comparison":periods,
    }


def main():
    p=argparse.ArgumentParser(description="Read-only historical actual-moonbag exit experiment")
    p.add_argument("--archive",help="Captured public historical_ohlcv_source.json")
    p.add_argument("--max-pools",type=int,default=8)
    p.add_argument("--bars",type=int,default=500)
    p.add_argument("--output",default="moonbag_historical_report.json")
    args=p.parse_args()
    if not 1<=args.max_pools<=15 or not 100<=args.bars<=1000:
        p.error("max-pools 1..15 and bars 100..1000")
    archived=json.loads(Path(args.archive).read_text(encoding="utf-8")) if args.archive else None
    report=research(archived=archived,max_pools=args.max_pools,bar_limit=args.bars)
    Path(args.output).write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    for label,period in report["comparison"].items():
        moonbag=period["moonbag"]
        print(f"{label}: Moonbag {moonbag['closed_trades']} closed PAPER trades, "
              f"TP1={moonbag['tp1_partial_sells']} TP2={moonbag['tp2_partial_sells']} "
              f"moonbag stops={moonbag['moonbag_trailing_stops']}; "
              f"net {moonbag['net_return_pct']:.2f}% vs prior baseline "
              f"{period['fixed_prior_single_exit_baseline']['net_return_pct']:.2f}%")
    print("No live fills. No proven net alpha. Research sample may be biased.")


if __name__=="__main__":
    main()
