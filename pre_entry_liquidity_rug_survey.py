"""Public read-only study of *why* GODLIKE does not reach genuine PAPER orders.

Unlike mocked BUY regression tests, this probes live currently reported
Solana candidate-pool data and the SAME strict source-level market/RugCheck
filters. Quote execution is never invoked; zero tokens are bought, no
private keys are loaded, and no safety filters are loosened.

This does NOT prove tradability: a RugCheck pass is a report-level check,
not an independently successful sell or a verified quote route.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import argparse
import json
import math
from pathlib import Path
import time

from godlike2026 import CONFIG, DexScreenerClient, RugCheckValidator
from market_discovery import discover_mints, valid_mint

def finite_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n=Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        return None
    return n if n.is_finite() else None


def market_gate(data, cfg, *, now=None):
    """Exact main-scanner initial market requirements before timed momentum."""
    if data is None or not isinstance(data,dict):
        return "MISSING_MARKET_DATA"
    now=time.time() if now is None else now
    price=finite_decimal(data.get("price_usd"))
    liquidity=finite_decimal(data.get("liquidity_usd"))
    minimum=finite_decimal(cfg.get("MIN_LIQUIDITY_USD"))
    if price is None or price<=0 or liquidity is None or minimum is None or liquidity<minimum:
        return "LOW_OR_INVALID_LIQUIDITY_OR_PRICE"
    timestamp=finite_decimal(data.get("observed_at"))
    maxage=finite_decimal(cfg.get("MAX_MARKET_DATA_AGE_SECONDS"))
    if timestamp is None or maxage is None or not 0<=Decimal(str(now))-timestamp<=maxage:
        return "MISSING_OR_STALE_MARKET_TIMESTAMP"
    buys=finite_decimal(data.get("buys_h1"))
    sells=finite_decimal(data.get("sells_h1"))
    volume=finite_decimal(data.get("volume_h1"))
    minvolume=finite_decimal(cfg.get("MIN_VOLUME_H1_USD"))
    if (buys is None or sells is None or volume is None or minvolume is None
            or buys<25 or sells<10 or volume<minvolume):
        return "INSUFFICIENT_TWO_SIDED_TRADE_FLOW"
    turnover=volume/liquidity
    if not Decimal("0.05")<=turnover<=Decimal("5"):
        return "TURNOVER_OUTSIDE_STRICT_RANGE"
    change=finite_decimal(data.get("price_change_h1"))
    if change is None:
        return "MISSING_HOURLY_PRICE_CHANGE"
    if change>30:
        return "LATE_HOURLY_PUMP_BLOCK"
    return "ELIGIBLE_FOR_TIMED_MOMENTUM_ONLY"


def survey(candidates, *, dex, rug, cfg=None, max_rug_probes=12, clock=time.time):
    """Inspect deterministic order, no buy/sell APIs or confidence overclaims."""
    cfg=dict(CONFIG if cfg is None else cfg)
    if not 1<=max_rug_probes<=20:
        raise ValueError("max_rug_probes must be 1..20")
    if len(candidates)>200 or any(not valid_mint(x) for x in candidates):
        raise ValueError("Must inspect up to 200 valid Solana base mints")
    records=[]
    by_gate=Counter()
    rug_attempts=0
    for offset in range(0,len(candidates),30):
        batch=candidates[offset:offset+30]
        try:
            prefetched=dex.prefetch_tokens(batch)
        except Exception as exc:
            # No success or synthetic fallback after an API failure.
            prefetched={mint:None for mint in batch}
            batch_error=f"{type(exc).__name__}: {str(exc)[:100]}"
        else:
            batch_error=None
        for mint in batch:
            data=prefetched.get(mint)
            stage=market_gate(data,cfg,now=clock())
            entry={"mint":mint,"symbol":str((data or {}).get("symbol") or "")[:35],
                   "pair_address":(data or {}).get("pair_address"),"market_status":stage,
                   "market_checked_at_utc":datetime.now(timezone.utc).isoformat(),
                   "observed_price_usd":str(data["price_usd"]) if data is not None else None,
                   "conservative_liquidity_usd":str(data["liquidity_usd"]) if data is not None else None,
                   "rug_status":"NOT_CHECKED","routing_status":"NOT_CHECKED",
                   "paper_order_submitted":False,"live_order_submitted":False}
            if batch_error is not None:
                entry["batch_error"]=batch_error
            by_gate[stage]+=1
            if stage=="ELIGIBLE_FOR_TIMED_MOMENTUM_ONLY" and rug_attempts<max_rug_probes:
                rug_attempts+=1
                try:
                    approved,reason,meta=rug.validate(mint,target_pair_address=data["pair_address"])
                    entry["rug_status"]="REPORT_FILTER_PASSED_NOT_SELLABILITY_PROVEN" if approved else "REPORT_FILTER_BLOCKED"
                    entry["rug_reason"]=str(reason)[:180]
                    if approved:
                        entry["rug_metadata_no_trade"]={
                            "decimals":meta.get("decimals"),
                            "paper_liquidity_cap_usd":str(meta.get("paper_liquidity_cap_usd"))}
                except Exception as exc:
                    entry["rug_status"]="RUG_REPORT_ERROR_FAIL_CLOSED"
                    entry["rug_reason"]=f"{type(exc).__name__}: {str(exc)[:160]}"
            elif stage=="ELIGIBLE_FOR_TIMED_MOMENTUM_ONLY":
                entry["rug_status"]="NOT_CHECKED_RUG_PROBE_BUDGET"
            records.append(entry)
    rug_summary=dict(Counter(row["rug_status"] for row in records))
    return {
        "mode":"READ_ONLY_PRE_ENTRY_DIAGNOSTIC",
        "actual_money_used":False, "live_approved":False,
        "profitability_validated":False,
        "not_an_executable_fill":True,
        "risk_policy_unchanged":True,
        "tokens_inspected":len(records),
        "market_funnel":dict(sorted(by_gate.items())),
        "rug_probe_count":rug_attempts,
        "rug_funnel":dict(sorted(rug_summary.items())),
        "notes":[
            "Trending/popular pool discovery has selection bias; this is NOT a trading strategy.",
            "No momentum entry or spread/tradability assumptions tested by this pre-entry audit.",
            "Report-level RugCheck approval NEVER means token is confirmed sellable or safe.",
            "Unknown Token-2022/unsupported programs and unverified alternate Jupiter routes remain blocked.",
            "No paper or live orders; no PnL claims."
        ],
        "observations":records,
    }


def main():
    p=argparse.ArgumentParser(description="Strict Solana candidate market/Rug pre-entry research, NO trades")
    p.add_argument("--inspect",type=int,default=180)
    p.add_argument("--rug-probes",type=int,default=12)
    p.add_argument("--output",default="market_rug_candidate_diagnostic.json")
    args=p.parse_args()
    if not 1<=args.inspect<=200 or not 1<=args.rug_probes<=20:
        p.error("--inspect 1..200, --rug-probes 1..20")
    candidates,discovery=discover_mints(max_mints=args.inspect,gecko_pages=1)
    report=survey(candidates,dex=DexScreenerClient(),rug=RugCheckValidator(),
                  max_rug_probes=args.rug_probes)
    report["discovery"]={key:discovery.get(key) for key in (
        "source_counts","total_unique","completion","errors","partial_failure")}
    Path(args.output).write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("observations","notes")},indent=2))
    print("NO TRADES, NO PROFIT CLAIM, NO LIVE.")


if __name__=="__main__":
    main()
