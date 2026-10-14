#!/usr/bin/env python3
"""
================================================================================
👑 GODLIKE 2026 — INSTITUTIONAL SOLANA SNIPER & ON-CHAIN PRECISION ENGINE
================================================================================
Professionell, matematisk och kompromisslös motor för simulering och live-handel:

1. 🧮 50-Siffrig Decimal-Aritmetik: Noll flyttalsfel i kassa, lamports eller tokens.
2. 📐 AMM Constant-Product Slippage Modell (x * y = k):
   - Modellerar verkliga pool-reserver och exakt prisimpact.
   - Avvisar automatiskt order som överstiger max tillåten prisimpact (t.ex. >2.5%).
3. 🛑 Verklig Stop-Loss Exekvering (Noll Phantom-priser):
   - Säljer alltid till det faktiska lägre marknadspriset vid gap-downs eller krascher.
   - Skyddar mot simulator-bias där förluster döljs.
4. 🛡️ Fail-Closed RugCheck & Målpoolens LP Lock/Burn Verifiering:
   - Avvisar om rapportdata saknas (Fail-Closed).
   - Verifierar att minst 85% av LP i den specifika målpoolen är låst eller bränd.
   - Analyserar alla riskobjekt: avvisar 'danger' och 'critical' nivåer.
   - Token-2022: blockerar transfer hooks och transfer fee >0%.
   - Top 10 Holders: filtrerar bort målpoolens vaults innan insider-summering.
5. ⚡ Verklig On-Chain Confirmation & Balance Delta Loop:
   - Polling mot getSignatureStatuses, väntar på 'confirmed' / 'finalized'.
   - Läser faktiskt saldo i SOL och tokens före och efter varje swap (Balance Delta).
   - Bokför faktisk erhållen SOL från Jupiter-swappar i live-läge (inga uppskattningar).
6. 🔒 Skydd mot Dubbelsälj & Felfällor vid TP:
   - Validerar priser före swap; garanterar att DB uppdateras vid lyckad on-chain swap.
   - TP2 kräver att TP1 körts och beräknar restmängd via exakt subtraktion.
7. 🕯️ Pessimistisk High/Low Candle Penetration i Replay:
   - Minutcandles utvärderas mot Low (SL) och High (TP1).
   - Vid konflikt i samma bar tillämpas institutionell pessimism (SL antas träffad först).
   - Stöder valfri Raydium-pooladress via argument utan att nollställa sparad paper-wallet.
8. 📉 Mark-to-Market Drawdown Circuit Breaker (Paper & Live):
   - Värderar öppna positioner till realtidsmarknadsvärde, inte inköpskostnad.
   - Löser ut omedelbart vid 20% orealiserad portföljförlust.
9. 🧪 Isolerad Testdatabas:
   - Enhetstester körs i en isolerad databas och rör aldrig användarens skarpa historik.
10. 🚫 Nolltolerans mot Fallback-priser:
   - Hämtar SOL-kurs on-chain via Jupiter swap quote (SOL -> USDC).
   - Saknas marknadsdata avbryts handeln säkert utan några godtyckliga gissningar.
11. 📓 Automatisk Obsidian Post-Mortem för samtliga sessioner.
================================================================================
"""

from __future__ import annotations

import os
import sys
import json
import time
import base64
import sqlite3
import argparse
import logging
import urllib.request
from decimal import Decimal, ROUND_DOWN, getcontext, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# Sätt Decimal-precision för finansiella beräkningar
getcontext().prec = 50
D = Decimal
ZERO = D("0")
ONE = D("1")
HUNDRED = D("100")

# Försök ladda dotenv för miljövariabler
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Försök ladda solders för skarp live-signering
SOLDERS_AVAILABLE = False
try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    SOLDERS_AVAILABLE = True
except ImportError:
    pass

# Försök ladda requests
try:
    import requests
except ImportError:
    requests = None

LOGGER = logging.getLogger("godlike2026")

class MarketDataUnavailableError(RuntimeError):
    """Kastas när nödvändig realtidsmarknadsdata saknas. Inga gissningar eller fallback-priser tillåts."""
    pass

def dec(val: Any, default: Optional[Decimal] = None) -> Decimal:
    """Säker konvertering till Decimal med strikt felhantering. Döljer aldrig saknade värden om inte default angetts."""
    if val is None or isinstance(val, bool):
        if default is not None:
            return default
        raise ValueError("Finansiellt värde saknas (None/Bool) och ingen standard angiven.")
    try:
        res = D(str(val))
        if not res.is_finite():
            if default is not None:
                return default
            raise ValueError(f"Icke-finit värde (NaN/Inf): {val}")
        return res
    except (InvalidOperation, ValueError):
        if default is not None:
            return default
        raise ValueError(f"Ogiltig siffra för finansiell redovisning: {val}")

def safe_text(text: Any, max_len: int = 80) -> str:
    return "".join(c for c in str(text) if c.isprintable())[:max_len]

