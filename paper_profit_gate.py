"""Fail-closed, read-only auditor for an existing GODLIKE paper SQLite database.
Never authorizes live orders or certifies an economic edge.
"""
from __future__ import annotations
import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3

def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Missing financial quantity")
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid financial quantity") from exc
    if not n.is_finite():
        raise ValueError("Nonfinite financial quantity")
    return n

def evaluate(db_path, min_days=30, min_closed=200):
    path = Path(db_path).expanduser().resolve()
    result = {"database": str(path), "mode":"PAPER_EVALUATION_ONLY",
              "live_approved":False, "profitability_validated":False,
              "paper_days_observed":0, "paper_days_with_substantial_coverage":0,
              "paper_min_covered_minutes_per_day":None,
              "paper_closed_positions":0, "paper_winning_closed_positions":0,
              "paper_mean_net_per_closed_position_usd":None,
              "paper_native_sol_balance_lamports":None,
              "paper_modeled_fee_per_fill_lamports":None,
              "paper_remaining_minimal_buy_sell_roundtrips":None,
              "paper_realized_net_usd":None, "paper_max_drawdown_pct":None,
              "out_of_sample_validated":False, "status":"NOT_READY", "reasons":[]}
    if min_days <= 0 or min_closed <= 0:
        raise ValueError("Positive minimum thresholds required")
    if not path.is_file():
        result["reasons"].append("No existing SQLite paper database found")
        return result
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as con:
            con.row_factory = sqlite3.Row
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"market_observations","positions","trade_history","equity_history"} <= tables:
                result["reasons"].append("Required paper accounting tables are missing")
                return result
            days = [row[0] for row in con.execute(
                "SELECT DISTINCT CAST(timestamp / 86400 AS INTEGER) FROM market_observations ORDER BY 1")]
            result["paper_days_observed"] = len(days)
            # A handful of quote polls on each calendar day is NOT full-day
            # forward coverage. Require at least 12h of distinct observed
            # UTC minutes per day as a conservative minimum diagnostic.
            coverages = [int(row[0]) for row in con.execute(
                "SELECT COUNT(DISTINCT CAST(timestamp / 60 AS INTEGER)) "
                "FROM market_observations GROUP BY CAST(timestamp / 86400 AS INTEGER)")]
            substantial_days = sum(minutes >= 720 for minutes in coverages)
            result["paper_days_with_substantial_coverage"] = substantial_days
            result["paper_min_covered_minutes_per_day"] = min(coverages) if coverages else None
            if substantial_days < min_days:
                result["reasons"].append(
                    f"Only {substantial_days} of required {min_days} days contain at least 720 distinct observed minutes; "
                    "brief scheduled GitHub runs are not continuous forward testing")
            if len(days) < min_days:
                result["reasons"].append(f"Observed only {len(days)} of required {min_days} UTC paper days")
            if days and days[-1] - days[0] + 1 != len(days):
                result["reasons"].append("Gap days exist in the market observation record")
            positions = con.execute("SELECT id, usd_invested, net_usd_received FROM positions "
                                    "WHERE is_paper=1 AND status='CLOSED'").fetchall()
            result["paper_closed_positions"] = len(positions)
            if len(positions) < min_closed:
                result["reasons"].append(f"Only {len(positions)} of required {min_closed} closed paper positions")
            partial = {}
            for row in con.execute("SELECT pos_id, usd_amount FROM trade_history "
                                   "WHERE is_paper=1 AND action IN ('TP1','TP2')"):
                partial[row[0]] = partial.get(row[0], Decimal(0)) + number(row[1])
            pnl = Decimal(0)
            winning = 0
            for pos in positions:
                trade_net = number(pos["net_usd_received"]) + partial.get(pos["id"], Decimal(0)) - number(pos["usd_invested"])
                pnl += trade_net
                winning += trade_net > 0
            result["paper_realized_net_usd"] = str(pnl)
            result["paper_winning_closed_positions"] = winning
            if positions:
                result["paper_mean_net_per_closed_position_usd"] = str(pnl / len(positions))
            if pnl <= 0:
                result["reasons"].append("Closed paper trades have no positive modeled net PnL")
            # For a $100 paper bank, even an otherwise profitable model can
            # stop after a few trades if native SOL fees are never replenished.
            # Count a MINIMUM of two fills per closed position; TP1/TP2 require
            # more. Do not silently assume free SOL or refundable rent.
            if {"execution_funding_evidence", "execution_balances"} <= tables:
                funding_row = con.execute(
                    "SELECT evidence_json FROM execution_funding_evidence WHERE id=1").fetchone()
                sol_row = con.execute(
                    "SELECT raw_amount FROM execution_balances WHERE mode='PAPER' AND asset='native:SOL'").fetchone()
                if funding_row is not None and sol_row is not None:
                    funding = json.loads(funding_row["evidence_json"])
                    fee = int(funding["policy"]["estimated_network_fee_lamports"])
                    sol = int(sol_row["raw_amount"])
                    if fee <= 0 or sol < 0:
                        raise ValueError("Invalid native SOL fee reserve/fee policy")
                    capacity = sol // (2 * fee)
                    result["paper_native_sol_balance_lamports"] = sol
                    result["paper_modeled_fee_per_fill_lamports"] = fee
                    result["paper_remaining_minimal_buy_sell_roundtrips"] = capacity
                    if len(positions) + capacity < min_closed:
                        result["reasons"].append(
                            f"Paper SOL fee reserve supports at most {capacity} further minimal two-fill roundtrips; "
                            f"{min_closed} closed trades cannot be achieved without explicit fee funding/recalibration")
                else:
                    result["reasons"].append("No verified initialized PAPER native-SOL fee reserve")
            # The first saved valuation may already reflect liquidation friction.
            # Starting the high-water mark there understates risk even with zero trades.
            # Fail closed if the original paper bankroll cannot be verified.
            initial_equity = None
            if "wallet" in tables:
                wallet_row = con.execute("SELECT initial_cash_usd FROM wallet WHERE id=1").fetchone()
                if wallet_row is not None:
                    initial_equity = number(wallet_row["initial_cash_usd"])
                    if initial_equity <= 0:
                        raise ValueError("Invalid initial PAPER bankroll")
            if initial_equity is None:
                result["reasons"].append("No verified initial PAPER bankroll for drawdown baseline")
            equity_rows = con.execute("SELECT equity_usd, complete FROM equity_history ORDER BY timestamp, id").fetchall()
            if not equity_rows:
                result["reasons"].append("No equity curve saved")
            elif any(row["complete"] != 1 or row["equity_usd"] is None for row in equity_rows):
                result["reasons"].append("Incomplete/unknown paper portfolio valuation exists")
            elif initial_equity is not None:
                peak, max_dd = initial_equity, Decimal(0)
                for row in equity_rows:
                    value = number(row["equity_usd"])
                    if value < 0:
                        raise ValueError("Negative portfolio value")
                    peak = value if peak is None else max(value,peak)
                    if peak > 0:
                        max_dd = max(max_dd,(peak-value)/peak*100)
                result["paper_max_drawdown_pct"] = str(max_dd.quantize(Decimal("0.01")))
            # A positive backtest or simulated profit cannot replace independent validation.
            result["reasons"].append("No independent point-in-time out-of-sample entry-strategy validation")
            result["reasons"].append("No verified live fill/failure/fee calibration; live remains locked")
    except (sqlite3.DatabaseError, ValueError, OSError) as exc:
        result["reasons"].append(f"Database verification failed closed: {type(exc).__name__}: {exc}")
    return result

def main():
    p = argparse.ArgumentParser(description="Read-only paper readiness auditor; never approves live")
    p.add_argument("--db", default="paper_quotes_100.db")
    p.add_argument("--output", help="Optional JSON output path")
    args = p.parse_args()
    report = evaluate(args.db)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded + "\n", encoding="utf-8")
    raise SystemExit(2 if not report["profitability_validated"] else 0)

if __name__ == "__main__":
    main()
