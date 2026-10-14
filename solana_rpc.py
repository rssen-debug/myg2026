"""Bounded, TLS-verified Solana reads and simulation; no broadcast API.

RPC acceptance and on-chain confirmation are different events. This transport
cannot sendTransaction, requestAirdrop, sign, or discover private credentials.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from telemetry import NETWORK_METRICS

try:
    import truststore
except ImportError:
    truststore = None

READ_METHODS = frozenset({"getGenesisHash", "getSlot", "getBlockHeight", "getLatestBlockhash",
    "getSignatureStatuses", "getTransaction", "getBalance", "getTokenAccountsByOwner",
    "getAccountInfo", "getMultipleAccounts", "getMinimumBalanceForRentExemption",
    "getFeeForMessage", "simulateTransaction", "isBlockhashValid", "getSignaturesForAddress"})
MAX_BYTES = 8*1024*1024


class RpcUnavailable(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RpcUnavailable("RPC redirect rejected")


class SolanaReadRpc:
    def __init__(self, endpoint="https://api.mainnet-beta.solana.com", *, timeout=8, transport=None):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("RPC requires an explicit HTTPS endpoint without embedded credentials/fragment")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ValueError("RPC timeout must be positive and at most 30 seconds")
        self.endpoint, self.timeout, self.host = endpoint, timeout, parsed.hostname
        self._transport = transport or self._http

    def _http(self, body):
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if truststore else ssl.create_default_context()
        opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(self.endpoint, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "godlike-read-rpc/1.0"}, method="POST")
        try:
            with opener.open(request, timeout=self.timeout) as response:
                payload = response.read(MAX_BYTES+1)
            if len(payload) > MAX_BYTES:
                raise RpcUnavailable("RPC response exceeds 8 MiB")
            return json.loads(payload)
        except urllib.error.HTTPError as exc:
            # Do not include endpoint query/API credentials or provider bodies.
            raise RpcUnavailable(f"RPC HTTP {exc.code}") from None
        except (ValueError, OSError) as exc:
            raise RpcUnavailable(f"RPC transport failure: {type(exc).__name__}") from None

    def call(self, method, params=None):
        if method not in READ_METHODS:
            raise ValueError("Only documented reads and non-broadcast simulation are permitted")
        if params is None:
            params = []
        if not isinstance(params, list):
            raise ValueError("RPC params must be a list")
        request_id = uuid.uuid4().hex
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, allow_nan=False).encode()
        if len(body) > 1024*1024:
            raise ValueError("RPC request exceeds 1 MiB")
        started = time.perf_counter()
        try:
            response = self._transport(body)
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
                raise RpcUnavailable("RPC response identity mismatch")
            if response.get("error") is not None:
                code = response["error"].get("code") if isinstance(response["error"], dict) else None
                raise RpcUnavailable(f"RPC method failed; code={code if isinstance(code, int) else 'unknown'}")
            if "result" not in response:
                raise RpcUnavailable("RPC result is missing")
            NETWORK_METRICS.record(self.host, (time.perf_counter()-started)*1000)
            return response["result"]
        except Exception as exc:
            NETWORK_METRICS.record(self.host, (time.perf_counter()-started)*1000, False, type(exc).__name__)
            if isinstance(exc, RpcUnavailable):
                raise
            raise RpcUnavailable(f"RPC failed: {type(exc).__name__}") from None

    def require_cluster(self, expected_genesis_hash):
        if not isinstance(expected_genesis_hash, str) or not expected_genesis_hash:
            raise ValueError("An independently configured genesis hash is required")
        observed = self.call("getGenesisHash")
        if observed != expected_genesis_hash:
            raise RpcUnavailable("RPC cluster differs from the configured genesis hash")
        return observed

    def get_transaction(self, signature):
        return self.call("getTransaction", [signature, {"encoding": "base64", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])

    def get_signature_status(self, signature):
        result = self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        if not isinstance(result, dict) or not isinstance(result.get("value"), list) or len(result["value"]) != 1:
            raise RpcUnavailable("Malformed signature status response")
        return result["value"][0]

    def simulate_unsigned(self, transaction_base64):
        return self.call("simulateTransaction", [transaction_base64,
            {"encoding": "base64", "commitment": "confirmed", "sigVerify": False,
             "replaceRecentBlockhash": True, "innerInstructions": True}])