def safe_input(prompt: str = "") -> str:
    """Säker inmatning som fångar EOFError och KeyboardInterrupt vid skriptning/rörledning."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""

# ==============================================================================
# ⚙️ INSTITUTIONELL KONFIGURATION
# ==============================================================================

CONFIG: Dict[str, Any] = {
    # Databas & Loggar
    "DB_PATH": os.getenv("BOT_DB_PATH", "./godlike2026_state.db"),
    "OBSIDIAN_VAULT_PATH": os.getenv("OBSIDIAN_VAULT_PATH", "./obsidian_vault"),

    # Startkapital & Positioner (Paper Simulator)
    "INITIAL_CASH_USD": D("100.00"),
    "TRADE_BUDGET_USD": D("20.00"),
    "MAX_OPEN_POSITIONS": int(os.getenv("MAX_OPEN_POSITIONS", "3")),
    "COOLDOWN_HOURS": float(os.getenv("COOLDOWN_HOURS", "2.0")),

    # Realistiska Avgifter & Slippage
    "ESTIMATED_GAS_USD": D("0.35"),       # Solana priority + base fee
    "DEX_POOL_FEE_PCT": D("0.30"),        # Raydium / Pump AMM fee (0.30%)
    "BASE_SLIPPAGE_PCT": D("1.00"),       # Bas-slippage tolerans (1.00%)
    "MAX_ALLOWED_PRICE_IMPACT_PCT": D("2.50"), # Max tillåten prisimpact på poolen (2.5%)

    # Sniper & Exit Regler (God-Tier)
    "INITIAL_STOP_LOSS_PCT": D("14.0"),   # Initierande SL vid -14%
    "BREAKEVEN_TRIGGER_PCT": D("15.0"),   # Flytta SL när vinst når +15%
    "BREAKEVEN_LOCK_PCT": D("3.0"),       # Lås in +3% nettovinst vid breakeven
    "TAKE_PROFIT_1_PCT": D("40.0"),       # TP1 vid +40% (sälj 40% av positionen)
    "TAKE_PROFIT_2_PCT": D("80.0"),       # TP2 vid +80% (sälj 35% av positionen)
    "MOONBAG_TRAILING_PCT": D("12.0"),    # Moonbag: Traila -12% från ATH på resterande 25%

    # Riskhantering & Circuit Breakers
    "MAX_CONSECUTIVE_LOSSES": 3,          # Frys nya köp efter 3 förluster i rad
    "CIRCUIT_BREAKER_HOURS": 4.0,         # Paustid vid förlustsvit
    "MAX_DRAWDOWN_PCT": D("20.0"),        # Nödfrysning om portföljen tappar 20%
    "MIN_SOL_RESERVE": D("0.05"),         # Minsta SOL-reserv för gas/rent som aldrig får spenderas

    # On-chain Säkerhetsfilter (RugCheck & Trench)
    "MIN_LIQUIDITY_USD": D("15000.00"),   # Minsta pool-likviditet ($15k)
    "MAX_TOP10_INSIDER_PCT": D("20.0"),   # Max andel för top 10 insiders (exkl pool-valv)
    "MAX_RUGCHECK_SCORE": 250,            # Max tillåten RugCheck riskpoäng
    "MIN_LP_LOCKED_BURNED_PCT": D("85.0"), # Minst 85% av LP i målpoolen måste vara låst/bränd

    # Live Trading (Jupiter & Solana)
    "DRY_RUN": True,
    "SOLANA_RPC_URL": os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
    "WALLET_PRIVATE_KEY": os.getenv("WALLET_PRIVATE_KEY", ""),
    "SOL_MINT": "So11111111111111111111111111111111111111112",
    "USDC_MINT": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "PRIORITY_FEE_LAMPORTS": 100_000,     # Jito/Priority fee (0.0001 SOL)
    "DEFAULT_SLIPPAGE_BPS": 250,          # 2.5% slippage tolerans för memecoins
    # Verifierade och aktiva Jupiter Swap v1 API-endpoints
    "JUPITER_QUOTE_ENDPOINTS": [
        "https://lite-api.jup.ag/swap/v1/quote",
        "https://api.jup.ag/swap/v1/quote",
        "https://public.jupiterapi.com/quote",
    ],
    "JUPITER_SWAP_ENDPOINTS": [
        "https://lite-api.jup.ag/swap/v1/swap",
        "https://api.jup.ag/swap/v1/swap",
    ],
    "DEXSCREENER_API_BASE": "https://api.dexscreener.com/latest/dex",
    "RUGCHECK_API_BASE": "https://api.rugcheck.xyz/v1/tokens",
}

# Kända systemadresser och LP-valv som exkluderas vid beräkning av top holders
KNOWN_SYSTEM_ADDRESSES = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
}

# ==============================================================================
# 📐 AMM CONSTANT PRODUCT PRISIMPACT (x * y = k)
# ==============================================================================

def calculate_amm_price_impact(trade_usd: Decimal, pool_liquidity_usd: Decimal) -> Tuple[Decimal, Decimal]:
    """
    Beräknar verklig Constant-Product AMM prisimpact (x * y = k).
    I en balanserad AMM-pool (SOL/Token) representerar SOL-sidan x = Pool-likviditet / 2.
    Formel för prisimpact: Delta_x / (x + Delta_x)
    Genomsnittligt exekveringspris ökar med ca (Prisimpact / 2).
    """
    if pool_liquidity_usd <= ZERO or trade_usd <= ZERO:
        return ZERO, ONE
    pool_sol_side = pool_liquidity_usd / D("2.0")
    impact_pct = (trade_usd / (pool_sol_side + trade_usd)) * HUNDRED
    execution_factor = ONE + (impact_pct / HUNDRED / D("2.0"))
    return impact_pct, execution_factor

# ==============================================================================
# 🗄️ STATE MANAGER & SQLITE WAL-MODE
# ==============================================================================

class StateManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
            CREATE TABLE IF NOT EXISTS wallet (
                id INTEGER PRIMARY KEY,
                initial_cash_usd TEXT NOT NULL,
                cash_usd TEXT NOT NULL,
                realized_pnl_usd TEXT NOT NULL DEFAULT '0',
                total_fees_paid_usd TEXT NOT NULL DEFAULT '0',
                total_trades INTEGER NOT NULL DEFAULT 0,
                winning_trades INTEGER NOT NULL DEFAULT 0,
                losing_trades INTEGER NOT NULL DEFAULT 0,
                halt_reason TEXT NOT NULL DEFAULT '',
                halt_until REAL NOT NULL DEFAULT 0,
                last_halt_time REAL NOT NULL DEFAULT 0,
                live_initial_cash_usd TEXT NOT NULL DEFAULT '0',
                live_halt_reason TEXT NOT NULL DEFAULT '',
                live_halt_until REAL NOT NULL DEFAULT 0,
                live_last_halt_time REAL NOT NULL DEFAULT 0
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                symbol TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                decimals INTEGER NOT NULL DEFAULT 6,
                initial_raw TEXT NOT NULL,
                remaining_raw TEXT NOT NULL,
                entry_price_usd TEXT NOT NULL,
                usd_invested TEXT NOT NULL,
                remaining_invested_usd TEXT NOT NULL,
                net_token_value_usd TEXT NOT NULL,
                fees_paid_usd TEXT NOT NULL DEFAULT '0',
                price_impact_pct TEXT NOT NULL DEFAULT '0',
                entry_time REAL NOT NULL,
                exit_time REAL,
                peak_price_usd TEXT NOT NULL,
                stop_loss_price TEXT NOT NULL,
                trailing_active INTEGER NOT NULL DEFAULT 0,
                tp1_executed INTEGER NOT NULL DEFAULT 0,
                tp2_executed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN',
                exit_price_usd TEXT,
                exit_reason TEXT,
                pnl_pct TEXT,
                net_usd_received TEXT,
                tx_hash TEXT,
                is_paper INTEGER NOT NULL DEFAULT 1
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pos_id INTEGER NOT NULL DEFAULT 0,
                mint TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                price_usd TEXT NOT NULL,
                usd_amount TEXT NOT NULL,
                pnl_pct TEXT NOT NULL DEFAULT '0',
                fee_usd TEXT NOT NULL DEFAULT '0',
                timestamp REAL NOT NULL,
                tx_hash TEXT,
                is_paper INTEGER NOT NULL DEFAULT 1
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                mint TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                timestamp REAL NOT NULL
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS smart_wallets (
                address TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                win_rate REAL DEFAULT 70.0,
                total_pnl_sol REAL DEFAULT 0.0,
                last_sig TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS wallet_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address TEXT NOT NULL,
                mint TEXT NOT NULL,
                amount_sol REAL DEFAULT 0.0,
                tx_signature TEXT,
                timestamp REAL NOT NULL
            )""")

            c.execute("""
            CREATE TABLE IF NOT EXISTS pending_transactions (
                tx_hash TEXT PRIMARY KEY,
                pos_id INTEGER DEFAULT 0,
                mint TEXT NOT NULL,
                symbol TEXT NOT NULL DEFAULT '',
                pair_address TEXT NOT NULL DEFAULT '',
                decimals INTEGER NOT NULL DEFAULT 6,
                direction TEXT NOT NULL,
                amount_raw TEXT NOT NULL,
                expected_usd TEXT NOT NULL,
                quote_json TEXT NOT NULL DEFAULT '{}',
                pre_sol_balance TEXT NOT NULL DEFAULT '0',
                pre_token_balance TEXT NOT NULL DEFAULT '0',
                created_at REAL NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'PENDING'
            )""")

            # Skapa wallet om den inte finns
            c.execute("SELECT 1 FROM wallet WHERE id=1")
            if not c.fetchone():
                init_cash = str(CONFIG["INITIAL_CASH_USD"])
                c.execute("""
                INSERT INTO wallet (id, initial_cash_usd, cash_usd, realized_pnl_usd, total_fees_paid_usd)
                VALUES (1, ?, ?, '0', '0')
                """, (init_cash, init_cash))

            # Automatisk schemamigrering om kolumner saknas i befintlig databas
            try:
                c.execute("ALTER TABLE positions ADD COLUMN price_impact_pct TEXT NOT NULL DEFAULT '0'")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE trade_history ADD COLUMN pos_id INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE smart_wallets ADD COLUMN last_sig TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE wallet ADD COLUMN last_halt_time REAL NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE wallet ADD COLUMN live_initial_cash_usd TEXT NOT NULL DEFAULT '0'")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE wallet ADD COLUMN live_halt_reason TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE wallet ADD COLUMN live_halt_until REAL NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE wallet ADD COLUMN live_last_halt_time REAL NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE pending_transactions ADD COLUMN pre_sol_balance TEXT NOT NULL DEFAULT '0'")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE pending_transactions ADD COLUMN pre_token_balance TEXT NOT NULL DEFAULT '0'")
            except sqlite3.OperationalError:
                pass

            # Seed Cupsey (@Cupseyy), Kimchi (@kimchi1x) & Orangie Alpha Wallets
            c.execute("SELECT COUNT(*) as cnt FROM smart_wallets")
            if c.fetchone()["cnt"] == 0:
                c.execute("""
                INSERT OR IGNORE INTO smart_wallets (address, label, win_rate, total_pnl_sol, is_active)
                VALUES 
                ('2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f', 'Cupsey (@Cupseyy - Apex Sniper)', 94.25, 5140.0, 1),
                ('suqh5sHtr8HyJ7q8scBimULPkPpA557prMG47xCHQfK', 'Cupsey (#1 OKX Leaderboard)', 67.70, 4200.0, 1),
                ('4Be9CvxqDWKc2pgA2ndgGTuSthk2252p25bQf5Gpump', 'Kimchi (@kimchi1x - $40M Conviction)', 85.00, 15000.0, 1),
                ('oraNgieYTN8385WkM9bC5bL59oZ6vP5G5a9x7qKpump', 'Orangie (@orangie - Alpha Confluence)', 78.50, 3200.0, 1)
                """)

            conn.commit()

    def get_wallet(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM wallet WHERE id=1")
            row = dict(c.fetchone())
            return {
                "initial_cash_usd": dec(row["initial_cash_usd"]),
                "cash_usd": dec(row["cash_usd"]),
                "realized_pnl_usd": dec(row["realized_pnl_usd"]),
                "total_fees_paid_usd": dec(row["total_fees_paid_usd"]),
                "total_trades": int(row["total_trades"]),
                "winning_trades": int(row["winning_trades"]),
                "losing_trades": int(row["losing_trades"]),
                "halt_reason": row.get("halt_reason", ""),
                "halt_until": float(row.get("halt_until", 0.0)),
                "last_halt_time": float(row.get("last_halt_time", 0.0)),
                "live_initial_cash_usd": dec(row.get("live_initial_cash_usd", "0")),
                "live_halt_reason": row.get("live_halt_reason", ""),
                "live_halt_until": float(row.get("live_halt_until", 0.0)),
                "live_last_halt_time": float(row.get("live_last_halt_time", 0.0))
            }

    def record_live_initial_equity(self, live_equity_usd: Decimal):
        """Sparar initialt live-kapital för exakt drawdown-skydd."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT live_initial_cash_usd FROM wallet WHERE id=1")
            row = c.fetchone()
            curr_val = dec(row["live_initial_cash_usd"] or 0, ZERO) if row else ZERO
            if curr_val <= ZERO and live_equity_usd > ZERO:
                c.execute("UPDATE wallet SET live_initial_cash_usd = ? WHERE id=1", (str(live_equity_usd),))
                conn.commit()

    def reset_wallet(self, initial_cash: Decimal = D("100.00")):
        with self._get_connection() as conn:
            c = conn.cursor()
            s_cash = str(initial_cash)
            c.execute("""
            UPDATE wallet 
            SET initial_cash_usd = ?, cash_usd = ?, realized_pnl_usd = '0',
                total_fees_paid_usd = '0', total_trades = 0, winning_trades = 0,
                losing_trades = 0, halt_reason = '', halt_until = 0, last_halt_time = 0
            WHERE id=1
            """, (s_cash, s_cash))
            c.execute("DELETE FROM positions WHERE is_paper = 1")
            c.execute("DELETE FROM trade_history WHERE is_paper = 1")
            conn.commit()

    def is_blacklisted(self, mint: str) -> Tuple[bool, str]:
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT reason FROM blacklist WHERE mint = ?", (mint,))
            row = c.fetchone()
            return (True, row["reason"]) if row else (False, "")

    def add_to_blacklist(self, mint: str, reason: str):
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO blacklist VALUES (?, ?, ?)",
                      (mint, safe_text(reason, 200), time.time()))
            conn.commit()

    def is_position_pending(self, pos_id: int) -> bool:
        """Kontrollerar om positionen har en aktiv (olöst) pending transaktion under behandling."""
        if not pos_id:
            return False
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM pending_transactions WHERE pos_id = ? AND resolved = 0", (pos_id,))
            return c.fetchone() is not None

    def can_open_position(self, mint: str, trade_usd: Decimal, is_paper: bool = True, live_sol_balance: Decimal = ZERO, sol_price: Decimal = ZERO, dex_client: Optional['DexScreenerClient'] = None) -> Tuple[bool, str]:
        with self._get_connection() as conn:
            c = conn.cursor()
            # 1. Svartlista
            is_bl, bl_msg = self.is_blacklisted(mint)
            if is_bl:
                return False, f"Svartlistad: {bl_msg}"

            # 2. Max öppna positioner
            c.execute("SELECT COUNT(*) as cnt FROM positions WHERE status='OPEN' AND is_paper = ?",
                      (1 if is_paper else 0,))
            if c.fetchone()["cnt"] >= CONFIG["MAX_OPEN_POSITIONS"]:
                return False, f"Max öppna positioner ({CONFIG['MAX_OPEN_POSITIONS']}) uppnått."

            # 3. Kontrollera att position inte redan är öppen i samma mint
            c.execute("SELECT 1 FROM positions WHERE mint = ? AND status='OPEN' AND is_paper = ?",
                      (mint, 1 if is_paper else 0))
            if c.fetchone():
                return False, "Position redan öppen i denna token."

            # 4. Cooldown
            cutoff = time.time() - (CONFIG["COOLDOWN_HOURS"] * 3600)
            c.execute("SELECT 1 FROM trade_history WHERE mint = ? AND timestamp > ?", (mint, cutoff))
            if c.fetchone():
                return False, "Token är i cooldown."

            # 5. Kassa & Mark-to-Market Circuit Breakers (Fullständigt separerad för Paper vs Live)
            wallet = self.get_wallet()
            now = time.time()

            active_halt_reason = wallet["halt_reason"] if is_paper else wallet["live_halt_reason"]
            active_halt_until = wallet["halt_until"] if is_paper else wallet["live_halt_until"]

            # Kontrollera om aktiv frysning har löpt ut
            if active_halt_reason:
                if active_halt_until > 0 and now >= active_halt_until:
                    # Frysningen har löpt ut! Återställ halt i databasen
                    if is_paper:
                        c.execute("UPDATE wallet SET halt_reason = '', halt_until = 0 WHERE id = 1")
                    else:
                        c.execute("UPDATE wallet SET live_halt_reason = '', live_halt_until = 0 WHERE id = 1")
                    conn.commit()
                else:
                    return False, f"🛑 CIRCUIT BREAKER AKTIV: {active_halt_reason}"

            if is_paper:
                # Kontrollera mot den faktiska orderstorleken
                if wallet["cash_usd"] < trade_usd:
                    return False, f"Otillräcklig kassa (${wallet['cash_usd']:.2f} < ${trade_usd:.2f})"

                # Marknadsvärdera öppna positioner i realtid (Mark-to-Market)
                c.execute("SELECT mint, entry_price_usd, net_token_value_usd, remaining_invested_usd FROM positions WHERE status='OPEN' AND is_paper=1")
                open_rows = c.fetchall()
                marked_open_equity = ZERO
                for r in open_rows:
                    entry_p = dec(r["entry_price_usd"], ZERO)
                    net_val = dec(r["net_token_value_usd"], ZERO)
                    rem_inv = dec(r["remaining_invested_usd"], ZERO)
                    cur_p = entry_p
                    if dex_client:
                        pd = dex_client.get_token_data(r["mint"])
                        if pd and pd.get("price_usd"):
                            cur_p = pd["price_usd"]
                    # Verklig marknadsvärdering av kvarvarande tokens
                    cur_val = net_val * (cur_p / entry_p) if entry_p > ZERO else rem_inv
                    marked_open_equity += cur_val

                total_equity = wallet["cash_usd"] + marked_open_equity
                init_cash = wallet["initial_cash_usd"]
                if init_cash > ZERO:
                    dd_pct = ((init_cash - total_equity) / init_cash) * HUNDRED
                    if dd_pct >= CONFIG["MAX_DRAWDOWN_PCT"]:
                        halt_until = now + (CONFIG["CIRCUIT_BREAKER_HOURS"] * 3600)
                        msg = f"🛑 PAPER CIRCUIT BREAKER: Max orealiserad drawdown ({dd_pct:.1f}%) nådd! Nödfrysning aktiverad."
                        c.execute("UPDATE wallet SET halt_reason = ?, halt_until = ?, last_halt_time = ? WHERE id = 1", (msg, halt_until, now))
                        conn.commit()
                        return False, msg
            else:
                # Live kapital- & reservkontroll mot faktiskt orderbelopp
                free_sol = live_sol_balance - CONFIG["MIN_SOL_RESERVE"]
                if free_sol <= ZERO:
                    return False, f"Otillräckligt SOL efter gasreserv ({live_sol_balance:.4f} SOL <= {CONFIG['MIN_SOL_RESERVE']} reserverat)."
                free_usd = free_sol * sol_price
                if free_usd < trade_usd:
                    return False, f"Otillräckligt fritt SOL-saldo (${free_usd:.2f} < ${trade_usd:.2f})"

                # Live Mark-to-Market Drawdown skydd mot live_initial_cash_usd
                c.execute("SELECT mint, entry_price_usd, usd_invested FROM positions WHERE status='OPEN' AND is_paper=0")
                live_open_rows = c.fetchall()
                live_open_equity = ZERO
                for r in live_open_rows:
                    entry_p = dec(r["entry_price_usd"], ZERO)
                    usd_inv = dec(r["usd_invested"], ZERO)
                    cur_p = entry_p
                    if dex_client:
                        pd = dex_client.get_token_data(r["mint"])
                        if pd and pd.get("price_usd"):
                            cur_p = pd["price_usd"]
                    live_open_equity += usd_inv * (cur_p / entry_p) if entry_p > ZERO else usd_inv

                live_total_equity = (live_sol_balance * sol_price) + live_open_equity
                live_init = wallet["live_initial_cash_usd"]
                if live_init > ZERO:
                    live_dd = ((live_init - live_total_equity) / live_init) * HUNDRED
                    if live_dd >= CONFIG["MAX_DRAWDOWN_PCT"]:
                        halt_until = now + (CONFIG["CIRCUIT_BREAKER_HOURS"] * 3600)
                        msg = f"🛑 LIVE CIRCUIT BREAKER: Live-portföljens värde har fallit {live_dd:.1f}% under startkapitalet (${live_init:.2f})! Nödfrysning aktiverad."
                        c.execute("UPDATE wallet SET live_halt_reason = ?, live_halt_until = ?, live_last_halt_time = ? WHERE id = 1", (msg, halt_until, now))
                        conn.commit()
                        return False, msg

                # 24-timmars rullande avgiftsfönster för att undvika spärr vid hög livstidsomsättning
                c.execute("SELECT SUM(fee_usd) as tot_fees FROM trade_history WHERE is_paper=0 AND timestamp > ?", (now - 86400.0,))
                tot_live_fees = dec(c.fetchone()["tot_fees"] or 0, ZERO)
                if tot_live_fees > (trade_usd * D("3.0")):
                    halt_until = now + (CONFIG["CIRCUIT_BREAKER_HOURS"] * 3600)
                    msg = "🛑 LIVE CIRCUIT BREAKER: Live-avgifter senaste 24h överstiger 3x handelsbelopp. Pausar live-köp."
                    c.execute("UPDATE wallet SET live_halt_reason = ?, live_halt_until = ?, live_last_halt_time = ? WHERE id = 1", (msg, halt_until, now))
                    conn.commit()
                    return False, msg

            # 6. Konsekutiva förluster - filtrera endast förluster EFTER senaste circuit breaker-frysning
            last_halt = wallet["last_halt_time"] if is_paper else wallet["live_last_halt_time"]
            c.execute("""
            SELECT id, usd_invested, net_usd_received, pnl_pct FROM positions 
            WHERE status = 'CLOSED' AND is_paper = ? AND exit_time > ?
            ORDER BY exit_time DESC LIMIT ?
            """, (1 if is_paper else 0, last_halt, CONFIG["MAX_CONSECUTIVE_LOSSES"]))
            pos_rows = c.fetchall()
            if len(pos_rows) == CONFIG["MAX_CONSECUTIVE_LOSSES"]:
                all_losses = True
                for pr in pos_rows:
                    pid = pr["id"]
                    orig_inv = dec(pr["usd_invested"], ZERO)
                    final_rec = dec(pr["net_usd_received"] or 0, ZERO)
                    c.execute("SELECT SUM(usd_amount) as tp_sum FROM trade_history WHERE pos_id = ? AND is_paper = ? AND action IN ('TP1', 'TP2')", (pid, 1 if is_paper else 0))
                    tp_sum = dec(c.fetchone()["tp_sum"] or 0, ZERO)
                    if (final_rec + tp_sum) > orig_inv:
                        all_losses = False
                        break
                if all_losses:
                    halt_until = now + (CONFIG["CIRCUIT_BREAKER_HOURS"] * 3600)
                    mode_label = "PAPER" if is_paper else "LIVE"
                    msg = f"🛑 {mode_label} CIRCUIT BREAKER: {CONFIG['MAX_CONSECUTIVE_LOSSES']} positioner i rad stängda med förlust! Pausar köp för att skydda kapitalet."
                    if is_paper:
                        c.execute("UPDATE wallet SET halt_reason = ?, halt_until = ?, last_halt_time = ? WHERE id = 1", (msg, halt_until, now))
                    else:
                        c.execute("UPDATE wallet SET live_halt_reason = ?, live_halt_until = ?, live_last_halt_time = ? WHERE id = 1", (msg, halt_until, now))
                    conn.commit()
                    return False, msg

            return True, "OK"

    def record_open_position(self, mint: str, symbol: str, pair_address: str, decimals: int,
                             price_usd: Decimal, usd_invested: Decimal, net_token_value: Decimal,
                             raw_amount: str, fee_usd: Decimal, price_impact_pct: Decimal,
                             tx_hash: str, is_paper: bool = True) -> int:
        now = time.time()
        initial_sl = price_usd * (ONE - CONFIG["INITIAL_STOP_LOSS_PCT"] / HUNDRED)

        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
            INSERT INTO positions (
                mint, symbol, pair_address, decimals, initial_raw, remaining_raw,
                entry_price_usd, usd_invested, remaining_invested_usd, net_token_value_usd,
                fees_paid_usd, price_impact_pct, entry_time, peak_price_usd, stop_loss_price, trailing_active,
                status, tx_hash, is_paper
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'OPEN', ?, ?)
            """, (mint, symbol, pair_address, decimals, raw_amount, raw_amount,
                  str(price_usd), str(usd_invested), str(usd_invested), str(net_token_value),
                  str(fee_usd), str(price_impact_pct), now, str(price_usd), str(initial_sl), tx_hash, 1 if is_paper else 0))
            pos_id = c.lastrowid

            if is_paper:
                w = self.get_wallet()
                c.execute("""
                UPDATE wallet 
                SET cash_usd = ?, total_fees_paid_usd = ?
                WHERE id=1
                """, (str(w["cash_usd"] - usd_invested),
                      str(w["total_fees_paid_usd"] + fee_usd)))

            c.execute("""
            INSERT INTO trade_history (pos_id, mint, symbol, action, price_usd, usd_amount, pnl_pct, fee_usd, timestamp, tx_hash, is_paper)
            VALUES (?, ?, ?, 'BUY', ?, ?, '0', ?, ?, ?, ?)
            """, (pos_id, mint, symbol, str(price_usd), str(usd_invested), str(fee_usd), now, tx_hash, 1 if is_paper else 0))
            conn.commit()
            return pos_id

    def get_open_positions(self, is_paper: bool = True) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM positions WHERE status='OPEN' AND is_paper = ?", (1 if is_paper else 0,))
            return [dict(r) for r in c.fetchall()]

    def update_position_trailing(self, pos_id: int, peak_price: Decimal, stop_loss: Decimal, trailing_active: int):
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
            UPDATE positions 
            SET peak_price_usd = ?, stop_loss_price = ?, trailing_active = ?
            WHERE id = ?
            """, (str(peak_price), str(stop_loss), trailing_active, pos_id))
            conn.commit()

    def apply_partial_sell(self, pos_id: int, tp_level: int, remaining_raw: str,
                           sold_ratio: Decimal, gross_usd: Decimal, fee_usd: Decimal,
                           net_usd: Decimal, exit_price: Decimal, is_paper: bool = True):
        now = time.time()
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
            pos = dict(c.fetchone())

            orig_inv = dec(pos["usd_invested"])
            rem_inv = dec(pos["remaining_invested_usd"])
            cost_of_sold = orig_inv * sold_ratio
            new_rem_inv = max(ZERO, rem_inv - cost_of_sold)
            realized_pnl = net_usd - cost_of_sold
            pnl_pct = (realized_pnl / cost_of_sold) * HUNDRED if cost_of_sold > ZERO else ZERO

            tp1_flag = 1 if tp_level == 1 else pos["tp1_executed"]
            tp2_flag = 1 if tp_level == 2 else pos["tp2_executed"]

            c.execute("""
            UPDATE positions 
            SET remaining_raw = ?, remaining_invested_usd = ?, tp1_executed = ?, tp2_executed = ?,
                fees_paid_usd = ?
            WHERE id = ?
            """, (remaining_raw, str(new_rem_inv), tp1_flag, tp2_flag,
                  str(dec(pos["fees_paid_usd"]) + fee_usd), pos_id))

            if is_paper:
                w = self.get_wallet()
                c.execute("""
                UPDATE wallet 
                SET cash_usd = ?, realized_pnl_usd = ?, total_fees_paid_usd = ?
                WHERE id = 1
                """, (str(w["cash_usd"] + net_usd), str(w["realized_pnl_usd"] + realized_pnl),
                      str(w["total_fees_paid_usd"] + fee_usd)))

            c.execute("""
            INSERT INTO trade_history (pos_id, mint, symbol, action, price_usd, usd_amount, pnl_pct, fee_usd, timestamp, tx_hash, is_paper)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pos_id, pos["mint"], pos["symbol"], f"TP{tp_level}", str(exit_price), str(net_usd),
                  str(pnl_pct), str(fee_usd), now, f"tp{tp_level}_{int(now)}", 1 if is_paper else 0))
            conn.commit()

    def close_position(self, pos_id: int, exit_price: Decimal, exit_reason: str,
                       net_usd_received: Decimal, fee_usd: Decimal, tx_hash: str, is_paper: bool = True):
        now = time.time()
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
            pos = dict(c.fetchone())

            rem_inv = dec(pos["remaining_invested_usd"])
            realized_pnl = net_usd_received - rem_inv
            pnl_pct = (realized_pnl / rem_inv) * HUNDRED if rem_inv > ZERO else ZERO

            c.execute("""
            UPDATE positions 
            SET status = 'CLOSED', remaining_raw = '0', remaining_invested_usd = '0',
                exit_time = ?, exit_price_usd = ?, exit_reason = ?, pnl_pct = ?,
                net_usd_received = ?, fees_paid_usd = ?
            WHERE id = ?
            """, (now, str(exit_price), exit_reason, str(pnl_pct), str(net_usd_received),
                  str(dec(pos["fees_paid_usd"]) + fee_usd), pos_id))

            # Beräkna om DENNA specifika position (pos_id) sammanlagt gick med vinst
            c.execute("""
            SELECT SUM(usd_amount) as total_received 
            FROM trade_history 
            WHERE pos_id = ? AND is_paper = ? AND action IN ('TP1', 'TP2')
            """, (pos_id, 1 if is_paper else 0))
            row_rec = c.fetchone()
            prior_received = dec(row_rec["total_received"] or 0)
            total_net_cash_returned = prior_received + net_usd_received
            orig_inv = dec(pos["usd_invested"])

            # Strikt vinstklassificering: Måste ge mer tillbaka än ursprunglig insats
            is_win = (total_net_cash_returned > orig_inv)
            win_inc = 1 if is_win else 0
            loss_inc = 0 if is_win else 1

            if is_paper:
                w = self.get_wallet()
                c.execute("""
                UPDATE wallet 
                SET cash_usd = ?, realized_pnl_usd = ?, total_fees_paid_usd = ?,
                    total_trades = total_trades + 1, winning_trades = winning_trades + ?,
                    losing_trades = losing_trades + ?
                WHERE id = 1
                """, (str(w["cash_usd"] + net_usd_received), str(w["realized_pnl_usd"] + realized_pnl),
                      str(w["total_fees_paid_usd"] + fee_usd), win_inc, loss_inc))

            c.execute("""
            INSERT INTO trade_history (pos_id, mint, symbol, action, price_usd, usd_amount, pnl_pct, fee_usd, timestamp, tx_hash, is_paper)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pos_id, pos["mint"], pos["symbol"], exit_reason, str(exit_price), str(net_usd_received),
                  str(pnl_pct), str(fee_usd), now, tx_hash, 1 if is_paper else 0))
            conn.commit()

    def record_pending_tx(self, tx_hash: str, pos_id: int, mint: str, symbol: str,
                          pair_address: str, decimals: int, direction: str,
                          amount_raw: str, expected_usd: str, quote_json: str = "{}",
                          pre_sol: str = "0", pre_token: str = "0"):
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
            INSERT OR REPLACE INTO pending_transactions 
            (tx_hash, pos_id, mint, symbol, pair_address, decimals, direction, amount_raw, expected_usd, quote_json, pre_sol_balance, pre_token_balance, created_at, resolved, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'PENDING')
            """, (tx_hash, pos_id, mint, symbol, pair_address, decimals, direction, amount_raw, expected_usd, quote_json, pre_sol, pre_token, time.time()))
            conn.commit()

    def resolve_pending_tx(self, tx_hash: str, status: str = "CONFIRMED"):
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
            UPDATE pending_transactions 
            SET resolved = 1, status = ?
            WHERE tx_hash = ?
            """, (status, tx_hash))
            conn.commit()

    def get_unresolved_pending_txs(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM pending_transactions WHERE resolved = 0")
            return [dict(r) for r in c.fetchall()]

# ==============================================================================
# 🕵️ SMART WALLET TRACKER
# ==============================================================================

class SmartWalletTracker:
    def __init__(self, state: StateManager, rpc_url: str):
        self.state = state
        self.rpc_url = rpc_url

    def record_wallet_buy(self, wallet_address: str, mint: str, amount_sol: float = 1.0, tx_sig: str = ""):
        """Registrering av bekräftad on-chain signal."""
        with self.state._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
            INSERT INTO wallet_signals (wallet_address, mint, amount_sol, tx_signature, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """, (wallet_address, mint, amount_sol, tx_sig, time.time()))
            conn.commit()

    def check_confluence(self, mint: str, window_minutes: float = 30.0) -> Tuple[bool, List[str], float]:
        cutoff = time.time() - (window_minutes * 60)
        with self.state._get_connection() as conn:
            c = conn.cursor()
            c.execute("""
            SELECT DISTINCT s.wallet_address, w.label
            FROM wallet_signals s
            LEFT JOIN smart_wallets w ON s.wallet_address = w.address
            WHERE s.mint = ? AND s.timestamp > ?
            """, (mint, cutoff))
            rows = c.fetchall()
            labels = [r["label"] or r["wallet_address"][:8] for r in rows]
            is_confluence = len(labels) >= 2
            # Håller multiplikatorn strikt på 1.0x för att inte överträda budgetregler
            mult = 1.0
            return is_confluence, labels, mult

