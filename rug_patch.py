class RugCheckValidator:
    """Conservative report filter, not a guarantee that a token is sellable."""
    CLASSIC_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

    def __init__(self, limiter: Optional[RateLimiter] = None, config: Optional[Dict[str, Any]] = None):
        self.limiter = limiter or RateLimiter(max_per_minute=45)
        self.cfg = dict(CONFIG)
        if config:
            self.cfg.update(config)

    def validate(self, mint: str, target_pair_address: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
        if not target_pair_address:
            return False, "Målpool måste anges för LP-kontroll", {}
        self.limiter.wait()
        url = f"{self.cfg['RUGCHECK_API_BASE']}/{mint}/report"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as res:
                data = json.loads(res.read().decode())
            return self._validate_report(data, target_pair_address)
        except Exception as exc:
            return False, f"RugCheck-data kunde inte verifieras ({safe_text(exc, 80)})", {}

    def _validate_report(self, data: Any, target_pair_address: str) -> Tuple[bool, str, Dict[str, Any]]:
        # This helper also makes all validation branches testable without services.
        try:
            if not isinstance(data, dict) or not isinstance(data.get("token"), dict):
                return False, "Tokenrapport saknas eller är ogiltig", {}
            token = data["token"]
            if data.get("rugged") is True:
                return False, "Token är flaggad som rugged", {"danger": True}
            if data.get("rugged") is not False:
                return False, "Rugged-status saknas eller är ogiltig", {}

            for authority in ("mintAuthority", "freezeAuthority"):
                values = [obj[authority] for obj in (data, token) if authority in obj]
                if not values:
                    return False, f"{authority} saknas", {}
                if any(value is not None for value in values):
                    return False, f"{authority} är aktiv eller rapporten är motsägelsefull", {"danger": True}

            programs = [obj["tokenProgram"] for obj in (data, token) if "tokenProgram" in obj]
            if not programs or any(p != self.CLASSIC_TOKEN_PROGRAM for p in programs):
                return False, "Endast verifierat klassiskt SPL Token-program stöds; Token-2022/okänt program blockerat", {}
            for obj in (data, token):
                for field in ("token_extensions", "extensions"):
                    if obj.get(field) not in (None, {}, []):
                        return False, "Token-extensions stöds inte av exekveringsmodellen", {}
                fee = obj.get("transferFee")
                if fee not in (None, {}, []):
                    if not isinstance(fee, dict) or "pct" not in fee or dec(fee["pct"]) != ZERO:
                        return False, "Transferavgift saknas eller stöds inte", {}

            if "decimals" not in token or isinstance(token["decimals"], bool):
                return False, "Exakta token-decimaler saknas", {}
            decimal_value = dec(token["decimals"])
            if decimal_value != decimal_value.to_integral_value() or not ZERO <= decimal_value <= D("18"):
                return False, "Token-decimaler är ogiltiga eller stöds inte (0–18)", {}
            decimals = int(decimal_value)

            risks = data.get("risks")
            if not isinstance(risks, list):
                return False, "Riskdata saknas", {}
            for risk in risks:
                if not isinstance(risk, dict) or not isinstance(risk.get("level"), str):
                    return False, "Riskpost saknar giltig risknivå", {}
                level = risk["level"].lower()
                if level in ("danger", "critical"):
                    return False, f"Kritisk risk: {safe_text(risk.get('name', 'okänd risk'))}", {"danger": True}
                if level not in ("warn", "warning", "info", "low", "medium"):
                    return False, f"Okänd risknivå: {safe_text(level)}", {}
            score = dec(data.get("score"))
            if score < ZERO or score != score.to_integral_value():
                return False, "Riskbetyg är ogiltigt", {}
            if score > self.cfg["MAX_RUGCHECK_SCORE"]:
                return False, f"För högt riskbetyg ({score})", {"score": int(score)}

            markets = data.get("markets")
            if not isinstance(markets, list) or any(not isinstance(m, dict) for m in markets):
                return False, "Marknadsdata saknas eller är ogiltig", {}
            matched = [m for m in markets if m.get("pubkey") == target_pair_address or m.get("pairAddress") == target_pair_address]
            if len(matched) != 1:
                return False, "Målpoolen saknas eller har motsägelsefulla rapportposter; annan pool får inte ersätta den", {}
            target_market = matched[0]
            lp = target_market.get("lp")
            if not isinstance(lp, dict) or not any(key in lp for key in ("lpLockedPct", "lpBurnedPct")):
                return False, "LP-bevis för målpoolen saknas", {}
            locked = dec(lp["lpLockedPct"]) if "lpLockedPct" in lp else ZERO
            burned = dec(lp["lpBurnedPct"]) if "lpBurnedPct" in lp else ZERO
            if not (ZERO <= locked <= HUNDRED and ZERO <= burned <= HUNDRED):
                return False, "LP-procent är ogiltig", {}
            # Reports do not prove disjoint lock and burn sets. Never double count.
            secured = max(locked, burned)
            if secured < self.cfg["MIN_LP_LOCKED_BURNED_PCT"]:
                return False, f"Målpoolens verifierade LP-andel är för låg ({secured:.1f}%)", {"danger": True}

            holders = data.get("topHolders")
            if not isinstance(holders, list) or not holders:
                return False, "Holder-data saknas", {}
            # Exclude actual token vaults only, never mints or generic program IDs.
            vaults = {m[key] for m in markets for key in ("baseVault", "quoteVault")
                      if isinstance(m.get(key), str) and m[key]}
            percentages = []
            seen = set()
            for holder in holders:
                if not isinstance(holder, dict) or not isinstance(holder.get("address"), str) or not holder["address"]:
                    return False, "Holder-post saknar adress", {}
                if holder["address"] in seen:
                    return False, "Dubblerad holder-post i rapporten", {}
                seen.add(holder["address"])
                pct = dec(holder.get("pct"))
                if not ZERO <= pct <= HUNDRED:
                    return False, "Holder-andel är ogiltig", {}
                if holder["address"] not in vaults:
                    percentages.append(pct)
            insider_sum = sum(sorted(percentages, reverse=True)[:10], ZERO)
            if insider_sum > self.cfg["MAX_TOP10_INSIDER_PCT"]:
                return False, f"Top 10-andel är för hög ({insider_sum:.1f}%)", {}
            return True, f"Rapportfilter godkänt (score {score}, målpoolens LP {secured:.1f}%); ingen säljbarhetsgaranti", {"decimals": decimals}
        except (ValueError, TypeError, InvalidOperation, KeyError) as exc:
            return False, f"Rapportdata kan inte verifieras ({safe_text(exc, 80)})", {}
