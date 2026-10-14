"""Conservative read-only capacity check for a proposed high-AUM paper strategy.

This is NOT an execution adapter, a recommendation to trade, or live approval.
Quotes and pool balances supplied by a caller are NOT independently authenticated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path


def positive_decimal(value, field, *, allow_zero=False):
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field} missing or boolean")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} invalid decimal") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise ValueError(f"{field} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    return result


def utc_datetime(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} not ISO 8601") from exc
    if result.tzinfo is None:
        raise ValueError(f"{field} requires explicit timezone")
    return result.astimezone(timezone.utc)


def integer_quantity(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field} must be positive integer")
    text = str(value)
    if not text.isdecimal() or int(text) <= 0:
        raise ValueError(f"{field} must be positive integer")
    return int(text)


def evaluate(payload, *, aum_usd="10000000", reserve_pct="20", max_pool_share_pct="2",
             max_impact_pct="2.5", max_roundtrip_cost_pct="3", max_quote_age_seconds=5,
             now=None):
    """Estimate theoretical deployability, failing closed on missing quote evidence.

    Each allocation must contain an exact-size buy quote and a sell quote for
    EXACTLY the bought raw token units. Each pool is counted once only. Neither
    a positive result nor a quote proves that the market would remain liquid.
    """
    aum = positive_decimal(aum_usd, "aum_usd")
    reserve = positive_decimal(reserve_pct, "reserve_pct", allow_zero=True)
    share = positive_decimal(max_pool_share_pct, "max_pool_share_pct")
    max_impact = positive_decimal(max_impact_pct, "max_impact_pct")
    max_roundtrip = positive_decimal(max_roundtrip_cost_pct, "max_roundtrip_cost_pct")
    if not 0 <= reserve < 100 or not 0 < share <= 10 or not 0 < max_impact <= 100:
        raise ValueError("Invalid risk policy")
    if isinstance(max_quote_age_seconds, bool) or not isinstance(max_quote_age_seconds, int) or max_quote_age_seconds < 1:
        raise ValueError("max_quote_age_seconds must be positive integer")
    required = aum * (Decimal(100) - reserve) / 100
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    now = now.astimezone(timezone.utc)
    report = {
        "status": "NOT_READY",
        "live_approved": False,
        "profitability_validated": False,
        "capacity_model_passed": False,
        "aum_usd": str(aum),
        "required_deployment_usd": str(required),
        "conservative_minimum_distinct_pool_liquidity_usd": str(required * 100 / share),
        "planned_deployment_usd": "0",
        "theoretical_pool_capacity_usd": "0",
        "estimated_roundtrip_cost_usd": None,
        "allocations_checked": 0,
        "reasons": [],
    }
    reasons = report["reasons"]
    if not isinstance(payload, dict) or not isinstance(payload.get("allocations"), list):
        reasons.append("Required allocation quote evidence missing")
        return report
    if not payload["allocations"]:
        reasons.append("No proposed allocation or contemporaneous buy/sell quotes")
        return report

    seen_pools = set()
    proposed, theoretical_capacity, total_roundtrip_cost = Decimal(0), Decimal(0), Decimal(0)
    for index, item in enumerate(payload["allocations"]):
        prefix = f"allocation[{index}]"
        if not isinstance(item, dict):
            reasons.append(f"{prefix}: expected an object")
            continue
        try:
            pool_id = item.get("pool_id")
            mint = item.get("mint")
            if not isinstance(pool_id, str) or not pool_id.strip() or not isinstance(mint, str) or not mint.strip():
                raise ValueError("pool_id and mint are required")
            if pool_id in seen_pools:
                raise ValueError("pool reused; cannot count the same liquidity twice")
            seen_pools.add(pool_id)
            amount = positive_decimal(item.get("planned_buy_usd"), "planned_buy_usd")
            liquidity = positive_decimal(item.get("pool_liquidity_usd"), "pool_liquidity_usd")
            bought = integer_quantity(item.get("buy_quote_output_token_raw"), "buy_quote_output_token_raw")
            sell_units = integer_quantity(item.get("sell_quote_input_token_raw"), "sell_quote_input_token_raw")
            quote_size = positive_decimal(item.get("buy_quote_input_usd"), "buy_quote_input_usd")
            proceeds = positive_decimal(item.get("sell_quote_output_usd"), "sell_quote_output_usd")
            network_fees = positive_decimal(item.get("roundtrip_network_fees_usd"), "roundtrip_network_fees_usd", allow_zero=True)
            impact_buy = positive_decimal(item.get("buy_impact_pct"), "buy_impact_pct", allow_zero=True)
            impact_sell = positive_decimal(item.get("sell_impact_pct"), "sell_impact_pct", allow_zero=True)
            buy_time = utc_datetime(item.get("buy_quote_at_utc"), "buy_quote_at_utc")
            sell_time = utc_datetime(item.get("sell_quote_at_utc"), "sell_quote_at_utc")
        except ValueError as exc:
            reasons.append(f"{prefix}: {exc}")
            continue
        report["allocations_checked"] += 1
        proposed += amount
        theoretical_capacity += liquidity * share / 100
        total_roundtrip_cost += amount - proceeds + network_fees
        if quote_size != amount:
            reasons.append(f"{prefix}: quoted buy size differs from planned size")
        if bought != sell_units:
            reasons.append(f"{prefix}: sell quote is not for the exact acquired raw tokens")
        if amount > liquidity * share / 100:
            reasons.append(f"{prefix}: planned order exceeds {share}% of pool liquidity")
        if impact_buy > max_impact or impact_sell > max_impact:
            reasons.append(f"{prefix}: quoted price impact exceeds {max_impact}%")
        cost_pct = (amount - proceeds + network_fees) / amount * 100
        if cost_pct > max_roundtrip:
            reasons.append(f"{prefix}: immediate roundtrip loss incl. modeled fees exceeds {max_roundtrip}%")
        for label, stamp in (("buy", buy_time), ("sell", sell_time)):
            age = (now - stamp).total_seconds()
            if age < 0 or age > max_quote_age_seconds:
                reasons.append(f"{prefix}: {label} quote is stale or in the future")
    report["planned_deployment_usd"] = str(proposed)
    report["theoretical_pool_capacity_usd"] = str(theoretical_capacity)
    report["estimated_roundtrip_cost_usd"] = str(total_roundtrip_cost)
    if proposed != required:
        reasons.append(f"Proposed deployment {proposed} does not equal policy target {required}")
    if theoretical_capacity < required:
        reasons.append("Distinct pool liquidity cannot support the intended deployment under the pool-share limit")
    report["capacity_model_passed"] = not reasons
    # Important: caller-provided quote values are not signed/verifiable market evidence.
    reasons.append("Unverified caller-provided quotes; independent executable routing/fill evidence required")
    reasons.append("Strategy net-of-cost out-of-sample and live failure/slippage calibration missing")
    reasons.append("No live authorization; ten-million-dollar execution needs independent risk review")
    return report


def main():
    parser = argparse.ArgumentParser(description="Read-only ten-million-dollar liquidity capacity audit; NEVER authorizes live")
    parser.add_argument("--input", help="JSON with same-time proposed allocations and exact-size buy/sell quotes")
    parser.add_argument("--aum-usd", default="10000000")
    parser.add_argument("--reserve-pct", default="20")
    parser.add_argument("--output", help="Optional JSON report destination")
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else {}
        report = evaluate(payload, aum_usd=args.aum_usd, reserve_pct=args.reserve_pct)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = {"status":"NOT_READY", "live_approved":False, "capacity_model_passed":False,
                  "reasons":[f"Invalid input: {type(exc).__name__}: {exc}"]}
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded + "\n", encoding="utf-8")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
