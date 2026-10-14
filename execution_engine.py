"""Shared intent/risk/quote orchestration and a quote-based paper adapter.

The main bot connects through quote_broker with atomic raw/strategy accounting;
execution_ledger.bind_paper_engine also supports isolated ledger tests. A paper
CONFIRMED state is explicitly a model outcome, never an on-chain confirmation.
Live adapters must implement this same protocol; no live adapter is silently
substituted with paper execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import time
from typing import Protocol

from order_lifecycle import OrderIntent, OrderStore, OrderExpired, ConcurrentOrderUpdate
from quote_provider import QuoteRequest, QuoteEvidence, QuoteUnavailable
from telemetry import Telemetry

SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True)
class ExecutionPolicy:
    max_price_impact_pct: Decimal = Decimal("2.5")
    quote_max_age_ms: int = 3000
    paper_delay_ms: int = 500
    paper_adverse_bps: int = 50
    estimated_network_fee_lamports: int = 105000
    # RFQ quote-only responses have no reserved taker transaction. Restrict
    # this model to Metis routes until RFQ execution has been calibrated.
    exclude_routers: tuple[str, ...] = ("jupiterz", "dflow", "okx")

    def __post_init__(self):
        if not isinstance(self.max_price_impact_pct, Decimal) or not self.max_price_impact_pct.is_finite() or not 0 < self.max_price_impact_pct <= 100:
            raise ValueError("A finite positive Decimal price-impact limit is required")
        for key, maximum in (("quote_max_age_ms", 30000), ("paper_delay_ms", 30000),
                             ("paper_adverse_bps", 9999), ("estimated_network_fee_lamports", 2**64-1)):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"Invalid execution policy {key}")
        if self.quote_max_age_ms == 0:
            raise ValueError("Quote age budget must be positive")


class ExecutionAdapter(Protocol):
    mode: str

    def execute(self, order: dict, quote: QuoteEvidence, store: OrderStore,
                policy: ExecutionPolicy, *, risk_check) -> dict: ...


def validate_execution_quote(quote, policy, now):
    quote.assert_fresh(now)
    if quote.price_impact_pct is None:
        raise QuoteUnavailable("Price impact is unknown")
    # V2 signed impacts can be negative when output is below reference value.
    if abs(quote.price_impact_pct) > policy.max_price_impact_pct:
        raise QuoteUnavailable("Quote exceeds maximum absolute price impact")


class PaperExecutionAdapter:
    """Requote after measured delay; reject missing routes or a breached limit.

    Network fees remain an explicit model assumption. Router fees are already
    in the output quote and are not subtracted twice. No random fill-success
    rate is fabricated; unmodelled inclusion risk remains recorded uncertainty.
    """
    mode = "PAPER"

    def __init__(self, provider, *, clock=time.time, sleep=time.sleep, apply_confirmation=None):
        self.provider, self.clock, self.sleep = provider, clock, sleep
        self.apply_confirmation = apply_confirmation

    def execute(self, order, quote, store, policy, *, risk_check):
        if order["mode"] != self.mode:
            raise ValueError("Paper adapter cannot execute a LIVE intent")
        self.sleep(policy.paper_delay_ms / 1000)
        validate_execution_quote(quote, policy, self.clock())
        delayed = self.provider.quote(quote.request)
        if delayed.request != quote.request:
            raise QuoteUnavailable("Delayed quote belongs to another request")
        validate_execution_quote(quote, policy, self.clock())
        validate_execution_quote(delayed, policy, self.clock())
        # Preserve the common route constraint, rather than silently changing
        # the venue after risk approval of the first route.
        if delayed.router != quote.router or set(delayed.amm_keys) != set(quote.amm_keys):
            raise QuoteUnavailable("Route changed during simulated propagation")
        output = delayed.out_amount_raw * (10000-policy.paper_adverse_bps) // 10000
        if output < quote.min_out_amount_raw:
            raise QuoteUnavailable("Delayed adverse execution would breach the original minimum output")
        # Liquidity/security/circuit-breaker state may change during delay or
        # provider throttling. Recheck before committing the modeled outcome.
        final_risk = risk_check(delayed)
        simulation = {"success": True, "execution": "PAPER", "actual_money_used": False,
                      "model": "delayed_requote_plus_adverse_margin_v1",
                      "delay_ms_assumption": policy.paper_delay_ms,
                      "adverse_bps_assumption": policy.paper_adverse_bps,
                      "delayed_quote": delayed.to_dict(), "output_received_raw": output,
                      "risk_before_confirmation": final_risk,
                      "inclusion_and_failure_rates_calibrated": False}
        order = store.transition(order["id"], "SIMULATED", expected_version=order["version"], evidence=simulation)
        return store.transition(order["id"], "CONFIRMED", expected_version=order["version"], apply_confirmation=self.apply_confirmation, evidence={
            "execution": "PAPER", "simulated": True, "actual_money_used": False,
            "input_spent_raw": quote.in_amount_raw, "output_received_raw": output,
            "fee_paid_raw": policy.estimated_network_fee_lamports, "fee_mint": SOL_MINT,
            "network_fee_is_assumption": True, "router_fees_already_in_output": True,
            "initial_quote_hash": quote.payload_sha256, "delayed_quote_hash": delayed.payload_sha256,
            "risk_before_confirmation": final_risk,
            "calibrated": False, "uncertainty": list(quote.uncertainty)})


class ExecutionEngine:
    """Same policy and quote path for both modes; adapter chooses execution.

    An independent risk callback is mandatory. It runs before external quote
    acquisition and again before execution, so a strategy approval cannot
    silently bypass a changed balance, exposure or circuit breaker.
    """
    def __init__(self, store, provider, adapters, risk_check, *, policy=None, clock=time.time):
        if not callable(risk_check):
            raise ValueError("An independent risk checker is required")
        self.store, self.provider = store, provider
        self.adapters = dict(adapters)
        self.risk_check = risk_check
        self.policy, self.clock = policy or ExecutionPolicy(), clock
        self.metrics = Telemetry()

    def quote_request(self, intent):
        return QuoteRequest(intent.input_mint, intent.output_mint, intent.amount_raw,
                            intent.slippage_bps, self.policy.quote_max_age_ms, self.policy.exclude_routers)

    def _risk(self, intent, quote=None):
        decision = self.risk_check(intent, quote)
        if not isinstance(decision, dict) or decision.get("allowed") is not True:
            reason = decision.get("reason", "No independent risk approval") if isinstance(decision, dict) else "No independent risk approval"
            raise QuoteUnavailable(str(reason))
        return decision

    def execute(self, intent: OrderIntent):
        order = self.store.create(intent)
        if order["state"] != "CREATED":
            # Same idempotency key never produces another quote or transaction.
            # A crashed in-flight intent requires explicit recovery/reconciliation.
            return order
        try:
            # Claim before any external work. Competing callers must not quote,
            # execute, or mark the winning caller's in-flight order as failed.
            order = self.store.transition(order["id"], "VALIDATING", expected_version=order["version"],
                                          evidence={"executor_claimed": True})
        except ConcurrentOrderUpdate:
            return self.store.get(order["id"])
        started = time.perf_counter()
        try:
            adapter = self.adapters.get(intent.mode)
            if adapter is None or adapter.mode != intent.mode:
                raise QuoteUnavailable(f"No enabled {intent.mode} execution adapter")
            before = self._risk(intent)
            request = self.quote_request(intent)
            quote = self.provider.quote(request)
            if quote.request != request:
                raise QuoteUnavailable("Provider returned a quote for another intent")
            validate_execution_quote(quote, self.policy, self.clock())
            expiry = min(quote.expires_at or float("inf"), quote.received_at+self.policy.quote_max_age_ms/1000)
            order = self.store.quote(order["id"], {"quote": quote.to_dict(), "risk_before_quote": before},
                                     expected_version=order["version"], expires_at=expiry,
                                     last_valid_block_height=quote.last_valid_block_height)
            self._risk(intent, quote)
            result = adapter.execute(order, quote, self.store, self.policy,
                                     risk_check=lambda current_quote: self._risk(intent, current_quote))
            self.metrics.record("intent_to_execution_result", (time.perf_counter()-started)*1000)
            return result
        except OrderExpired as exc:
            self.metrics.record("intent_to_execution_result", (time.perf_counter()-started)*1000, False, str(exc))
            return exc.order
        except Exception as exc:
            self.metrics.record("intent_to_execution_result", (time.perf_counter()-started)*1000, False, type(exc).__name__)
            current = self.store.get(order["id"])
            if current["state"] in {"CREATED", "VALIDATING", "QUOTED", "SIMULATED", "PREPARED"}:
                return self.store.transition(order["id"], "FAILED", expected_version=current["version"],
                                             evidence={"reason": str(exc)[:900] or type(exc).__name__,
                                                       "exception_type": type(exc).__name__, "submitted": False})
            if current["state"] in {"SUBMITTING", "SUBMITTED"}:
                return self.store.transition(order["id"], "UNCERTAIN", expected_version=current["version"],
                                             evidence={"reason": "Executor exception after possible broadcast",
                                                       "exception_type": type(exc).__name__})
            return current
