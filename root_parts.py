def calculate_amm_price_impact(trade_usd, pool_liquidity_usd):
    """Balanced constant-product approximation; liquidity is total pool USD TVL."""
    amount, liquidity = dec(trade_usd), dec(pool_liquidity_usd)
    if liquidity <= ZERO or amount <= ZERO:
        raise ValueError("Belopp och likviditet måste vara positiva")
    reserve = liquidity / D(2)
    return amount / (reserve + amount) * HUNDRED, ONE + amount / reserve


def _paper_inputs(price, liquidity, decimals, config):
    cfg = dict(CONFIG)
    cfg.update(config or {})
    price, liquidity = dec(price), dec(liquidity)
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
        raise ValueError("Token decimals måste anges som heltal 0..18")
    if price <= ZERO or liquidity <= ZERO:
        raise ValueError("Pris och historisk/pool-specifik likviditet måste vara positiva")
    fee = dec(cfg["DEX_POOL_FEE_PCT"]) / HUNDRED
    slip = dec(cfg["BASE_SLIPPAGE_PCT"]) / HUNDRED
    gas = dec(cfg["ESTIMATED_GAS_USD"])
    limit = dec(cfg["MAX_ALLOWED_PRICE_IMPACT_PCT"])
    if not (ZERO <= fee < ONE and ZERO <= slip < ONE and gas >= ZERO and ZERO < limit < HUNDRED):
        raise ValueError("Ogiltiga avgifts-, slippage- eller impact-inställningar")
    return cfg, price, liquidity / D(2), D(10) ** decimals, fee, slip, gas, limit


def simulate_paper_buy(budget_usd, price, liquidity, decimals, config=None):
    """All-in budget. Pool fee on input; adverse movement separate from paid fees.

    Assumes balanced x*y=k, NOT a quote for concentrated/bonding-curve pools.
    """
    cfg, price, reserve, scale, fee, slip, gas, limit = _paper_inputs(price, liquidity, decimals, config)
    budget = dec(budget_usd)
    spend = budget - gas
    if spend <= ZERO:
        raise ValueError("Orderbudgeten täcker inte gas")
    pool_fee = spend * fee
    effective = spend - pool_fee
    impact = effective / (reserve + effective) * HUNDRED
    if impact > limit:
        raise ValueError(f"Prisimpact {impact:.4f}% över gränsen {limit}%")
    units = (reserve / price) * effective / (reserve + effective) * (ONE - slip)
    raw = int((units * scale).to_integral_value(rounding=ROUND_DOWN))
    if raw <= 0:
        raise ValueError("Ordern ger mindre än en raw token")
    units = D(raw) / scale
    return {"raw_amount": raw, "net_token_value": units * price,
            "fee_usd": gas + pool_fee, "execution_price": spend / units,
            "price_impact_pct": impact, "usd_invested": budget,
            "slippage_cost_usd": (reserve / price) * effective / (reserve + effective) * price * slip}


def simulate_paper_sell(raw_amount, decimals, price, liquidity, config=None):
    """Net proceeds include gas; a dust exit can debit cash, never erase its gas."""
    cfg, price, reserve, scale, fee, slip, gas, limit = _paper_inputs(price, liquidity, decimals, config)
    if isinstance(raw_amount, bool) or not isinstance(raw_amount, int) or raw_amount <= 0:
        raise ValueError("Säljantal måste vara ett positivt heltal raw tokens")
    units = D(raw_amount) / scale
    notional = units * price
    effective = notional * (ONE - fee)
    impact = effective / (reserve + effective) * HUNDRED
    if impact > limit:
        raise ValueError(f"Prisimpact {impact:.4f}% över gränsen {limit}%")
    pool_out = reserve * effective / (reserve + effective)
    gross = pool_out * (ONE - slip)
    return {"gross_usd": gross, "fee_usd": gas + notional * fee,
            "net_usd": gross - gas, "execution_price": gross / units,
            "price_impact_pct": impact, "slippage_cost_usd": pool_out * slip}