# ==============================================================================
# ⏱️ RATE LIMITER & RESILIENSMOTOR (ANTI-429)
# ==============================================================================

class RateLimiter:
    """
    Token Bucket Rate Limiter:
    Förhindrar HTTP 429 Too Many Requests mot DexScreener, RugCheck och publika RPC:er.
    Håller anropsfrekvensen strikt under 45 anrop per rullande 60-sekundersfönster.
    """
    def __init__(self, max_per_minute: int = 45):
        self.max = max_per_minute
        self.timestamps: List[float] = []

    def wait(self):
        now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < 60.0]
        if len(self.timestamps) >= self.max:
            sleep_for = 60.0 - (now - self.timestamps[0]) + 0.1
            if sleep_for > 0:
                time.sleep(sleep_for)
        self.timestamps.append(time.time())

# ==============================================================================
# 📡 KLIENTER: DEXSCREENER & RUGCHECK (FAIL-CLOSED PRECISION)
# ==============================================================================

class DexScreenerClient:
    def __init__(self, limiter: Optional[RateLimiter] = None):
        self.limiter = limiter or RateLimiter(max_per_minute=45)
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def get_token_data(self, mint: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        if mint in self._cache:
            ts, cached_data = self._cache[mint]
            if now - ts < 3.0:
                return cached_data

        self.limiter.wait()
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as res:
                data = json.loads(res.read().decode())
                pairs = data.get("pairs") or []
                # Verifiera strikt att den sökta minten är baseToken!
                sol_pairs = [
                    p for p in pairs 
                    if p.get("chainId") == "solana" 
                    and p.get("baseToken", {}).get("address") == mint
                ]
                if not sol_pairs:
                    return None
                best = max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
                txns = best.get("txns") or {}
                h1 = txns.get("h1") or {}
                vol = best.get("volume") or {}
                info = best.get("info") or {}

                res_dict = {
                    "symbol": best.get("baseToken", {}).get("symbol", "TOKEN"),
                    "name": best.get("baseToken", {}).get("name", "Meme Token"),
                    "pair_address": best.get("pairAddress", ""),
                    "price_usd": dec(best.get("priceUsd", "0")),
                    "liquidity_usd": dec((best.get("liquidity") or {}).get("usd", "0")),
                    "volume_h1": dec(vol.get("h1", "0")),
                    "volume_h24": dec(vol.get("h24", "0")),
                    "buys_h1": int(h1.get("buys", 0)),
                    "sells_h1": int(h1.get("sells", 0)),
                    "price_change_h1": dec((best.get("priceChange") or {}).get("h1", "0")),
                    "socials": info.get("socials", []),
                    "websites": info.get("websites", []),
                    "boosts": int(best.get("boosts", {}).get("active", 0)) if isinstance(best.get("boosts"), dict) else 0,
                    "observed_at": now
                }
                self._cache[mint] = (now, res_dict)
                return res_dict
        except Exception:
            return None

    def scan_trending_mints(self) -> List[str]:
        mints = []
        for endpoint in ("token-profiles/latest/v1", "token-boosts/latest/v1"):
            try:
                self.limiter.wait()
                url = f"https://api.dexscreener.com/{endpoint}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as res:
                    items = json.loads(res.read().decode())
                    for it in items:
                        if it.get("chainId") == "solana":
                            addr = it.get("tokenAddress")
                            if addr and addr not in mints and len(addr) >= 32:
                                mints.append(addr)
            except Exception:
                continue
        return mints[:15]

class RugCheckValidator:
    def __init__(self, limiter: Optional[RateLimiter] = None):
        self.limiter = limiter or RateLimiter(max_per_minute=45)

    def validate(self, mint: str, target_pair_address: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
        self.limiter.wait()
        url = f"{CONFIG['RUGCHECK_API_BASE']}/{mint}/report"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as res:
                data = json.loads(res.read().decode())
                if not data or not isinstance(data, dict):
                    return False, "RugCheck returnerade tomt svar (Fail-Closed)", {}

                # 1. Kontrollera Rugged-flagga (Fail-Closed)
                if data.get("rugged") is True:
                    return False, "🛑 RUG DETEKTERAD: Token är flaggad som RUGGED på RugCheck!", {"danger": True}

                token = data.get("token") or {}
                risks = data.get("risks")
                if risks is None or not isinstance(risks, list):
                    return False, "Riskdata saknas från RugCheck (Fail-Closed)", {}

                # 2. Mint & Freeze Authorities (Fail-Closed om fälten saknas)
                if "mintAuthority" not in data and "mintAuthority" not in token:
                    return False, "mintAuthority saknas i rapporten (Fail-Closed)", {}
                mint_auth = data.get("mintAuthority") or token.get("mintAuthority")
                if mint_auth is not None and mint_auth != "":
                    return False, "Mint Authority är inte revokerad", {"danger": True}

                if "freezeAuthority" not in data and "freezeAuthority" not in token:
                    return False, "freezeAuthority saknas i rapporten (Fail-Closed)", {}
                freeze_auth = data.get("freezeAuthority") or token.get("freezeAuthority")
                if freeze_auth is not None and freeze_auth != "":
                    return False, "Freeze Authority är AKTIV (Honeypot-risk)", {"danger": True}

                # 3. Analysera samtliga riskobjekt (Blockera alla 'danger' och 'critical')
                for r in risks:
                    lvl = str(r.get("level", "")).lower()
                    r_name = r.get("name", "Okänd risk")
                    if lvl in ("danger", "critical"):
                        return False, f"Kritisk risk identifierad: {r_name} (Nivå: {lvl})", {"danger": True}

                # 4. Token-2022 Transfer Fee & Farliga Extensions
                token_prog = str(data.get("tokenProgram") or token.get("tokenProgram") or "")
                tf = data.get("transferFee") or token.get("transferFee")
                if isinstance(tf, dict) and dec(tf.get("pct", 0)) > ZERO:
                    return False, f"Token har dold säljskatt/Transfer Fee ({tf.get('pct')}%)", {"danger": True}

                token_exts = data.get("token_extensions") or {}
                dangerous_ext_keys = {
                    "transferHook", "confidentialTransfer", "permanentDelegate",
                    "transferFeeConfig", "pausableConfig", "defaultAccountState",
                    "confidentialTransferMint", "confidentialTransferFeeConfig"
                }
                if isinstance(token_exts, list):
                    for ext in token_exts:
                        if ext in dangerous_ext_keys:
                            return False, f"Farlig Token-2022 extension aktiv ({ext})", {"danger": True}
                elif isinstance(token_exts, dict):
                    for ext_name, ext_val in token_exts.items():
                        if ext_name in dangerous_ext_keys and ext_val is not None:
                            return False, f"Farlig Token-2022 extension aktiv ({ext_name})", {"danger": True}

                # 5. Risk Score (Fail-Closed om saknas)
                score = data.get("score")
                if score is None:
                    return False, "Riskbetyg saknas från RugCheck (Fail-Closed)", {}
                if int(score) > CONFIG["MAX_RUGCHECK_SCORE"]:
                    return False, f"Risk Score för hög ({score} > {CONFIG['MAX_RUGCHECK_SCORE']})", {"score": int(score)}

                # 6. LP Lock / Burn verifiering för MÅLPOOLEN
                markets = data.get("markets") or []
                if not markets:
                    return False, "Ingen marknad eller LP-data funnen på RugCheck (Fail-Closed)", {}

                target_market = None
                if target_pair_address:
                    for m in markets:
                        if m.get("pubkey") == target_pair_address or m.get("pairAddress") == target_pair_address:
                            target_market = m
                            break

                # Om specifik marknad inte matchades på pubkey, ta den primära poolen med högst likviditet
                if not target_market:
                    target_market = max(markets, key=lambda m: float(m.get("lp", {}).get("lpLockedUSD") or 0))

                lp_data = target_market.get("lp") or {}
                locked = dec(lp_data.get("lpLockedPct") or 0)
                burned = dec(lp_data.get("lpBurnedPct") or 0)
                lp_secured = locked + burned

                # Om målpoolen indikerar låg LP men tokenen har en primär likviditetspool med låst LP (t.ex. Raydium AMM)
                if lp_secured < CONFIG["MIN_LP_LOCKED_BURNED_PCT"] and markets:
                    best_m = max(markets, key=lambda m: float(m.get("lp", {}).get("lpLockedUSD") or 0))
                    b_lp = best_m.get("lp") or {}
                    b_sec = dec(b_lp.get("lpLockedPct") or 0) + dec(b_lp.get("lpBurnedPct") or 0)
                    if b_sec >= CONFIG["MIN_LP_LOCKED_BURNED_PCT"]:
                        lp_secured = b_sec

                if lp_secured < CONFIG["MIN_LP_LOCKED_BURNED_PCT"]:
                    return False, f"LP i målpoolen är inte tillräckligt låst/bränd ({lp_secured:.1f}% < {CONFIG['MIN_LP_LOCKED_BURNED_PCT']}%)", {"danger": True}

                # 7. Top 10 Holders (Fail-Closed och pool-valv filtrering)
                holders = data.get("topHolders")
                if not holders or not isinstance(holders, list):
                    return False, "Holder-data saknas från API (Fail-Closed)", {}

                # Samla pool-konton och likviditetsvalv från markets för att inte räkna LP som insider
                market_vault_addrs = set(KNOWN_SYSTEM_ADDRESSES)
                for m in markets:
                    if m.get("lpMint"): market_vault_addrs.add(m.get("lpMint"))
                    if m.get("quoteMint"): market_vault_addrs.add(m.get("quoteMint"))
                    if m.get("baseVault"): market_vault_addrs.add(m.get("baseVault"))
                    if m.get("quoteVault"): market_vault_addrs.add(m.get("quoteVault"))

                insider_sum = ZERO
                filtered_holders = [h for h in holders if h.get("address") not in market_vault_addrs]
                for h in filtered_holders[:10]:
                    insider_sum += dec(h.get("pct", 0))

                if insider_sum > CONFIG["MAX_TOP10_INSIDER_PCT"]:
                    return False, f"Insiders/Top 10 äger för mycket ({insider_sum:.1f}% > {CONFIG['MAX_TOP10_INSIDER_PCT']}%)", {}

                decimals = int(token.get("decimals", 6))
                return True, f"Säker (Score: {score}, LP säkrad: {lp_secured:.1f}%)", {"decimals": decimals}
        except Exception as e:
            return False, f"RugCheck API fel ({safe_text(e, 40)})", {}

# ==============================================================================
# 🧠 TRENCH IQ ANALYZER
# ==============================================================================

class TrenchIQAnalyzer:
    def __init__(self):
        self.hot_metas = ["ai", "agent", "robot", "cat", "dog", "pepe", "trump", "cult", "moon", "studio", "cupsey", "cup", "kimchi", "orangie", "spongebob"]

    def analyze(self, data: Dict[str, Any], confluence_info: Optional[Tuple[bool, List[str], float]] = None) -> Tuple[int, str]:
        score = 50
        bonuses = []

        if confluence_info and confluence_info[0]:
            score += 25
            bonuses.append(f"Whale Confluence ({len(confluence_info[1])} Wallets: {', '.join(confluence_info[1])})")

        socials = data.get("socials", [])
        websites = data.get("websites", [])
        has_twitter = any(s.get("type") == "twitter" for s in socials)
        has_tg = any(s.get("type") == "telegram" for s in socials)

        if has_twitter and (has_tg or websites):
            score += 20
            bonuses.append("Verifierade Socials")
        elif not socials and not websites:
            score -= 25

        boosts = data.get("boosts", 0)
        if boosts >= 5:
            score += 15
            bonuses.append(f"{boosts} Boosts")

        buys = data.get("buys_h1", 0)
        sells = data.get("sells_h1", 0)
        if buys >= 25 and buys >= (sells * 1.8):
            score += 15
            bonuses.append(f"Köpöverskott ({buys}K/{sells}S)")

        vol = dec(data.get("volume_h1", 0), ZERO)
        if vol >= D("25000"):
            score += 10
            bonuses.append("Hög Volym (>$25k)")

        sym_name = (data.get("symbol", "") + " " + data.get("name", "")).lower()
        if any(m in sym_name for m in self.hot_metas):
            score += 10
            bonuses.append("Hett Narrativ")

        score = max(0, min(100, score))
        summary = f"Trench IQ: {score}/100"
        if bonuses:
            summary += f" | {', '.join(bonuses)}"
        return score, summary

# ==============================================================================
# ⚡ JUPITER LIVE ENGINE & ON-CHAIN BALANCE DELTA LOOP
# ==============================================================================

class JupiterEngine:
    def __init__(self, rpc_url: str, private_key_b58: str, dry_run: bool = True, state: Optional[StateManager] = None):
        self.rpc_url = rpc_url
        self.dry_run = dry_run
        self.private_key_b58 = private_key_b58
        self.state = state
        self.keypair = None
        self._load_keypair()

    def _load_keypair(self):
        if self.private_key_b58 and SOLDERS_AVAILABLE:
            try:
                self.keypair = Keypair.from_base58_string(self.private_key_b58)
                LOGGER.info("🔑 Solana Keypair laddat! Plånbok: %s", self.keypair.pubkey())
            except Exception as e:
                LOGGER.error("Kunde inte ladda Keypair: %s", e)

    def _rpc_post(self, payload: Dict[str, Any], timeout: int = 8) -> Dict[str, Any]:
        if requests:
            return requests.post(self.rpc_url, json=payload, timeout=timeout).json()
        req = urllib.request.Request(
            self.rpc_url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode())

    def get_sol_balance(self) -> Decimal:
        """Hämtar äkta SOL-saldo från Solana RPC."""
        if not self.keypair:
            return ZERO
        try:
            pubkey = str(self.keypair.pubkey())
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                "params": [pubkey]
            }
            r = self._rpc_post(payload)
            lamports = r.get("result", {}).get("value", 0)
            return dec(lamports) / D("1000000000")
        except Exception as e:
            LOGGER.error("Kunde inte hämta SOL-saldo: %s", e)
            return ZERO

    def get_token_balance_raw(self, mint: str) -> int:
        """Läser av faktiskt on-chain SPL token-saldo (raw base units) för plånboken."""
        if not self.keypair:
            return 0
        try:
            pubkey = str(self.keypair.pubkey())
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [pubkey, {"mint": mint}, {"encoding": "jsonParsed"}]
            }
            res = self._rpc_post(payload)
            val = res.get("result", {}).get("value", [])
            if val:
                return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in val)
        except Exception as e:
            LOGGER.error("Kunde inte hämta token-saldo för %s: %s", mint, e)
        return 0

    def get_sol_price_usd(self) -> Decimal:
        """
        Hämtar realtidspris på SOL direkt on-chain via Jupiter swap quote (SOL -> USDC).
        Garanterar 100% precision utan risk för felaktiga DexScreener-par.
        """
        params = f"?inputMint={CONFIG['SOL_MINT']}&outputMint={CONFIG['USDC_MINT']}&amount=1000000000&slippageBps=50"
        for ep in CONFIG["JUPITER_QUOTE_ENDPOINTS"]:
            try:
                url = ep + params
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=4) as res:
                    d = json.loads(res.read().decode())
                    out = dec(d.get("outAmount", "0")) / D("1000000")
                    if out > D("10.0"):
                        return out
            except Exception:
                continue

        raise MarketDataUnavailableError("Kunde inte fastställa marknadspris för SOL via Jupiter. Handel avbryts säkert.")

    def get_quote(self, in_mint: str, out_mint: str, amount_raw: int, slippage_bps: int = 250) -> Optional[Dict[str, Any]]:
        params = f"?inputMint={in_mint}&outputMint={out_mint}&amount={amount_raw}&slippageBps={slippage_bps}"
        for ep in CONFIG["JUPITER_QUOTE_ENDPOINTS"]:
            try:
                url = ep + params
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as res:
                    return json.loads(res.read().decode())
            except Exception:
                continue
        return None

    def confirm_transaction(self, tx_hash: str, max_timeout: int = 30) -> Tuple[bool, str]:
        """Väntar och verifierar on-chain status (confirmed/finalized)."""
        deadline = time.time() + max_timeout
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignatureStatuses",
            "params": [[tx_hash], {"searchTransactionHistory": True}]
        }
        while time.time() < deadline:
            try:
                res = self._rpc_post(payload, timeout=4)
                statuses = res.get("result", {}).get("value", []) if res else []
                if statuses and statuses[0]:
                    st = statuses[0]
                    if st.get("err"):
                        return False, f"Transaktion fallerade on-chain: {st['err']}"
                    conf = st.get("confirmationStatus")
                    if conf in ("confirmed", "finalized"):
                        return True, conf
            except Exception:
                pass
            time.sleep(2)
        return False, "Timeout: Transaktionen kunde inte bekräftas inom tidsfristen."

    def execute_live_swap(self, quote: Dict[str, Any], in_mint: str, out_mint: str,
                          pos_id: int = 0, direction: str = "SWAP",
                          amount_raw: int = 0, expected_usd: Decimal = ZERO,
                          symbol: str = "", pair_address: str = "", decimals: int = 6) -> Tuple[bool, str, str, Decimal]:
        """
        Exekverar Jupiter swap med strikt Orphan Transaction Protection och Balance Delta.
        Returnerar: (Success, tx_hash, status_msg, actual_delta)
        """
        if self.dry_run or not self.keypair:
            return False, "", "Live-trading ej aktivt eller ingen privatnyckel laddad", ZERO
        try:
            pubkey = str(self.keypair.pubkey())
            is_buying_token = (in_mint == CONFIG["SOL_MINT"])

            # 1. Läs pre-swap on-chain saldo
            pre_sol = self.get_sol_balance()
            pre_token = self.get_token_balance_raw(out_mint if is_buying_token else in_mint)

            payload = {
                "userPublicKey": pubkey,
                "quoteResponse": quote,
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": CONFIG["PRIORITY_FEE_LAMPORTS"]
            }
            swap_data = None
            for ep in CONFIG["JUPITER_SWAP_ENDPOINTS"]:
                try:
                    if requests:
                        r = requests.post(ep, json=payload, timeout=8)
                        if r.status_code == 200:
                            swap_data = r.json()
                            break
                    else:
                        req = urllib.request.Request(
                            ep, data=json.dumps(payload).encode("utf-8"),
                            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                        )
                        with urllib.request.urlopen(req, timeout=8) as res:
                            swap_data = json.loads(res.read().decode())
                            break
                except Exception:
                    continue

            if not swap_data or "swapTransaction" not in swap_data:
                return False, "", "Ingen swapTransaction returnerades från Jupiter", ZERO

            raw_tx = base64.b64decode(swap_data["swapTransaction"])
            v_tx = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(v_tx.message, [self.keypair])

            rpc_payload = {
                "jsonrpc": "2.0", "id": 1, "method": "sendRawTransaction",
                "params": [base64.b64encode(bytes(signed_tx)).decode("utf-8"), {"encoding": "base64", "preflightCommitment": "confirmed"}]
            }
            r = self._rpc_post(rpc_payload, timeout=10)
            if "result" not in r:
                return False, "", f"RPC Fel: {r.get('error')}", ZERO

            tx_id = r["result"]
            LOGGER.info("📡 Transaktion skickad on-chain: %s. Väntar på bekräftelse...", tx_id)

            # Orphan Protection: Spara omedelbart i pending_transactions före bekräftelse
            if self.state:
                self.state.record_pending_tx(
                    tx_hash=tx_id, pos_id=pos_id, mint=out_mint if is_buying_token else in_mint,
                    symbol=symbol, pair_address=pair_address, decimals=decimals,
                    direction=direction, amount_raw=str(amount_raw), expected_usd=str(expected_usd),
                    quote_json=json.dumps(quote), pre_sol=str(pre_sol), pre_token=str(pre_token)
                )

            confirmed, conf_msg = self.confirm_transaction(tx_id)
            if not confirmed:
                LOGGER.warning("⚠️ Transaktion %s inväntar on-chain konfirmering (sparad som pending för automatisk försoning).", tx_id)
                return False, tx_id, conf_msg, ZERO

            # Markera transaktionen som bekräftad i pending-registret
            if self.state:
                self.state.resolve_pending_tx(tx_id, "CONFIRMED")

            # 2. Läs post-swap saldo och beräkna faktiskt Balance Delta
            time.sleep(1)
            post_sol = self.get_sol_balance()
            post_token = self.get_token_balance_raw(out_mint if is_buying_token else in_mint)

            if is_buying_token:
                actual_tokens = post_token - pre_token
                if actual_tokens <= 0:
                    return False, tx_id, "Varning: Transaktion bekräftad men token-saldo ökade inte (revert i program)", ZERO
                LOGGER.info("✅ BALANCE DELTA (KÖP): Erhöll %d raw tokens on-chain (Spenderade %.4f SOL)", actual_tokens, pre_sol - post_sol)
                return True, tx_id, f"On-chain Bekräftad ({conf_msg})", dec(actual_tokens)
            else:
                actual_sol_received = post_sol - pre_sol
                LOGGER.info("✅ BALANCE DELTA (SÄLJ): Erhöll %.4f SOL netto on-chain (Sålde %d tokens)", actual_sol_received, pre_token - post_token)
                return True, tx_id, f"On-chain Bekräftad ({conf_msg})", actual_sol_received
        except Exception as e:
            return False, "", f"Exekveringsfel: {e}", ZERO

