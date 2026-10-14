"""Trench evidence for paper research, not a buy signal or a rug probability.

Inputs are DexScreener aggregated reports. They are not independently verified
on-chain wallet, developer, bundle or wash-trading intelligence. Missing data
must never be converted into safe/verified signals.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import time

VERSION = "trench-evidence-v1"


def _nonnegative(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _integer(value):
    number = _nonnegative(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def assess_pair(pair, now=None):
    """Return facts and unknowns; never turn promotional activity into trust.

    This is read-only telemetry. Explicit buy/rug/execution checks elsewhere
    must still pass and can never be overridden by this output.
    """
    result = {
        "version": VERSION,
        "use": "RESEARCH_ONLY",
        "trading_approval": False,
        "rug_safety_verified": False,
        "developer_history_verified": False,
        "holder_wallet_clusters_verified": False,
        "bundled_launch_verified": False,
        "wash_trading_verified": False,
        "onchain_wallet_confluence_verified": False,
        "source": "DexScreener aggregated observations, not independent on-chain evidence",
        "label": "INSUFFICIENT_DATA",
        "flags": [],
        "observations": {},
    }
    if not isinstance(pair, dict):
        result["flags"].append("NO_PAIR_OBSERVATION")
        return result
    now = time.time() if now is None else float(now)
    stamp = _nonnegative(pair.get("observed_at"))
    if stamp is None or not -5 <= now - float(stamp) <= 30:
        result["flags"].append("TIMESTAMP_MISSING_OR_STALE")
    buy = _integer(pair.get("buys_h1"))
    sell = _integer(pair.get("sells_h1"))
    volume = _nonnegative(pair.get("volume_h1"))
    liquidity = _nonnegative(pair.get("liquidity_usd"))
    change = pair.get("price_change_h1")
    try:
        change = Decimal(str(change))
        if isinstance(pair.get("price_change_h1"), bool) or not change.is_finite():
            change = None
    except (InvalidOperation, TypeError, ValueError):
        change = None
    boosts = _integer(pair.get("boosts"))
    result["observations"] = {
        "buys_h1": buy,
        "sells_h1": sell,
        "volume_h1_usd": str(volume) if volume is not None else None,
        "conservative_liquidity_usd": str(liquidity) if liquidity is not None else None,
        "price_change_h1_pct": str(change) if change is not None else None,
        "paid_boosts_reported": boosts,
        "observed_at_unix": str(stamp) if stamp is not None else None,
    }
    required = (buy, sell, volume, liquidity, change)
    if any(value is None for value in required) or liquidity == 0:
        result["flags"].append("MARKET_FIELDS_INCOMPLETE")
    else:
        turnover = volume / liquidity
        result["observations"]["hourly_volume_to_liquidity"] = str(turnover)
        if buy + sell < 35:
            result["flags"].append("SPARSE_TWO_SIDED_TRADE_COUNT")
        if buy == 0 or sell == 0:
            result["flags"].append("ONE_SIDED_REPORTED_FLOW")
        if sell > 2 * max(1, buy):
            result["flags"].append("SELL_DOMINANCE_REPORTED")
        if turnover > Decimal("5"):
            result["flags"].append("EXCESSIVE_VOLUME_TO_LIQUIDITY")
        if change > Decimal("30"):
            result["flags"].append("SHARP_REPORTED_HOURLY_RISE")
        if change < Decimal("-30"):
            result["flags"].append("SHARP_REPORTED_HOURLY_DROP")
        if liquidity < Decimal("15000"):
            result["flags"].append("LOW_CONSERVATIVE_LIQUIDITY")
    if boosts is not None and boosts > 0:
        result["flags"].append("PAID_BOOSTS_ARE_NOT_ORGANIC_VALIDATION")
    # Facts about transactions from an aggregated market service cannot prove
    # that the wallets are independent, that the developer is trusted, or that
    # token distribution / pool safety is genuine.
    if ("MARKET_FIELDS_INCOMPLETE" in result["flags"] or
            "TIMESTAMP_MISSING_OR_STALE" in result["flags"]):
        result["label"] = "INSUFFICIENT_DATA"
    elif any(flag in result["flags"] for flag in (
            "ONE_SIDED_REPORTED_FLOW", "SELL_DOMINANCE_REPORTED",
            "EXCESSIVE_VOLUME_TO_LIQUIDITY", "SHARP_REPORTED_HOURLY_DROP",
            "LOW_CONSERVATIVE_LIQUIDITY")):
        result["label"] = "ELEVATED_OBSERVABLE_RISK"
    else:
        result["label"] = "OBSERVABLE_MARKET_FLOW_ONLY"
    return result
