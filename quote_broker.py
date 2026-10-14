"""Connect the existing strategy/exit monitor to quote execution and raw assets.

The legacy position tables are the strategy/display projection; raw balances
and order evidence are canonical. Both projections commit on the same SQLite
connection. No signing, submission or LIVE adapter is enabled by this module.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import json
import threading
import time

from execution_engine import ExecutionEngine, ExecutionPolicy, PaperExecutionAdapter, SOL_MINT, validate_execution_quote
from execution_ledger import RawPortfolioLedger, LedgerLimits, NATIVE_SOL
from order_lifecycle import OrderIntent, OrderStore, OrderError
from quote_provider import JupiterV2QuoteProvider, QuoteRequest, QuoteUnavailable

D = Decimal
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
VERSION = "moderate-momentum-quote-v1"


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Missing numeric evidence")
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite numeric evidence")
    return result


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str, allow_nan=False)


class QuoteBroker:
    def __init__(self, bot, *, provider=None, clock=time.time, sleep=time.sleep):
        self.bot, self.state, self.cfg = bot, bot.state, bot.cfg
        self.provider = provider or JupiterV2QuoteProvider()
        self.clock, self.sleep = clock, sleep
        self.store = OrderStore(self.cfg["DB_PATH"], clock=clock)
        self.policy = ExecutionPolicy(
            max_price_impact_pct=number(self.cfg["MAX_ALLOWED_PRICE_IMPACT_PCT"]),
            quote_max_age_ms=int(self.cfg.get("QUOTE_MAX_AGE_MS", 3000)),
            paper_delay_ms=int(self.cfg.get("PAPER_EXECUTION_DELAY_MS", 500)),
            paper_adverse_bps=int(self.cfg.get("PAPER_ADVERSE_BPS", 50)),
            estimated_network_fee_lamports=int(self.cfg.get("PAPER_NETWORK_FEE_LAMPORTS", 3105000)))
        self.slippage = int(number(self.cfg["BASE_SLIPPAGE_PCT"]) * 100)
        self.ledger = self.engine = None
        self._mark_cache = None
        self._funding_lock = threading.RLock()
        self.entry_allowed = lambda: True
        with self.store._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS execution_strategy_context (
                    order_id TEXT PRIMARY KEY REFERENCES execution_orders(id),
                    context_json TEXT NOT NULL, context_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_position_links (
                    raw_position_id INTEGER PRIMARY KEY REFERENCES execution_positions(id),
                    strategy_position_id INTEGER NOT NULL UNIQUE REFERENCES positions(id)
                );
                CREATE TABLE IF NOT EXISTS execution_funding_evidence (
                    id INTEGER PRIMARY KEY CHECK(id=1), evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_portfolio_marks (
                    id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, evidence_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS execution_context_no_update
                    BEFORE UPDATE ON execution_strategy_context BEGIN
                    SELECT RAISE(ABORT,'Strategy context is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS execution_context_no_delete
                    BEFORE DELETE ON execution_strategy_context BEGIN
                    SELECT RAISE(ABORT,'Strategy context is immutable'); END;
            """)
            prior = conn.execute("SELECT evidence_json FROM execution_funding_evidence WHERE id=1").fetchone()
            if prior is None and conn.execute("SELECT 1 FROM trade_history LIMIT 1").fetchone():
                raise ValueError("Offertläget kräver ny --db; befintlig poolmodellhistorik får inte blandas med offertutfall")
        if prior is not None:
            evidence = json.loads(prior[0])
            if evidence["policy"] != json.loads(encode(asdict(self.policy))):
                raise ValueError("Offertkostnader skiljer sig från sparad körning; välj ny --db")
            self._connect_ledger(LedgerLimits(**evidence["limits"]))

    def _connect_ledger(self, limits):
        self.ledger = RawPortfolioLedger(self.store, limits)
        def independent_risk(intent, quote):
            approval = self.ledger.risk_check(intent, quote, estimated_fee_lamports=self.policy.estimated_network_fee_lamports)
            if not approval["allowed"]:
                return approval
            return self._market_risk(intent, quote)
        adapter = PaperExecutionAdapter(self.provider, clock=self.clock, sleep=self.sleep,
                                        apply_confirmation=self._post_confirmation)
        self.engine = ExecutionEngine(self.store, self.provider, {"PAPER": adapter}, independent_risk,
                                      policy=self.policy, clock=self.clock)

    def _fx(self):
        observations = {}
        for mint in (USDC, SOL_MINT):
            data = self.bot.dex.get_token_data(mint)
            if not data or not data.get("pair_address"):
                raise ValueError("Verifierbar USDC/SOL-värdering saknas")
            price = number(data.get("price_usd"))
            age = self.clock() - float(data.get("observed_at", 0))
            if price <= 0 or not 0 <= age <= self.cfg["MAX_MARKET_DATA_AGE_SECONDS"]:
                raise ValueError("USDC/SOL-värdering är ogiltig eller inaktuell")
            observations[mint] = {"price_usd": str(price), "observed_at": data["observed_at"],
                                  "pair_address": data["pair_address"], "source": "DexScreener"}
        return observations

    def _check_fx(self, fx):
        for mint in (USDC, SOL_MINT):
            if number(fx[mint]["price_usd"]) <= 0 or not 0 <= self.clock()-float(fx[mint]["observed_at"]) <= self.cfg["MAX_MARKET_DATA_AGE_SECONDS"]:
                raise ValueError("Orderns sparade FX-underlag har löpt ut")

    def ensure_funded(self):
        with self._funding_lock:
            return self._ensure_funded()

    def _ensure_funded(self):
        if self.ledger is not None:
            return
        fx = self._fx()
        initial = self.state.get_wallet()["initial_cash_usd"]
        reserve_usd = min(number(self.cfg.get("PAPER_FEE_ALLOCATION_USD", "2")), initial / 10)
        if not 0 < reserve_usd < initial:
            raise ValueError("Ogiltig finansiering av SOL-avgiftsreserv")
        usdc_price, sol_price = number(fx[USDC]["price_usd"]), number(fx[SOL_MINT]["price_usd"])
        settlement = int(((initial-reserve_usd)/usdc_price*D(10**6)).to_integral_value(rounding=ROUND_DOWN))
        native = int((reserve_usd/sol_price*D(10**9)).to_integral_value(rounding=ROUND_DOWN))
        maximum = int((number(self.cfg["TRADE_BUDGET_USD"])/usdc_price*D(10**6)).to_integral_value(rounding=ROUND_DOWN))
        limits = LedgerLimits(USDC, maximum, int(self.cfg["MAX_OPEN_POSITIONS"]), self.policy.estimated_network_fee_lamports)
        self._connect_ledger(limits)
        evidence = {"initial_capital_usd": str(initial), "fx": fx, "limits": asdict(limits),
                    "policy": json.loads(encode(asdict(self.policy))), "native_sol_lamports": native,
                    "usdc_raw": settlement, "actual_money_used": False,
                    "funding_assumption": "Pre-funded virtual USDC/SOL wallet; both assets come from total capital",
                    "rounding_unallocated_usd": str(initial-D(settlement)/D(10**6)*usdc_price-D(native)/D(10**9)*sol_price)}
        def persist_funding(conn):
            conn.execute("INSERT INTO execution_funding_evidence VALUES(1,?)", (encode(evidence),))
        try:
            created = self.ledger.initialize_paper(settlement_raw=settlement, native_sol_lamports=native,
                                                   on_initialize=persist_funding)
            if not created:
                with self.store._connection() as conn:
                    if not conn.execute("SELECT 1 FROM execution_funding_evidence WHERE id=1").fetchone():
                        raise ValueError("Råsaldots finansiering saknar bevarat FX-underlag")
        except BaseException:
            self.ledger = self.engine = None
            raise

    def _save_context(self, intent, context):
        order = self.store.create(intent)
        encoded = encode(context)
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT context_json FROM execution_strategy_context WHERE order_id=?", (order["id"],)).fetchone()
            if existing is None:
                conn.execute("INSERT INTO execution_strategy_context VALUES(?,?,?)",
                             (order["id"], encoded, hashlib.sha256(encoded.encode()).hexdigest()))
            # An idempotent retry retains its original immutable decision/FX.
            conn.commit()
        return order

    def _context(self, conn, order_id):
        row = conn.execute("SELECT * FROM execution_strategy_context WHERE order_id=?", (order_id,)).fetchone()
        if row is None or hashlib.sha256(row["context_json"].encode()).hexdigest() != row["context_hash"]:
            raise OrderError("Order lacks intact strategy/FX context")
        return json.loads(row["context_json"])

    def _market_risk(self, intent, quote):
        try:
            with self.store._connection() as conn:
                order = conn.execute("SELECT id FROM execution_orders WHERE idempotency_key=?", (intent.idempotency_key,)).fetchone()
                context = self._context(conn, order["id"])
            self._check_fx(context["fx"])
            current_fx = self._fx()
            for mint in (USDC, SOL_MINT):
                if abs(number(current_fx[mint]["price_usd"])/number(context["fx"][mint]["price_usd"])-1)*100 > self.cfg["BASE_SLIPPAGE_PCT"]:
                    raise ValueError("FX-kursen flyttades för långt under orderförberedelsen")
            data = self.bot.dex.get_pair_data(context["mint"], context["pair_address"])
            if (not data or data.get("pair_address") != context["pair_address"] or
                    not 0 <= self.clock()-float(data.get("observed_at", 0)) <= self.cfg["MAX_MARKET_DATA_AGE_SECONDS"] or
                    number(data.get("liquidity_usd")) <= 0):
                raise ValueError("Aktuell marknadsdata för positionens pool saknas")
            if intent.side == "BUY":
                effective_usd = D(intent.amount_raw)/D(10**6)*number(current_fx[USDC]["price_usd"])
                effective_usd += D(self.policy.estimated_network_fee_lamports)/D(10**9)*number(current_fx[SOL_MINT]["price_usd"])
                if effective_usd > self.cfg["TRADE_BUDGET_USD"]:
                    raise ValueError("Köpet inklusive SOL-kostnad överskrider USD-ordergränsen")
                if not self.entry_allowed():
                    raise ValueError("Nya köp pausade/stoppade under orderförberedelsen")
                allowed, reason = self.state.can_open_position(context["mint"], number(self.cfg["TRADE_BUDGET_USD"]), dex_client=self.bot.dex)
                if not allowed:
                    raise ValueError(reason)
                if number(context["liquidity_cap_usd"]) < self.cfg["MIN_LIQUIDITY_USD"]:
                    raise ValueError("Verifierat reservtak understiger minsta likviditet")
                if not 0 <= self.clock()-context["security_checked_at"] <= self.cfg["MAX_MARKET_DATA_AGE_SECONDS"]:
                    raise ValueError("Tokenens säkerhetsunderlag har löpt ut")
                if not 0 <= self.clock()-context["sell_probe"]["received_at"] <= 30:
                    raise ValueError("Säljbarhetskontrollens offert har löpt ut")
                if (number(data["liquidity_usd"]) < number(context["observed_liquidity_usd"]) * D("0.9") or
                        abs(number(data["price_usd"])/number(context["signal_price_usd"])-1)*100 > self.cfg["BASE_SLIPPAGE_PCT"]):
                    raise ValueError("Pris eller likviditet flyttades under offertförberedelsen")
                if quote is not None and quote.out_amount_raw > context["sell_probe_raw"]:
                    raise ValueError("Köpmängden överskrider verifierad säljoffertstorlek")
            if quote is not None:
                self._validate_target_route(quote, context["mint"], context["pair_address"])
            return {"allowed": True, "source": "existing_strategy_and_portfolio_risk", "context_order_id": order["id"], "fx": current_fx}
        except (ValueError, KeyError, TypeError, QuoteUnavailable) as exc:
            return {"allowed": False, "reason": str(exc)}

    @staticmethod
    def _validate_target_route(quote, mint, pool):
        route = json.loads(quote.route_json)
        supported = {USDC, SOL_MINT, mint}
        touched = False
        for step in route:
            info = step["swapInfo"]
            if info["inputMint"] not in supported or info["outputMint"] not in supported:
                raise ValueError("Route har ännu ej verifierad mellanliggande token")
            if mint in (info["inputMint"], info["outputMint"]):
                touched = True
                if info["ammKey"] != pool:
                    raise ValueError("Offerten använder en annan målpool än säkerhetskontrollen")
        if not touched:
            raise ValueError("Målpool saknas i offerten")

    def _quote(self, input_mint, output_mint, amount, *, valuation=False):
        policy = replace(self.policy, quote_max_age_ms=30000) if valuation else self.policy
        request = QuoteRequest(input_mint, output_mint, amount, self.slippage, policy.quote_max_age_ms, policy.exclude_routers)
        quote = self.provider.quote(request)
        if quote.request != request:
            raise QuoteUnavailable("Offertens identitet avviker från förfrågan")
        validate_execution_quote(quote, policy, self.clock())
        return quote

    def buy(self, mint, pair, metadata, liquidity_cap):
        try:
            self.ensure_funded()
            fx = self._fx()
            fee = D(self.policy.estimated_network_fee_lamports)/D(10**9)*number(fx[SOL_MINT]["price_usd"])
            # The configured USD order budget includes the modeled network fee.
            spend = number(self.cfg["TRADE_BUDGET_USD"])/(1+number(self.cfg["BASE_SLIPPAGE_PCT"])/100) - fee
            amount = int((spend/number(fx[USDC]["price_usd"])*D(10**6)).to_integral_value(rounding=ROUND_DOWN))
            amount = min(amount, self.ledger.limits.max_order_raw)
            probe_raw = int((spend/number(pair["price_usd"])*D(10**metadata["decimals"])*D("1.1")).to_integral_value(rounding=ROUND_UP))
            probe = self._quote(mint, USDC, probe_raw, valuation=True)
            self._validate_target_route(probe, mint, pair["pair_address"])
            intent = OrderIntent(f"{VERSION}:{mint}:{pair['observed_at']}", "PAPER", "BUY", USDC, mint,
                                 amount, self.slippage, VERSION)
            context = {"mint": mint, "symbol": pair["symbol"], "pair_address": pair["pair_address"],
                       "decimals": metadata["decimals"], "liquidity_cap_usd": str(liquidity_cap),
                       "observed_liquidity_usd": str(pair["liquidity_usd"]), "signal_price_usd": str(pair["price_usd"]),
                       "security_checked_at": float(metadata["validated_at"]), "security_metadata": metadata,
                       "fx": fx, "reason": "MOMENTUM_ENTRY", "sell_probe_raw": probe_raw, "sell_probe": probe.to_dict()}
            self._save_context(intent, context)
            result = self.engine.execute(intent)
            if result["state"] != "CONFIRMED":
                return {"status": "REJECTED", "reason": f"Offertorder {result['state']}: {self._failure_reason(result)}", "order_id": result["id"]}
            posting = self.store.get_posting(result["id"])["result"]
            self._mark_cache = None
            return {"status": "BOUGHT", "reason": "Offertbaserat paperköp; lönsamhet och kedjeexekvering ej verifierade",
                    "pos_id": posting["strategy_position_id"], "order_id": result["id"], "symbol": pair["symbol"],
                    "mint": mint, "usd_invested": posting["usd_invested"]}
        except (ValueError, KeyError, TypeError, QuoteUnavailable) as exc:
            return {"status": "REJECTED", "reason": str(exc)}

    def _failure_reason(self, order):
        events = self.store.events(order["id"])
        return str(events[-1]["evidence"].get("reason", "Order inväntar verifierat utfall"))

    def sell(self, position, raw_amount, reason, *, is_paper=True):
        if not is_paper:
            raise ValueError("LIVE adapter är inte aktiverad")
        try:
            self.ensure_funded()
            fx = self._fx()
            data = self.bot.dex.get_pair_data(position["mint"], position["pair_address"])
            if not data:
                raise ValueError("Säljorder saknar aktuell poolobservation")
            with self.store._connection() as conn:
                link = conn.execute("SELECT raw_position_id FROM execution_position_links WHERE strategy_position_id=?", (position["id"],)).fetchone()
            if link is None:
                raise ValueError("Position saknar råsaldo-/orderkoppling")
            intent = OrderIntent(f"exit:{position['id']}:{reason}:{position['remaining_raw']}:{data['observed_at']}",
                                 "PAPER", "SELL", position["mint"], USDC, raw_amount, self.slippage, VERSION, link[0])
            self._save_context(intent, {"mint": position["mint"], "pair_address": position["pair_address"],
                                       "strategy_position_id": position["id"], "fx": fx, "reason": reason,
                                       "observed_price_usd": str(data["price_usd"])})
            result = self.engine.execute(intent)
            if result["state"] != "CONFIRMED":
                raise ValueError(f"Säljorder {result['state']}: {self._failure_reason(result)}")
            receipt = self.store.get_posting(result["id"])["result"]
            self._mark_cache = None
            return {"booked": True, "tx_hash": result["id"], "execution_price": number(receipt["execution_price_usd"]),
                    "gross_usd": number(receipt["gross_usd"]), "fee_usd": number(receipt["fee_usd"]),
                    "net_usd": number(receipt["net_usd"]), "price_impact_pct": D(0)}
        except QuoteUnavailable as exc:
            raise ValueError(str(exc)) from exc

    def _post_confirmation(self, conn, order):
        context = self._context(conn, order["id"])
        self._check_fx(context["fx"])
        intent, confirmation = order["intent"], order["confirmation"]
        final_fx = confirmation.get("risk_before_confirmation", {}).get("fx")
        if final_fx is None:
            raise OrderError("Confirmed outcome lacks final FX risk evidence")
        self._check_fx(final_fx)
        if intent["side"] == "SELL":
            strategy = conn.execute("SELECT remaining_raw FROM positions WHERE id=?", (context["strategy_position_id"],)).fetchone()
            inventory = conn.execute("SELECT remaining_raw FROM execution_positions WHERE id=?", (intent["position_id"],)).fetchone()
            if strategy is None or inventory is None or strategy[0] != inventory[0]:
                raise OrderError("Strategy quantity and canonical raw inventory disagree; reconcile before selling")
        receipt = self.ledger.apply_confirmation(conn, order)
        fx_usdc, fx_sol = number(final_fx[USDC]["price_usd"]), number(final_fx[SOL_MINT]["price_usd"])
        fee = D(confirmation["fee_paid_raw"])/D(10**9)*fx_sol
        if intent["side"] == "BUY":
            token_value = D(confirmation["input_spent_raw"])/D(10**6)*fx_usdc
            units = D(confirmation["output_received_raw"])/D(10**context["decimals"])
            price = token_value/units
            invested = token_value + fee
            strategy_id = self.state.record_open_position(context["mint"], context["symbol"], context["pair_address"],
                context["decimals"], price, invested, token_value, str(confirmation["output_received_raw"]), fee,
                abs(number(order["quote"]["quote"]["price_impact_pct"])), order["id"], True,
                entry_liquidity_cap_usd=number(context["liquidity_cap_usd"]), _connection=conn)
            conn.execute("INSERT INTO execution_position_links VALUES(?,?)", (receipt["position_id"], strategy_id))
            receipt.update(strategy_position_id=strategy_id, usd_invested=str(invested))
        else:
            strategy_id = context["strategy_position_id"]
            position = conn.execute("SELECT * FROM positions WHERE id=?", (strategy_id,)).fetchone()
            link = conn.execute("SELECT raw_position_id FROM execution_position_links WHERE strategy_position_id=?", (strategy_id,)).fetchone()
            if position is None or link is None or link[0] != receipt["position_id"]:
                raise OrderError("Strategy and raw position identities disagree")
            gross = D(confirmation["output_received_raw"])/D(10**6)*fx_usdc
            net = gross-fee
            price = gross/(D(confirmation["input_spent_raw"])/D(10**position["decimals"]))
            remaining = int(position["remaining_raw"])-confirmation["input_spent_raw"]
            reason = context["reason"]
            if reason in {"TP1", "TP2"}:
                booked = self.state.apply_partial_sell(strategy_id, int(reason[-1]), str(remaining),
                    D(confirmation["input_spent_raw"])/D(position["initial_raw"]), gross, fee, net, price, True,
                    _connection=conn, _order_id=order["id"])
                peak = max(number(position["peak_price_usd"]), number(context["observed_price_usd"]))
                stop = number(position["stop_loss_price"])
                trailing = int(position["trailing_active"])
                if reason == "TP1":
                    stop = max(stop, number(position["entry_price_usd"])*D("1.15"))
                    trailing = max(trailing, 1)
                else:
                    trailing = 2
                    stop = max(stop, peak*(1-number(self.cfg["MOONBAG_TRAILING_PCT"])/100))
                conn.execute("UPDATE positions SET peak_price_usd=?,stop_loss_price=?,trailing_active=? WHERE id=?",
                             (str(peak), str(stop), trailing, strategy_id))
            else:
                if remaining != 0:
                    raise OrderError("A final exit must sell the entire remaining position")
                booked = self.state.close_position(strategy_id, price, reason, net, fee, order["id"], True, _connection=conn)
            if not booked:
                raise OrderError("Strategy exit already posted or inconsistent")
            receipt.update(strategy_position_id=strategy_id, execution_price_usd=str(price), gross_usd=str(gross),
                           net_usd=str(net), fee_usd=str(fee))
        return receipt

    def mark_portfolio(self):
        now = self.clock()
        with self.store._connection() as conn:
            revision = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM execution_order_postings").fetchone()[0]
        if self._mark_cache is not None and self._mark_cache[2] == revision and 0 <= now-self._mark_cache[0] < 10:
            return self._mark_cache[1]
        quotes, unpriced, details = [], [], {"execution_model": "JUPITER_V2_DELAYED_REQUOTE", "network_costs_calibrated": False}
        try:
            self.ensure_funded()
            fx = self._fx()
            snapshot = self.ledger.snapshot()
            positions = self.state.get_open_positions(True)
            raw_positions = {p["id"]: p for p in snapshot["positions"] if p["status"] == "OPEN"}
            if len(positions) != len(raw_positions):
                raise ValueError("Antalet strategi- och råsaldopositioner avviker; avstämning krävs")
            with self.store._connection() as conn:
                links = {r["strategy_position_id"]: r["raw_position_id"] for r in conn.execute("SELECT * FROM execution_position_links")}
            for position in positions:
                inventory = raw_positions.get(links.get(position["id"]))
                if (inventory is None or inventory["mint"] != position["mint"] or
                        int(inventory["remaining_raw"]) != int(position["remaining_raw"])):
                    raise ValueError("Strategins innehav avviker från råsaldot; avstämning krävs")
            fee_raw = self.policy.estimated_network_fee_lamports
            native = snapshot["balances_raw"].get(NATIVE_SOL, 0)
            cash = D(snapshot["balances_raw"].get(USDC, 0))/D(10**6)*number(fx[USDC]["price_usd"])
            open_value = D(0)
            for position in positions:
                try:
                    quote = self._quote(position["mint"], USDC, int(position["remaining_raw"]), valuation=True)
                    self._validate_target_route(quote, position["mint"], position["pair_address"])
                    quotes.append(quote)
                    open_value += D(quote.min_out_amount_raw)/D(10**6)*number(fx[USDC]["price_usd"])
                except (ValueError, QuoteUnavailable) as exc:
                    unpriced.append({"id": position["id"], "mint": position["mint"], "reason": str(exc)})
            # Fees for all full exits and the native-SOL conversion are paid
            # from native cash. Do not subtract them again from token outputs.
            after_exit_fees = native - len(positions)*fee_raw
            if after_exit_fees < 0:
                raise ValueError("SOL-reserv kan inte finansiera samtliga modellerade exits")
            convertible = after_exit_fees-fee_raw
            if convertible > 0:
                native_quote = self._quote(SOL_MINT, USDC, convertible, valuation=True)
                quotes.append(native_quote)
                cash += D(native_quote.min_out_amount_raw)/D(10**6)*number(fx[USDC]["price_usd"])
            else:
                details["unrecoverable_fee_dust_lamports"] = after_exit_fees
            self._check_fx(fx)
            for quote in quotes:
                quote.assert_fresh(self.clock())
            with self.store._connection() as conn:
                if conn.execute("SELECT COALESCE(MAX(rowid),0) FROM execution_order_postings").fetchone()[0] != revision:
                    raise ValueError("Portföljen ändrades under värderingen; ny observation krävs")
            details.update(fx=fx, quotes=[q.to_dict() for q in quotes], native_sol_before_fees=native,
                           exit_fees_reserved_lamports=len(positions)*fee_raw,
                           valuation_window_seconds=self.clock()-now,
                           native_conversion_assumption="Configured SOL charge includes a conservative unrecovered account/wrap allowance; not calibrated")
            complete = not unpriced
            mark = {"cash_usd": cash, "open_value_usd": open_value if complete else None,
                    "equity_usd": cash+open_value if complete else None, "complete": complete,
                    "unpriced_positions": unpriced, "details": details}
            # Spendable inventory uses observed FX, never liquidation proceeds
            # as if the positions had actually been sold.
            actual_cash = D(snapshot["balances_raw"].get(USDC, 0))/D(10**6)*number(fx[USDC]["price_usd"])+D(native)/D(10**9)*number(fx[SOL_MINT]["price_usd"])
            with self.store._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT COALESCE(MAX(rowid),0) FROM execution_order_postings").fetchone()[0] != revision:
                    raise ValueError("Portföljen ändrades före publicering av värdering")
                conn.execute("UPDATE wallet SET cash_usd=? WHERE id=1", (str(actual_cash),))
                conn.commit()
        except (ValueError, TypeError, KeyError, QuoteUnavailable) as exc:
            mark = {"cash_usd": self.state.get_wallet()["cash_usd"], "open_value_usd": None, "equity_usd": None,
                    "complete": False, "unpriced_positions": unpriced + [{"id": None, "mint": "PORTFOLIO", "reason": str(exc)}], "details": details}
        mark["as_of"] = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
        with self.store._connection() as conn:
            conn.execute("INSERT INTO execution_portfolio_marks(timestamp,evidence_json) VALUES(?,?)", (self.clock(), encode(mark)))
        self._mark_cache = (self.clock(), mark, revision)
        return mark