class StateUpdates:
    def __init__(self, db_path: str, config=None):
        self.cfg = dict(CONFIG)
        self.cfg.update(config or {})
        self.db_path = db_path
        self._init_db()
        with self._get_connection() as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(wallet)")}
            if "peak_equity_usd" not in columns:
                conn.execute("ALTER TABLE wallet ADD COLUMN peak_equity_usd TEXT NOT NULL DEFAULT '0'")
                conn.execute("UPDATE wallet SET peak_equity_usd=initial_cash_usd")
            conn.execute("""CREATE TABLE IF NOT EXISTS market_observations (
                id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, mint TEXT NOT NULL,
                market_json TEXT NOT NULL, decision_json TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS equity_history (
                id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, equity_usd TEXT,
                complete INTEGER NOT NULL, details_json TEXT NOT NULL)""")

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_position_trailing(self, pos_id, peak_price, stop_loss, trailing_active):
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pos = conn.execute("SELECT * FROM positions WHERE id=? AND status='OPEN'", (pos_id,)).fetchone()
            if not pos:
                return
            conn.execute("UPDATE positions SET peak_price_usd=?,stop_loss_price=?,trailing_active=? WHERE id=?",
                         (str(max(dec(peak_price), dec(pos["peak_price_usd"]))),
                          str(max(dec(stop_loss), dec(pos["stop_loss_price"]))),
                          max(int(trailing_active), pos["trailing_active"]), pos_id))

    def paper_equity(self, dex_client):
        wallet = self.get_wallet()
        open_value = ZERO
        unpriced = []
        for pos in self.get_open_positions(True):
            try:
                data = dex_client.get_token_data(pos["mint"]) if dex_client else None
                if not data:
                    raise ValueError("Marknadsdata saknas")
                if data.get("pair_address") != pos["pair_address"]:
                    raise ValueError("Marknadsdata avser en annan pool")
                observed = data.get("observed_at")
                if observed is not None and not 0 <= time.time() - float(observed) <= self.cfg["MAX_MARKET_DATA_AGE_SECONDS"]:
                    raise ValueError("Inaktuell marknadsdata")
                fill = simulate_paper_sell(int(pos["remaining_raw"]), int(pos["decimals"]),
                                           data["price_usd"], data["liquidity_usd"], config=self.cfg)
                if wallet["cash_usd"] + fill["net_usd"] < ZERO:
                    raise ValueError("Kassan räcker inte för den modellerade säljavgiften")
                open_value += fill["net_usd"]
            except (ValueError, KeyError, TypeError, MarketDataUnavailableError) as exc:
                unpriced.append({"id": pos["id"], "mint": pos["mint"], "reason": str(exc)})
        return {"cash_usd": wallet["cash_usd"], "open_value_usd": open_value if not unpriced else None,
                "equity_usd": wallet["cash_usd"] + open_value if not unpriced else None,
                "complete": not unpriced, "unpriced_positions": unpriced}

    def can_open_position(self, mint, trade_usd, is_paper=True, live_sol_balance=ZERO,
                          sol_price=ZERO, dex_client=None):
        if not is_paper:
            return False, LIVE_BLOCK_REASON
        try:
            trade_usd = dec(trade_usd)
        except ValueError as exc:
            return False, str(exc)
        if trade_usd <= ZERO:
            return False, "Orderbelopp måste vara positivt"
        blacklisted, reason = self.is_blacklisted(mint)
        if blacklisted:
            return False, f"Svartlistad: {reason}"
        wallet = self.get_wallet()
        now = time.time()
        if wallet["halt_reason"] and (wallet["halt_until"] <= 0 or now < wallet["halt_until"]):
            return False, wallet["halt_reason"]
        if trade_usd > wallet["cash_usd"]:
            return False, "Otillräcklig kassa"
        with self._get_connection() as conn:
            if conn.execute("SELECT 1 FROM pending_transactions WHERE resolved=0").fetchone():
                return False, "Olöst transaktion kräver avstämning"
            positions = self.get_open_positions(True)
            if len(positions) >= self.cfg["MAX_OPEN_POSITIONS"]:
                return False, "Max öppna positioner uppnått"
            if any(p["mint"] == mint for p in positions):
                return False, "Position redan öppen i token"
            cutoff = now - self.cfg["COOLDOWN_HOURS"] * 3600
            if conn.execute("SELECT 1 FROM trade_history WHERE mint=? AND is_paper=1 AND timestamp>?", (mint, cutoff)).fetchone():
                return False, "Token är i cooldown"
        mark = self.paper_equity(dex_client)
        if not mark["complete"]:
            return False, "Köp spärrat: öppna positioner saknar verifierbar likvidationsvärdering"
        with self._get_connection() as conn:
            peak = max(dec(conn.execute("SELECT peak_equity_usd FROM wallet WHERE id=1").fetchone()[0]), mark["equity_usd"])
            dd = (peak - mark["equity_usd"]) / peak * HUNDRED if peak > ZERO else ZERO
            conn.execute("UPDATE wallet SET peak_equity_usd=? WHERE id=1", (str(peak),))
            conn.execute("INSERT INTO equity_history(timestamp,equity_usd,complete,details_json) VALUES(?,?,1,?)",
                         (now, str(mark["equity_usd"]), json.dumps(mark, default=str)))
            if dd >= self.cfg["MAX_DRAWDOWN_PCT"]:
                reason = f"PAPER stopp: drawdown från högsta portföljvärde {dd:.2f}%"
                conn.execute("UPDATE wallet SET halt_reason=?,halt_until=0,last_halt_time=? WHERE id=1", (reason, now))
                return False, reason
            losses = conn.execute("""SELECT pnl_pct FROM positions WHERE status='CLOSED'
                AND is_paper=1 AND exit_time>? ORDER BY exit_time DESC LIMIT ?""",
                (wallet["last_halt_time"], self.cfg["MAX_CONSECUTIVE_LOSSES"])).fetchall()
            if len(losses) == self.cfg["MAX_CONSECUTIVE_LOSSES"] and all(dec(r[0]) < ZERO for r in losses):
                reason = "PAPER stopp efter förlustsvit"
                conn.execute("UPDATE wallet SET halt_reason=?,halt_until=?,last_halt_time=? WHERE id=1",
                             (reason, now + self.cfg["CIRCUIT_BREAKER_HOURS"] * 3600, now))
                return False, reason
            conn.execute("UPDATE wallet SET halt_reason='',halt_until=0 WHERE id=1")
        return True, "OK"

    def apply_partial_sell(self, pos_id, tp_level, remaining_raw, sold_ratio, gross_usd,
                           fee_usd, net_usd, exit_price, is_paper=True):
        if tp_level not in (1, 2):
            raise ValueError("TP-nivå måste vara 1 eller 2")
        raw_new = dec(remaining_raw)
        if raw_new != raw_new.to_integral_value() or raw_new < ZERO:
            raise ValueError("Kvarvarande antal måste vara ett icke-negativt heltal")
        raw_new = int(raw_new)
        net_usd, fee_usd, exit_price = dec(net_usd), dec(fee_usd), dec(exit_price)
        if fee_usd < ZERO or exit_price <= ZERO:
            raise ValueError("Ogiltigt pris eller avgift")
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
            if not row:
                raise ValueError("Position saknas")
            pos = dict(row)
            if pos["is_paper"] != int(is_paper):
                raise ValueError("Fel redovisningsläge")
            if pos["status"] != "OPEN" or pos[f"tp{tp_level}_executed"]:
                return False
            if tp_level == 2 and not pos["tp1_executed"]:
                raise ValueError("TP2 kräver TP1")
            previous_raw = int(pos["remaining_raw"])
            if not 0 < raw_new < previous_raw:
                raise ValueError("Delsälj måste lämna tokens och minska innehavet")
            remaining_cost = dec(pos["remaining_invested_usd"])
            new_cost = dec(pos["usd_invested"]) * D(raw_new) / D(pos["initial_raw"])
            cost_sold = remaining_cost - new_cost
            pnl = net_usd - cost_sold
            pct = pnl / cost_sold * HUNDRED
            conn.execute(f"""UPDATE positions SET remaining_raw=?,remaining_invested_usd=?,
                tp{tp_level}_executed=1,fees_paid_usd=? WHERE id=?""",
                (str(raw_new), str(new_cost), str(dec(pos["fees_paid_usd"]) + fee_usd), pos_id))
            if is_paper:
                wallet = conn.execute("SELECT * FROM wallet WHERE id=1").fetchone()
                if dec(wallet["cash_usd"]) + net_usd < ZERO:
                    raise ValueError("Kassa kan inte finansiera säljavgiften")
                conn.execute("UPDATE wallet SET cash_usd=?,realized_pnl_usd=?,total_fees_paid_usd=? WHERE id=1",
                    (str(dec(wallet["cash_usd"]) + net_usd), str(dec(wallet["realized_pnl_usd"]) + pnl),
                     str(dec(wallet["total_fees_paid_usd"]) + fee_usd)))
            now = time.time()
            conn.execute("""INSERT INTO trade_history
                (pos_id,mint,symbol,action,price_usd,usd_amount,pnl_pct,fee_usd,timestamp,tx_hash,is_paper)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (pos_id, pos["mint"], pos["symbol"], f"TP{tp_level}",
                str(exit_price), str(net_usd), str(pct), str(fee_usd), now, f"paper_tp{tp_level}_{time.time_ns()}", int(is_paper)))
        return True

    def close_position(self, pos_id, exit_price, exit_reason, net_usd_received, fee_usd, tx_hash, is_paper=True):
        received, fee, price = dec(net_usd_received), dec(fee_usd), dec(exit_price)
        if fee < ZERO or price < ZERO:
            raise ValueError("Ogiltigt avslut")
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
            if not row:
                raise ValueError("Position saknas")
            pos = dict(row)
            if pos["is_paper"] != int(is_paper):
                raise ValueError("Fel redovisningsläge")
            if pos["status"] != "OPEN":
                return False
            prior = sum((dec(r[0]) for r in conn.execute("SELECT usd_amount FROM trade_history WHERE pos_id=? AND action IN ('TP1','TP2')", (pos_id,))), ZERO)
            total_pnl = prior + received - dec(pos["usd_invested"])
            pct = total_pnl / dec(pos["usd_invested"]) * HUNDRED
            pnl = received - dec(pos["remaining_invested_usd"])
            now = time.time()
            conn.execute("""UPDATE positions SET status='CLOSED',remaining_raw='0',remaining_invested_usd='0',
                exit_time=?,exit_price_usd=?,exit_reason=?,pnl_pct=?,net_usd_received=?,fees_paid_usd=? WHERE id=?""",
                (now, str(price), exit_reason, str(pct), str(received), str(dec(pos["fees_paid_usd"]) + fee), pos_id))
            if is_paper:
                wallet = conn.execute("SELECT * FROM wallet WHERE id=1").fetchone()
                if dec(wallet["cash_usd"]) + received < ZERO:
                    raise ValueError("Kassa kan inte finansiera säljavgiften")
                conn.execute("""UPDATE wallet SET cash_usd=?,realized_pnl_usd=?,total_fees_paid_usd=?,
                    total_trades=total_trades+1,winning_trades=winning_trades+?,losing_trades=losing_trades+? WHERE id=1""",
                    (str(dec(wallet["cash_usd"]) + received), str(dec(wallet["realized_pnl_usd"]) + pnl),
                     str(dec(wallet["total_fees_paid_usd"]) + fee), int(total_pnl > ZERO), int(total_pnl < ZERO)))
            conn.execute("""INSERT INTO trade_history
                (pos_id,mint,symbol,action,price_usd,usd_amount,pnl_pct,fee_usd,timestamp,tx_hash,is_paper)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (pos_id, pos["mint"], pos["symbol"], exit_reason,
                str(price), str(received), str(pct), str(fee), now, tx_hash, int(is_paper)))
        return True

    def performance_report(self, dex_client):
        mark = self.paper_equity(dex_client)
        wallet = self.get_wallet()
        with self._get_connection() as conn:
            positions = conn.execute("SELECT * FROM positions WHERE is_paper=1 AND status='CLOSED'").fetchall()
            pnls = []
            for pos in positions:
                returned = sum((dec(r[0]) for r in conn.execute("SELECT usd_amount FROM trade_history WHERE pos_id=? AND action IN ('TP1','TP2')", (pos["id"],))), ZERO)
                pnls.append(returned + dec(pos["net_usd_received"]) - dec(pos["usd_invested"]))
            conn.execute("INSERT INTO equity_history(timestamp,equity_usd,complete,details_json) VALUES(?,?,?,?)",
                         (time.time(), str(mark["equity_usd"]) if mark["complete"] else None,
                          int(mark["complete"]), json.dumps(mark, default=str)))
            equities = [dec(r[0]) for r in conn.execute("SELECT equity_usd FROM equity_history WHERE complete=1 ORDER BY id")]
            unknown_marks = conn.execute("SELECT COUNT(*) FROM equity_history WHERE complete=0").fetchone()[0]
            remaining_basis = sum((dec(p["remaining_invested_usd"]) for p in self.get_open_positions()), ZERO)
            stored_peak = dec(conn.execute("SELECT peak_equity_usd FROM wallet WHERE id=1").fetchone()[0])
            observed_peak = max([wallet["initial_cash_usd"], stored_peak] + equities)
            conn.execute("UPDATE wallet SET peak_equity_usd=? WHERE id=1", (str(observed_peak),))
        peak, max_dd = wallet["initial_cash_usd"], ZERO
        for equity in equities:
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak-equity)/peak*HUNDRED if peak > ZERO else ZERO)
        gains = sum((p for p in pnls if p > ZERO), ZERO)
        losses = -sum((p for p in pnls if p < ZERO), ZERO)
        pnl = mark["equity_usd"] - wallet["initial_cash_usd"] if mark["complete"] else None
        return {"initial_cash": wallet["initial_cash_usd"], "cash": wallet["cash_usd"],
                "open_val": mark["open_value_usd"], "total_port": mark["equity_usd"],
                "net_pnl": pnl, "net_pnl_pct": pnl / wallet["initial_cash_usd"] * HUNDRED if pnl is not None else None,
                "realized_pnl": wallet["realized_pnl_usd"], "fees": wallet["total_fees_paid_usd"],
                "unrealized_pnl": mark["open_value_usd"] - remaining_basis if mark["complete"] else None,
                "total_trades": len(pnls), "winning_trades": sum(p>ZERO for p in pnls),
                "losing_trades": sum(p<ZERO for p in pnls), "breakeven_trades": sum(p==ZERO for p in pnls),
                "win_rate": D(sum(p>ZERO for p in pnls))/D(len(pnls))*HUNDRED if pnls else None,
                "profit_factor": gains/losses if losses else None, "max_drawdown_pct": max_dd,
                "max_drawdown_complete": unknown_marks == 0,
                "expectancy_usd": sum(pnls,ZERO)/D(len(pnls)) if pnls else None,
                "valuation_complete": mark["complete"], "unpriced_positions": mark["unpriced_positions"],
                "conclusion": "Ej bevisad lönsamhet. Simulerade resultat med antagna kostnader och poolmodell."}