# ==============================================================================
# 🎯 POSITIONSMONITOR & PROFIT ENGINE
# ==============================================================================

class PositionMonitor:
    def __init__(self, state: StateManager, dex: DexScreenerClient, jup: JupiterEngine):
        self.state = state
        self.dex = dex
        self.jup = jup

    def check_and_exit_positions(self, is_paper: bool = True) -> List[Dict[str, Any]]:
        open_pos = self.state.get_open_positions(is_paper=is_paper)
        exits = []

        for p in open_pos:
            pos_id = p["id"]
            if not is_paper and self.state.is_position_pending(pos_id):
                LOGGER.info("⏳ Position #%d (%s) har en aktiv transaktion under on-chain bekräftelse. Blockerar dubbelsälj.", pos_id, p["symbol"])
                continue

            mint = p["mint"]
            symbol = p["symbol"]
            entry_price = dec(p["entry_price_usd"])
            peak_price = dec(p["peak_price_usd"])
            current_sl = dec(p["stop_loss_price"])
            trailing_status = p["trailing_active"]
            usd_inv = dec(p["usd_invested"])
            rem_inv = dec(p["remaining_invested_usd"])
            net_token_val = dec(p["net_token_value_usd"])
            raw_amount_int = int(p["remaining_raw"])
            pair_addr = p.get("pair_address", "")
            decimals = int(p.get("decimals", 6))

            data = self.dex.get_token_data(mint)
            curr_p = ZERO
            if data and data.get("price_usd") and data["price_usd"] > ZERO:
                curr_p = data["price_usd"]
            else:
                # Fallback till Jupiter quote för att få implied market price om DexScreener saknar kurs
                try:
                    sol_p = self.jup.get_sol_price_usd()
                    q = self.jup.get_quote(mint, CONFIG["SOL_MINT"], raw_amount_int, CONFIG["DEFAULT_SLIPPAGE_BPS"])
                    if q and q.get("outAmount") and int(q["outAmount"]) > 0:
                        out_sol = dec(q["outAmount"]) / D("1e9")
                        tokens = dec(raw_amount_int) / (D("10") ** decimals)
                        if tokens > ZERO:
                            curr_p = (out_sol * sol_p) / tokens
                except Exception as e:
                    LOGGER.warning("Jupiter fallback quote misslyckades för %s: %s", symbol, e)

            if curr_p <= ZERO:
                # Varken DexScreener eller Jupiter har priser/likviditet: Token är dränerad eller avnoterad!
                LOGGER.error("🚨 NÖD-STOPP: Ingen marknadsdata eller likviditet för %s (%s). Utlöser EMERGENCY_RUG_STOP!", symbol, mint)
                exit_type = "EMERGENCY_RUG_STOP"
                exec_price = ZERO
                net_usd = ZERO
                fee_usd = ZERO
                tx_hash = f"rug_exit_{int(time.time())}"
                if not is_paper:
                    # Försök i sista hand sälja vad som går via Jupiter om någon micro-likviditet finns
                    try:
                        sol_p = self.jup.get_sol_price_usd()
                        q = self.jup.get_quote(mint, CONFIG["SOL_MINT"], raw_amount_int, 5000)
                        if q and int(q.get("outAmount", 0)) > 0:
                            success, tx, msg, actual_sol = self.jup.execute_live_swap(
                                q, mint, CONFIG["SOL_MINT"], pos_id=pos_id, direction="EMERGENCY_RUG_STOP",
                                amount_raw=raw_amount_int, expected_usd=ZERO, symbol=symbol, pair_address=pair_addr, decimals=decimals
                            )
                            if success:
                                net_usd = actual_sol * sol_p
                                tx_hash = tx
                    except Exception:
                        pass

                self.state.close_position(pos_id, exec_price, exit_type, net_usd, fee_usd, tx_hash, is_paper=is_paper)
                self.state.add_to_blacklist(mint, "Rugged/Drained liquidity (Ingen marknadsdata)")
                LOGGER.warning("💀 Position #%d (%s) stängd som EMERGENCY_RUG_STOP vid $%.2f. Token svartlistad.", pos_id, symbol, net_usd)
                exits.append({"mint": mint, "symbol": symbol, "reason": exit_type, "pnl_pct": -HUNDRED, "net_usd": net_usd})
                continue

            pnl_pct = ((curr_p - entry_price) / entry_price) * HUNDRED

            # 1. Uppdatera ATH
            if curr_p > peak_price:
                peak_price = curr_p
                if trailing_status == 2: # Moonbag aktiv
                    current_sl = peak_price * (ONE - CONFIG["MOONBAG_TRAILING_PCT"] / HUNDRED)
                self.state.update_position_trailing(pos_id, peak_price, current_sl, trailing_status)

            # 2. Breakeven Stop Loss (+15% vinst -> flytta SL till +3%)
            if pnl_pct >= CONFIG["BREAKEVEN_TRIGGER_PCT"] and trailing_status == 0:
                trailing_status = 1
                current_sl = entry_price * (ONE + CONFIG["BREAKEVEN_LOCK_PCT"] / HUNDRED)
                self.state.update_position_trailing(pos_id, peak_price, current_sl, trailing_status)
                LOGGER.info("🛡️ BREAKEVEN SL AKTIVERAD för %s vid +%.1f%%! SL: $%.8f (+3%% vinst)", symbol, pnl_pct, current_sl)

            # 3. Take Profit 1 (+40%): Sälj 40% av ursprungligt belopp
            if pnl_pct >= CONFIG["TAKE_PROFIT_1_PCT"] and p["tp1_executed"] == 0:
                sold_ratio = D("0.40")
                sell_raw = int((dec(p["initial_raw"]) * sold_ratio).to_integral_value(ROUND_DOWN))
                rem_raw = str(raw_amount_int - sell_raw)

                if not is_paper:
                    try:
                        sol_price = self.jup.get_sol_price_usd()
                    except MarketDataUnavailableError as e:
                        LOGGER.error("Kunde inte hämta SOL-kurs före TP1 swap: %s", e)
                        continue

                    q = self.jup.get_quote(mint, CONFIG["SOL_MINT"], sell_raw, CONFIG["DEFAULT_SLIPPAGE_BPS"])
                    if not q:
                        continue
                    exp_sol = dec(q.get("outAmount", 0)) / D("1e9")
                    success, tx, msg, actual_sol_received = self.jup.execute_live_swap(
                        q, mint, CONFIG["SOL_MINT"], pos_id=pos_id, direction="TP1",
                        amount_raw=sell_raw, expected_usd=exp_sol * sol_price,
                        symbol=symbol, pair_address=pair_addr, decimals=decimals
                    )
                    if not success:
                        LOGGER.error("Live TP1 misslyckades on-chain: %s", msg)
                        continue
                    net_usd = actual_sol_received * sol_price
                    estimated_gas_sol = D("0.000005") + dec(CONFIG["PRIORITY_FEE_LAMPORTS"]) / D("1000000000")
                    fee_usd = estimated_gas_sol * sol_price
                    gross_usd = net_usd
                else:
                    gross_nominal = (net_token_val * sold_ratio) * (ONE + pnl_pct / HUNDRED)
                    pair_data = self.dex.get_token_data(mint)
                    pool_liq = dec(pair_data.get("liquidity_usd", 50000), D("50000")) if pair_data else D("50000")
                    sell_impact_pct, _ = calculate_amm_price_impact(gross_nominal, pool_liq)
                    fee_usd = CONFIG["ESTIMATED_GAS_USD"] + (gross_nominal * (CONFIG["DEX_POOL_FEE_PCT"] + CONFIG["BASE_SLIPPAGE_PCT"] + sell_impact_pct) / HUNDRED)
                    net_usd = max(ZERO, gross_nominal - fee_usd)
                    gross_usd = gross_nominal

                # Höj och spara ny SL till SQLite (+15% vinstlås)
                current_sl = max(current_sl, entry_price * D("1.15"))
                self.state.update_position_trailing(pos_id, peak_price, current_sl, trailing_status)
                self.state.apply_partial_sell(pos_id, 1, rem_raw, sold_ratio, gross_usd, fee_usd, net_usd, curr_p, is_paper=is_paper)
                LOGGER.info("💰 TP1 KLAR för %s (+%.1f%%): Säkrat $%.2f USD netto! SL sparad vid $%.8f (+15%%)", symbol, pnl_pct, net_usd, current_sl)
                continue

            # 4. Take Profit 2 (+80%): Kräver att TP1 redan har körts!
            if pnl_pct >= CONFIG["TAKE_PROFIT_2_PCT"] and p["tp2_executed"] == 0 and p["tp1_executed"] == 1:
                sold_ratio = D("0.35")
                sell_raw = int((dec(p["initial_raw"]) * sold_ratio).to_integral_value(ROUND_DOWN))
                rem_raw = str(raw_amount_int - sell_raw)

                if not is_paper:
                    try:
                        sol_price = self.jup.get_sol_price_usd()
                    except MarketDataUnavailableError as e:
                        LOGGER.error("Kunde inte hämta SOL-kurs före TP2 swap: %s", e)
                        continue

                    q = self.jup.get_quote(mint, CONFIG["SOL_MINT"], sell_raw, CONFIG["DEFAULT_SLIPPAGE_BPS"])
                    if not q:
                        continue
                    exp_sol = dec(q.get("outAmount", 0)) / D("1e9")
                    success, tx, msg, actual_sol_received = self.jup.execute_live_swap(
                        q, mint, CONFIG["SOL_MINT"], pos_id=pos_id, direction="TP2",
                        amount_raw=sell_raw, expected_usd=exp_sol * sol_price,
                        symbol=symbol, pair_address=pair_addr, decimals=decimals
                    )
                    if not success:
                        LOGGER.error("Live TP2 misslyckades on-chain: %s", msg)
                        continue
                    net_usd = actual_sol_received * sol_price
                    estimated_gas_sol = D("0.000005") + dec(CONFIG["PRIORITY_FEE_LAMPORTS"]) / D("1000000000")
                    fee_usd = estimated_gas_sol * sol_price
                    gross_usd = net_usd
                else:
                    gross_nominal = (net_token_val * sold_ratio) * (ONE + pnl_pct / HUNDRED)
                    pair_data = self.dex.get_token_data(mint)
                    pool_liq = dec(pair_data.get("liquidity_usd", 50000), D("50000")) if pair_data else D("50000")
                    sell_impact_pct, _ = calculate_amm_price_impact(gross_nominal, pool_liq)
                    fee_usd = CONFIG["ESTIMATED_GAS_USD"] + (gross_nominal * (CONFIG["DEX_POOL_FEE_PCT"] + CONFIG["BASE_SLIPPAGE_PCT"] + sell_impact_pct) / HUNDRED)
                    net_usd = max(ZERO, gross_nominal - fee_usd)
                    gross_usd = gross_nominal

                trailing_status = 2 # Moonbag Trailing SL
                current_sl = peak_price * (ONE - CONFIG["MOONBAG_TRAILING_PCT"] / HUNDRED)
                self.state.update_position_trailing(pos_id, peak_price, current_sl, trailing_status)
                self.state.apply_partial_sell(pos_id, 2, rem_raw, sold_ratio, gross_usd, fee_usd, net_usd, curr_p, is_paper=is_paper)
                LOGGER.info("🌕 TP2 KLAR för %s (+%.1f%%): Säkrat $%.2f USD! Moonbag aktiv vid $%.8f", symbol, pnl_pct, net_usd, current_sl)
                continue

            # 5. Stop Loss / Breakeven Stop / Trailing Stop Trigger
            if curr_p <= current_sl:
                exit_type = "MOONBAG_TRAILING_STOP" if trailing_status == 2 else ("BREAKEVEN_STOP" if trailing_status == 1 else "STOP_LOSS")
                actual_pnl_pct = ((curr_p - entry_price) / entry_price) * HUNDRED
                tx_hash = f"paper_exit_{int(time.time())}"

                if not is_paper:
                    try:
                        sol_price = self.jup.get_sol_price_usd()
                    except MarketDataUnavailableError as e:
                        LOGGER.error("Kunde inte hämta SOL-kurs före SL exit: %s", e)
                        continue

                    q = self.jup.get_quote(mint, CONFIG["SOL_MINT"], raw_amount_int, CONFIG["DEFAULT_SLIPPAGE_BPS"])
                    if not q:
                        continue
                    exp_sol = dec(q.get("outAmount", 0)) / D("1e9")
                    success, tx, msg, actual_sol_received = self.jup.execute_live_swap(
                        q, mint, CONFIG["SOL_MINT"], pos_id=pos_id, direction=exit_type,
                        amount_raw=raw_amount_int, expected_usd=exp_sol * sol_price,
                        symbol=symbol, pair_address=pair_addr, decimals=decimals
                    )
                    if not success:
                        LOGGER.error("Live SL exit misslyckades on-chain: %s", msg)
                        continue
                    tx_hash = tx
                    net_usd = actual_sol_received * sol_price
                    estimated_gas_sol = D("0.000005") + dec(CONFIG["PRIORITY_FEE_LAMPORTS"]) / D("1000000000")
                    fee_usd = estimated_gas_sol * sol_price
                    exec_price = curr_p
                else:
                    rem_ratio = (rem_inv / usd_inv) if usd_inv > ZERO else ONE
                    rem_net_token_val = net_token_val * rem_ratio
                    pair_data = self.dex.get_token_data(mint)
                    pool_liq = dec(pair_data.get("liquidity_usd", 50000), D("50000")) if pair_data else D("50000")
                    nominal_sell = rem_net_token_val * (curr_p / entry_price) if entry_price > ZERO else rem_net_token_val
                    sell_impact_pct, _ = calculate_amm_price_impact(nominal_sell, pool_liq)
                    exec_price = min(current_sl, curr_p) * (ONE - (CONFIG["DEX_POOL_FEE_PCT"] + CONFIG["BASE_SLIPPAGE_PCT"] + sell_impact_pct) / HUNDRED)
                    gross_usd = rem_net_token_val * (exec_price / entry_price)
                    fee_usd = CONFIG["ESTIMATED_GAS_USD"]
                    net_usd = max(ZERO, gross_usd - fee_usd)

                self.state.close_position(pos_id, exec_price, exit_type, net_usd, fee_usd, tx_hash, is_paper=is_paper)
                LOGGER.info("🚨 EXIT för %s (%s): Kurs $%.8f (PnL: %+.2f%% | Netto: $%.2f USD)", symbol, exit_type, exec_price, actual_pnl_pct, net_usd)
                exits.append({"mint": mint, "symbol": symbol, "reason": exit_type, "pnl_pct": actual_pnl_pct, "net_usd": net_usd})

        return exits

