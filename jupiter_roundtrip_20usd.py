"""Read-only $20 round-trip Jupiter V2 quote diagnostics on observed Solana pools.

Not trades, not pre-approved tokens, not fill estimates calibrated to chain.
Quote-only calls have NO taker, wallet, transaction builder, keys or broadcasts.
Inputs are fixed from prior 2026-10-14 scanner WAITING candidates to avoid
promotional sample-picking. RugCheck may still block all of them.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

from quote_provider import JupiterV2QuoteProvider, QuoteRequest
from execution_engine import SOL_MINT, ExecutionPolicy, validate_execution_quote
from quote_broker import USDC

D=Decimal
CANDIDATES=(
    ("STONK","6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx",
     "zxTpi4BtaWX3mgdAPoezkMD1hxx8CdeCfrqXMWvSCLX"),
    ("CYBERLEEK","ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg",
     "G8kgi7aUpeX8EVR8VMkrth9SKEv5BietWC33UjAiiMGh"),
    ("XTO","XtogpWjiAgvkemc5gr1auxkh77w3vKgSQN1nxJeJfHt",
     "4pBedygxZVUVR94sgTJC6C5CX7nMGBdnv1x8ac9oPJ7o"),
)


def target_pool_only(quote,mint,pool):
    route=json.loads(quote.route_json)
    supported={USDC,SOL_MINT,mint}
    touched=0
    for step in route:
        info=step["swapInfo"]
        if info["inputMint"] not in supported or info["outputMint"] not in supported:
            return False,"UNVERIFIED_INTERMEDIATE_MINT"
        if mint in (info["inputMint"],info["outputMint"]):
            touched+=1
            if info["ammKey"]!=pool:
                return False,"QUOTE_ROUTE_NOT_RUGCHECK_TARGET_POOL"
    return (touched>0,"EXPECTED_POOL" if touched else "NO_TARGET_POOL_ROUTE")


def run(provider=None):
    provider=provider or JupiterV2QuoteProvider()
    policy=ExecutionPolicy()
    result={"mode":"QUOTE_ONLY_READONLY","live_approved":False,
            "actual_money_used":False,"profitability_validated":False,
            "quote_timestamp_utc":datetime.now(timezone.utc).isoformat(),
            "source":"Fixed historically observed candidates; no current RugCheck or safety approval",
            "cost_assumptions":{
                "usd_order": "20",
                "conservative_each_side_slippage_bps":100,
                "adverse_selling_margin_bps":50,
                "assumed_network_fee_lamports_per_side":3105000,
            },
            "tokens":[],"notes":[
                "No on-chain BUY/SELL, no fill success proof; exact quote routes may change between calls.",
                "Sell quote uses the BUY minimum token output, not its optimistic expected output.",
                "Network fees are assumed not observed; rent may be refunded or require additional accounts.",
                "No profitability or 10-million-dollar capacity inference from small nominal quote sizes.",
            ]}
    try:
        sol=provider.quote(QuoteRequest(SOL_MINT,USDC,1_000_000_000,100,
                                        policy.quote_max_age_ms,policy.exclude_routers))
        validate_execution_quote(sol,policy,sol.received_at)
        sol_usd=D(sol.out_amount_raw)/D(10**6)
        fee_usd=D("3105000")/D(10**9)*sol_usd
        result["sol_usd_indicative"]=str(sol_usd)
        result["assumed_network_fee_usd_per_side"]=str(fee_usd)
    except Exception as exc:
        result["sol_quote_error"]=f"{type(exc).__name__}: {str(exc)[:240]}"
        return result

    for symbol,mint,pool in CANDIDATES:
        item={"symbol":symbol,"mint":mint,"pool_expected":pool,
              "status":"QUOTE_UNAVAILABLE","paper_order_submitted":False}
        result["tokens"].append(item)
        try:
            buy=provider.quote(QuoteRequest(USDC,mint,20_000_000,100,
                                            policy.quote_max_age_ms,policy.exclude_routers))
            validate_execution_quote(buy,policy,buy.received_at)
            item["buy_router"]=buy.router
            item["buy_price_impact_pct"]=str(buy.price_impact_pct)
            item["buy_pool_valid"],item["buy_pool_check"]=target_pool_only(buy,mint,pool)
            # Reverse quote is for guaranteed minimum token units, not hoped-for fill.
            sell=provider.quote(QuoteRequest(mint,USDC,buy.min_out_amount_raw,100,
                                             policy.quote_max_age_ms,policy.exclude_routers))
            validate_execution_quote(sell,policy,sell.received_at)
            item["sell_router"]=sell.router
            item["sell_price_impact_pct"]=str(sell.price_impact_pct)
            item["sell_pool_valid"],item["sell_pool_check"]=target_pool_only(sell,mint,pool)
            received=D(sell.min_out_amount_raw)/D(10**6)*D("0.995")-fee_usd*2
            gross_cost=D("20")
            item["quote_dependent_modeled_liquidation_usd"]=str(received)
            item["quote_dependent_modeled_roundtrip_loss_pct"]=str(
                (gross_cost-received)/gross_cost*100)
            item["status"]=("ROUTES_MATCH_BUT_NOT_FILL_VERIFIED"
                           if item["buy_pool_valid"] and item["sell_pool_valid"]
                           else "TARGET_POOL_OR_ROUTE_MISMATCH")
        except Exception as exc:
            item["error"]=f"{type(exc).__name__}: {str(exc)[:240]}"
    return result


def main():
    result=run()
    Path("jupiter_roundtrip_20usd_report.json").write_text(
        json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    for record in result["tokens"]:
        print(record["symbol"],record["status"],
              "modeled cost %:",record.get("quote_dependent_modeled_roundtrip_loss_pct","N/A"),
              record.get("error",""))
    print("READ-ONLY QUOTES; NO REAL OR PAPER ORDER FILL. LIVE BLOCKED.")


if __name__=="__main__":
    main()