class BotUpdates:
    def __init__(self, config):
        self.cfg = dict(CONFIG)
        self.cfg.update(config)
        self.cfg["DRY_RUN"] = True
        self.rate_limiter = RateLimiter(max_per_minute=45)
        self.state = StateManager(self.cfg["DB_PATH"], config=self.cfg)
        self.dex = DexScreenerClient(limiter=self.rate_limiter)
        self.rug = RugCheckValidator(limiter=self.rate_limiter)
        self.rug.cfg = self.cfg
        self.trench = TrenchIQAnalyzer()
        self.smart_wallets = SmartWalletTracker(self.state, self.cfg["SOLANA_RPC_URL"])
        self.jup = JupiterEngine(self.cfg["SOLANA_RPC_URL"], "", True, state=self.state)
        self.monitor = PositionMonitor(self.state, self.dex, self.jup)
        self._signals = {}

    def evaluate_and_buy(self, mint, is_paper=True):
        if not is_paper:
            return {"status": "BLOCKED", "reason": LIVE_BLOCK_REASON}
        pair = self.dex.get_token_data(mint)
        def finish(status, reason, **extra):
            result = dict(status=status, reason=reason, **extra)
            with self.state._get_connection() as conn:
                conn.execute("INSERT INTO market_observations(timestamp,mint,market_json,decision_json) VALUES(?,?,?,?)",
                             (time.time(), mint, json.dumps(pair, default=str), json.dumps(result, default=str)))
            return result
        if not pair:
            return finish("REJECTED", "Marknadsdata saknas")
        try:
            price, liquidity = dec(pair["price_usd"]), dec(pair["liquidity_usd"])
            if price <= ZERO or liquidity < self.cfg["MIN_LIQUIDITY_USD"]:
                return finish("REJECTED", "För låg likviditet eller ogiltigt pris")
            now = time.time()
            if not 0 <= now - float(pair["observed_at"]) <= self.cfg["MAX_MARKET_DATA_AGE_SECONDS"]:
                return finish("REJECTED", "Marknadsdata är inaktuell")
            # These are explicit research hypotheses, not calibrated alpha scores.
            # Social links, paid boosts and claimed celebrity wallet wins are NOT buy signals.
            buys, sells = int(pair["buys_h1"]), int(pair["sells_h1"])
            vol = dec(pair["volume_h1"])
            turnover = vol / liquidity
            if buys < 25 or sells < 10 or vol < self.cfg["MIN_VOLUME_H1_USD"]:
                return finish("REJECTED", "Otillräcklig tvåsidig handel")
            if not D("0.05") <= turnover <= D("5"):
                return finish("REJECTED", "Omsättning/likviditet utanför experimentets gränser")
            if dec(pair["price_change_h1"]) > D("30"):
                return finish("REJECTED", "Köp efter kraftig entimmesuppgång spärrat")
            history = self._signals.setdefault(mint, [])
            if history and (now-history[-1][0] > 180 or history[-1][3] != pair["pair_address"]):
                history.clear()
            if not history or float(pair["observed_at"]) > history[-1][0] + 5:
                history.append((float(pair["observed_at"]), price, liquidity, pair["pair_address"]))
            history[:] = history[-12:]
            if len(history) < 3 or history[-1][0] - history[0][0] < self.cfg["SIGNAL_MIN_SECONDS"]:
                return finish("WAITING", "Samlar oberoende prisobservationer före signal")
            momentum = (price/history[0][1]-ONE)*HUNDRED
            if liquidity < max(h[2] for h in history) * D("0.9"):
                return finish("REJECTED", "Observerat likviditetsfall över 10%")
            if not D("1") <= momentum <= D("10") or buys < sells:
                return finish("REJECTED", "Ingen bekräftad måttlig momentumsignal")
            allowed, reason = self.state.can_open_position(mint, self.cfg["TRADE_BUDGET_USD"], dex_client=self.dex)
            if not allowed:
                return finish("SKIPPED", reason)
            accepted, reason, meta = self.rug.validate(mint, target_pair_address=pair["pair_address"])
            if not accepted:
                return finish("RUG_PREVENTED", reason)
            # Rug API latency must not turn an old observation into a new fill.
            fresh = self.dex.get_token_data(mint)
            if not fresh or fresh["pair_address"] != pair["pair_address"]:
                return finish("REJECTED", "Pooldata ändrades under kontrollen")
            if not 0 <= time.time() - float(fresh["observed_at"]) <= self.cfg["MAX_MARKET_DATA_AGE_SECONDS"]:
                return finish("REJECTED", "Prisobservation för gammal för order")
            if dec(fresh["liquidity_usd"]) < liquidity * D("0.9"):
                return finish("REJECTED", "Likviditet föll under riskkontrollen")
            fresh_price = dec(fresh["price_usd"])
            drift = abs(fresh_price / price - ONE) * HUNDRED
            fresh_momentum = (fresh_price / history[0][1] - ONE) * HUNDRED
            if drift > self.cfg["BASE_SLIPPAGE_PCT"] or not D("1") <= fresh_momentum <= D("10"):
                return finish("REJECTED", "Priset flyttade sig för långt under riskkontrollen")
            fill = simulate_paper_buy(self.cfg["TRADE_BUDGET_USD"], fresh["price_usd"],
                                      fresh["liquidity_usd"], meta["decimals"], config=self.cfg)
            # Check that the original position can also be valued on an exit.
            simulate_paper_sell(fill["raw_amount"], meta["decimals"], fresh["price_usd"],
                                fresh["liquidity_usd"], config=self.cfg)
            pos_id = self.state.record_open_position(mint, pair["symbol"], pair["pair_address"],
                meta["decimals"], fill["execution_price"], self.cfg["TRADE_BUDGET_USD"],
                fill["net_token_value"], str(fill["raw_amount"]), fill["fee_usd"],
                fill["price_impact_pct"], f"paper_buy_{time.time_ns()}", True)
            return finish("BOUGHT", "Experimentell paper-signal; lönsamhet ej verifierad", pos_id=pos_id,
                          symbol=pair["symbol"], mint=mint, usd_invested=self.cfg["TRADE_BUDGET_USD"])
        except (ValueError, KeyError, TypeError, MarketDataUnavailableError) as exc:
            return finish("REJECTED", str(exc))

    def reconcile_pending_transactions(self):
        if self.state.get_unresolved_pending_txs():
            LOGGER.error(LIVE_BLOCK_REASON + ". Befintliga transaktioner lämnas orörda för avstämning.")

    def print_hud(self, is_paper=True):
        if not is_paper:
            print(LIVE_BLOCK_REASON)
            return
        print_performance(self.state.performance_report(self.dex))

    def run_live_loop(self, poll_interval=5):
        print(LIVE_BLOCK_REASON)

    def export_obsidian_report(self, mode):
        if mode == "LIVE_SESSION":
            return
        report = self.state.performance_report(self.dex)
        folder = Path(self.cfg["OBSIDIAN_VAULT_PATH"]) / "paper_reports"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{time.time_ns()}-{safe_text(mode)}.json"
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"Rapport: {target}")