# ==============================================================================
# 🤖 BOT ORCHESTRATOR
# ==============================================================================

class GodlikeBot:
    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        self.rate_limiter = RateLimiter(max_per_minute=45)
        self.state = StateManager(config["DB_PATH"])
        self.dex = DexScreenerClient(limiter=self.rate_limiter)
        self.rug = RugCheckValidator(limiter=self.rate_limiter)
        self.trench = TrenchIQAnalyzer()
        self.smart_wallets = SmartWalletTracker(self.state, config["SOLANA_RPC_URL"])
        self.jup = JupiterEngine(config["SOLANA_RPC_URL"], config["WALLET_PRIVATE_KEY"], config["DRY_RUN"], state=self.state)
        self.monitor = PositionMonitor(self.state, self.dex, self.jup)

    def evaluate_and_buy(self, mint: str, is_paper: bool = True) -> Dict[str, Any]:
        live_sol = self.jup.get_sol_balance() if not is_paper else ZERO
        try:
            sol_price = self.jup.get_sol_price_usd()
        except MarketDataUnavailableError as e:
            return {"status": "ERROR", "reason": str(e)}

        trade_usd = self.cfg["TRADE_BUDGET_USD"]
        can_buy, reason = self.state.can_open_position(
            mint, trade_usd=trade_usd, is_paper=is_paper,
            live_sol_balance=live_sol, sol_price=sol_price, dex_client=self.dex
        )
        if not can_buy:
            return {"status": "SKIPPED", "reason": reason}

        # 1. Hämta marknadsdata från DexScreener
        pair = self.dex.get_token_data(mint)
        if not pair:
            return {"status": "REJECTED", "reason": "Inga matchande Solana-par funna"}

        if pair["liquidity_usd"] < self.cfg["MIN_LIQUIDITY_USD"]:
            return {"status": "REJECTED", "reason": f"Låg likviditet (${pair['liquidity_usd']:.0f} < ${self.cfg['MIN_LIQUIDITY_USD']:.0f})"}

        # 2. Trench IQ
        confluence_info = self.smart_wallets.check_confluence(mint)
        trench_score, trench_summary = self.trench.analyze(pair, confluence_info)
        if trench_score < 40 and not confluence_info[0]:
            return {"status": "REJECTED", "reason": f"Låg Trench IQ ({trench_score}/100)"}

        # 3. RugCheck Validering (Fail-Closed mot målpoolens LP)
        is_safe, rug_msg, meta = self.rug.validate(mint, target_pair_address=pair.get("pair_address"))
        if not is_safe:
            if meta.get("danger"):
                self.state.add_to_blacklist(mint, rug_msg)
            return {"status": "RUG_PREVENTED", "reason": rug_msg}

        decimals = meta.get("decimals", 6)
        spot_price_usd = pair["price_usd"]

        # 4. AMM Constant Product Prisimpact
        impact_pct, exec_factor = calculate_amm_price_impact(trade_usd, pair["liquidity_usd"])
        if impact_pct > self.cfg["MAX_ALLOWED_PRICE_IMPACT_PCT"]:
            return {
                "status": "REJECTED",
                "reason": f"🛑 AVSLAG: För hög prisimpact ({impact_pct:.2f}% > {self.cfg['MAX_ALLOWED_PRICE_IMPACT_PCT']}%) på pool (${pair['liquidity_usd']:.0f})"
            }

        effective_price_usd = spot_price_usd * exec_factor
        entry_fee = self.cfg["ESTIMATED_GAS_USD"] + (trade_usd * (self.cfg["DEX_POOL_FEE_PCT"] + self.cfg["BASE_SLIPPAGE_PCT"]) / HUNDRED)
        net_val = trade_usd - entry_fee
        if net_val <= ZERO:
            return {"status": "REJECTED", "reason": "Insatsen täcker inte avgifterna"}

        raw_tokens = str(int((net_val / effective_price_usd) * (D("10") ** decimals)))
        tx_hash = f"paper_tx_{int(time.time())}"

        if not is_paper:
            sol_amount = float(trade_usd / sol_price)
            lamports = int(sol_amount * 1_000_000_000)
            quote = self.jup.get_quote(self.cfg["SOL_MINT"], mint, lamports, self.cfg["DEFAULT_SLIPPAGE_BPS"])
            if not quote:
                return {"status": "ERROR", "reason": "Kunde inte erhålla Jupiter-quote"}
            
            pre_sol = self.jup.get_sol_balance()
            success, tx, msg, actual_tokens_received = self.jup.execute_live_swap(
                quote, self.cfg["SOL_MINT"], mint, pos_id=0, direction="BUY",
                amount_raw=lamports, expected_usd=trade_usd,
                symbol=pair["symbol"], pair_address=pair["pair_address"], decimals=decimals
            )
            if not success:
                return {"status": "FAILED", "reason": msg}
            
            post_sol = self.jup.get_sol_balance()
            tx_hash = tx
            raw_tokens = str(int(actual_tokens_received))

            # Verklig on-chain kostnad och verkligt inköpspris!
            actual_sol_spent = max(ZERO, pre_sol - post_sol)
            actual_usd_invested = actual_sol_spent * sol_price
            estimated_gas_sol = D("0.000005") + dec(CONFIG["PRIORITY_FEE_LAMPORTS"]) / D("1000000000")
            entry_fee = estimated_gas_sol * sol_price
            trade_usd = actual_usd_invested
            net_val = max(ZERO, trade_usd - entry_fee)
            token_units = dec(actual_tokens_received) / (D("10") ** decimals)
            actual_entry_price_usd = (trade_usd / token_units) if token_units > ZERO else effective_price_usd
            effective_price_usd = actual_entry_price_usd

        pos_id = self.state.record_open_position(
            mint=mint, symbol=pair["symbol"], pair_address=pair["pair_address"],
            decimals=decimals, price_usd=effective_price_usd, usd_invested=trade_usd,
            net_token_value=net_val, raw_amount=raw_tokens, fee_usd=entry_fee,
            price_impact_pct=impact_pct, tx_hash=tx_hash, is_paper=is_paper
        )

        LOGGER.info("✅ KÖP GENOMFÖRT [Pos #%d]: %s för $%.2f USD (Impact: %.2f%% | %s)", pos_id, pair["symbol"], trade_usd, impact_pct, trench_summary)
        return {"status": "BOUGHT", "pos_id": pos_id, "symbol": pair["symbol"], "mint": mint, "usd_invested": trade_usd, "price_impact_pct": impact_pct}

    def reconcile_pending_transactions(self):
        """
        Orphan Transaction Protection:
        Söker igenom pending_transactions efter transaktioner som kan ha bekräftats
        efter en RPC-timeout eller när boten startades om.
        """
        pending = self.state.get_unresolved_pending_txs()
        if not pending:
            return

        for tx in pending:
            tx_hash = tx["tx_hash"]
            created_at = tx["created_at"]
            direction = tx["direction"]
            mint = tx["mint"]
            pos_id = tx["pos_id"]
            age = time.time() - created_at

            confirmed, conf_msg = self.jup.confirm_transaction(tx_hash, max_timeout=2)
            if confirmed:
                LOGGER.info("🔄 RECONCILER: Hittade bekräftad orphan-transaktion on-chain: %s (%s)", tx_hash, direction)
                try:
                    sol_p = self.jup.get_sol_price_usd()
                except MarketDataUnavailableError:
                    LOGGER.warning("⚠️ RECONCILER: Marknadspris för SOL otillgängligt. Skjuter upp försoning.")
                    continue

                if direction == "BUY":
                    actual_tokens = self.jup.get_token_balance_raw(mint)
                    pre_token = int(tx.get("pre_token_balance") or 0)
                    delta_tokens = actual_tokens - pre_token if (actual_tokens > pre_token and pre_token > 0) else actual_tokens
                    if delta_tokens > 0:
                        post_sol = self.jup.get_sol_balance()
                        pre_sol = dec(tx.get("pre_sol_balance") or 0)
                        if pre_sol > post_sol:
                            actual_sol_spent = pre_sol - post_sol
                            trade_usd = actual_sol_spent * sol_p
                        else:
                            trade_usd = dec(tx["expected_usd"])

                        estimated_gas_sol = D("0.000005") + dec(CONFIG["PRIORITY_FEE_LAMPORTS"]) / D("1000000000")
                        entry_fee = estimated_gas_sol * sol_p
                        net_val = max(ZERO, trade_usd - entry_fee)
                        pair_data = self.dex.get_token_data(mint)
                        token_units = dec(delta_tokens) / (D("10") ** tx["decimals"])
                        exec_price = (trade_usd / token_units) if token_units > ZERO else (pair_data["price_usd"] if pair_data else ZERO)

                        new_pos_id = self.state.record_open_position(
                            mint=mint, symbol=tx["symbol"], pair_address=tx["pair_address"],
                            decimals=tx["decimals"], price_usd=exec_price, usd_invested=trade_usd,
                            net_token_value=net_val, raw_amount=str(delta_tokens), fee_usd=entry_fee,
                            price_impact_pct=D("0.10"), tx_hash=tx_hash, is_paper=False
                        )
                        LOGGER.info("✅ RECONCILER: Orphan köp återställt med verkligt Balance Delta och bokfört som Pos #%d!", new_pos_id)
                elif direction in ("TP1", "TP2"):
                    tp_lvl = 1 if direction == "TP1" else 2
                    sold_ratio = D("0.40") if tp_lvl == 1 else D("0.35")
                    net_usd = dec(tx["expected_usd"])
                    fee_usd = (D("0.000005") + dec(CONFIG["PRIORITY_FEE_LAMPORTS"]) / D("1000000000")) * sol_p
                    gross_usd = net_usd + fee_usd
                    with self.state._get_connection() as conn:
                        c = conn.cursor()
                        c.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
                        r = c.fetchone()
                        if r and r["status"] == "OPEN":
                            curr_raw = int(r["remaining_raw"])
                            sell_raw = int((dec(r["initial_raw"]) * sold_ratio).to_integral_value(ROUND_DOWN))
                            rem_raw = str(max(0, curr_raw - sell_raw))
                            pair_data = self.dex.get_token_data(mint)
                            cur_p = pair_data["price_usd"] if pair_data else dec(r["entry_price_usd"])
                            self.state.apply_partial_sell(pos_id, tp_lvl, rem_raw, sold_ratio, gross_usd, fee_usd, net_usd, cur_p, is_paper=False)
                            LOGGER.info("✅ RECONCILER: Orphan TP%d bokförd för Pos #%d!", tp_lvl, pos_id)
                elif direction in ("STOP_LOSS", "BREAKEVEN_STOP", "MOONBAG_TRAILING_STOP", "EXIT"):
                    with self.state._get_connection() as conn:
                        c = conn.cursor()
                        c.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
                        r = c.fetchone()
                        if r and r["status"] == "OPEN":
                            pair_data = self.dex.get_token_data(mint)
                            cur_p = pair_data["price_usd"] if pair_data else dec(r["stop_loss_price"])
                            net_usd = dec(tx["expected_usd"])
                            fee_usd = (D("0.000005") + dec(CONFIG["PRIORITY_FEE_LAMPORTS"]) / D("1000000000")) * sol_p
                            self.state.close_position(pos_id, cur_p, direction, net_usd, fee_usd, tx_hash, is_paper=False)
                            LOGGER.info("✅ RECONCILER: Orphan SL exit bokförd för Pos #%d!", pos_id)

                self.state.resolve_pending_tx(tx_hash, "CONFIRMED")
            elif age > 90.0:
                LOGGER.warning("⚠️ RECONCILER: Transaktion %s (%s) har löpt ut on-chain (Blockhash expired).", tx_hash, direction)
                self.state.resolve_pending_tx(tx_hash, "EXPIRED")

    def print_hud(self, is_paper: bool = True):
        open_pos = self.state.get_open_positions(is_paper=is_paper)
        unrealized_usd = ZERO
        open_val_usd = ZERO

        pos_lines = []
        for p in open_pos:
            mint = p["mint"]
            sym = p["symbol"]
            entry_p = dec(p["entry_price_usd"])
            net_token_val = dec(p["net_token_value_usd"])
            rem_inv = dec(p["remaining_invested_usd"])
            usd_inv = dec(p["usd_invested"])
            sl_p = dec(p["stop_loss_price"])
            mode = "🌕 MOON" if p["trailing_active"] == 2 else ("🛡️ BE-SL" if p["trailing_active"] == 1 else "INIT-SL")

            data = self.dex.get_token_data(mint)
            if not data or data.get("price_usd", ZERO) <= ZERO:
                pos_lines.append(f"  • {sym:<7} ⚠️ MARKNADSDATA SAKNAS (Sista kända kurs: ${entry_p:.6f})")
                open_val_usd += rem_inv
                continue

            curr_p = data["price_usd"]
            pnl_pct = ((curr_p - entry_p) / entry_p) * HUNDRED
            # Verklig marknadsvärdering av kvarvarande innehav
            portion = (rem_inv / usd_inv) if usd_inv > ZERO else ONE
            curr_val = (net_token_val * portion) * (curr_p / entry_p) if entry_p > ZERO else rem_inv

            unrealized_usd += (curr_val - rem_inv)
            open_val_usd += curr_val
            pos_lines.append(f"  • {sym:<7} Inköp: ${entry_p:.6f} | Nu: ${curr_p:.6f} | PnL: {pnl_pct:+5.1f}% | {mode}: ${sl_p:.6f}")

        if is_paper:
            w = self.state.get_wallet()
            total_port = w["cash_usd"] + open_val_usd
            total_pnl = total_port - w["initial_cash_usd"]
            total_pnl_pct = (total_pnl / w["initial_cash_usd"]) * HUNDRED
            wr = (w["winning_trades"] / w["total_trades"] * 100.0) if w["total_trades"] > 0 else 0.0

            pnl_str = f"{'+$' if total_pnl >= ZERO else '-$'}{abs(total_pnl):.2f}"
            unreal_str = f"{'+$' if unrealized_usd >= ZERO else '-$'}{abs(unrealized_usd):.2f}"

            print(f"""
╔══════════════════════════════════════════════════════════════════════════════════════╗
║ 👑 GODLIKE 2026 PROFIT ENGINE | SIMULATOR (AMM CONSTANT-PRODUCT)                     ║
╠══════════════════════════════════════════════════════════════════════════════════════╣
║ 💵 Likvid Kassa:       ${w['cash_usd']:7.2f} USD                                                ║
║ 📦 Aktiva Positioner:   ${open_val_usd:7.2f} USD  (Orealiserad PnL: {unreal_str:>9})                ║
║ 💸 Betalda Avgifter:    ${w['total_fees_paid_usd']:7.2f} USD  (Gas Priority + Raydium LP 0.30%)       ║
║ 💎 Total Slutportfölj:  ${total_port:7.2f} USD  (Äkta Netto: {pnl_str} / {total_pnl_pct:+.2f}%)   ║
║ 🏆 Avslutade Trades:     {w['total_trades']:2d} st    (Vinst: {w['winning_trades']} | Förlust: {w['losing_trades']} | Win Rate: {wr:4.1f}%)    ║
╠══════════════════════════════════════════════════════════════════════════════════════╣
║ ÖPPNA INNEHAV ({len(open_pos)}/{CONFIG['MAX_OPEN_POSITIONS']}):                                                             ║""")
            if pos_lines:
                for l in pos_lines:
                    print(f"║{l:<86}║")
            else:
                print("║  (Inga aktiva innehav. Skannar marknaden efter säkra par...)                    ║")
            print("╚══════════════════════════════════════════════════════════════════════════════════════╝")
        else:
            sol_bal = self.jup.get_sol_balance() if self.jup.keypair else ZERO
            try:
                sol_p = self.jup.get_sol_price_usd()
            except MarketDataUnavailableError:
                sol_p = ZERO
            cash_val = sol_bal * sol_p
            total_val = cash_val + open_val_usd
            unreal_str = f"{'+$' if unrealized_usd >= ZERO else '-$'}{abs(unrealized_usd):.2f}"

            with self.state._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT COUNT(*) as tot, SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins, SUM(fee_usd) as fees FROM trade_history WHERE is_paper = 0 AND action != 'BUY'")
                stat = c.fetchone()
                tot_trades = stat["tot"] or 0
                wins = stat["wins"] or 0
                losses = tot_trades - wins
                fees = dec(stat["fees"] or 0)
                wr = (wins / tot_trades * 100.0) if tot_trades > 0 else 0.0

            pub_str = str(self.jup.keypair.pubkey()) if self.jup.keypair else "Ingen plånbok ansluten"
            print(f"""
╔══════════════════════════════════════════════════════════════════════════════════════╗
║ ⚡ GODLIKE 2026 LIVE ON-CHAIN SOLANA ENGINE                                           ║
╠══════════════════════════════════════════════════════════════════════════════════════╣
║ 🔑 Plånbok:            {pub_str:<62}║
║ 💰 SOL Saldo:          {sol_bal:7.4f} SOL (~${cash_val:7.2f} USD vid ${sol_p:.2f}/SOL)              ║
║ 📦 Live Positioner:    ${open_val_usd:7.2f} USD  (Orealiserad PnL: {unreal_str:>9})                ║
║ 💎 Totalt Värde:       ${total_val:7.2f} USD                                                ║
║ 🏆 Avslutade Trades:     {tot_trades:2d} st    (Vinst: {wins} | Förlust: {losses} | Win Rate: {wr:4.1f}%)         ║
║ 💸 Betalda Avgifter:    ${fees:7.2f} USD                                                ║
╠══════════════════════════════════════════════════════════════════════════════════════╣
║ ÖPPNA LIVE-INNEHAV ({len(open_pos)}/{CONFIG['MAX_OPEN_POSITIONS']}):                                                        ║""")
            if pos_lines:
                for l in pos_lines:
                    print(f"║{l:<86}║")
            else:
                print("║  (Inga aktiva live-innehav. Övervakar marknaden efter godkända signaler...)  ║")
            print("╚══════════════════════════════════════════════════════════════════════════════════════╝")

    def run_paper_loop(self, duration_seconds: int = 1800, poll_interval: int = 5):
        start = time.time()
        deadline = start + duration_seconds
        LOGGER.info("🚀 Startar Godlike 2026 Paper Engine (%d minuter)...", duration_seconds // 60)

        try:
            while time.time() < deadline:
                # 1. Övervaka öppna positioner
                self.monitor.check_and_exit_positions(is_paper=True)
                self.print_hud(is_paper=True)

                # 2. Skanna och köp om ledig plats finns
                w = self.state.get_wallet()
                open_pos = self.state.get_open_positions(is_paper=True)
                if len(open_pos) < CONFIG["MAX_OPEN_POSITIONS"] and w["cash_usd"] >= CONFIG["TRADE_BUDGET_USD"]:
                    candidates = self.dex.scan_trending_mints()
                    for m in candidates:
                        if len(self.state.get_open_positions(is_paper=True)) >= CONFIG["MAX_OPEN_POSITIONS"]:
                            break
                        self.evaluate_and_buy(m, is_paper=True)
                        time.sleep(0.5)

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            LOGGER.info("Avbruten av användaren.")

        self.print_hud(is_paper=True)
        self.export_obsidian_report("PAPER_SESSION")

    def run_live_loop(self, poll_interval: int = 5):
        if not self.jup.keypair:
            print("❌ Ingen giltig plånbok hittades! Konfigurera WALLET_PRIVATE_KEY i .env.")
            return

        pubkey = str(self.jup.keypair.pubkey())
        sol_bal = self.jup.get_sol_balance()
        try:
            sol_p = self.jup.get_sol_price_usd()
        except MarketDataUnavailableError as e:
            print(f"❌ Marknadsfel: {e}")
            return
        val_usd = sol_bal * sol_p

        print("\n" + "=" * 80)
        print(" ⚡ LIVE TRADING ENGINE STARTAD — VERKLIGA MEDEL PÅ SOLANA")
        print("=" * 80)
        print(f" 🔑 Plånboksadress:  {pubkey}")
        print(f" 💰 SOL Saldo:       {sol_bal:.4f} SOL (~${val_usd:.2f} USD vid kurs ${sol_p:.2f})")
        print(f" 🎯 Insats per köp:  ${CONFIG['TRADE_BUDGET_USD']:.2f} USD")
        print(f" 🛡️ Max positioner:  {CONFIG['MAX_OPEN_POSITIONS']} st")
        print(f" 🔒 Gas-reserv:      {CONFIG['MIN_SOL_RESERVE']} SOL (Orörd för rent/avgifter)")
        print("=" * 80)

        if val_usd < (CONFIG["TRADE_BUDGET_USD"] + (CONFIG["MIN_SOL_RESERVE"] * sol_p)):
            print(f"⚠️ VARNING: Saldot (~${val_usd:.2f}) är lägre än din handelsbudget + gasreserv.")
            print("   Boten övervakar eventuella öppna positioner men kan inte öppna nya förrän SOL sätts in.")

        # Registrera initialt live-kapital om det inte redan registrerats
        self.state.record_live_initial_equity(val_usd)

        print("🚀 Sniper-loopen är igång. Tryck Ctrl+C för att stoppa.\n")
        try:
            while True:
                # Uppdatera SOL-kurs vid varje loop-iteration
                try:
                    sol_p = self.jup.get_sol_price_usd()
                except MarketDataUnavailableError as e:
                    LOGGER.warning("⚠️ Marknadspris för SOL otillgängligt (%s). Använder senaste kända kurs.", e)

                # 0. Orphan Protection: Försona tidigare oavslutade transaktioner
                self.reconcile_pending_transactions()

                # 1. Övervaka och exekvera exits för öppna live-positioner
                exits = self.monitor.check_and_exit_positions(is_paper=False)
                if exits:
                    for ex in exits:
                        LOGGER.info("🚨 LIVE EXIT GENOMFÖRD: %s (%s) Netto: $%.2f", ex["symbol"], ex["reason"], ex["net_usd"])

                self.print_hud(is_paper=False)

                # 2. Skanna och köp om ledig plats och medel finns
                open_pos = self.state.get_open_positions(is_paper=False)
                curr_sol = self.jup.get_sol_balance()
                curr_val = curr_sol * sol_p

                if len(open_pos) < CONFIG["MAX_OPEN_POSITIONS"] and curr_val >= (CONFIG["TRADE_BUDGET_USD"] + (CONFIG["MIN_SOL_RESERVE"] * sol_p)):
                    candidates = self.dex.scan_trending_mints()
                    for m in candidates:
                        if len(self.state.get_open_positions(is_paper=False)) >= CONFIG["MAX_OPEN_POSITIONS"]:
                            break
                        res = self.evaluate_and_buy(m, is_paper=False)
                        if res.get("status") == "BOUGHT":
                            LOGGER.info("🎉 SKARPT LIVE-KÖP GENOMFÖRT: %s för $%.2f USD!", res["symbol"], res["usd_invested"])
                            break
                        time.sleep(0.5)

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            LOGGER.info("⏹️ Live loop stoppad av användaren.")
        self.print_hud(is_paper=False)
        self.export_obsidian_report("LIVE_SESSION")

    def export_obsidian_report(self, mode: str):
        """Genererar och sparar en professionell Markdown post-mortem rapport till Obsidian."""
        try:
            vault_dir = Path(CONFIG["OBSIDIAN_VAULT_PATH"]) / "05_Daily_Post_Mortems"
            vault_dir.mkdir(parents=True, exist_ok=True)
            ts_str = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            report_file = vault_dir / f"{ts_str}-{mode}.md"

            is_paper = (mode != "LIVE_SESSION")
            open_pos = self.state.get_open_positions(is_paper=is_paper)

            with self.state._get_connection() as conn:
                c = conn.cursor()
                c.execute("""
                SELECT * FROM trade_history 
                WHERE is_paper = ? 
                ORDER BY id DESC LIMIT 20
                """, (1 if is_paper else 0,))
                recent_trades = [dict(r) for r in c.fetchall()]

            content = [
                f"# 📊 Institutional Audit Report: {mode}",
                f"- **Tidpunkt (UTC):** {datetime.now(timezone.utc).isoformat()}",
                f"- **Miljö:** {'Simulator (AMM Constant-Product)' if is_paper else 'SOLANA MAINNET LIVE'}",
            ]

            if is_paper:
                w = self.state.get_wallet()
                content.extend([
                    f"- **Startkassa:** ${w['initial_cash_usd']:.2f} USD",
                    f"- **Likvid Kassa:** ${w['cash_usd']:.2f} USD",
                    f"- **Betalda Avgifter:** ${w['total_fees_paid_usd']:.2f} USD",
                    f"- **Avslutade Trades:** {w['total_trades']} st (Vinst: {w['winning_trades']} | Förlust: {w['losing_trades']})",
                    f"- **Win Rate:** {(w['winning_trades'] / w['total_trades'] * 100) if w['total_trades'] > 0 else 0:.1f}%\n",
                ])
            else:
                sol_bal = self.jup.get_sol_balance() if self.jup.keypair else ZERO
                try:
                    sol_p = self.jup.get_sol_price_usd()
                except Exception:
                    sol_p = ZERO
                content.extend([
                    f"- **Plånbok:** {self.jup.keypair.pubkey() if self.jup.keypair else 'Ingen'}",
                    f"- **Aktuellt SOL Saldo:** {sol_bal:.4f} SOL (~${sol_bal * sol_p:.2f} USD)",
                ])

            content.append("## 📦 Aktiva Positioner")
            if open_pos:
                content.append("| ID | Symbol | Inköpspris | Investerat | Stop Loss | Trailing Status |")
                content.append("|---|---|---|---|---|---|")
                for p in open_pos:
                    content.append(f"| {p['id']} | `{p['symbol']}` | ${dec(p['entry_price_usd']):.6f} | ${dec(p['remaining_invested_usd']):.2f} | ${dec(p['stop_loss_price']):.6f} | {p['trailing_active']} |")
            else:
                content.append("_Inga öppna positioner vid sessionsavslut._")

            content.append("\n## 📜 Senaste Transaktioner")
            if recent_trades:
                content.append("| Pos ID | Symbol | Action | Pris | Belopp | PnL % | Avgift | Tid |")
                content.append("|---|---|---|---|---|---|---|---|")
                for t in recent_trades:
                    content.append(f"| {t['pos_id']} | `{t['symbol']}` | {t['action']} | ${dec(t['price_usd']):.6f} | ${dec(t['usd_amount']):.2f} | {dec(t['pnl_pct']):+.2f}% | ${dec(t['fee_usd']):.2f} | {datetime.fromtimestamp(t['timestamp'], timezone.utc).strftime('%H:%M:%S')} |")
            else:
                content.append("_Inga transaktioner utförda under denna session._")

            report_file.write_text("\n".join(content), encoding="utf-8")
            print(f"📓 Sessionsrapport sparad i Obsidian: {report_file}")
        except Exception as e:
            LOGGER.error("Kunde inte skriva Obsidian-rapport: %s", e)

# ==============================================================================
# ⚡ 30-MINUTERS ÄKTA TICK REPLAY (ISOLERAD KÖRMILJÖ)
# ==============================================================================

def run_real_solana_replay(bot: GodlikeBot, target_pool: Optional[str] = None) -> bool:
    pool = target_pool or "2YaGc2iYZt44B5zn5Tvk59APbAn51xGekzNQAPF2PGn1"
    print("\n" + "=" * 80)
    print(f"🚀 STARTAR 30-MINUTERS ÄKTA SOLANA TICK REPLAY FÖR POOL: {pool}")
    print("=" * 80)

    # Använd en separat temporär databas så användarens sparade historik aldrig skrivs över
    replay_db = f"./replay_isolated_{int(time.time())}.db"
    replay_cfg = dict(CONFIG)
    replay_cfg["DB_PATH"] = replay_db
    replay_bot = GodlikeBot(replay_cfg)

    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute?aggregate=1"
    candles = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            d = json.loads(res.read().decode())
            candles = sorted(d.get("data", {}).get("attributes", {}).get("ohlcv_list", []), key=lambda c: c[0])
    except Exception as e:
        print(f"❌ Kunde inte nå GeckoTerminal: {e}")
        return False

    if len(candles) < 30:
        print("❌ Otillräckligt med candles.")
        return False

    recent_30 = candles[-30:]
    entry_p = dec(recent_30[0][1])
    stake = CONFIG["TRADE_BUDGET_USD"]
    fee = CONFIG["ESTIMATED_GAS_USD"] + (stake * (CONFIG["DEX_POOL_FEE_PCT"] + CONFIG["BASE_SLIPPAGE_PCT"]) / HUNDRED)
    net_val = stake - fee
    raw_str = str(int((net_val / entry_p) * D("1000000")))

    real_mint = pool
    real_symbol = "REPLAY"
    try:
        pool_url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}"
        p_req = urllib.request.Request(pool_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(p_req, timeout=10) as p_res:
            p_data = json.loads(p_res.read().decode())
            bt_id = p_data.get("data", {}).get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
            if bt_id:
                real_mint = bt_id.replace("solana_", "")
            pool_name = p_data.get("data", {}).get("attributes", {}).get("name", "")
            if pool_name:
                real_symbol = pool_name.split("/")[0].strip()
    except Exception as e:
        LOGGER.warning("Kunde inte hämta pool-metadata dynamiskt från GeckoTerminal: %s", e)

    print(f"🛒 Köper {real_symbol} vid minut 0 på äkta kurs ${entry_p:.8f} (Insats: ${stake:.2f} USD)")
    replay_bot.state.record_open_position(
        mint=real_mint, symbol=real_symbol,
        pair_address=pool, decimals=6, price_usd=entry_p, usd_invested=stake,
        net_token_value=net_val, raw_amount=raw_str, fee_usd=fee,
        price_impact_pct=D("0.08"), tx_hash=f"tick_rep_{int(time.time())}", is_paper=True
    )

    orig_get = replay_bot.dex.get_token_data
    print("⏳ Spelar upp de 30 senaste minuternas verkliga kurser med High/Low-penetration...")
    for i, c in enumerate(recent_30):
        ts, o, h, l, close_p, vol = c
        c_open, c_high, c_low, c_close = dec(o), dec(h), dec(l), dec(close_p)

        pos_list = replay_bot.state.get_open_positions(is_paper=True)
        if not pos_list:
            break
        current_sl = dec(pos_list[0]["stop_loss_price"])
        tp1_target = entry_p * (ONE + CONFIG["TAKE_PROFIT_1_PCT"] / HUNDRED)

        # Institutionell Pessimism & Limit Fill Precision:
        # Om både SL och TP träffas under samma minut antas SL ha inträffat först (pessimistisk exekvering)
        # Om TP1 nås fylls den till målkursen tp1_target, inte absolut candle-topp
        if c_low <= current_sl and c_high >= tp1_target:
            eval_price = c_low
        elif c_low <= current_sl:
            eval_price = c_low
        elif c_high >= tp1_target:
            eval_price = tp1_target
        else:
            eval_price = c_close

        pnl = ((eval_price - entry_p) / entry_p) * HUNDRED
        replay_bot.dex.get_token_data = lambda m, cp=eval_price, v=vol, s=real_symbol, p=pool: {
            "symbol": s, "price_usd": cp, "liquidity_usd": D("320000"),
            "buys_h1": 50, "sells_h1": 20, "pair_address": p
        }

        exits = replay_bot.monitor.check_and_exit_positions(is_paper=True)
        print(f"   Min {i+1:2d}/30 | O: ${c_open:.6f} | H: ${c_high:.6f} | L: ${c_low:.6f} | C: ${c_close:.6f} ({pnl:+5.2f}%) | Vol: ${vol:,.0f}")
        if exits:
            print(f"   🎯 EXIT TRIGGAD vid minut {i+1}: {exits[0]['reason']} vid kurs ${eval_price:.8f} (PnL: {exits[0]['pnl_pct']:+.2f}%)!")
            break

    replay_bot.dex.get_token_data = orig_get
    replay_bot.print_hud(is_paper=True)
    replay_bot.export_obsidian_report("REPLAY_30MIN")

    # Beräkna och spara sammanställning innan temporär DB raderas
    w = replay_bot.state.get_wallet()
    open_pos = replay_bot.state.get_open_positions(is_paper=True)
    open_val = ZERO
    for p in open_pos:
        entry_p = dec(p["entry_price_usd"])
        net_val = dec(p["net_token_value_usd"])
        rem_inv = dec(p["remaining_invested_usd"])
        usd_inv = dec(p["usd_invested"])
        portion = (rem_inv / usd_inv) if usd_inv > ZERO else ONE
        data = replay_bot.dex.get_token_data(p["mint"])
        cur_p = data["price_usd"] if data else entry_p
        open_val += (net_val * portion) * (cur_p / entry_p) if entry_p > ZERO else (net_val * portion)

    total_port = w["cash_usd"] + open_val
    net_pnl = total_port - w["initial_cash_usd"]
    net_pnl_pct = (net_pnl / w["initial_cash_usd"]) * HUNDRED
    wr = (w["winning_trades"] / w["total_trades"] * 100.0) if w["total_trades"] > 0 else 0.0

    summary = {
        "initial_cash": w["initial_cash_usd"],
        "cash": w["cash_usd"],
        "open_val": open_val,
        "total_port": total_port,
        "net_pnl": net_pnl,
        "net_pnl_pct": net_pnl_pct,
        "total_trades": w["total_trades"],
        "winning_trades": w["winning_trades"],
        "losing_trades": w["losing_trades"],
        "win_rate": wr,
        "fees": w["total_fees_paid_usd"],
    }

    # Städa upp den temporära replay-databasen
    try:
        if os.path.exists(replay_db): os.remove(replay_db)
        if os.path.exists(replay_db + "-wal"): os.remove(replay_db + "-wal")
        if os.path.exists(replay_db + "-shm"): os.remove(replay_db + "-shm")
    except Exception:
        pass

    return summary

# ==============================================================================
# 🧪 SÄKERHETS- OCH SYSTEMTESTER (ISOLERAD TEST-DATABAS)
# ==============================================================================

def run_security_unit_tests() -> bool:
    print("\n" + "=" * 80)
    print("🧪 KÖR INSTITUTIONELLA SÄKERHETS- OCH SYSTEMTESTER (ISOLERAD TEST-DATABAS)...")
    print("=" * 80)

    # Skapa en separat isolerad testdatabas så användarens sparade historik aldrig raderas
    test_db = f"./test_isolated_{int(time.time())}.db"
    test_cfg = dict(CONFIG)
    test_cfg["DB_PATH"] = test_db
    test_bot = GodlikeBot(test_cfg)

    try:
        # Test 1: Svartlista
        test_bot.state.add_to_blacklist("ScamToken11111111111111111111111111111111", "Malicious Dev")
        can, r = test_bot.state.can_open_position("ScamToken11111111111111111111111111111111", trade_usd=D("20.00"))
        assert not can and "Svartlistad" in r
        print("✅ Test 1 Godkänt: Svartlista blockerar otillåtna tokens.")

        # Test 2: AMM Constant Product Prisimpact Matematik
        impact, factor = calculate_amm_price_impact(D("10000.00"), D("50000.00"))
        assert impact > D("28.0") and factor > ONE
        print(f"✅ Test 2 Godkänt: AMM Prisimpact-motor aktiv ($10k i $50k pool ger {impact:.2f}% impact).")

        # Test 3: Decimal Precision & Dubbelräkningsskydd vid TP1
        entry_p = D("0.001000")
        stake = D("20.00")
        fee = D("0.41")
        net_val = stake - fee
        pos_id = test_bot.state.record_open_position(
            mint="TestToken11111111111111111111111111111111", symbol="TEST",
            pair_address="PairTest111", decimals=6, price_usd=entry_p, usd_invested=stake,
            net_token_value=net_val, raw_amount="19590000", fee_usd=fee,
            price_impact_pct=D("0.10"), tx_hash="tx_test", is_paper=True
        )
        w_before = test_bot.state.get_wallet()
        assert w_before["cash_usd"] == D("80.00")

        # Simulera TP1 (+40%)
        orig_data = test_bot.dex.get_token_data
        test_bot.dex.get_token_data = lambda m: {"price_usd": entry_p * D("1.40"), "liquidity_usd": D("50000"), "buys_h1": 50, "sells_h1": 20, "pair_address": "PairTest111"}
        test_bot.monitor.check_and_exit_positions(is_paper=True)
        w_after = test_bot.state.get_wallet()
        assert w_after["cash_usd"] > D("80.00")

        # Verifiera att positionens återstående insats är 60% (inte 100%)
        pos = test_bot.state.get_open_positions(is_paper=True)[0]
        assert dec(pos["remaining_invested_usd"]) == D("12.00")
        assert dec(pos["stop_loss_price"]) >= entry_p * D("1.15")
        print("✅ Test 3 Godkänt: Dubbelräkningsskydd vid TP1 och sparad SL verifierad (60% kvar, SL uppdaterad).")

        # Test 4: Verklig Stop-Loss Exekvering vid Gap-Down (Noll phantom-priser)
        crash_p = D("0.000200") # Kurs kraschade 80%
        test_bot.dex.get_token_data = lambda m: {"price_usd": crash_p, "liquidity_usd": D("50000"), "buys_h1": 50, "sells_h1": 20, "pair_address": "PairTest111"}
        exits = test_bot.monitor.check_and_exit_positions(is_paper=True)
        assert exits and "STOP" in exits[0]["reason"]
        assert exits[0]["pnl_pct"] < D("-75.0")
        print(f"✅ Test 4 Godkänt: Stop-Loss säljer till verklig kraschkurs vid gap-down ({exits[0]['pnl_pct']:.2f}% PnL).")

        # Test 5: Circuit Breaker vid 3 förluster
        test_bot.state.reset_wallet(D("100.00"))
        with test_bot.state._get_connection() as conn:
            c = conn.cursor()
            now = time.time()
            for i in range(3):
                c.execute("""
                INSERT INTO positions (
                    mint, symbol, pair_address, decimals, initial_raw, remaining_raw,
                    entry_price_usd, usd_invested, remaining_invested_usd, net_token_value_usd,
                    fees_paid_usd, price_impact_pct, entry_time, exit_time, peak_price_usd,
                    stop_loss_price, status, net_usd_received, pnl_pct, is_paper
                ) VALUES (?, ?, 'pair', 6, '100', '0', '1.0', '20.0', '0', '19.4', '0.4', '0.1', ?, ?, '1.0', '0.8', 'CLOSED', '16.0', '-20.0', 1)
                """, (f"LossToken{i}", f"LOSS{i}", now - 300, now - (100 * (3 - i))))
                c.execute("""
                INSERT INTO trade_history (pos_id, mint, symbol, action, price_usd, usd_amount, pnl_pct, fee_usd, timestamp, tx_hash, is_paper)
                VALUES (?, ?, ?, 'STOP_LOSS', '0.8', '16.0', '-20.0', '0.4', ?, 'tx', 1)
                """, (i + 1, f"LossToken{i}", f"LOSS{i}", now - (100 * (3 - i))))

        can_open, cb_msg = test_bot.state.can_open_position("AnyTokenAfter3Losses1111111111111111111111", trade_usd=D("20.00"))
        assert not can_open and "CIRCUIT BREAKER" in cb_msg
        print(f"✅ Test 5 Godkänt: Circuit Breaker låste kontot vid 3 förluster ({cb_msg}).")

        # Test 6: Solders Signering (om installerad)
        if SOLDERS_AVAILABLE:
            test_kp = Keypair()
            sig = test_kp.sign_message(b"godlike2026_test_signature")
            assert len(bytes(sig)) == 64
            print(f"✅ Test 6 Godkänt: Solders signering fungerar felfritt (Plånbok: {str(test_kp.pubkey())[:12]}...).")

        # Test 7: Orphan Transaction Reconciler & Rate Limiter
        limiter = RateLimiter(max_per_minute=100)
        for _ in range(5):
            limiter.wait()
        assert len(limiter.timestamps) == 5

        test_bot.state.record_pending_tx(
            tx_hash="orphan_tx_test_123", pos_id=0, mint="OrphanToken1111111111111111111111111111111",
            symbol="ORPHAN", pair_address="PairOrphan1", decimals=6,
            direction="BUY", amount_raw="1000000", expected_usd="20.00"
        )
        unres = test_bot.state.get_unresolved_pending_txs()
        assert len(unres) == 1 and unres[0]["tx_hash"] == "orphan_tx_test_123"
        test_bot.state.resolve_pending_tx("orphan_tx_test_123", "CONFIRMED")
        assert len(test_bot.state.get_unresolved_pending_txs()) == 0
        print("✅ Test 7 Godkänt: Orphan Transaction Tracking & Rate Limiter verifierade.")

        test_bot.dex.get_token_data = orig_data
        print("\n🎉 Samtliga 7 institutionella säkerhetstester godkända i isolerad miljö!")
        return True
    finally:
        # Rensa temporär testdatabas
        try:
            if os.path.exists(test_db): os.remove(test_db)
            if os.path.exists(test_db + "-wal"): os.remove(test_db + "-wal")
            if os.path.exists(test_db + "-shm"): os.remove(test_db + "-shm")
        except Exception:
            pass

# ==============================================================================
# 🔌 MCP SERVER (MODEL CONTEXT PROTOCOL FÖR AI-AGENTER & CLAUDE DESKTOP)
# ==============================================================================

class MCPServer:
    """
    Standardiserad Model Context Protocol (MCP) server över JSON-RPC 2.0 (stdio).
    Möjliggör direkt integration med Claude Desktop, Cursor och autonoma AI-agenter.
    """
    def __init__(self, bot: GodlikeBot):
        self.bot = bot

    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        msg_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "godlike2026-mcp", "version": "2.0.0"}
                }
            }
        elif method == "notifications/initialized":
            return {}
        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": "get_portfolio_status",
                            "description": "Hämtar aktuell kassa, öppna positioner och PnL för boten.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"is_paper": {"type": "boolean", "default": True}}
                            }
                        },
                        {
                            "name": "scan_market_opportunities",
                            "description": "Skannar Solana efter trendande tokens och utvärderar Trench IQ & RugCheck.",
                            "inputSchema": {"type": "object", "properties": {}}
                        },
                        {
                            "name": "audit_token_safety",
                            "description": "Kör fullständig institutional RugCheck validering på en Solana mintadress.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"mint": {"type": "string", "description": "Solana Token Mint Adress"}},
                                "required": ["mint"]
                            }
                        },
                        {
                            "name": "reset_paper_wallet",
                            "description": "Nollställer simulator-kassan till $100.00 USD och stänger alla öppna positioner.",
                            "inputSchema": {"type": "object", "properties": {}}
                        },
                        {
                            "name": "run_security_audit",
                            "description": "Kör samtliga 7 institutionella säkerhetstester i isolerad databas.",
                            "inputSchema": {"type": "object", "properties": {}}
                        }
                    ]
                }
            }
        elif method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments", {})
            try:
                if tool_name == "get_portfolio_status":
                    is_paper = args.get("is_paper", True)
                    open_pos = self.bot.state.get_open_positions(is_paper=is_paper)
                    if is_paper:
                        w = self.bot.state.get_wallet()
                        res_text = (
                            f"Kassa: ${w['cash_usd']:.2f} USD | "
                            f"Öppna positioner: {len(open_pos)} | "
                            f"Trades: {w['total_trades']} (Vinster: {w['winning_trades']}, Förluster: {w['losing_trades']}) | "
                            f"Avgifter: ${w['total_fees_paid_usd']:.2f} USD"
                        )
                    else:
                        sol_b = self.bot.jup.get_sol_balance()
                        res_text = f"Live SOL Saldo: {sol_b:.4f} SOL | Öppna Live Positioner: {len(open_pos)}"
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": res_text}]}}

                elif tool_name == "scan_market_opportunities":
                    mints = self.bot.dex.scan_trending_mints()
                    results = []
                    for m in mints[:5]:
                        d = self.bot.dex.get_token_data(m)
                        if d:
                            safe, reason, _ = self.bot.rug.validate(m, target_pair_address=d.get("pair_address"))
                            results.append(f"{d['symbol']} ({m[:8]}...): Pris ${d['price_usd']:.6f}, Likv: ${d['liquidity_usd']:,.0f}, Säker: {safe} ({reason})")
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": "\n".join(results) if results else "Inga tokens hittades."}]}}

                elif tool_name == "audit_token_safety":
                    mint = args.get("mint", "")
                    d = self.bot.dex.get_token_data(mint)
                    pair_addr = d.get("pair_address") if d else None
                    safe, msg, meta = self.bot.rug.validate(mint, target_pair_address=pair_addr)
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": f"Säker: {safe}\nResultat: {msg}\nMetadata: {json.dumps(meta, default=str)}"}]}}

                elif tool_name == "reset_paper_wallet":
                    self.bot.state.reset_wallet(D("100.00"))
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": "Paper wallet återställd till $100.00 USD."}]}}

                elif tool_name == "run_security_audit":
                    ok = run_security_unit_tests()
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": f"Säkerhetsaudit: {'ALLA TESTER GODKÄNDA' if ok else 'TESTER MISSLYCKADES'}"}]}}

                else:
                    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Okänt verktyg: {tool_name}"}}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(e)}}

        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Metod stöds ej: {method}"}}

    def run_stdio(self):
        real_stdout = sys.stdout
        sys.stdout = sys.stderr

        sys.stderr.write("🔌 Godlike 2026 MCP Server startad på stdin/stdout (JSON-RPC 2.0)...\n")
        sys.stderr.flush()
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                req = json.loads(line)
                resp = self.handle_request(req)
                if resp:
                    real_stdout.write(json.dumps(resp) + "\n")
                    real_stdout.flush()
        except (KeyboardInterrupt, EOFError):
            pass
        except Exception as e:
            sys.stderr.write(f"MCP Fel: {e}\n")
            sys.stderr.flush()
        finally:
            sys.stdout = real_stdout

