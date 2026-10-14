"""Jupiter V2 /build -> real unsigned Solana v0 transaction, never broadcast.

The current API returns route_v2 bytes absent from the inspected published
Jupiter IDLs. Headers/accounts/fees are checked, but the binary route's spending
and min-output semantics are NOT yet fully verified. Signing stays forbidden.
This limitation is explicit on every result, not bypassed by a successful build.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import math
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from quote_provider import QuoteRequest, QuoteUnavailable, KeylessRateLimiter, _RejectRedirects, truststore
from transaction_reconciliation import integer, key
from telemetry import NETWORK_METRICS

ENDPOINT = "https://api.jup.ag/swap/v2/build"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM = "11111111111111111111111111111111"
COMPUTE = "ComputeBudget111111111111111111111111111111"


def associated_token_account(wallet, mint):
    address, _ = Pubkey.find_program_address([bytes(Pubkey.from_string(wallet)), bytes(Pubkey.from_string(TOKEN)),
                                             bytes(Pubkey.from_string(mint))], Pubkey.from_string(ATA))
    return str(address)


def _instruction(payload, wallet):
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list) or len(payload["accounts"]) > 128:
        raise ValueError("Malformed instruction accounts")
    program = Pubkey.from_string(key(payload.get("programId")))
    accounts = []
    for account in payload["accounts"]:
        if not isinstance(account, dict):
            raise ValueError("Malformed instruction account")
        address = key(account.get("pubkey"))
        if type(account.get("isSigner")) is not bool or type(account.get("isWritable")) is not bool:
            raise ValueError("Instruction flags must be explicit booleans")
        if account["isSigner"] and address != wallet:
            raise ValueError("Unexpected additional signer")
        accounts.append(AccountMeta(Pubkey.from_string(address), account["isSigner"], account["isWritable"]))
    try:
        data = base64.b64decode(payload["data"], validate=True)
    except Exception as exc:
        raise ValueError("Invalid instruction data") from exc
    if len(data) > 1232:
        raise ValueError("Instruction data exceeds transaction packet size")
    return Instruction(program, data, accounts)


@dataclass(frozen=True)
class UnsignedSwapBuild:
    wallet: str
    request: QuoteRequest
    transaction_base64: str
    message_sha256: str
    raw_response_json: str
    raw_response_sha256: str
    received_at: float
    last_valid_block_height: int
    compute_unit_limit: int
    compute_unit_price_micro_lamports: int
    maximum_priority_fee_lamports: int
    route_instruction_discriminator: str
    min_output_from_response: int
    signing_allowed: bool = field(default=False, init=False)
    binary_route_constraints_verified: bool = field(default=False, init=False)
    lookup_tables_verified_onchain: bool = field(default=False, init=False)

    def require_signable(self):
        raise ValueError("Signing blocked: current route binary constraints and on-chain lookup tables are not fully verified")

    def to_dict(self):
        return {"wallet": self.wallet, "message_sha256": self.message_sha256,
                "raw_response_sha256": self.raw_response_sha256, "received_at": self.received_at,
                "last_valid_block_height": self.last_valid_block_height, "compute_unit_limit": self.compute_unit_limit,
                "maximum_priority_fee_lamports": self.maximum_priority_fee_lamports,
                "route_instruction_discriminator": self.route_instruction_discriminator,
                "min_output_from_response": self.min_output_from_response,
                "signing_allowed": False, "binary_route_constraints_verified": False,
                "lookup_tables_verified_onchain": False, "actual_money_used": False,
                "transaction_bytes": len(base64.b64decode(self.transaction_base64)),
                "purpose": "Unsigned construction/simulation only; not a verified executable trade"}


def compile_unsigned_build(request, wallet, payload, *, received_at, now=None, compute_unit_limit=600000,
                           priority_fee_cap_lamports=100000):
    key(wallet)
    if not isinstance(request, QuoteRequest) or not isinstance(payload, dict):
        raise ValueError("A validated quote request and build response are required")
    now = time.time() if now is None else now
    if (isinstance(received_at, bool) or not isinstance(received_at, (float, int)) or
            not math.isfinite(received_at) or not 0 <= now-received_at <= request.max_age_ms/1000):
        raise ValueError("Build response is stale or timestamp invalid")
    if payload.get("error") or payload.get("errorCode") is not None:
        raise QuoteUnavailable("Jupiter rejected transaction build")
    if payload.get("inputMint") != request.input_mint or payload.get("outputMint") != request.output_mint:
        raise ValueError("Build mints differ from request")
    if integer(payload.get("inAmount"), "inAmount", 1, strings=True) != request.in_amount_raw or payload.get("swapMode") != "ExactIn":
        raise ValueError("Build amount or swap mode differs from intent")
    output = integer(payload.get("outAmount"), "outAmount", 1, strings=True)
    minimum = integer(payload.get("otherAmountThreshold"), "minimum output", 1, strings=True)
    slip = integer(payload.get("slippageBps"), "slippageBps", maximum=request.slippage_bps)
    if not output*(10000-slip)//10000 <= minimum <= output:
        raise ValueError("Build response minimum exceeds approved slippage")
    if integer(payload.get("transactionVersion"), "transactionVersion", maximum=0) != 0:
        raise ValueError("Only Solana v0 construction is supported")
    integer(compute_unit_limit, "compute_unit_limit", 1, 1400000)
    integer(priority_fee_cap_lamports, "priority fee cap")
    compute = payload.get("computeBudgetInstructions")
    if not isinstance(compute, list) or len(compute) != 1:
        raise ValueError("Expected exactly one provider compute-unit-price instruction")
    price_ix = _instruction(compute[0], wallet)
    if str(price_ix.program_id) != COMPUTE or price_ix.accounts or len(price_ix.data) != 9 or price_ix.data[0] != 3:
        raise ValueError("Unexpected compute-budget instruction")
    price = struct.unpack("<Q", price_ix.data[1:])[0]
    priority = (price*compute_unit_limit+999999)//1000000
    if priority > priority_fee_cap_lamports:
        raise ValueError("Maximum priority fee exceeds approved cap")
    setup = payload.get("setupInstructions")
    if not isinstance(setup, list) or len(setup) > 4:
        raise ValueError("Unsupported setup instruction count")
    instructions = [set_compute_unit_limit(compute_unit_limit), price_ix]
    for raw_ix in setup:
        ix = _instruction(raw_ix, wallet)
        if str(ix.program_id) != ATA or bytes(ix.data) != b"\x01" or len(ix.accounts) != 6:
            raise ValueError("Only idempotent classic-token ATA setup is currently supported")
        addresses = [str(account.pubkey) for account in ix.accounts]
        if (addresses[0] != wallet or addresses[2] != wallet or addresses[3] not in {request.input_mint, request.output_mint}
                or addresses[4:] != [SYSTEM, TOKEN] or addresses[1] != associated_token_account(wallet, addresses[3])):
            raise ValueError("ATA setup changes owner, mint, funding source, or destination")
        instructions.append(ix)
    if payload.get("otherInstructions") not in ([], None) or payload.get("tipInstruction") is not None or payload.get("cleanupInstruction") is not None:
        raise ValueError("Unapproved additional, tip, or unwrap/cleanup instruction")
    swap = _instruction(payload.get("swapInstruction"), wallet)
    if str(swap.program_id) != JUPITER or len(swap.data) < 8:
        raise ValueError("Unexpected swap program")
    instructions.append(swap)
    mappings = payload.get("addressesByLookupTableAddress")
    if not isinstance(mappings, dict) or len(mappings) > 32:
        raise ValueError("Malformed address lookup mappings")
    tables = []
    for address, values in mappings.items():
        if not isinstance(values, list) or len(values) > 256:
            raise ValueError("Invalid address lookup table length")
        tables.append(AddressLookupTableAccount(Pubkey.from_string(key(address)), [Pubkey.from_string(key(value)) for value in values]))
    block = payload.get("blockhashWithMetadata")
    if not isinstance(block, dict) or not isinstance(block.get("blockhash"), list) or len(block["blockhash"]) != 32:
        raise ValueError("Missing recent blockhash")
    blockhash = Hash(bytes(integer(byte, "blockhash byte", maximum=255) for byte in block["blockhash"]))
    height = integer(block.get("lastValidBlockHeight"), "last valid block height", 1)
    message = MessageV0.try_compile(Pubkey.from_string(wallet), instructions, tables, blockhash)
    if message.header.num_required_signatures != 1 or str(message.account_keys[0]) != wallet:
        raise ValueError("Compiled transaction has unexpected fee payer/signers")
    unsigned = VersionedTransaction.populate(message, [Signature.default()])
    wire = bytes(unsigned)
    if len(wire) > 1232:
        raise ValueError("Compiled transaction exceeds the v0 packet size")
    raw_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw_json) > 2*1024*1024:
        raise ValueError("Build response exceeds 2 MiB")
    return UnsignedSwapBuild(wallet, request, base64.b64encode(wire).decode(),
        hashlib.sha256(to_bytes_versioned(message)).hexdigest(), raw_json, hashlib.sha256(raw_json.encode()).hexdigest(),
        received_at, height, compute_unit_limit, price, priority, bytes(swap.data[:8]).hex(), minimum)


def _get_build(url, timeout):
    if urllib.parse.urlsplit(url)._replace(query="").geturl() != ENDPOINT:
        raise QuoteUnavailable("Unexpected build endpoint")
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if truststore else ssl.create_default_context()
    opener = urllib.request.build_opener(_RejectRedirects(), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(url, timeout=timeout) as response:
            encoded = response.read(2*1024*1024+1)
        if len(encoded) > 2*1024*1024:
            raise QuoteUnavailable("Build response exceeds 2 MiB")
        return json.loads(encoded)
    except urllib.error.HTTPError as exc:
        raise QuoteUnavailable(f"Jupiter build HTTP {exc.code}") from None


class JupiterBuildClient:
    def __init__(self, *, fetch=None, limiter=None, clock=time.time):
        self.fetch, self.limiter, self.clock = fetch or _get_build, limiter or KeylessRateLimiter(), clock

    def build_unsigned(self, request, wallet, **limits):
        key(wallet)
        if not isinstance(request, QuoteRequest) or "metis" in request.exclude_routers:
            raise ValueError("Build requires a Metis-compatible quote request")
        params = {"inputMint": request.input_mint, "outputMint": request.output_mint,
                  "amount": str(request.in_amount_raw), "slippageBps": str(request.slippage_bps),
                  "taker": wallet, "wrapAndUnwrapSol": "false", "platformFeeBps": "0",
                  "transactionVersion": "0", "maxAccounts": "48"}
        self.limiter.wait()
        started = time.perf_counter()
        try:
            payload = self.fetch(ENDPOINT+"?"+urllib.parse.urlencode(params), 8)
            elapsed = (time.perf_counter()-started)*1000
            if elapsed > request.max_age_ms:
                raise QuoteUnavailable("Build request exceeded freshness budget")
            received = self.clock()
            result = compile_unsigned_build(request, wallet, payload, received_at=received, now=self.clock(), **limits)
            NETWORK_METRICS.record("api.jup.ag", elapsed)
            return result
        except Exception as exc:
            NETWORK_METRICS.record("api.jup.ag", (time.perf_counter()-started)*1000, False, type(exc).__name__)
            if isinstance(exc, (ValueError, QuoteUnavailable)):
                raise
            raise QuoteUnavailable(f"Build unavailable: {type(exc).__name__}") from None
