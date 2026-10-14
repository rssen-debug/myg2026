"""Atomic raw-asset portfolio for the shared order engine.

Native SOL pays network fees; wrapped SOL is a separate token balance. No USD
peg or token price is assumed. Settlement-currency gains and SOL fees remain
separate until a timestamped FX/liquidation valuation is available. This is
connected to the quote engine through bind_paper_engine and to the main bot by
quote_broker. LIVE funding/reconciliation is not implemented here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import json

from execution_engine import ExecutionEngine, ExecutionPolicy, PaperExecutionAdapter, SOL_MINT
from order_lifecycle import OrderError, OrderIntent
from quote_provider import _pubkey

NATIVE_SOL = "native:SOL"
U64_MAX = 2**64 - 1


def raw(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= U64_MAX:
        raise OrderError(f"Invalid raw quantity: {name}")
    return value


@dataclass(frozen=True)
class LedgerLimits:
    settlement_mint: str
    max_order_raw: int
    max_positions: int = 5
    fee_reserve_lamports: int = 105000

    def __post_init__(self):
        if not _pubkey(self.settlement_mint) or self.settlement_mint == SOL_MINT:
            raise OrderError("A valid non-SOL settlement mint is required")
        raw(self.max_order_raw, "max_order_raw", 1)
        raw(self.max_positions, "max_positions", 1)
        raw(self.fee_reserve_lamports, "fee_reserve_lamports")


class RawPortfolioLedger:
    def __init__(self, store, limits):
        if not isinstance(limits, LedgerLimits):
            raise OrderError("Explicit ledger limits are required")
        self.store, self.limits = store, limits
        with store._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS execution_portfolios (
                    mode TEXT PRIMARY KEY CHECK(mode IN ('PAPER','LIVE')),
                    initial_balances_json TEXT NOT NULL,
                    limits_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_balances (
                    mode TEXT NOT NULL REFERENCES execution_portfolios(mode),
                    asset TEXT NOT NULL,
                    raw_amount TEXT NOT NULL,
                    PRIMARY KEY(mode,asset)
                );
                CREATE TABLE IF NOT EXISTS execution_positions (
                    id INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL REFERENCES execution_portfolios(mode),
                    mint TEXT NOT NULL,
                    settlement_mint TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    entry_order_id TEXT NOT NULL UNIQUE REFERENCES execution_orders(id),
                    initial_raw TEXT NOT NULL,
                    remaining_raw TEXT NOT NULL,
                    initial_spent_raw TEXT NOT NULL,
                    remaining_cost_fraction TEXT NOT NULL,
                    realized_settlement_fraction TEXT NOT NULL DEFAULT '0',
                    network_fees_lamports TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS execution_one_open_token
                    ON execution_positions(mode,mint) WHERE status='OPEN';
                CREATE TABLE IF NOT EXISTS execution_asset_movements (
                    order_id TEXT NOT NULL REFERENCES execution_orders(id),
                    mode TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    delta_raw TEXT NOT NULL,
                    balance_after_raw TEXT NOT NULL,
                    PRIMARY KEY(order_id,asset)
                );
                CREATE TRIGGER IF NOT EXISTS execution_movements_no_update
                    BEFORE UPDATE ON execution_asset_movements BEGIN
                    SELECT RAISE(ABORT,'Asset movements are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS execution_movements_no_delete
                    BEFORE DELETE ON execution_asset_movements BEGIN
                    SELECT RAISE(ABORT,'Asset movements are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS execution_portfolios_no_update
                    BEFORE UPDATE ON execution_portfolios BEGIN
                    SELECT RAISE(ABORT,'Portfolio funding and limits are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS execution_portfolios_no_delete
                    BEFORE DELETE ON execution_portfolios BEGIN
                    SELECT RAISE(ABORT,'Portfolio funding and limits are immutable'); END;
            """)

    def initialize_paper(self, *, settlement_raw, native_sol_lamports, on_initialize=None):
        """Idempotent explicit virtual balances; never silently replenish funds.

        These two balances together form the initial portfolio. SOL is not a
        free allowance on top of a claimed dollar capital amount. The caller
        must value both before describing total funding in dollars.
        """
        raw(settlement_raw, "settlement_raw", 1)
        raw(native_sol_lamports, "native_sol_lamports")
        funding = json.dumps({self.limits.settlement_mint: settlement_raw, NATIVE_SOL: native_sol_lamports}, sort_keys=True)
        limits = json.dumps(asdict(self.limits), sort_keys=True)
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM execution_portfolios WHERE mode='PAPER'").fetchone()
            if existing is not None:
                if existing["initial_balances_json"] != funding or existing["limits_json"] != limits:
                    raise OrderError("Existing portfolio funding/limits differ; use a separate evaluation database")
                conn.commit()
                return False
            conn.execute("INSERT INTO execution_portfolios VALUES('PAPER',?,?)", (funding, limits))
            conn.executemany("INSERT INTO execution_balances VALUES('PAPER',?,?)",
                             [(asset, str(amount)) for asset, amount in json.loads(funding).items()])
            if on_initialize is not None:
                on_initialize(conn)
            conn.commit()
            return True

    @staticmethod
    def _balances(conn, mode):
        return {r["asset"]: int(r["raw_amount"]) for r in conn.execute(
            "SELECT * FROM execution_balances WHERE mode=?", (mode,))}

    def _validate(self, conn, intent, fee):
        raw(fee, "fee")
        account = conn.execute("SELECT * FROM execution_portfolios WHERE mode=?", (intent.mode,)).fetchone()
        if account is None:
            raise OrderError("Portfolio has no initialized/reconciled funding for this mode")
        if json.loads(account["limits_json"]) != asdict(self.limits):
            raise OrderError("Runtime limits differ from the persisted portfolio limits")
        balances = self._balances(conn, intent.mode)
        if balances.get(intent.input_mint, 0) < intent.amount_raw:
            raise OrderError("Insufficient input-token balance")
        positions = list(conn.execute("SELECT * FROM execution_positions WHERE mode=? AND status='OPEN'", (intent.mode,)))
        if intent.side == "BUY":
            if intent.input_mint != self.limits.settlement_mint or intent.position_id is not None:
                raise OrderError("BUY must spend settlement currency and open a new position")
            if intent.amount_raw > self.limits.max_order_raw:
                raise OrderError("Order exceeds independent size limit")
            if len(positions) >= self.limits.max_positions:
                raise OrderError("Maximum concurrent positions reached")
            if any(p["mint"] == intent.output_mint for p in positions):
                raise OrderError("Position already open; averaging down is not enabled")
            # Retain modeled exit fees for all held positions plus new entry.
            required_fee_balance = fee * (len(positions) + 2) + self.limits.fee_reserve_lamports
        else:
            position = next((p for p in positions if p["id"] == intent.position_id), None)
            if (position is None or position["mint"] != intent.input_mint or
                    position["settlement_mint"] != intent.output_mint or
                    intent.amount_raw > int(position["remaining_raw"])):
                raise OrderError("SELL does not match an open position and its remaining quantity")
            required_fee_balance = fee
        # Transaction fees are paid before output exists. Wrapped SOL received
        # from a buy cannot fund its own network fee.
        if balances.get(NATIVE_SOL, 0) < required_fee_balance:
            raise OrderError("Insufficient native SOL for fees and required entry reserve")
        return balances

    def risk_check(self, intent, quote=None, *, estimated_fee_lamports):
        try:
            with self.store._connection() as conn:
                conn.execute("BEGIN")
                self._validate(conn, intent, estimated_fee_lamports)
            return {"allowed": True, "source": "independent_raw_portfolio",
                    "fee_estimate_lamports": estimated_fee_lamports,
                    "liquidity_security_and_market_risk_require_separate_approval": True}
        except OrderError as exc:
            return {"allowed": False, "reason": str(exc)}

    def apply_confirmation(self, conn, order):
        """Use only as OrderStore's callback, inside its confirmation transaction."""
        if not conn.in_transaction or order["state"] != "CONFIRMED":
            raise OrderError("Portfolio posting requires an atomic confirmed-order transaction")
        intent = OrderIntent(**order["intent"])
        evidence = order["confirmation"]
        spent = raw(evidence["input_spent_raw"], "input_spent_raw", 1)
        received = raw(evidence["output_received_raw"], "output_received_raw", 1)
        fee = raw(evidence["fee_paid_raw"], "fee_paid_raw")
        if spent != intent.amount_raw or evidence.get("fee_mint") != SOL_MINT:
            raise OrderError("Unsupported input amount or network fee currency")
        balances = self._validate(conn, intent, fee)
        deltas = {intent.input_mint: -spent, intent.output_mint: received, NATIVE_SOL: -fee}
        for asset, delta in deltas.items():
            balance = raw(balances.get(asset, 0) + delta, "post-trade balance")
            conn.execute("""INSERT INTO execution_balances(mode,asset,raw_amount) VALUES(?,?,?)
                ON CONFLICT(mode,asset) DO UPDATE SET raw_amount=excluded.raw_amount""", (intent.mode, asset, str(balance)))
            conn.execute("INSERT INTO execution_asset_movements VALUES(?,?,?,?,?)",
                         (order["id"], intent.mode, asset, str(delta), str(balance)))
        realized = Fraction(0)
        if intent.side == "BUY":
            cursor = conn.execute("""INSERT INTO execution_positions
                (mode,mint,settlement_mint,strategy_version,entry_order_id,initial_raw,remaining_raw,
                 initial_spent_raw,remaining_cost_fraction,network_fees_lamports,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,'OPEN')""", (intent.mode, intent.output_mint, intent.input_mint,
                intent.strategy_version, order["id"], str(received), str(received), str(spent), str(spent), str(fee)))
            position_id = cursor.lastrowid
        else:
            position_id = intent.position_id
            position = conn.execute("SELECT * FROM execution_positions WHERE id=?", (position_id,)).fetchone()
            remaining = int(position["remaining_raw"]) - spent
            # Fractions conserve cost exactly even for enormous token counts
            # and repeated partial exits that have non-terminating decimals.
            remaining_cost = Fraction(int(position["initial_spent_raw"]) * remaining, int(position["initial_raw"]))
            released_cost = Fraction(position["remaining_cost_fraction"]) - remaining_cost
            realized = Fraction(received) - released_cost
            total_realized = Fraction(position["realized_settlement_fraction"]) + realized
            conn.execute("""UPDATE execution_positions SET remaining_raw=?,remaining_cost_fraction=?,
                realized_settlement_fraction=?,network_fees_lamports=?,status=? WHERE id=?""",
                (str(remaining), str(remaining_cost), str(total_realized),
                 str(int(position["network_fees_lamports"]) + fee), "CLOSED" if remaining == 0 else "OPEN", position_id))
        return {"position_id": position_id, "mode": intent.mode, "simulated": order["confirmed_simulated"],
                "asset_deltas_raw": {asset: str(value) for asset, value in deltas.items()},
                "realized_settlement_raw_fraction_before_network_fees": str(realized),
                "network_fee_lamports": fee, "net_pnl_usd": None,
                "valuation_status": "FX_AND_LIQUIDATION_EVIDENCE_REQUIRED"}

    def snapshot(self, mode="PAPER"):
        with self.store._connection() as conn:
            conn.execute("BEGIN")
            balances = self._balances(conn, mode)
            positions = [dict(p) for p in conn.execute("SELECT * FROM execution_positions WHERE mode=? ORDER BY id", (mode,))]
            total_fees = sum(int(p["network_fees_lamports"]) for p in positions)
            realized = sum((Fraction(p["realized_settlement_fraction"]) for p in positions), Fraction(0))
        return {"mode": mode, "balances_raw": balances, "positions": positions,
                "network_fees_lamports": total_fees,
                "realized_settlement_raw_fraction_before_network_fees": str(realized),
                "net_pnl_usd": None, "liquidation_equity_usd": None,
                "valuation_status": "FX_AND_LIQUIDATION_EVIDENCE_REQUIRED"}


def bind_paper_engine(ledger, provider, market_risk_check, *, policy=None, clock=None, sleep=None):
    """Connect quote execution to raw balances; require market/security risk too.

    No process is started. The caller owns decisions, initialization, and market
    risk evidence. Missing LIVE adapter cannot silently fall back to PAPER.
    """
    if not callable(market_risk_check):
        raise OrderError("Independent market/security risk checks are required")
    policy = policy or ExecutionPolicy()
    def combined_risk(intent, quote):
        portfolio = ledger.risk_check(intent, quote, estimated_fee_lamports=policy.estimated_network_fee_lamports)
        if portfolio["allowed"] is not True:
            return portfolio
        market = market_risk_check(intent, quote)
        if not isinstance(market, dict) or market.get("allowed") is not True:
            return market
        return {"allowed": True, "portfolio": portfolio, "market": market}
    options = {}
    if clock is not None:
        options["clock"] = clock
    if sleep is not None:
        options["sleep"] = sleep
    adapter = PaperExecutionAdapter(provider, apply_confirmation=ledger.apply_confirmation, **options)
    return ExecutionEngine(ledger.store, provider, {"PAPER": adapter}, combined_risk, policy=policy,
                           **({"clock": clock} if clock is not None else {}))