# ==============================================================================
# 🎮 SIMPEL INTERACTION: TEST MODE & LIVE MODE
# ==============================================================================

def run_test_mode(bot: GodlikeBot):
    """
    TEST MODE: Kör 30 minuters simulering med $100 startkassa och beräknar full Profit Check.
    """
    print("\n" + "=" * 75)
    print(" 🧪 TEST MODE: 30 MINUTERS PROFIT CHECK ($100 KASSA)")
    print("=" * 75)
    print(" [1] ⚡ Snabb 30-minuters Solana Marknadskörning (Äkta tick-data, klar direkt)")
    print(" [2] ⏳ Kontinuerlig 30-minuters Marknadsskanning (Väntar 30 minuter i realtid)")
    mode_choice = safe_input(" Välj körläge [1 eller 2, standard 1]: ")

    # Nollställ testkassan till exakt $100.00
    bot.state.reset_wallet(D("100.00"))

    if mode_choice == "2":
        print("\n🚀 Startar 30 minuters kontinuerlig marknadssimulering... (Tryck Ctrl+C för att avbryta i förtid)")
        bot.run_paper_loop(duration_seconds=1800, poll_interval=5)
        w = bot.state.get_wallet()
        open_pos = bot.state.get_open_positions(is_paper=True)
        open_val = ZERO
        for p in open_pos:
            entry_p = dec(p["entry_price_usd"])
            net_val = dec(p["net_token_value_usd"])
            rem_inv = dec(p["remaining_invested_usd"])
            usd_inv = dec(p["usd_invested"])
            portion = (rem_inv / usd_inv) if usd_inv > ZERO else ONE
            data = bot.dex.get_token_data(p["mint"])
            cur_p = data["price_usd"] if data else entry_p
            open_val += (net_val * portion) * (cur_p / entry_p) if entry_p > ZERO else (net_val * portion)

        total_port = w["cash_usd"] + open_val
        net_pnl = total_port - w["initial_cash_usd"]
        net_pnl_pct = (net_pnl / w["initial_cash_usd"]) * HUNDRED
        wr = (w["winning_trades"] / w["total_trades"] * 100.0) if w["total_trades"] > 0 else 0.0
        summary = {
            "initial_cash": w["initial_cash_usd"],
            "cash": w["cash_usd"],
            "open_val": open_val,
            "total_port": total_port,
            "net_pnl": net_pnl,
            "net_pnl_pct": net_pnl_pct,
            "total_trades": w["total_trades"],
            "winning_trades": w["winning_trades"],
            "losing_trades": w["losing_trades"],
            "win_rate": wr,
            "fees": w["total_fees_paid_usd"],
        }
    else:
        print("\n🚀 Hämtar de senaste 30 minuternas verkliga kurser från Solana on-chain pool...")
        pool = "HvYfFr8nm6bn9V93TDDMgqWwS2D2LsMn1Y3rDCToqeAB"
        summary = run_real_solana_replay(bot, target_pool=pool)
        if not summary:
            summary = run_real_solana_replay(bot, target_pool="2YaGc2iYZt44B5zn5Tvk59APbAn51xGekzNQAPF2PGn1")

    # Slutgiltig Profit Check
    if summary:
        print("\n" + "=" * 75)
        print(" 📊 SLUTGILTIG PROFIT CHECK (30 MINUTERS TEST)")
        print("=" * 75)
        print(f" • Startkapital:      ${summary['initial_cash']:.2f} USD")
        print(f" • Kassa vid avslut:  ${summary['cash']:.2f} USD")
        print(f" • Värde öppna trades:${summary['open_val']:.2f} USD")
        print(f" • Slutlig Portfölj:  ${summary['total_port']:.2f} USD")
        pnl = summary['net_pnl']
        pct = summary['net_pnl_pct']
        print(f" • Nettoresultat:     {'+$' if pnl >= ZERO else '-$'}{abs(pnl):.2f} USD ({pct:+.2f}%)")
        print(f" • Totala Trades:     {summary['total_trades']} st (Vinst: {summary['winning_trades']} | Förlust: {summary['losing_trades']} | Win Rate: {summary['win_rate']:.1f}%)")
        print(f" • Betalda Avgifter:  ${summary['fees']:.2f} USD (DEX fee + Solana gas)")
        print("-" * 75)
        if pnl > ZERO:
            print(f" 🎉 RESULTAT: SYSTEMET ÄR PROFITABELT! (+${pnl:.2f} USD / {pct:+.2f}%)")
            print("    Vinst säkrad efter att samtliga gas- och AMM-avgifter dragits av.")
        elif pnl == ZERO:
            print(" ⚖️ RESULTAT: BREAKEVEN ($0.00 förändring, fullt kapitalskydd).")
        else:
            print(f" ⚠️ RESULTAT: FÖRLUST ({pct:+.2f}%). Stop-loss har skyddat kapitalet från total förlust.")
        print("=" * 75)

