"""Offline execution/exit replay; deliberately NOT a scanner profitability backtest.

CSV timestamps identify bar opens in UTC (Unix seconds or timezone-aware ISO 8601).
liquidity_usd must be an independently recorded historical snapshot known at that
bar's open. Never populate it from today's liquidity or candle volume. We assume
that snapshot remains applicable until the next observation. Optional
close_liquidity_usd supplies a historical close snapshot for equity valuation.

An explicit manual entry signal occurs after signal_index. Orders execute at the
next bar open through the same cost/quantity functions as paper trading. High/low
only generate signals; candle highs and retrospective stop levels are never fills.
Stops beat take-profit when both are touched. Trailing levels become available
after the candle that establishes its high. No present-day market API is used.
"""
from __future__ import annotations

import csv
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Callable, Mapping

D = Decimal
ZERO = D("0")
HUNDRED = D("100")
REQUIRED_COLUMNS = {"timestamp", "open", "high", "low", "close", "liquidity_usd"}
CONFIG_KEYS = (
    "INITIAL_CASH_USD", "TRADE_BUDGET_USD", "ESTIMATED_GAS_USD",
    "DEX_POOL_FEE_PCT", "BASE_SLIPPAGE_PCT", "MAX_ALLOWED_PRICE_IMPACT_PCT",
    "INITIAL_STOP_LOSS_PCT", "BREAKEVEN_TRIGGER_PCT", "BREAKEVEN_LOCK_PCT",
    "TAKE_PROFIT_1_PCT", "TAKE_PROFIT_2_PCT", "MOONBAG_TRAILING_PCT",
)


def number(value, name="value") -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"Missing/invalid {name}")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid {name}: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"Non-finite {name}")
    return result


def _timestamp(value: str) -> int:
    try:
        numeric = D(value)
    except InvalidOperation:
        numeric = None
    if numeric is not None:
        if not numeric.is_finite() or numeric != numeric.to_integral_value() or numeric < 0:
            raise ValueError("timestamp must be nonnegative whole Unix seconds")
        return int(numeric)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be Unix seconds or timezone-aware ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.microsecond:
        raise ValueError("ISO timestamp needs timezone and whole seconds")
    return int(parsed.timestamp())


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    liquidity_usd: Decimal
    close_liquidity_usd: Decimal | None = None


