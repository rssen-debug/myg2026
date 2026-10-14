class PositionMonitor:
    """One decision per observed price, with fills based on raw token balances.

    Stop orders use the market observation, never the trigger as a fill price.
    A newly raised stop only applies to subsequent observations. Unknown market
    data leaves the position open; it is not evidence of a completed sale.
    """

    def __init__(self, state: StateManager, dex: DexScreenerClient, jup: JupiterEngine):
        self.state = state
        self.dex = dex
        self.jup = jup
        self.cfg = getattr(state, "cfg", CONFIG)
        self._exit_lock = threading.RLock()

    def _execute_sell(self, p: Dict[str, Any], raw_amount: int, price: Decimal,
                      liquidity: Decimal, reason: str, is_paper: bool) -> Dict[str, Any]:
        if raw_amount <= 0 or raw_amount > int(p["remaining_raw"]):
            raise ValueError("Ogiltig säljkvantitet; positionen lämnas oförändrad")
        decimals = int(p["decimals"])
        if is_paper:
            fill = simulate_paper_sell(raw_amount, decimals, price, liquidity, config=self.cfg)
            return dict(fill, tx_hash=f"paper_exit_{p['id']}_{reason}_{time.time_ns()}")

        sol_price = self.jup.get_sol_price_usd()
        quote = self.jup.get_quote(p["mint"], CONFIG["SOL_MINT"], raw_amount,
                                   CONFIG["DEFAULT_SLIPPAGE_BPS"])
        if not quote or int(quote.get("outAmount", 0)) <= 0:
            raise ValueError("Ingen exekverbar säljquote")
        expected = dec(quote["outAmount"]) / D("1e9") * sol_price
        success, tx, message, actual_sol = self.jup.execute_live_swap(
            quote, p["mint"], CONFIG["SOL_MINT"], pos_id=p["id"], direction=reason,
            amount_raw=raw_amount, expected_usd=expected, symbol=p["symbol"],
            pair_address=p.get("pair_address", ""), decimals=decimals
        )
        if not success:
            raise ValueError(f"Säljtransaktion ej slutbokförd: {message}")
        # Actual SOL balance delta already includes transaction gas. Do not
        # subtract gas a second time from the cash credited to the wallet.
        net_usd = dec(actual_sol) * sol_price
        gas_usd = (D("0.000005") + dec(CONFIG["PRIORITY_FEE_LAMPORTS"]) / D("1e9")) * sol_price
        token_units = dec(raw_amount) / (D("10") ** decimals)
        return {"gross_usd": net_usd + gas_usd, "fee_usd": gas_usd,
                "net_usd": net_usd, "execution_price": (net_usd + gas_usd) / token_units,
                "price_impact_pct": ZERO, "tx_hash": tx}

    def check_and_exit_positions(self, is_paper: bool = True) -> List[Dict[str, Any]]:
        if not is_paper:
            LOGGER.error("Live-sälj är spärrad: transaktionsvis saldoförsoning är inte validerad i denna version")
            return []
        # A fresh DB snapshot is taken inside this lock, so repeated/concurrent
        # calls on this monitor cannot submit the same TP from stale flags.
        with self._exit_lock:
            return self._check_positions(is_paper)

    def _check_positions(self, is_paper: bool) -> List[Dict[str, Any]]:
        exits = []
        for p in self.state.get_open_positions(is_paper=is_paper):
            pos_id = p["id"]
            if not is_paper and self.state.is_position_pending(pos_id):
                LOGGER.info("Position #%d inväntar transaktion; ny försäljning blockeras", pos_id)
                continue
            try:
                raw_remaining = int(p["remaining_raw"])
                raw_initial = int(p["initial_raw"])
                entry = dec(p["entry_price_usd"])
                old_stop = dec(p["stop_loss_price"])
                old_peak = dec(p["peak_price_usd"])
                trailing = int(p["trailing_active"])
                if raw_remaining <= 0 or raw_initial < raw_remaining or entry <= ZERO:
                    raise ValueError("Ogiltigt positionssaldo eller ingångspris")
                data = self.dex.get_token_data(p["mint"])
                if not data:
                    raise ValueError("Marknadsdata saknas")
                if p.get("pair_address") and data.get("pair_address") != p["pair_address"]:
                    raise ValueError("Marknadsdata gäller inte positionens pool")
                if data.get("observed_at") is not None:
                    age = dec(time.time()) - dec(data["observed_at"])
                    max_age = dec(self.cfg.get("MAX_MARKET_DATA_AGE_SECONDS", 30))
                    if age > max_age or age < D("-5"):
                        raise ValueError("Marknadsdata är för gammal eller har ogiltig tidsstämpel")
                price = dec(data.get("price_usd"))
                liquidity = dec(data.get("liquidity_usd"))
                if price <= ZERO or liquidity <= ZERO:
                    raise ValueError("Exekverbart pris eller likviditet saknas")
            except (ValueError, TypeError, KeyError, MarketDataUnavailableError) as exc:
                LOGGER.warning("Position #%d lämnas öppen: %s", pos_id, exc)
                continue

            pnl_pct = (price / entry - ONE) * HUNDRED

            # Existing stop takes priority. A gap executes at the new market
            # price and includes impact/fees; no guaranteed stop-price fills.
            if price <= old_stop:
                reason = ("MOONBAG_TRAILING_STOP" if trailing == 2 else
                          "BREAKEVEN_STOP" if trailing == 1 else "STOP_LOSS")
                try:
                    fill = self._execute_sell(p, raw_remaining, price, liquidity, reason, is_paper)
                except (ValueError, MarketDataUnavailableError) as exc:
                    LOGGER.warning("Position #%d kunde inte säljas: %s", pos_id, exc)
                    continue
                try:
                    booked = self.state.close_position(pos_id, fill["execution_price"], reason,
                                                       fill["net_usd"], fill["fee_usd"],
                                                       fill["tx_hash"], is_paper=is_paper)
                except ValueError as exc:
                    LOGGER.warning("Avslut för #%d kunde inte bokföras: %s", pos_id, exc)
                    continue
                if not booked:
                    continue
                exits.append({"mint": p["mint"], "symbol": p["symbol"], "reason": reason,
                              "pnl_pct": pnl_pct, "net_usd": fill["net_usd"]})
                continue

            peak = max(old_peak, price)
            stop = old_stop
            if trailing == 2:
                stop = max(stop, peak * (ONE - dec(self.cfg["MOONBAG_TRAILING_PCT"]) / HUNDRED))
            elif trailing == 0 and pnl_pct >= dec(self.cfg["BREAKEVEN_TRIGGER_PCT"]):
                trailing = 1
                stop = max(stop, entry * (ONE + dec(self.cfg["BREAKEVEN_LOCK_PCT"]) / HUNDRED))

            tp_level = 0
            if not p["tp1_executed"] and pnl_pct >= dec(self.cfg["TAKE_PROFIT_1_PCT"]):
                tp_level = 1
            elif p["tp1_executed"] and not p["tp2_executed"] and pnl_pct >= dec(self.cfg["TAKE_PROFIT_2_PCT"]):
                tp_level = 2

            if tp_level:
                # Quantities are percentages of ORIGINAL units, rounded down.
                # Residual dust belongs to the moonbag, never to phantom sales.
                target_ratio = D("0.40") if tp_level == 1 else D("0.35")
                sell_raw = int((dec(raw_initial) * target_ratio).to_integral_value(rounding=ROUND_DOWN))
                if not 0 < sell_raw < raw_remaining:
                    LOGGER.warning("TP%d för #%d saknar giltig råkvantitet", tp_level, pos_id)
                else:
                    try:
                        fill = self._execute_sell(p, sell_raw, price, liquidity, f"TP{tp_level}", is_paper)
                    except (ValueError, MarketDataUnavailableError) as exc:
                        LOGGER.warning("TP%d för #%d ej utförd: %s", tp_level, pos_id, exc)
                    else:
                        # Allocate cost by the quantity actually sold, including
                        # raw-unit rounding (40% is not exact for every token).
                        sold_ratio = dec(sell_raw) / dec(raw_initial)
                        try:
                            booked = self.state.apply_partial_sell(
                                pos_id, tp_level, str(raw_remaining - sell_raw), sold_ratio,
                                fill["gross_usd"], fill["fee_usd"], fill["net_usd"],
                                fill["execution_price"], is_paper=is_paper
                            )
                        except ValueError as exc:
                            LOGGER.warning("TP%d för #%d kunde inte bokföras: %s", tp_level, pos_id, exc)
                            continue
                        if not booked:
                            continue
                        if tp_level == 1:
                            stop = max(stop, entry * D("1.15"))
                        else:
                            trailing = 2
                            stop = max(stop, peak * (ONE - dec(self.cfg["MOONBAG_TRAILING_PCT"]) / HUNDRED))
                        LOGGER.info("TP%d klar för %s: %d råenheter sålda, $%.2f netto",
                                    tp_level, p["symbol"], sell_raw, fill["net_usd"])

            self.state.update_position_trailing(pos_id, peak, stop, trailing)
        return exits
