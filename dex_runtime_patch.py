"""OBSOLETE development fragment, already merged into godlike2026.py.

Do not reapply: production has subsequent cancellation and discovery changes.
Not shipped; tests exercise the actual godlike2026.DexScreenerClient.
"""

DEX_CLASS = r'''class DexScreenerClient:
    CACHE_TTL_SECONDS = 3.0
    BASE58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

    def __init__(self, limiter: Optional[RateLimiter] = None):
        self.limiter = limiter or RateLimiter(max_per_minute=45)
        self._cache = {}
        self._negative_cache = {}
        self.last_error = ""
        self.discovery_limiter = RateLimiter(max_per_minute=45)
        self.discovery_stats = {}
        self.prefetch_stats = {}

    def _cached(self, key):
        item = self._cache.get(key)
        if item is not None and 0 <= time.time() - item[0] < self.CACHE_TTL_SECONDS:
            return item[1]
        return None

    def _invalidate_mint(self, mint):
        for key in list(self._cache):
            if key == mint or (isinstance(key, tuple) and key[0] == mint):
                self._cache.pop(key, None)
        for key in list(self._negative_cache):
            if key == mint or (isinstance(key, tuple) and key[0] == mint):
                self._negative_cache.pop(key, None)

    def _mark_negative(self, key, reason):
        # This is a retry deadline, not a market observation or price snapshot.
        self._negative_cache[key] = (time.time(), reason)

    def _negative_reason(self, key):
        entry = self._negative_cache.get(key)
        if entry is not None and 0 <= time.time() - entry[0] < self.CACHE_TTL_SECONDS:
            return entry[1]
        self._negative_cache.pop(key, None)
        return None

    def _normalise_pair(self, pair, mint, received_at):
        if (not isinstance(pair, dict) or pair.get("chainId") != "solana"
                or not isinstance(pair.get("baseToken"), dict)
                or pair["baseToken"].get("address") != mint
                or not isinstance(pair.get("pairAddress"), str) or not pair["pairAddress"]):
            return None
        h1 = (pair.get("txns") or {}).get("h1") or {}
        vol, info = pair.get("volume") or {}, pair.get("info") or {}
        reserves = pair.get("liquidity") or {}
        result = {
            "symbol": safe_text(pair["baseToken"].get("symbol", "TOKEN"), 30),
            "name": safe_text(pair["baseToken"].get("name", "Token")),
            "pair_address": pair["pairAddress"],
            "price_usd": dec(pair.get("priceUsd", "0")),
            "liquidity_usd": dec(reserves.get("usd", "0")),
            "volume_h1": dec(vol.get("h1", "0")), "volume_h24": dec(vol.get("h24", "0")),
            "buys_h1": int(h1.get("buys", 0)), "sells_h1": int(h1.get("sells", 0)),
            "price_change_h1": dec((pair.get("priceChange") or {}).get("h1", "0")),
            "socials": info.get("socials", []), "websites": info.get("websites", []),
            "boosts": int((pair.get("boosts") or {}).get("active", 0)),
            "observed_at": received_at,
            "source": "DexScreener aggregated API; upstream tick time unavailable",
        }
        if result["price_usd"] <= ZERO or result["liquidity_usd"] < ZERO:
            return None
        # Preserve the conservative quote-side cap; total TVL is not depth.
        if reserves.get("base") is not None:
            base_usd = dec(reserves["base"]) * result["price_usd"]
            quote_usd = result["liquidity_usd"] - base_usd
            result["reported_liquidity_usd"] = result["liquidity_usd"]
            result["liquidity_usd"] = max(ZERO, min(result["liquidity_usd"], D(2)*base_usd, D(2)*quote_usd))
            result["liquidity_model"] = "conservative 2*minimum USD reserve; approximate"
        # priceUsd and TVL are rounded independently: subtracting base*price
        # from TVL can invent thousands of dollars in a nearly one-sided pool.
        # Cross-check the actual quote quantity using the pair's quote price
        # implied by USD price / priceNative. No stablecoin peg is assumed.
        if reserves.get("quote") is not None and pair.get("priceNative") is not None:
            quote_units, native_price = dec(reserves["quote"]), dec(pair["priceNative"])
            if quote_units < ZERO or native_price <= ZERO:
                return None
            quote_reserve_usd = quote_units * result["price_usd"] / native_price
            result["estimated_quote_reserve_usd"] = quote_reserve_usd
            result["liquidity_usd"] = min(result["liquidity_usd"], D(2)*quote_reserve_usd)
            result["liquidity_model"] = "conservative minimum including explicit quote quantity; approximate"
        return result

    def _store_pairs(self, mint, pairs, received_at, select_default=True, only_pair=None):
        candidates = {}
        ambiguous = set()
        for pair in pairs:
            try:
                result = self._normalise_pair(pair, mint, received_at)
            except (ValueError, TypeError, InvalidOperation, KeyError, AttributeError):
                continue
            if result is None or (only_pair and result["pair_address"] != only_pair):
                continue
            address = result["pair_address"]
            if address in candidates:
                # Conflicting repeated rows cannot establish one price.
                if candidates[address] != result:
                    ambiguous.add(address)
                continue
            candidates[address] = result
        for address in ambiguous:
            candidates.pop(address, None)
        for address, result in candidates.items():
            key = (mint, address)
            self._cache[key] = (received_at, result)
            self._negative_cache.pop(key, None)
        if not candidates:
            return None
        best = max(candidates.values(), key=lambda data: data["liquidity_usd"])
        if select_default:
            self._cache[mint] = (received_at, best)
            self._negative_cache.pop(mint, None)
        return best

    def prefetch_tokens(self, mints):
        """Fetch at most 30 mints in one documented batch GET (300/min route).

        Returns mint -> current data or None. Every quote keeps the single
        response-receipt timestamp. Failed or absent results invalidate prior
        quotes and briefly suppress retries; they never renew an old price.
        """
        unique = list(dict.fromkeys(m for m in mints if isinstance(m, str)))
        if len(unique) > 30:
            raise ValueError("Dex batch supports at most 30 distinct mints")
        valid = [mint for mint in unique if 32 <= len(mint) <= 44
                 and all(char in self.BASE58_ALPHABET for char in mint)]
        results = {mint: None for mint in unique}
        for mint in unique:
            self._invalidate_mint(mint)
            if mint not in valid:
                self._mark_negative(mint, "Ogiltig Solana-mint i batch")
        self.prefetch_stats = {"requested": len(unique), "received": 0, "missing": len(unique), "error": None}
        if not valid:
            return results
        try:
            self.limiter.wait()
            url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(valid)
            pairs = get_public_json(url, timeout=6)
            received_at = time.time()
            if not isinstance(pairs, list):
                raise ValueError("Dex batch-svar måste vara en lista")
            grouped = {mint: [] for mint in valid}
            for pair in pairs:
                if isinstance(pair, dict) and isinstance(pair.get("baseToken"), dict):
                    mint = pair["baseToken"].get("address")
                    if mint in grouped:
                        grouped[mint].append(pair)
            for mint in valid:
                data = self._store_pairs(mint, grouped[mint], received_at)
                results[mint] = data
                if data is None:
                    self._mark_negative(mint, f"Inget verifierbart Solana-baspar i batch för {mint[:12]}")
            self.prefetch_stats["received"] = sum(data is not None for data in results.values())
            self.prefetch_stats["missing"] = len(unique) - self.prefetch_stats["received"]
            self.last_error = ""
        except Exception as exc:
            reason = f"DexScreener batch: {safe_text(exc, 160)}"
            # Invalidate partial results too if processing unexpectedly fails.
            for mint in valid:
                self._invalidate_mint(mint)
                self._mark_negative(mint, reason)
                results[mint] = None
            self.last_error = reason
            self.prefetch_stats["error"] = reason
            LOGGER.warning(reason)
        return results

    def get_token_data(self, mint: str, pair_address: Optional[str] = None) -> Optional[Dict[str, Any]]:
        from urllib.parse import quote
        cache_key = (mint, pair_address) if pair_address else mint
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        negative = self._negative_reason(cache_key)
        if negative:
            self.last_error = negative
            return None
        # A batch returns only some pools. An absent held pool needs its own
        # exact-pair request; it must not borrow the best pool's quote.
        if pair_address:
            self._cache.pop(cache_key, None)
            url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{quote(str(pair_address), safe='')}"
        else:
            self._invalidate_mint(mint)
            url = f"https://api.dexscreener.com/latest/dex/tokens/{quote(str(mint), safe='')}"
        try:
            self.limiter.wait()
            response = get_public_json(url, timeout=6)
            received_at = time.time()
            if not isinstance(response, dict) or not isinstance(response.get("pairs"), (list, type(None))):
                raise ValueError("Dex token-/parsvar saknar giltig parlista")
            pairs = response.get("pairs") or []
            result = self._store_pairs(mint, pairs, received_at,
                                       select_default=not pair_address, only_pair=pair_address)
            if result is None:
                self.last_error = f"Inget verifierbart Solana-baspar hittades för {safe_text(mint, 12)}"
                self._mark_negative(cache_key, self.last_error)
                return None
            self.last_error = ""
            return result
        except Exception as exc:
            self.last_error = f"DexScreener: {safe_text(exc, 160)}"
            self._mark_negative(cache_key, self.last_error)
            LOGGER.warning(self.last_error)
            return None

    def get_pair_data(self, mint, pair_address):
        return self.get_token_data(mint, pair_address=pair_address)

    def scan_trending_mints(self) -> List[str]:
        from market_discovery import discover_mints
        try:
            mints, stats = discover_mints(fetch=get_public_json, max_mints=200,
                                          limiter=self.discovery_limiter)
            self.discovery_stats = stats
            if stats.get("partial_failure"):
                errors = stats.get("errors") or []
                details = "; ".join(f"{safe_text(item.get('source', 'källa'), 45)}: {safe_text(item.get('error', 'fel'), 100)}"
                                    for item in errors[:3] if isinstance(item, dict))
                self.last_error = details or "Några upptäcktskällor svarade inte; se källstatus"
            else:
                self.last_error = ""
            return mints
        except Exception as exc:
            self.last_error = f"Skanning: {safe_text(exc, 160)}"
            self.discovery_stats = {"sources": [], "partial_failure": True, "error": self.last_error}
            LOGGER.warning(self.last_error)
            return []

'''
