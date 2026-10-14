"""Read-only Jupiter Swap API V2 quotes with immutable audit evidence.

No taker, wallet, key, signer, transaction builder, or submission method exists
here. A route quote is not a fill, a wallet simulation, or a landing guarantee.
Official schema: https://developers.jup.ag/docs/openapi-spec/swap/v2/swap.yaml
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from telemetry import NETWORK_METRICS

try:
    import truststore
except ImportError:
    truststore = None

ENDPOINT = "https://api.jup.ag/swap/v2/order"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ROUTERS = frozenset({"metis", "jupiterz", "dflow", "okx"})
U64_MAX = 2**64 - 1
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class QuoteUnavailable(RuntimeError):
    """No usable current quote. Callers must not substitute a fabricated fill."""


class QuoteValidationError(QuoteUnavailable):
    pass


def _pubkey(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        return False
    number = 0
    for char in value:
        if char not in BASE58:
            return False
        number = number * 58 + BASE58.index(char)
    return (number.bit_length() + 7) // 8 + len(value) - len(value.lstrip("1")) == 32


def _integer(value, name, minimum=0, maximum=U64_MAX, allow_string=True):
    if allow_string and isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise QuoteValidationError(f"Invalid {name}")
    return value


def _decimal(value, name):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise QuoteValidationError(f"Invalid {name}") from exc


def _optional_integer(payload, name, maximum=U64_MAX):
    value = payload.get(name)
    return None if value is None else _integer(value, name, maximum=maximum)


@dataclass(frozen=True)
class QuoteRequest:
    input_mint: str
    output_mint: str
    in_amount_raw: int
    slippage_bps: int = 100
    max_age_ms: int = 3000
    exclude_routers: tuple[str, ...] = ()

    def __post_init__(self):
        if not _pubkey(self.input_mint) or not _pubkey(self.output_mint) or self.input_mint == self.output_mint:
            raise QuoteValidationError("Distinct valid Solana input/output mint addresses required")
        _integer(self.in_amount_raw, "in_amount_raw", minimum=1, allow_string=False)
        _integer(self.slippage_bps, "slippage_bps", maximum=9999, allow_string=False)
        _integer(self.max_age_ms, "max_age_ms", minimum=1, maximum=30000, allow_string=False)
        if (not isinstance(self.exclude_routers, tuple) or len(set(self.exclude_routers)) != len(self.exclude_routers)
                or any(router not in ROUTERS for router in self.exclude_routers)):
            raise QuoteValidationError("exclude_routers must be a tuple of supported, unique router names")


@dataclass(frozen=True)
class QuoteFees:
    # These amounts describe the provider quote. Platform/route costs are
    # already reflected in quoted amounts; do not subtract them again.
    fee_bps: int | None
    fee_mint: str | None
    platform_fee_bps: int | None
    platform_fee_raw: int | None
    signature_fee_lamports: int | None
    prioritization_fee_lamports: int | None
    rent_fee_lamports: int | None
    platform_fees_in_quote: bool = True
    priority_includes_tips: bool = True
    network_fee_estimate_complete: bool = False


@dataclass(frozen=True)
class QuoteEvidence:
    request: QuoteRequest
    provider: str
    endpoint: str
    request_id: str
    quote_id: str | None
    router: str
    in_amount_raw: int
    out_amount_raw: int
    min_out_amount_raw: int
    slippage_bps: int
    price_impact_pct: Decimal | None
    fees: QuoteFees
    amm_keys: tuple[str, ...]
    request_started_at: float
    received_at: float
    latency_ms: float
    expires_at: float | None
    last_valid_block_height: int | None
    context_slot: int | None
    route_json: str
    raw_json: str
    payload_sha256: str
    execution_ready: bool = False
    uncertainty: tuple[str, ...] = (
        "Quote-only; wallet balance, account setup and transaction simulation unverified",
        "No signing, submission, confirmation or realized profit",
        "No live calibration; zero quoted gas does not prove a free executable trade",
    )

    def assert_fresh(self, now=None):
        now = time.time() if now is None else now
        if (isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now)
                or not 0 <= (now - self.received_at) * 1000 <= self.request.max_age_ms):
            raise QuoteValidationError("Quote is stale or clock moved backwards")
        if self.expires_at is not None and now >= self.expires_at:
            raise QuoteValidationError("Provider quote has expired")
        if hashlib.sha256(self.raw_json.encode("utf-8")).hexdigest() != self.payload_sha256:
            raise QuoteValidationError("Quote evidence integrity mismatch")
        # Frozen dataclasses can still be reconstructed/replaced by callers.
        # Bind every interpreted amount, route and constraint to the saved
        # provider response, not only the hash of an unrelated JSON string.
        restored = validate_quote(self.request, json.loads(self.raw_json),
                                  request_started_at=self.request_started_at,
                                  received_at=self.received_at, latency_ms=self.latency_ms)
        if self != restored:
            raise QuoteValidationError("Interpreted quote fields differ from saved provider evidence")
        return self

    def to_dict(self):
        result = asdict(self)
        result["price_impact_pct"] = None if self.price_impact_pct is None else str(self.price_impact_pct)
        result["response"] = json.loads(result.pop("raw_json"))
        result["route_plan"] = json.loads(result.pop("route_json"))
        result["received_at_utc"] = datetime.fromtimestamp(self.received_at, timezone.utc).isoformat()
        return result


def validate_quote(request, payload, *, request_started_at, received_at, latency_ms):
    if not isinstance(request, QuoteRequest) or not isinstance(payload, dict):
        raise QuoteValidationError("Quote request and JSON object required")
    if payload.get("error") or payload.get("errorCode") is not None or payload.get("errorMessage"):
        raise QuoteUnavailable("Provider rejected quote or transaction construction")
    if payload.get("inputMint") != request.input_mint or payload.get("outputMint") != request.output_mint:
        raise QuoteValidationError("Quote mint identity does not match request")
    if payload.get("swapMode") != "ExactIn":
        raise QuoteValidationError("Only ExactIn quotes supported")
    amount = _integer(payload.get("inAmount"), "inAmount", minimum=1)
    if amount != request.in_amount_raw:
        raise QuoteValidationError("Quote input amount does not match request")
    output = _integer(payload.get("outAmount"), "outAmount", minimum=1)
    minimum = _integer(payload.get("otherAmountThreshold"), "otherAmountThreshold", minimum=1)
    slip = _integer(payload.get("slippageBps"), "slippageBps", maximum=request.slippage_bps)
    permitted_minimum = output * (10000 - request.slippage_bps) // 10000
    if not permitted_minimum <= minimum <= output:
        raise QuoteValidationError("Quoted minimum output exceeds permitted slippage")
    router = payload.get("router")
    if router not in ROUTERS or router in request.exclude_routers:
        raise QuoteValidationError("Unknown or excluded router")
    if payload.get("taker") is not None or payload.get("transaction") not in (None, ""):
        raise QuoteValidationError("Unexpected wallet or transaction in quote-only response")
    request_id = payload.get("requestId")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
        raise QuoteValidationError("Missing valid requestId")
    quote_id = payload.get("quoteId")
    if quote_id is not None and (not isinstance(quote_id, str) or not quote_id or len(quote_id) > 256):
        raise QuoteValidationError("Invalid quoteId")
    route = payload.get("routePlan")
    if not isinstance(route, list) or not route or len(route) > 100:
        raise QuoteValidationError("Missing or oversized route plan")
    amm_keys, edges = [], []
    for step in route:
        if not isinstance(step, dict) or not isinstance(step.get("swapInfo"), dict):
            raise QuoteValidationError("Malformed route step")
        info = step["swapInfo"]
        if any(not _pubkey(info.get(field)) for field in ("ammKey", "inputMint", "outputMint")):
            raise QuoteValidationError("Invalid route token or pool identity")
        _integer(info.get("inAmount"), "route inAmount", minimum=1)
        _integer(info.get("outAmount"), "route outAmount", minimum=1)
        if "bps" in step:
            _integer(step["bps"], "route bps", minimum=1, maximum=10000)
        elif "percent" in step:
            percent = _decimal(step["percent"], "route percent")
            if not 0 < percent <= 100:
                raise QuoteValidationError("Invalid route percent")
        else:
            raise QuoteValidationError("Missing route allocation")
        amm_keys.append(info["ammKey"])
        edges.append((info["inputMint"], info["outputMint"]))
    reachable = {request.input_mint}
    for _ in range(len(edges)):
        reachable.update(target for source, target in edges if source in reachable)
    if request.output_mint not in reachable:
        raise QuoteValidationError("Route does not connect input to output mint")
    reaches_output = {request.output_mint}
    for _ in range(len(edges)):
        reaches_output.update(source for source, target in edges if target in reaches_output)
    if any(source == target or source not in reachable or target not in reaches_output for source, target in edges):
        raise QuoteValidationError("Route contains a disconnected or dead-end step")
    for name, value in (("request_started_at", request_started_at), ("received_at", received_at), ("latency_ms", latency_ms)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise QuoteValidationError(f"Invalid {name}")
    if (received_at < request_started_at or latency_ms > request.max_age_ms or
            (received_at-request_started_at)*1000 > request.max_age_ms):
        raise QuoteValidationError("Quote request exceeded freshness budget or clock moved backwards")
    expiry = payload.get("expireAt")
    if expiry is not None:
        try:
            parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone missing")
            expiry = parsed.timestamp()
        except (AttributeError, ValueError, TypeError, OverflowError) as exc:
            raise QuoteValidationError("Invalid provider expiry timestamp") from exc
        if expiry <= received_at:
            raise QuoteValidationError("Provider quote already expired")
    platform = payload.get("platformFee")
    if platform is None:
        platform = {}
    if not isinstance(platform, dict):
        raise QuoteValidationError("Invalid platform fee object")
    fee_mint = payload.get("feeMint")
    if fee_mint is not None and not _pubkey(fee_mint):
        raise QuoteValidationError("Invalid fee mint")
    fees = QuoteFees(
        fee_bps=_optional_integer(payload, "feeBps", 10000), fee_mint=fee_mint,
        platform_fee_bps=_optional_integer(platform, "feeBps", 10000),
        platform_fee_raw=_optional_integer(platform, "amount"),
        signature_fee_lamports=_optional_integer(payload, "signatureFeeLamports"),
        prioritization_fee_lamports=_optional_integer(payload, "prioritizationFeeLamports"),
        rent_fee_lamports=_optional_integer(payload, "rentFeeLamports"))
    impact = None
    if payload.get("priceImpact") is not None:
        impact = _decimal(payload["priceImpact"], "priceImpact")
    elif payload.get("priceImpactPct") is not None:
        impact = _decimal(payload["priceImpactPct"], "priceImpactPct") * 100
    try:
        raw_json = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise QuoteValidationError("Non-JSON or nonfinite quote evidence") from exc
    if len(raw_json.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise QuoteValidationError("Quote evidence exceeds size limit")
    return QuoteEvidence(
        request=request, provider="jupiter_swap_v2", endpoint=ENDPOINT,
        request_id=request_id, quote_id=quote_id, router=router,
        in_amount_raw=amount, out_amount_raw=output, min_out_amount_raw=minimum,
        slippage_bps=slip, price_impact_pct=impact, fees=fees,
        amm_keys=tuple(dict.fromkeys(amm_keys)), request_started_at=request_started_at,
        received_at=received_at, latency_ms=latency_ms, expires_at=expiry,
        last_valid_block_height=_optional_integer(payload, "lastValidBlockHeight"),
        context_slot=_optional_integer(payload, "contextSlot"),
        route_json=json.dumps(route, sort_keys=True, separators=(",", ":"), allow_nan=False),
        raw_json=raw_json, payload_sha256=hashlib.sha256(raw_json.encode("utf-8")).hexdigest())


class KeylessRateLimiter:
    """At most 30 attempts per rolling minute, evenly spaced, thread safe."""
    def __init__(self, monotonic=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = monotonic, sleep
        self._times = deque()
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            while True:
                now = self.clock()
                while self._times and now - self._times[0] >= 60:
                    self._times.popleft()
                delay = max(0, self._times[-1] + 2.05 - now) if self._times else 0
                if len(self._times) >= 30:
                    delay = max(delay, self._times[0] + 60.01 - now)
                if delay <= 0:
                    self._times.append(now)
                    return
                self.sleep(delay)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise QuoteUnavailable("Jupiter redirect rejected")


def _fetch_order(url, timeout):
    if urllib.parse.urlsplit(url)._replace(query="").geturl() != ENDPOINT:
        raise QuoteUnavailable("Unexpected quote endpoint")
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if truststore is not None else ssl.create_default_context()
    opener = urllib.request.build_opener(_RejectRedirects(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "godlike-paper-quote/1.0"}, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise QuoteUnavailable(f"Jupiter HTTP {response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise QuoteUnavailable("Jupiter response exceeds size limit")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise QuoteUnavailable(f"Jupiter HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise QuoteUnavailable(f"Jupiter quote unavailable ({type(exc).__name__})") from exc


class JupiterV2QuoteProvider:
    """Only GET /order without taker. No automatic AMM fallback on failure."""
    def __init__(self, *, fetch=None, limiter=None, timeout_seconds=6,
                 clock=time.time, monotonic=time.monotonic):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise QuoteValidationError("timeout_seconds must be positive and at most 30")
        self.fetch = fetch or _fetch_order
        self.limiter = limiter or KeylessRateLimiter()
        self.timeout = timeout_seconds
        self.clock, self.monotonic = clock, monotonic

    def quote(self, request: QuoteRequest) -> QuoteEvidence:
        if not isinstance(request, QuoteRequest):
            raise QuoteValidationError("QuoteRequest required")
        params = {"inputMint": request.input_mint, "outputMint": request.output_mint,
                  "amount": str(request.in_amount_raw), "slippageBps": str(request.slippage_bps)}
        if request.exclude_routers:
            params["excludeRouters"] = ",".join(request.exclude_routers)
        self.limiter.wait()
        started_at, started_mono = self.clock(), self.monotonic()
        try:
            payload = self.fetch(ENDPOINT + "?" + urllib.parse.urlencode(params), self.timeout)
            received_at, elapsed = self.clock(), (self.monotonic() - started_mono) * 1000
            quote = validate_quote(request, payload, request_started_at=started_at,
                                   received_at=received_at, latency_ms=elapsed)
            NETWORK_METRICS.record("api.jup.ag", elapsed)
            return quote
        except Exception as exc:
            NETWORK_METRICS.record("api.jup.ag", (self.monotonic()-started_mono)*1000, False, type(exc).__name__)
            if isinstance(exc, QuoteUnavailable):
                raise
            raise QuoteUnavailable(f"Jupiter quote unavailable ({type(exc).__name__})") from exc
