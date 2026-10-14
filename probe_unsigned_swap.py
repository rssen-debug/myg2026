"""Bounded public build/simulation diagnostic. No signing or broadcast path.

Creates a random unfunded public test identity, requests one USDC->WSOL build,
and asks the RPC to simulate the UNSIGNED bytes. AccountNotFound is expected
for an unfunded identity; it proves no successful trade or funded simulation.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from pathlib import Path

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from quote_provider import QuoteRequest
from solana_rpc import SolanaReadRpc
from swap_build import JupiterBuildClient

# Official Solana network identity, FULL getGenesisHash value, not CAIP-2:
# https://solana.com/docs/payments/agentic-payments/x402
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"


def probe(*, rpc=None, builder=None):
    rpc, builder = rpc or SolanaReadRpc(), builder or JupiterBuildClient()
    # Secret is never exported, retained, printed, saved or used for signing.
    wallet = str(Keypair().pubkey())
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "purpose": "Read-only unsigned build/simulation diagnostic, not a trade",
              "actual_money_used": False, "broadcast_attempts": 0, "signing_attempts": 0,
              "user_wallet_loaded": False, "temporary_public_key": wallet,
              "funded_swap_verified": False, "live_ready": False, "checks": {}}
    checks = report["checks"]
    try:
        checks["genesis_hash"] = rpc.require_cluster(MAINNET_GENESIS)
        balance = rpc.call("getBalance", [wallet, {"commitment": "confirmed"}])
        if not isinstance(balance, dict) or type(balance.get("value")) is not int or balance["value"] != 0:
            raise ValueError("New public test identity is not verified unfunded")
        checks["native_balance_lamports"] = balance["value"]
        build = builder.build_unsigned(QuoteRequest(USDC, SOL, 1000000), wallet)
        checks["build"] = build.to_dict()
        # Public, unsigned audit evidence only; no private/signed material.
        checks["build_response"] = json.loads(build.raw_response_json)
        checks["unsigned_transaction_base64"] = build.transaction_base64
        tx = VersionedTransaction.from_bytes(base64.b64decode(build.transaction_base64, validate=True))
        checks["fee_for_message"] = rpc.call("getFeeForMessage", [base64.b64encode(to_bytes_versioned(tx.message)).decode(),
                                                        {"commitment": "confirmed"}])
        simulation = rpc.simulate_unsigned(build.transaction_base64)
        if not isinstance(simulation, dict) or not isinstance(simulation.get("value"), dict) or "err" not in simulation["value"]:
            raise ValueError("RPC did not return simulation outcome metadata")
        checks["unsigned_simulation"] = simulation
        report["transport_probe_completed"] = True
        report["simulation_success"] = simulation["value"]["err"] is None
        report["interpretation"] = ("Unsigned RPC simulation responded; unfunded-wallet errors are expected. "
                                     "No funded execution, sellability, inclusion or profitability established.")
    except Exception as exc:
        report["transport_probe_completed"] = False
        # Never expose endpoint credentials or opaque provider response bodies.
        report["error_type"] = type(exc).__name__
        if type(exc).__name__ in {"RpcUnavailable", "QuoteUnavailable", "QuoteValidationError"}:
            report["error"] = str(exc)
        report["interpretation"] = "Diagnostic incomplete; failed stages are not successful execution evidence."
    return report


if __name__ == "__main__":
    result = probe()
    path = Path(__file__).parent/"execution_research"/"unsigned_build_rpc_smoke.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"report": str(path), "completed": result["transport_probe_completed"],
        "simulation_success": result.get("simulation_success"), "actual_money_used": False,
        "interpretation": result["interpretation"]}, indent=2))