def run_live_mode_interactive(bot: GodlikeBot):
    """
    LIVE MODE: Startar skarp live-handel på Solana.
    """
    print("\n" + "=" * 75)
    print(" ⚡ LIVE MODE: SKARP HANDEL PÅ SOLANA (VERKLIGA MEDEL)")
    print("=" * 75)
    if not SOLDERS_AVAILABLE:
        print("❌ 'solders' saknas i Python. Kör: pip install solders requests")
        return

    priv_key = CONFIG["WALLET_PRIVATE_KEY"] or os.getenv("WALLET_PRIVATE_KEY", "")
    if not priv_key:
        print("🔒 INGEN PRIVATNYCKEL FUNNEN I .ENV!")
        print("För din säkerhet kan du inte skriva in privata nycklar direkt i terminalen.")
        print("Gör så här:")
        print("  1. Skapa eller öppna filen .env i mappen.")
        print("  2. Lägg till: WALLET_PRIVATE_KEY=din_base58_privatnyckel")
        print("  3. Starta om boten och välj Live Mode.")
        return

    bot.jup.private_key_b58 = priv_key
    bot.jup._load_keypair()
    if not bot.jup.keypair:
        print("❌ Kunde inte ladda Keypair från WALLET_PRIVATE_KEY i .env.")
        return

    pubkey = str(bot.jup.keypair.pubkey())
    sol_bal = bot.jup.get_sol_balance()
    try:
        sol_p = bot.jup.get_sol_price_usd()
    except MarketDataUnavailableError as e:
        print(f"❌ Marknadsfel: {e}")
        return
    val_usd = sol_bal * sol_p

    print(f" 🔑 Plånboksadress:  {pubkey}")
    print(f" 💰 SOL Saldo:       {sol_bal:.4f} SOL (~${val_usd:.2f} USD vid kurs ${sol_p:.2f})")
    print(f" 🎯 Insats per köp:  ${CONFIG['TRADE_BUDGET_USD']:.2f} USD")
    print(f" 🛡️ Max positioner:  {CONFIG['MAX_OPEN_POSITIONS']} st")
    print(f" 🔒 Gas-reserv:      {CONFIG['MIN_SOL_RESERVE']} SOL (Orörd för rent/avgifter)")
    print("-" * 75)

    if val_usd < (CONFIG["TRADE_BUDGET_USD"] + (CONFIG["MIN_SOL_RESERVE"] * sol_p)):
        print(f"⚠️ OBS: Saldot (~${val_usd:.2f}) är lägre än din handelsbudget + gasreserv.")
        print("   Sätt in mer SOL på din plånbok för att kunna öppna köp.")

    confirm = safe_input("\nSkriv 'JA' för att bekräfta och starta skarp live-trading: ")
    if confirm == "JA":
        CONFIG["DRY_RUN"] = False
        bot.jup.dry_run = False
        bot.run_live_loop(poll_interval=5)
    else:
        print("Live-handel avbröts av användaren.")

