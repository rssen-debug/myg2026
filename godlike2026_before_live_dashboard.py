#!/usr/bin/env python3
"""GODLIKE 2026: experimental paper trading and auditable exit replay.
No demonstrated trading edge, guaranteed fills, anti-rug guarantee or live readiness.
See README_SV.md for verified behavior, model assumptions and remaining limitations.
"""

from __future__ import annotations

import os
import sys
import json
import time
import base64
import threading
from contextlib import contextmanager
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

LIVE_BLOCK_REASON = "Livehandel spärrad: transaktionsvis avstämning, återstart och orderkontroller är inte verifierade. Denna version är endast för paper/replay."

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
    "BREAKEVEN_LOCK_PCT": D("3.0"),       # Prisbaserad stopnivå +3%; ingen garanti om nettovinst
    "TAKE_PROFIT_1_PCT": D("40.0"),       # TP1 vid +40% (sälj 40% av positionen)
    "TAKE_PROFIT_2_PCT": D("80.0"),       # TP2 vid +80% (sälj 35% av positionen)
    "MOONBAG_TRAILING_PCT": D("12.0"),    # Moonbag: Traila -12% från ATH på resterande 25%

    # Riskhantering & Circuit Breakers
    "MAX_CONSECUTIVE_LOSSES": 3,          # Frys nya köp efter 3 förluster i rad
    "CIRCUIT_BREAKER_HOURS": 4.0,         # Paustid vid förlustsvit
    "MAX_DRAWDOWN_PCT": D("20.0"),        # Spärra nya köp vid 20% drawdown från observerad topp
    "MIN_SOL_RESERVE": D("0.05"),         # Minsta SOL-reserv för gas/rent som aldrig får spenderas

    # On-chain Säkerhetsfilter (RugCheck & Trench)
    "MIN_LIQUIDITY_USD": D("15000.00"),   # Minsta pool-likviditet ($15k)
    "MAX_TOP10_INSIDER_PCT": D("20.0"),   # Max andel för top 10 insiders (exkl pool-valv)
    "MAX_RUGCHECK_SCORE": 250,            # Max tillåten RugCheck riskpoäng
    "MIN_LP_LOCKED_BURNED_PCT": D("85.0"), # Minst 85% av LP i målpoolen måste vara låst/bränd

    # Live Trading (Jupiter & Solana)
    "DRY_RUN": True,
    "MAX_MARKET_DATA_AGE_SECONDS": 30,
    "SIGNAL_MIN_SECONDS": 30,
    "MIN_VOLUME_H1_USD": D("25000"),
    "SOLANA_RPC_URL": os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
    "WALLET_PRIVATE_KEY": "",  # Paper build never loads private keys
    "SOL_MINT": "So11111111111111111111111111111111111111112",
    "USDC_MINT": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "PRIORITY_FEE_LAMPORTS": 100_000,     # Jito/Priority fee (0.0001 SOL)
    "DEFAULT_SLIPPAGE_BPS": 250,          # 2.5% slippage tolerans för memecoins
    # Legacy Jupiter v1-referenser; live är spärrat och API-integrationen ej verifierad
    "JUPITER_QUOTE_ENDPOINTS": [
        "https://api.jup.ag/swap/v1/quote",
    ],
    "JUPITER_SWAP_ENDPOINTS": [
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

def calculate_amm_price_impact(trade_usd, pool_liquidity_usd):
    """Balanced constant-product approximation; liquidity is total pool USD TVL."""
    amount, liquidity = dec(trade_usd), dec(pool_liquidity_usd)
    if liquidity <= ZERO or amount <= ZERO:
        raise ValueError("Belopp och likviditet måste vara positiva")
    reserve = liquidity / D(2)
    return amount / (reserve + amount) * HUNDRED, ONE + amount / reserve

# ==============================================================================
# 🗄️ STATE MANAGER & SQLITE WAL-MODE
# ==============================================================================

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

def print_performance(summary):
    print("\nPAPER-RESULTAT — simulerat, ej bevisad live-lönsamhet")
    print(f"Kassa: ${summary['cash']:.2f} | Avgifter: ${summary['fees']:.2f}")
    if summary.get("valuation_complete", True):
        print(f"Nettoportfölj: ${summary['total_port']:.2f} | Resultat: ${summary['net_pnl']:+.2f} ({summary['net_pnl_pct']:+.2f}%)")
    else:
        print("Portföljvärde och total PnL: OKÄNT — minst ett innehav kan inte värderas för försäljning.")
    print(f"Avslutade positioner: {summary['total_trades']} | Vinster: {summary['winning_trades']} | Förluster: {summary['losing_trades']}")
    print("Ett positivt scenario är inget bevis på en lönsam strategi. Öppen moonbag är inte realiserad vinst.")


class StateManager:
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
                init_cash = str(self.cfg["INITIAL_CASH_USD"])
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
        initial_cash = dec(initial_cash)
        if initial_cash <= ZERO:
            raise ValueError("Startkapital måste vara positivt")
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
            c.execute("UPDATE wallet SET peak_equity_usd=? WHERE id=1", (str(initial_cash),))
            c.execute("DELETE FROM equity_history")
            c.execute("DELETE FROM market_observations")
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

    def record_open_position(self, mint: str, symbol: str, pair_address: str, decimals: int,
                             price_usd: Decimal, usd_invested: Decimal, net_token_value: Decimal,
                             raw_amount: str, fee_usd: Decimal, price_impact_pct: Decimal,
                             tx_hash: str, is_paper: bool = True) -> int:
        raw_value = dec(raw_amount)
        usd_invested, price_usd, fee_usd = dec(usd_invested), dec(price_usd), dec(fee_usd)
        net_token_value = dec(net_token_value)
        if raw_value <= ZERO or raw_value != raw_value.to_integral_value():
            raise ValueError("Köp kräver positiva heltal raw tokens")
        if usd_invested <= ZERO or price_usd <= ZERO or fee_usd < ZERO or net_token_value <= ZERO:
            raise ValueError("Ogiltig köpbokföring")
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
            raise ValueError("Ogiltiga decimals")
        raw_amount = str(int(raw_value))
        now = time.time()
        initial_sl = price_usd * (ONE - self.cfg["INITIAL_STOP_LOSS_PCT"] / HUNDRED)

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            c = conn.cursor()
            if conn.execute("SELECT 1 FROM positions WHERE mint=? AND is_paper=? AND status='OPEN'", (mint, int(is_paper))).fetchone():
                raise ValueError("Position redan öppen")
            if is_paper:
                current = conn.execute("SELECT cash_usd FROM wallet WHERE id=1").fetchone()
                if dec(current[0]) < usd_invested:
                    raise ValueError("Otillräcklig kassa")
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
        self.keypair = None  # No signing keys are loaded by this paper build.

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
        Indicativ exekveringsquote, inte en exakt USD-markering. Legacy API kräver validerad integration.
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

    def execute_live_swap(self, quote, in_mint, out_mint, pos_id=0, direction="SWAP", amount_raw=0,
                          expected_usd=ZERO, symbol="", pair_address="", decimals=6):
        return False, "", LIVE_BLOCK_REASON, ZERO

# ==============================================================================
# 🎯 POSITIONSMONITOR & PROFIT ENGINE
# ==============================================================================

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

# ==============================================================================
# 🤖 BOT ORCHESTRATOR
# ==============================================================================

class GodlikeBot:
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
                if len(open_pos) < self.cfg["MAX_OPEN_POSITIONS"] and w["cash_usd"] >= self.cfg["TRADE_BUDGET_USD"]:
                    candidates = self.dex.scan_trending_mints()
                    for m in candidates:
                        if len(self.state.get_open_positions(is_paper=True)) >= self.cfg["MAX_OPEN_POSITIONS"]:
                            break
                        self.evaluate_and_buy(m, is_paper=True)
                        time.sleep(0.5)

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            LOGGER.info("Avbruten av användaren.")

        self.print_hud(is_paper=True)
        self.export_obsidian_report("PAPER_SESSION")

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

# ==============================================================================
# ⚡ 30-MINUTERS ÄKTA TICK REPLAY (ISOLERAD KÖRMILJÖ)
# ==============================================================================

def run_real_solana_replay(bot: GodlikeBot, target_pool: Optional[str] = None, *,
                           csv_path=None, decimals=None, output_dir=None):
    """Compatibility entry point: require real historical liquidity, never invent it."""
    from replay_csv import run_replay
    cfg = bot.cfg
    csv_path = csv_path or cfg.get("REPLAY_CSV_PATH")
    decimals = cfg.get("REPLAY_TOKEN_DECIMALS") if decimals is None else decimals
    if not csv_path or decimals is None:
        print("Replay kräver CSV med historiska OHLC-priser och likviditet vid varje bars öppning, samt tokenens faktiska decimals.")
        print("Använd --replay-csv FIL --decimals N. Enbart GeckoTerminal OHLCV räcker inte för realistiska fills.")
        print("Detta är ett test av en manuell entry och exitregler, inte ett bevis på lönsam tokenurvalsstrategi.")
        return False
    try:
        summary = run_replay(
            csv_path, config=cfg, decimals=decimals, output_dir=output_dir,
            buy_fn=lambda budget, price, liquidity, digits: simulate_paper_buy(budget, price, liquidity, digits, config=cfg),
            sell_fn=lambda raw, digits, price, liquidity: simulate_paper_sell(raw, digits, price, liquidity, config=cfg),
        )
    except (ValueError, OSError) as exc:
        print(f"Replay avbruten: {exc}")
        return False
    print(f"Offline exitreplay klar. Bestående rapport: {summary['report_path']}")
    if summary.get("synthetic_input"):
        print("SYNTETISKA DEMODATA: påhittade priser/likviditet; resultatet visar ingen marknadslönsamhet.")
    print("Strategins lönsamhet är INTE verifierad av ett test med en manuellt vald entry.")
    return summary

# ==============================================================================
# 🧪 SÄKERHETS- OCH SYSTEMTESTER (ISOLERAD TEST-DATABAS)
# ==============================================================================

def run_security_unit_tests():
    import unittest
    tests = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_*.py")
    return unittest.TextTestRunner(verbosity=2).run(tests).wasSuccessful()

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
                            "description": "Läser trendande Solana-tokens och riskrapporter; detta är inte verifierade köpsignaler.",
                            "inputSchema": {"type": "object", "properties": {}}
                        },
                        {
                            "name": "audit_token_safety",
                            "description": "Granskar RugCheck-rapporten för en uttryckligen vald pool; ingen säkerhetsgaranti.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"mint": {"type": "string", "description": "Solana Token Mint Adress"}},
                                "required": ["mint"]
                            }
                        },
                        {
                            "name": "reset_paper_wallet",
                            "description": "Raderar paperhistoriken och återställer konfigurerat startkapital. Detta är ingen försäljning.",
                            "inputSchema": {"type": "object", "properties": {}}
                        },
                        {
                            "name": "run_security_audit",
                            "description": "Kör offline-regressionstester med isolerade fixtures; ingen live-certifiering.",
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
                    if is_paper:
                        res_text = json.dumps(self.bot.state.performance_report(self.bot.dex), default=str, ensure_ascii=False)
                    else:
                        res_text = LIVE_BLOCK_REASON
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": res_text}]}}

                elif tool_name == "scan_market_opportunities":
                    mints = self.bot.dex.scan_trending_mints()
                    results = []
                    for m in mints[:5]:
                        d = self.bot.dex.get_token_data(m)
                        if d:
                            safe, reason, _ = self.bot.rug.validate(m, target_pair_address=d.get("pair_address"))
                            results.append(f"{d['symbol']} ({m[:8]}...): Pris ${d['price_usd']:.6f}, Likv: ${d['liquidity_usd']:,.0f}, Filter godkänt: {safe} ({reason})")
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": "\n".join(results) if results else "Inga tokens hittades."}]}}

                elif tool_name == "audit_token_safety":
                    mint = args.get("mint", "")
                    d = self.bot.dex.get_token_data(mint)
                    pair_addr = d.get("pair_address") if d else None
                    safe, msg, meta = self.bot.rug.validate(mint, target_pair_address=pair_addr)
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": f"Filter godkänt: {safe}\nResultat: {msg}\nMetadata: {json.dumps(meta, default=str)}"}]}}

                elif tool_name == "reset_paper_wallet":
                    self.bot.state.reset_wallet(self.bot.cfg["INITIAL_CASH_USD"])
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": f"Ny paperhistorik med startkassa ${self.bot.cfg['INITIAL_CASH_USD']:.2f}. Inga verkliga säljorder gjordes."}]}}

                elif tool_name == "run_security_audit":
                    ok = run_security_unit_tests()
                    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": f"Offline-kodtester: {'ALLA TESTER GODKÄNDA' if ok else 'TESTER MISSLYCKADES'}"}]}}

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

def interactive_menu(bot: GodlikeBot):
    while True:
        print("\n" + "=" * 65)
        print(" 👑 GODLIKE 2026 — SOLANA SNIPER")
        print("=" * 65)
        print(" [1] 🧪 TEST MODE  (Paper / CSV — simulerat resultat)")
        print(" [2] ⚡ LIVE MODE  (Spärrat i denna granskningsversion)")
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

if __name__ == "__main__":
    main()