def print_performance(summary):
    print("\nPAPER-RESULTAT — simulerat, ej bevisad live-lönsamhet")
    print(f"Kassa: ${summary['cash']:.2f} | Avgifter: ${summary['fees']:.2f}")
    if summary.get("valuation_complete", True):
        print(f"Nettoportfölj: ${summary['total_port']:.2f} | Resultat: ${summary['net_pnl']:+.2f} ({summary['net_pnl_pct']:+.2f}%)")
    else:
        print("Portföljvärde och total PnL: OKÄNT — minst ett innehav kan inte värderas för försäljning.")
    print(f"Avslutade positioner: {summary['total_trades']} | Vinster: {summary['winning_trades']} | Förluster: {summary['losing_trades']}")
    print("Ett positivt scenario är inget bevis på en lönsam strategi. Öppen moonbag är inte realiserad vinst.")


def run_test_mode(bot):
    print("1: Offline CSV-replay (kräver historiskt pris + likviditet)\n2: 30 min paper med ny isolerad databas")
    choice = safe_input("Välj [1/2]: ")
    if choice == "2":
        cfg = dict(bot.cfg)
        cfg["DB_PATH"] = str(Path(bot.cfg["DB_PATH"]).parent / f"paper_test_{time.time_ns()}.db")
        isolated = GodlikeBot(cfg)
        isolated.run_paper_loop(1800, 5)
    else:
        run_real_solana_replay(bot)


