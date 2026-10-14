"""Read-only followthrough for three previously blocked live-PAPER Solana pairs.

Verify strict Token-2022 metadata-only onchain attestation, full RugCheck
risk/LP/holder/pool validation, then exact target-pool Jupiter buy AND sell
quotes for a $20 hypothetical round trip. There is NO order or wallet.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

from godlike2026 import RugCheckValidator
from jupiter_roundtrip_20usd import target_pool_only
from quote_provider import JupiterV2QuoteProvider, QuoteRequest
from execution_engine import ExecutionPolicy, SOL_MINT, validate_execution_quote
from quote_broker import USDC

CASES=(
    ("HUMPH","7jNwX4Me9zGEjmWY7zARDG2LYjVs1WXD6DZ1JvSfpump","9pQRnaHbKjD61KyS2K4FEnLCpvAi5hu5jbMbEsunHLJz"),
    ("SEALS","3sKaoPryg4KrXubLWtoqF7MznLT7QNqKRxMMkkxTpump","HvYfFr8nm6bn9V93TDDMgqWwS2D2LsMn1Y3rDCToqeAB"),
    ("CRAWLER","CQCDTp8rsFV3PhaPeYjcCW5LdGTxi6jFx7eC3UXnpump","AnrCHvfJUzMtos8DGjpT2XKurVPPYAVLDturASNwXcd4"),
)

def run(rug=None,provider=None):
    rug=rug or RugCheckValidator(config={"TOKEN2022_METADATA_PAPER_EXPERIMENT":True})
    provider=provider or JupiterV2QuoteProvider()
    policy=ExecutionPolicy()
    r={"mode":"TOKEN2022_OPTIN_READONLY_RUG_PLUS_JUPITER","actual_money_used":False,
       "live_approved":False,"profitability_validated":False,"orders_submitted":0,
       "timestamp_utc":datetime.now(timezone.utc).isoformat(),"cases":[]}
    for symbol,mint,pool in CASES:
        item={"symbol":symbol,"mint":mint,"expected_pool":pool,"status":"NOT_VERIFIED",
              "buy_executed":False,"sell_executed":False}
        r["cases"].append(item)
        try:
            allowed,reason,meta=rug.validate(mint,target_pair_address=pool)
            item["rug_filter_passed"]=bool(allowed)
            item["rug_reason"]=str(reason)[:220]
            if not allowed:
                item["status"]="RUG_OR_ONCHAIN_DENIED"
                continue
            att=meta.get("token2022_paper_attestation")
            if not isinstance(att,dict):
                item["status"]="MISSING_ONCHAIN_PROOF"
                continue
            item["verified_metadata_mint_extensions"]=att.get("extensions")
            item["verified_onchain_slot"]=att.get("onchain_slot")
            buy=provider.quote(QuoteRequest(USDC,mint,20_000_000,100,
                                    policy.quote_max_age_ms,policy.exclude_routers))
            validate_execution_quote(buy,policy,buy.received_at)
            item["buy_route_matches_exact_rug_pool"],item["buy_route_reason"]=target_pool_only(buy,mint,pool)
            item["buy_price_impact_pct"]=str(buy.price_impact_pct)
            if not item["buy_route_matches_exact_rug_pool"]:
                item["status"]="BUY_ROUTE_MISMATCH_NOT_EXECUTABLE"
                continue
            sell=provider.quote(QuoteRequest(mint,USDC,buy.min_out_amount_raw,100,
                                     policy.quote_max_age_ms,policy.exclude_routers))
            validate_execution_quote(sell,policy,sell.received_at)
            item["sell_route_matches_exact_rug_pool"],item["sell_route_reason"]=target_pool_only(sell,mint,pool)
            item["sell_price_impact_pct"]=str(sell.price_impact_pct)
            if not item["sell_route_matches_exact_rug_pool"]:
                item["status"]="SELL_ROUTE_MISMATCH_NOT_EXECUTABLE"
                continue
            # Model SOL network/rent fee against a fresh read-only quote.
            # No assumed fixed SOL/USD value, no gratis SOL; failures leave
            # economics unknown. Not an on-chain fill or refundable-rent proof.
            sol_quote=provider.quote(QuoteRequest(SOL_MINT,USDC,1_000_000_000,100,
                                        policy.quote_max_age_ms,policy.exclude_routers))
            validate_execution_quote(sol_quote,policy,sol_quote.received_at)
            sol_usd=Decimal(sol_quote.out_amount_raw)/Decimal(10**6)
            fees=Decimal(3105000*2)/Decimal(10**9)*sol_usd
            item["assumed_roundtrip_SOL_network_and_rent_cost_usd"]=str(fees)
            received=Decimal(sell.min_out_amount_raw)/Decimal(10**6)*Decimal("0.995")-fees
            item["indicative_net_roundtrip_usd"]=str(received)
            item["indicative_loss_pct"] = str((Decimal(20)-received)/Decimal(20)*100)
            item["status"]="TWO_EXACT_POOL_READONLY_QUOTES_NO_ORDERS"
        except Exception as exc:
            item["status"]="READONLY_EXCEPTION_FAIL_CLOSED"
            item["exception"]=f"{type(exc).__name__}: {str(exc)[:180]}"
    return r

if __name__=="__main__":
    report=run()
    Path("t2022_readonly_followthrough.json").write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    for item in report["cases"]:
        print(item["symbol"],item["status"],item.get("rug_reason"),item.get("buy_route_reason"),item.get("sell_route_reason"))
    print("No PnL, no orders, no LIVE. Strict paper eligibility research only.")