def read_candles(path: str | Path) -> list[Candle]:
    bars = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError("CSV has duplicate column names")
        missing = REQUIRED_COLUMNS - set(fields)
        if missing:
            raise ValueError("CSV missing historical fields: " + ", ".join(sorted(missing)))
        for line, row in enumerate(reader, start=2):
            try:
                if None in row:
                    raise ValueError("Too many columns")
                prices = {name: number(row[name], name) for name in ("open", "high", "low", "close")}
                if any(value <= ZERO for value in prices.values()):
                    raise ValueError("OHLC prices must be positive")
                if not (prices["low"] <= min(prices["open"], prices["close"]) <=
                        max(prices["open"], prices["close"]) <= prices["high"]):
                    raise ValueError("Invalid OHLC range")
                liquidity = number(row["liquidity_usd"], "liquidity_usd")
                close_liq = row.get("close_liquidity_usd")
                close_liq = number(close_liq, "close_liquidity_usd") if close_liq else None
                if liquidity < ZERO or (close_liq is not None and close_liq < ZERO):
                    raise ValueError("Historical liquidity cannot be negative")
                bars.append(Candle(_timestamp(row["timestamp"]), **prices,
                                   liquidity_usd=liquidity, close_liquidity_usd=close_liq))
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"CSV line {line}: {exc}") from exc
    if len(bars) < 2:
        raise ValueError("At least two historical bars are required for next-open execution")
    intervals = [b.timestamp - a.timestamp for a, b in zip(bars, bars[1:])]
    if min(intervals) <= 0:
        raise ValueError("CSV timestamps must be unique and strictly increasing; data is never silently sorted")
    if len(set(intervals)) != 1:
        raise ValueError("CSV contains missing/irregular bars; supply a continuous, uniformly spaced history")
    return bars


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def run_replay(csv_path, *, config: Mapping, buy_fn: Callable, sell_fn: Callable,
               decimals: int, initial_cash=None, budget=None, output_dir=None,
               signal_index: int = 0) -> dict:
    """Run one explicit entry and persist inputs, attempted fills and equity.

    buy_fn(budget, price, liquidity, decimals) and
    sell_fn(raw_amount, decimals, price, liquidity) must include all modeled costs.
    An impossible liquidation remains unvalued, rather than becoming fake cash.
    """
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
        raise ValueError("Supply the token's actual decimals as an integer from 0 through 18")
    bars = read_candles(csv_path)
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as source:
        source_labels = sorted({row["data_kind"] for row in csv.DictReader(source) if row.get("data_kind")})
    synthetic = "synthetic" in Path(csv_path).name.lower() or any("synthetic" in label.lower() for label in source_labels)
    if isinstance(signal_index, bool) or not isinstance(signal_index, int) or not 0 <= signal_index < len(bars) - 1:
        raise ValueError("signal_index must leave at least one following bar to execute the entry")
    initial_cash = number(config["INITIAL_CASH_USD"] if initial_cash is None else initial_cash, "initial cash")
    budget = number(config["TRADE_BUDGET_USD"] if budget is None else budget, "budget")
    if initial_cash <= ZERO or not ZERO < budget <= initial_cash:
        raise ValueError("Cash must be positive and 0 < trade budget <= cash")
    stop_pct = number(config["INITIAL_STOP_LOSS_PCT"])
    trail_pct = number(config["MOONBAG_TRAILING_PCT"])
    be_trigger = number(config["BREAKEVEN_TRIGGER_PCT"])
    be_lock = number(config["BREAKEVEN_LOCK_PCT"])
    tp1_pct = number(config["TAKE_PROFIT_1_PCT"])
    tp2_pct = number(config["TAKE_PROFIT_2_PCT"])
    if not (ZERO < stop_pct < HUNDRED and ZERO < trail_pct < HUNDRED and
            ZERO <= be_lock < be_trigger < tp1_pct < tp2_pct):
        raise ValueError("Invalid exit thresholds")

    cash = initial_cash
    fees = realized = ZERO
    remaining_basis = ZERO
    initial_raw = remaining_raw = 0
    entry_price = peak_price = stop = ZERO
    tp1 = tp2 = False
    trailing_status = 0
    pending = None
    fills = []
    signals = []
    equity = []
    closed_pnls = []
    equity_peak = initial_cash
    max_drawdown = ZERO
    valuations_missing = 0
    entry_filled = False
    warnings = [
        "ONE MANUALLY SELECTED ENTRY: this tests execution and exits, not token selection or a profitable trading edge.",
        "No profitability guarantee: OHLC does not identify intrabar ordering, MEV, order queues, RPC latency, or real fill availability.",
        "Historical liquidity snapshots and this pool approximation cannot guarantee real executable liquidity.",
        "All orders execute at the following bar open. This adds one-bar signal latency; it is not a tick-level replay.",
        "Maximum drawdown is sampled at bar closes only and can understate intrabar losses.",
        "Failed simulated orders do not include failed-transaction gas; fees and adverse execution assumptions require calibration.",
        "Input authenticity is not verified: a source hash preserves reproducibility, not proof that prices/liquidity are real historical observations.",
    ]
    if synthetic:
        warnings.insert(0, "SYNTHETIC DEMONSTRATION DATA: invented prices/liquidity for code verification. These results say nothing about market profitability.")
    if any(bar.close_liquidity_usd is None for bar in bars):
        warnings.append("Close equity estimates use the last supplied opening-liquidity snapshot when close_liquidity_usd is absent.")

    def schedule(action, index, raw=None):
        nonlocal pending
        pending = {"action": action, "signal_bar": index,
                   "signal_timestamp": bars[index].timestamp,
                   "signal_available_timestamp": bars[index].timestamp + (bars[1].timestamp - bars[0].timestamp),
                   "raw_amount": raw}
        signals.append(dict(pending))

    def stop_name():
        return "MOONBAG_TRAILING_STOP" if trailing_status == 2 else "BREAKEVEN_STOP" if trailing_status == 1 else "STOP_LOSS"

    for index, bar in enumerate(bars):
        if pending is not None and index > pending["signal_bar"]:
            order = dict(pending)
            action = order["action"]
            # A known opening gap through the established stop takes precedence
            # over a take-profit market order waiting from the previous candle.
            if remaining_raw and action in ("TP1", "TP2") and bar.open <= stop:
                action = stop_name()
                order["action"] = action
                order["raw_amount"] = remaining_raw
            attempt = {**order, "fill_bar": index, "timestamp": bar.timestamp,
                       "market_price_usd": bar.open, "liquidity_usd": bar.liquidity_usd}
            try:
                if action == "BUY":
                    result = buy_fn(budget, bar.open, bar.liquidity_usd, decimals)
                    raw = int(result["raw_amount"])
                    if raw <= 0 or number(result["execution_price"]) <= ZERO:
                        raise ValueError("Buy yielded no positive integer token quantity/price")
                    initial_raw = remaining_raw = raw
                    entry_price = number(result["execution_price"])
                    peak_price = max(entry_price, bar.open)
                    stop = entry_price * (1 - stop_pct / HUNDRED)
                    cash -= budget
                    remaining_basis = budget
                    entry_filled = True
                    attempt["cash_flow_usd"] = -budget
                    attempt["raw_amount"] = raw
                else:
                    requested_raw = order["raw_amount"] if action in ("TP1", "TP2") else remaining_raw
                    raw = min(remaining_raw, int(requested_raw))
                    if raw <= 0:
                        raise ValueError("Partial exit rounds to zero atomic tokens")
                    result = sell_fn(raw, decimals, bar.open, bar.liquidity_usd)
                    proceeds = number(result["net_usd"])
                    if cash + proceeds < ZERO:
                        raise ValueError("Not enough cash to pay modeled exit fees")
                    basis = remaining_basis if raw == remaining_raw else budget * D(raw) / D(initial_raw)
                    remaining_raw -= raw
                    remaining_basis -= basis
                    leg_pnl = proceeds - basis
                    realized += leg_pnl
                    cash += proceeds
                    attempt.update(raw_amount=raw, cash_flow_usd=proceeds,
                                   realized_pnl_usd=leg_pnl, allocated_cost_basis_usd=basis)
                    if action == "TP1":
                        tp1 = True
                        trailing_status = max(1, trailing_status)
                        stop = max(stop, entry_price * D("1.15"))
                    elif action == "TP2":
                        tp2 = True
                        trailing_status = 2
                        stop = max(stop, peak_price * (1 - trail_pct / HUNDRED))
                    if remaining_raw == 0:
                        closed_pnls.append(realized)
                paid_fee = number(result["fee_usd"])
                fees += paid_fee
                attempt.update(status="FILLED", execution_price_usd=number(result["execution_price"]),
                               fee_usd=paid_fee, price_impact_pct=number(result["price_impact_pct"]))
                pending = None
            except ValueError as exc:
                attempt.update(status="REJECTED", reason=str(exc))
                # A refused buy is not a held position. Exit orders remain pending
                # and retry at subsequent opens; a failure never creates sale cash.
                if action == "BUY":
                    pending = None
                else:
                    pending = order
            fills.append(attempt)

        if index == signal_index:
            schedule("BUY", index)

        if remaining_raw:
            # Stop threshold is the level known before this candle's high.
            stop_triggered = bar.low <= stop
            if stop_triggered:
                schedule(stop_name(), index, remaining_raw)
            elif pending is None:
                if not tp1 and bar.high >= entry_price * (1 + tp1_pct / HUNDRED):
                    schedule("TP1", index, int((D(initial_raw) * D("0.40")).to_integral_value(ROUND_DOWN)))
                elif tp1 and not tp2 and bar.high >= entry_price * (1 + tp2_pct / HUNDRED):
                    schedule("TP2", index, int((D(initial_raw) * D("0.35")).to_integral_value(ROUND_DOWN)))
            if not stop_triggered:
                peak_price = max(peak_price, bar.high)
                if trailing_status == 2:
                    stop = max(stop, peak_price * (1 - trail_pct / HUNDRED))
                elif trailing_status == 0 and bar.high >= entry_price * (1 + be_trigger / HUNDRED):
                    trailing_status = 1
                    stop = max(stop, entry_price * (1 + be_lock / HUNDRED))
                # The close definitely occurred after the high; unlike the candle low,
                # it can establish a breach of a stop raised by that high.
                if bar.close <= stop:
                    schedule(stop_name(), index, remaining_raw)

        liquidation = ZERO
        valuation_error = None
        if remaining_raw:
            try:
                liq = bar.liquidity_usd if bar.close_liquidity_usd is None else bar.close_liquidity_usd
                quote = sell_fn(remaining_raw, decimals, bar.close, liq)
                liquidation = number(quote["net_usd"])
                if cash + liquidation < ZERO:
                    raise ValueError("Not enough cash to pay modeled exit fees")
            except ValueError as exc:
                liquidation = None
                valuation_error = str(exc)
                valuations_missing += 1
        value = None if liquidation is None else cash + liquidation
        if value is not None:
            equity_peak = max(equity_peak, value)
            max_drawdown = max(max_drawdown, (equity_peak - value) / equity_peak * HUNDRED)
        equity.append({"bar": index, "timestamp": bar.timestamp,
                       "valuation_at": "bar_close", "cash_usd": cash,
                       "remaining_raw": remaining_raw, "remaining_cost_basis_usd": remaining_basis,
                       "net_liquidation_value_usd": liquidation, "equity_usd": value,
                       "valuation_error": valuation_error, "stop_price_usd": stop if remaining_raw else None})

    final = equity[-1]
    final_value = final["equity_usd"]
    net_pnl = None if final_value is None else final_value - initial_cash
    wins = sum(p > ZERO for p in closed_pnls)
    losses = sum(p < ZERO for p in closed_pnls)
    gross_wins = sum((p for p in closed_pnls if p > ZERO), ZERO)
    gross_losses = -sum((p for p in closed_pnls if p < ZERO), ZERO)
    if valuations_missing:
        warnings.append(f"{valuations_missing} bar-close liquidation valuations were unavailable; drawdown is incomplete.")
    if pending:
        warnings.append("A signal remains pending at the end of the dataset. It has NOT been filled at a hindsight/fabricated price.")
    summary = {
        "kind": "manual_single_entry_exit_replay", "profitability_validated": False,
        "synthetic_input": synthetic,
        "initial_cash": initial_cash, "budget": budget, "cash": cash,
        "open_val": final["net_liquidation_value_usd"], "total_port": final_value,
        "net_pnl": net_pnl, "net_pnl_pct": None if net_pnl is None else net_pnl / initial_cash * HUNDRED,
        "realized_pnl": realized,
        "unrealized_pnl": None if final["net_liquidation_value_usd"] is None else final["net_liquidation_value_usd"] - remaining_basis,
        "total_trades": len(closed_pnls), "winning_trades": wins, "losing_trades": losses,
        "win_rate": None if not closed_pnls else D(wins) / D(len(closed_pnls)) * HUNDRED,
        "profit_factor": None if not gross_losses else gross_wins / gross_losses,
        "profit_factor_note": "undefined without completed losing positions" if not gross_losses else "net position profits / absolute net position losses",
        "fees": fees, "max_drawdown_pct": max_drawdown,
        "max_drawdown_complete": valuations_missing == 0,
        "valuation_complete": final_value is not None,
        "entry_filled": entry_filled, "remaining_raw": remaining_raw,
        "pending_signal": pending, "filled_orders": sum(f["status"] == "FILLED" for f in fills),
        "rejected_orders": sum(f["status"] == "REJECTED" for f in fills),
    }
    out = Path(output_dir) if output_dir else Path.cwd() / "replay_reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = out / f"exit_replay_{stamp}_{uuid.uuid4().hex[:8]}.json"
    summary["report_path"] = str(report_path.resolve())
    report = {
        "schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_file": str(Path(csv_path).resolve()),
        "source_sha256": hashlib.sha256(Path(csv_path).read_bytes()).hexdigest(),
        "source_labels": source_labels, "synthetic_input": synthetic,
        "token_decimals": decimals, "entry_signal_bar": signal_index,
        "config": {key: config[key] for key in CONFIG_KEYS if key in config},
        "execution_model": "completed bar signal -> next open; old stop/low wins same-bar conflicts; each fill uses its own historical opening-liquidity snapshot",
        "warnings": warnings, "summary": summary, "candles": [asdict(b) for b in bars],
        "signals": signals, "orders": fills, "equity": equity,
    }
    report_path.write_text(json.dumps(report, indent=2, default=_json_default, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary
