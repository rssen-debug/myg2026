"""Broad, bounded Solana candidate discovery, never a recommendation or order.

Primary endpoint references checked 2026-10-13:
https://docs.dexscreener.com/api/reference
https://api.geckoterminal.com/docs/v2/swagger.json

Profiles, boosts and community takeovers are promotional/listing feeds; inclusion
does not establish safety, popularity, liquidity or a profitable signal. New
pools are not necessarily meme tokens and do not cover every Solana launch.
"""
from __future__ import annotations

from collections import deque
from public_http import get_public_json


DEX_FEEDS = (
    ("dex_profiles_latest", "token-profiles/latest/v1"),
    ("dex_boosts_latest", "token-boosts/latest/v1"),
    ("dex_boosts_top", "token-boosts/top/v1"),
    ("dex_profiles_updated", "token-profiles/recent-updates/v1"),
    ("dex_community_takeovers", "community-takeovers/latest/v1"),
)
GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
QUOTE_ASSETS = frozenset({
    "11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
})


def valid_mint(value):
    """Validate a 32-byte base58 public key; this is not a token safety check."""
    if not isinstance(value, str) or not 32 <= len(value) <= 44 or value in QUOTE_ASSETS:
        return False
    number = 0
    for char in value:
        if char not in BASE58:
            return False
        number = number * 58 + BASE58.index(char)
    leading_zero_bytes = len(value) - len(value.lstrip("1"))
    size = (number.bit_length() + 7) // 8
    return size + leading_zero_bytes == 32


def _dex_mints(payload):
    if not isinstance(payload, list):
        raise ValueError("Expected a list of token feed entries")
    values, invalid = [], 0
    for item in payload:
        if not isinstance(item, dict):
            invalid += 1
            continue
        if item.get("chainId") != "solana":
            continue
        mint = item.get("tokenAddress")
        if valid_mint(mint):
            values.append(mint)
        else:
            invalid += 1
    return list(dict.fromkeys(values)), invalid


def _gecko_mints(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Expected GeckoTerminal data list")
    values, invalid = [], 0
    for pool in payload["data"]:
        try:
            identifier = pool["relationships"]["base_token"]["data"]["id"]
            if not isinstance(identifier, str) or not identifier.startswith("solana_"):
                raise ValueError("Wrong network or malformed token identifier")
            mint = identifier[len("solana_"):]
            if valid_mint(mint):
                values.append(mint)
            else:
                invalid += 1
        except (KeyError, TypeError, ValueError):
            invalid += 1
    return list(dict.fromkeys(values)), invalid


def _interleave(groups, maximum):
    """A long first feed must not consume the whole watchlist allocation."""
    queues = [deque(group) for group in groups]
    result, seen = [], set()
    while any(queues) and len(result) < maximum:
        for queue in queues:
            while queue and queue[0] in seen:
                queue.popleft()
            if queue and len(result) < maximum:
                mint = queue.popleft()
                seen.add(mint)
                result.append(mint)
    return result


def discover_mints(fetch=get_public_json, max_mints=200, limiter=None, gecko_pages=5, should_stop=None):
    """Return (deduplicated candidates, auditable per-feed status).

    Run at most once a minute: five Dex GETs plus five Gecko GETs by default.
    More than five rapid Gecko pages produced HTTP 429 in the public smoke test;
    callers may explicitly choose up to eight, but never retry an error here.
    Gecko pagination stops on the first failure/empty page, retaining prior data.
    `limiter`, if supplied, applies to every request. Public rate limits can
    change; failures are visible in stats, never disguised as successful feeds.
    `should_stop`, if supplied, is checked between requests and after limiter
    waits. Cancellation preserves data received so far and sets `cancelled`.
    """
    if isinstance(max_mints, bool) or not isinstance(max_mints, int) or not 1 <= max_mints <= 200:
        raise ValueError("max_mints must be an integer from 1 through 200")
    if isinstance(gecko_pages, bool) or not isinstance(gecko_pages, int) or not 0 <= gecko_pages <= 8:
        raise ValueError("gecko_pages must be an integer from 0 through 8")
    sources, groups = [], []
    cancelled = False

    class DiscoveryCancelled(Exception):
        pass

    def stop_requested():
        return bool(should_stop is not None and should_stop())

    def request(url):
        if limiter is not None:
            limiter.wait()
        if stop_requested():
            raise DiscoveryCancelled()
        return fetch(url, timeout=6)

    for name, endpoint in DEX_FEEDS:
        if stop_requested():
            cancelled = True
            break
        source = {"source": name, "status": "OK", "count": 0, "invalid_entries": 0}
        try:
            values, invalid = _dex_mints(request("https://api.dexscreener.com/" + endpoint))
            source.update(count=len(values), invalid_entries=invalid)
            groups.append(values)
        except DiscoveryCancelled:
            cancelled = True
            break
        except Exception as exc:
            source.update(status="ERROR", error=f"{type(exc).__name__}: {exc}"[:240])
            groups.append([])
        sources.append(source)

    if gecko_pages and not cancelled:
        source = {"source": "gecko_new_pools", "status": "OK", "count": 0,
                  "invalid_entries": 0, "pages_requested": 0, "pages_received": 0}
        values = []
        for page in range(1, gecko_pages + 1):
            if stop_requested():
                source["status"] = "CANCELLED"
                cancelled = True
                break
            try:
                source["pages_requested"] += 1
                payload = request(f"{GECKO_BASE}?include=base_token&page={page}")
                page_values, invalid = _gecko_mints(payload)
                source["invalid_entries"] += invalid
                source["pages_received"] += 1
                values.extend(page_values)
                if not payload["data"]:
                    break
            except DiscoveryCancelled:
                source["pages_requested"] -= 1
                source["status"] = "CANCELLED"
                cancelled = True
                break
            except Exception as exc:
                source.update(status="PARTIAL" if values else "ERROR", error=f"{type(exc).__name__}: {exc}"[:240])
                break
        values = list(dict.fromkeys(values))
        source["count"] = len(values)
        groups.append(values)
        sources.append(source)

    candidates = _interleave(groups, max_mints)
    total_unique = len(set(mint for group in groups for mint in group))
    errors = [{"source": source["source"], "error": source["error"]}
              for source in sources if "error" in source]
    stats = {"sources": sources, "source_counts": {source["source"]: source["count"] for source in sources},
             "total_unique": total_unique,
             "candidates_returned": len(candidates), "capacity": max_mints,
             "partial_failure": bool(errors) or cancelled, "errors": errors,
             "cancelled": cancelled,
             "completion": "CANCELLED" if cancelled else "PARTIAL" if errors else "COMPLETE",
             "disclaimer": "Discovery only; promotional feeds and new Solana pools. Not every token is a meme or safe to trade."}
    return candidates, stats