def run_live_mode_interactive(bot):
    print(LIVE_BLOCK_REASON)


def run_security_unit_tests():
    import unittest
    tests = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_*.py")
    return unittest.TextTestRunner(verbosity=2).run(tests).wasSuccessful()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description="GODLIKE 2026 — experimentell paperhandel och granskbar exit-replay")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--test", action="store_true")
    modes.add_argument("--realtime", action="store_true", help="Paperhandel och observationslogg")
    modes.add_argument("--live", action="store_true", help="Visar kvarvarande hinder; live är spärrat")
    modes.add_argument("--unit-tests", action="store_true")
    modes.add_argument("--mcp", action="store_true")
    modes.add_argument("--replay-csv", help="CSV med timestamp,open,high,low,close,liquidity_usd")
    parser.add_argument("--decimals", type=int, help="Verifierade token-decimaler för CSV")
    parser.add_argument("--cash", type=dec, default=CONFIG["INITIAL_CASH_USD"])
    parser.add_argument("--trade-budget", type=dec, default=CONFIG["TRADE_BUDGET_USD"])
    parser.add_argument("--db", help="Explicit paperdatabas; befintlig historik behålls")
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--report-dir", default=str(Path(__file__).parent / "reports"))
    parser.add_argument("--stress", action="store_true", help="CSV: kör även 2x gas/DEX fee/slippage")
    args = parser.parse_args()
    if args.unit_tests:
        raise SystemExit(0 if run_security_unit_tests() else 1)
    if args.live:
        print(LIVE_BLOCK_REASON)
        raise SystemExit(2)
    if args.cash <= ZERO or args.trade_budget <= ZERO or args.trade_budget > args.cash or args.duration <= 0:
        parser.error("Kapital, orderstorlek och tid måste vara positiva; order <= kapital")
    if args.stress and not args.replay_csv:
        parser.error("--stress kräver --replay-csv")
    cfg = dict(CONFIG)
    cfg.update(INITIAL_CASH_USD=args.cash, TRADE_BUDGET_USD=args.trade_budget,
               OBSIDIAN_VAULT_PATH=args.report_dir, REPLAY_CSV_PATH=args.replay_csv,
               REPLAY_TOKEN_DECIMALS=args.decimals)
    cfg["DB_PATH"] = args.db or str(Path(__file__).parent / f"paper_session_{time.time_ns()}.db")
    if args.replay_csv and args.decimals is None:
        parser.error("--replay-csv kräver --decimals")
    if args.replay_csv:
        from types import SimpleNamespace
        bot = SimpleNamespace(cfg=cfg)
        summary = run_real_solana_replay(bot, csv_path=args.replay_csv, decimals=args.decimals, output_dir=args.report_dir)
        if not summary:
            raise SystemExit(2)
        print_performance(summary)
        if args.stress:
            stress_cfg = dict(cfg)
            for key in ("ESTIMATED_GAS_USD", "DEX_POOL_FEE_PCT", "BASE_SLIPPAGE_PCT"):
                stress_cfg[key] *= D(2)
            bot.cfg = stress_cfg
            print("\nKOSTNADSSTRESS: 2x antagen gas, poolavgift och ogynnsam prisrörelse")
            stress = run_real_solana_replay(bot, csv_path=args.replay_csv, decimals=args.decimals,
                                           output_dir=str(Path(args.report_dir) / "stress_2x"))
            if not stress:
                raise SystemExit(2)
            print_performance(stress)
    else:
        bot = GodlikeBot(cfg)
        if args.mcp:
            MCPServer(bot).run_stdio()
        elif args.realtime:
            bot.run_paper_loop(args.duration, 5)
        elif args.test:
            run_test_mode(bot)
        else:
            interactive_menu(bot)
