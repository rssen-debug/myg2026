"""Reconcile exact prepared transaction bytes against confirmed RPC metadata.

Only classic SPL-to-SPL swaps with native SOL used for fees/account rent are
supported here. Wrapped SOL remains a token. Unexplained SOL transfers, changed
token ownership, missing metadata, or mismatched signatures stay UNCERTAIN.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json

from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from solana_rpc import RpcUnavailable

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
U64_MAX = 2**64-1


class ReconciliationUncertain(ValueError):
    pass


def integer(value, field, minimum=0, maximum=U64_MAX, strings=False):
    if strings and isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ReconciliationUncertain(f"Invalid {field}")
    return value


def key(value):
    try:
        return str(Pubkey.from_string(value))
    except Exception as exc:
        raise ReconciliationUncertain("Invalid account/mint public key") from exc


@dataclass(frozen=True)
class ExpectedSwap:
    signature: str
    message_sha256: str
    wallet: str
    input_mint: str
    output_mint: str
    input_raw: int
    minimum_output_raw: int
    input_decimals: int
    output_decimals: int
    max_fee_lamports: int
    max_rent_debit_lamports: int

    def __post_init__(self):
        try:
            Signature.from_string(self.signature)
        except Exception as exc:
            raise ReconciliationUncertain("Invalid expected transaction signature") from exc
        if (not isinstance(self.message_sha256, str) or len(self.message_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.message_sha256)):
            raise ReconciliationUncertain("Expected message SHA256 is required")
        for value in (self.wallet, self.input_mint, self.output_mint):
            key(value)
        if self.input_mint == self.output_mint:
            raise ReconciliationUncertain("Swap mints must differ")
        integer(self.input_raw, "input_raw", 1)
        integer(self.minimum_output_raw, "minimum_output_raw", 1)
        integer(self.input_decimals, "input_decimals", maximum=18)
        integer(self.output_decimals, "output_decimals", maximum=18)
        integer(self.max_fee_lamports, "max_fee_lamports")
        integer(self.max_rent_debit_lamports, "max_rent_debit_lamports")


def _token_balances(items, accounts):
    if not isinstance(items, list):
        raise ReconciliationUncertain("Token balance metadata is missing")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ReconciliationUncertain("Malformed token balance row")
        index = integer(item.get("accountIndex"), "accountIndex", maximum=len(accounts)-1)
        if index in result:
            raise ReconciliationUncertain("Duplicate token balance account index")
        mint, owner = key(item.get("mint")), key(item.get("owner"))
        if item.get("programId") != TOKEN_PROGRAM:
            raise ReconciliationUncertain("Missing or unsupported token program in balance metadata")
        amount = item.get("uiTokenAmount")
        if not isinstance(amount, dict):
            raise ReconciliationUncertain("Raw token quantity is missing")
        result[index] = {"mint": mint, "owner": owner,
                         "raw": integer(amount.get("amount"), "token amount", strings=True),
                         "decimals": integer(amount.get("decimals"), "token decimals", maximum=18)}
    return result


def reconcile_transaction(expected: ExpectedSwap, result, signature_status):
    """Pure validation; no network, portfolio mutation or guessed fills."""
    if not isinstance(signature_status, dict) or signature_status.get("confirmationStatus") not in {"confirmed", "finalized"}:
        raise ReconciliationUncertain("Signature is not confirmed/finalized")
    if not isinstance(result, dict) or not isinstance(result.get("meta"), dict):
        raise ReconciliationUncertain("Confirmed transaction metadata is unavailable")
    slot = integer(result.get("slot"), "transaction slot")
    if integer(signature_status.get("slot"), "status slot") != slot:
        raise ReconciliationUncertain("Signature status and transaction refer to different slots")
    encoded = result.get("transaction")
    if not isinstance(encoded, list) or len(encoded) != 2 or encoded[1] != "base64" or not isinstance(encoded[0], str):
        raise ReconciliationUncertain("Exact base64 transaction bytes are required")
    try:
        wire = base64.b64decode(encoded[0], validate=True)
        if not 0 < len(wire) <= 1232:
            raise ValueError("Unsupported transaction size")
        tx = VersionedTransaction.from_bytes(wire)
        tx.sanitize()
        if bytes(tx) != wire:
            raise ValueError("Noncanonical transaction encoding")
        if not tx.signatures or not all(tx.verify_with_results()):
            raise ValueError("Signature verification failed")
    except Exception as exc:
        raise ReconciliationUncertain("Transaction bytes/signatures cannot be verified") from exc
    if str(tx.signatures[0]) != expected.signature:
        raise ReconciliationUncertain("Transaction signature differs from prepared identity")
    message_bytes = to_bytes_versioned(tx.message)
    if hashlib.sha256(message_bytes).hexdigest() != expected.message_sha256:
        raise ReconciliationUncertain("Confirmed message differs from the prepared message")
    if tx.message.header.num_required_signatures != 1 or str(tx.message.account_keys[0]) != expected.wallet:
        raise ReconciliationUncertain("Expected wallet must be the sole signer and fee payer")
    meta = result["meta"]
    if "err" not in meta or "err" not in signature_status or meta["err"] != signature_status["err"]:
        raise ReconciliationUncertain("Transaction and signature error evidence disagree")
    accounts = [str(account) for account in tx.message.account_keys]
    if isinstance(tx.message, MessageV0):
        loaded = meta.get("loadedAddresses")
        if not isinstance(loaded, dict):
            raise ReconciliationUncertain("Address lookup metadata is missing")
        for field, attr in (("writable", "writable_indexes"), ("readonly", "readonly_indexes")):
            addresses = loaded.get(field)
            expected_count = sum(len(getattr(table, attr)) for table in tx.message.address_table_lookups)
            if not isinstance(addresses, list) or len(addresses) != expected_count:
                raise ReconciliationUncertain("Address lookup lengths disagree with the signed message")
            accounts.extend(key(address) for address in addresses)
    if len(set(accounts)) != len(accounts):
        raise ReconciliationUncertain("Ambiguous duplicated resolved accounts")
    pre_sol, post_sol = meta.get("preBalances"), meta.get("postBalances")
    if not isinstance(pre_sol, list) or not isinstance(post_sol, list) or len(pre_sol) != len(accounts) or len(post_sol) != len(accounts):
        raise ReconciliationUncertain("Native balance vector does not match message accounts")
    pre_sol = [integer(value, "preBalance") for value in pre_sol]
    post_sol = [integer(value, "postBalance") for value in post_sol]
    fee = integer(meta.get("fee"), "transaction fee")
    if fee > expected.max_fee_lamports:
        raise ReconciliationUncertain("Confirmed fee exceeds approved fee cap; manual reconciliation required")
    pre = _token_balances(meta.get("preTokenBalances"), accounts)
    post = _token_balances(meta.get("postTokenBalances"), accounts)
    deltas, rent_delta, owned_accounts = {}, 0, []
    expected_decimals = {expected.input_mint: expected.input_decimals, expected.output_mint: expected.output_decimals}
    for index in sorted(set(pre) | set(post)):
        before, after = pre.get(index), post.get(index)
        if before and after and (before["owner"], before["mint"], before["decimals"]) != (after["owner"], after["mint"], after["decimals"]):
            raise ReconciliationUncertain("Token account identity or authority changed")
        identity = after or before
        if identity["owner"] != expected.wallet:
            continue
        mint = identity["mint"]
        if mint in expected_decimals and identity["decimals"] != expected_decimals[mint]:
            raise ReconciliationUncertain("Confirmed token decimals differ from approved metadata")
        change = (after["raw"] if after else 0)-(before["raw"] if before else 0)
        deltas[mint] = deltas.get(mint, 0)+change
        # Wrapped SOL's token amount is backed by lamports in its token
        # account. A swap changing that backing is not an account-rent fee.
        backing_delta = change if mint == "So11111111111111111111111111111111111111112" else 0
        rent_delta += post_sol[index]-pre_sol[index]-backing_delta
        owned_accounts.append({"address": accounts[index], "mint": mint, "delta_raw": change,
                               "lamports_delta": post_sol[index]-pre_sol[index]})
    if any(amount != 0 for mint, amount in deltas.items() if mint not in expected_decimals):
        raise ReconciliationUncertain("Unexpected wallet token changed in the transaction")
    native_delta = post_sol[0]-pre_sol[0]
    if native_delta + rent_delta + fee != 0:
        raise ReconciliationUncertain("Native SOL movement is not explained by fee and owned token-account rent")
    if rent_delta > expected.max_rent_debit_lamports:
        raise ReconciliationUncertain("Account-rent debit exceeds approved cap")
    error = meta["err"]
    if error is not None:
        if any(deltas.values()) or rent_delta != 0:
            raise ReconciliationUncertain("Failed transaction contains unexpected asset changes")
        state, spent, received = "FAILED", 0, 0
    else:
        spent, received = -deltas.get(expected.input_mint, 0), deltas.get(expected.output_mint, 0)
        if spent != expected.input_raw or received < expected.minimum_output_raw:
            raise ReconciliationUncertain("Confirmed input/minimum-output constraints are not satisfied")
        state = "CONFIRMED"
    evidence = {"execution": "LIVE", "simulated": False, "actual_money_used": True,
                "transaction_id": expected.signature, "signed_message_hash": expected.message_sha256,
                "confirmation_status": signature_status["confirmationStatus"], "reconciled": True,
                "success": error is None, "onchain_error": error, "slot": slot,
                "input_spent_raw": spent, "output_received_raw": received,
                "fee_paid_raw": fee, "fee_mint": "So11111111111111111111111111111111111111112",
                "native_balance_delta_raw": native_delta, "token_account_rent_delta_raw": rent_delta,
                "owned_token_account_changes": owned_accounts,
                "rent_is_separate_from_network_fee": True,
                "transaction_response_sha256": hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
    if error is not None:
        evidence["reason"] = "Confirmed on-chain failure; network fee paid without swap fill"
    return {"state": state, "evidence": evidence}


def fetch_reconciliation(rpc, expected, *, expected_genesis_hash):
    """A timeout/missing transaction is never converted to an expired/failed fill."""
    try:
        rpc.require_cluster(expected_genesis_hash)
        status = rpc.get_signature_status(expected.signature)
        if not isinstance(status, dict) or status.get("confirmationStatus") not in {"confirmed", "finalized"}:
            raise ReconciliationUncertain("Signature status is absent or not yet confirmed")
        result = rpc.get_transaction(expected.signature)
        return reconcile_transaction(expected, result, status)
    except (RpcUnavailable, ReconciliationUncertain) as exc:
        return {"state": "UNCERTAIN", "evidence": {"transaction_id": expected.signature,
                "reason": str(exc), "reconciled": False, "retry_must_not_resubmit": True}}