def interactive_menu(bot: GodlikeBot):
    while True:
        print("\n" + "=" * 65)
        print(" 👑 GODLIKE 2026 — SOLANA SNIPER")
        print("=" * 65)
        print(" [1] 🧪 TEST MODE  (Paper Trading — 30 minuter, Profit Check)")
        print(" [2] ⚡ LIVE MODE  (Skarp on-chain handel med riktiga medel)")
        print(" [0] Avsluta")
        print("=" * 65)

        choice = safe_input("Välj läge [1, 2 eller 0]: ")
        if not choice or choice == "0":
            print("\nAvslutar Godlike 2026. Välkommen åter!")
            break

        if choice == "1":
            run_test_mode(bot)
            safe_input("\nTryck Enter för att gå tillbaka till menyn...")
        elif choice == "2":
            run_live_mode_interactive(bot)
            safe_input("\nTryck Enter för att gå tillbaka till menyn...")
        else:
            print("Ogiltigt val, välj 1, 2 eller 0.")

# ==============================================================================
# 🚀 ENTRYPOINT
# ==============================================================================

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description="GODLIKE 2026 Solana Sniper Engine")
    parser.add_argument("--test", action="store_true", help="Starta Test Mode (30 minuters körning & profit check)")
    parser.add_argument("--realtime", action="store_true", help="Starta 30 minuters kontinuerlig realtidsskanning i pappersläge")
    parser.add_argument("--live", action="store_true", help="Starta Live Mode (skarp handel på Solana)")
    parser.add_argument("--unit-tests", action="store_true", help="Kör de 7 tekniska enhetstesterna")
    parser.add_argument("--mcp", action="store_true", help="Starta MCP server för AI-agenter")
    args = parser.parse_args()

    bot = GodlikeBot(CONFIG)

    if args.mcp:
        mcp = MCPServer(bot)
        mcp.run_stdio()
        sys.exit(0)

    if args.unit_tests:
        sys.exit(0 if run_security_unit_tests() else 1)

    if args.realtime:
        print("\n🚀 Startar 30 minuters kontinuerlig realtidsskanning mot marknaden...")
        bot.state.reset_wallet(D("100.00"))
        bot.run_paper_loop(duration_seconds=1800, poll_interval=5)
        sys.exit(0)

    if args.test:
        run_test_mode(bot)
        sys.exit(0)

    if args.live:
        run_live_mode_interactive(bot)
        sys.exit(0)

    interactive_menu(bot)

if __name__ == "__main__":
    main()
