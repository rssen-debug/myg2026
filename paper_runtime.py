"""Visible real-market PAPER session. All trading stays in one worker thread."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from telemetry import NETWORK_METRICS, Telemetry
from console_dashboard import ConsoleDashboard


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(target)


class SessionLock:
    """OS releases this lock even if the terminal/process crashes."""
    def __init__(self, db_path):
        self.path = Path(str(Path(db_path).resolve()) + ".lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("Denna paperdatabas körs redan i ett annat fönster.") from exc
        return self

    def __exit__(self, *_):
        if self.file is not None:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()


class PaperSession:
    """Separates a responsive read-only screen from serialized paper decisions."""
    def __init__(self, bot, duration_seconds=0, poll_interval=5):
        self.bot = bot
        self.duration_seconds = duration_seconds
        self.poll_interval = max(1, poll_interval)
        self.stop = threading.Event()
        self.entries_paused = threading.Event()
        if getattr(bot, "quote_broker", None) is not None:
            bot.quote_broker.entry_allowed = lambda: not (self.stop.is_set() or self.entries_paused.is_set() or self.stop_path.exists())
        self.latency = Telemetry()
        self.guard = threading.Lock()
        self.db_path = Path(bot.cfg["DB_PATH"]).resolve()
        self.status_path = self.db_path.with_suffix(".status.json")
        self.stop_path = self.db_path.with_suffix(".stop")
        self.log_path = self.db_path.with_suffix(".log")
        self.counts = Counter()
        self.reasons = Counter()
        self.watchlist = []
        self.watch_index = 0
        self.evaluated_mints = set()
        self.cycle_evaluated = 0
        self.cycle_seconds = 0
        self.discovery_stats = {}
        self.last_scan = float("-inf")
        self.events = deque(maxlen=8)
        self.quotes = {}
        self.snapshot = {"mode": "PAPER_REAL_MARKET", "status": "STARTING", "pid": os.getpid(),
                         "started_at": utc_now(), "updated_at": utc_now(), "stage": "Initierar",
                         "database": str(self.db_path), "log": str(self.log_path),
                         "trade_budget_usd": bot.cfg["TRADE_BUDGET_USD"],
                         "exit_rules": {key: str(bot.cfg[key]) for key in ("INITIAL_STOP_LOSS_PCT", "TAKE_PROFIT_1_PCT", "TAKE_PROFIT_2_PCT", "MOONBAG_TRAILING_PCT")},
                         "profitability_validated": False, "actual_money_used": False}
        self.snapshot["execution_model"] = bot.cfg.get("EXECUTION_MODEL", "pool-model")

    def stage(self, text):
        with self.guard:
            self.snapshot["stage"] = text

    def event(self, text):
        clean = "".join(c for c in str(text) if c.isprintable())[:240]
        self.events.append({"time": utc_now(), "text": clean})
        logging.getLogger("godlike2026").info(clean)

    def snapshot_copy(self):
        with self.guard:
            return copy.deepcopy(self.snapshot)

    def publish(self, status="RUNNING"):
        self.stage("Beräknar nettovärde efter modellerade säljkostnader")
        report = self.bot.state.performance_report(self.bot.dex)
        positions = self.bot.state.get_open_positions(True)
        with self.guard:
            self.snapshot.update(status=status, updated_at=utc_now(), report=report, report_as_of=report.get("valuation_as_of") or utc_now(),
                positions=positions, decisions=dict(self.counts),
                rejection_reasons=dict(self.reasons.most_common(6)), candidates=len(self.watchlist),
                unique_evaluated=len(self.evaluated_mints), cycle_evaluated=self.cycle_evaluated,
                cycle_seconds=round(self.cycle_seconds, 2), discovery=self.discovery_stats,
                network_metrics=NETWORK_METRICS.snapshot(), pipeline_metrics=self.latency.snapshot(),
                entries_paused=self.entries_paused.is_set(),
                events=list(self.events), quotes=list(self.quotes.values())[-8:],
                market_error=getattr(self.bot.dex, "last_error", ""),
                rugcheck_error=getattr(self.bot.rug, "last_error", ""))

    def _save_run_settings(self):
        # Preserve comparability when resuming a database; do not silently switch costs.
        keys = [k for k in self.bot.cfg if k.endswith("_PCT") or k.endswith("_USD")]
        keys += ["MAX_OPEN_POSITIONS", "MAX_CONSECUTIVE_LOSSES", "SIGNAL_MIN_SECONDS",
                 "COOLDOWN_HOURS", "CIRCUIT_BREAKER_HOURS", "MAX_MARKET_DATA_AGE_SECONDS"]
        keys += ["EXECUTION_MODEL", "PAPER_NETWORK_FEE_LAMPORTS", "PAPER_EXECUTION_DELAY_MS", "PAPER_ADVERSE_BPS", "QUOTE_MAX_AGE_MS"]
        settings = {k: str(self.bot.cfg[k]) for k in sorted(set(keys)) if k in self.bot.cfg}
        settings["INITIAL_CASH_USD"] = str(self.bot.state.get_wallet()["initial_cash_usd"])
        settings_path = self.db_path.with_suffix(".settings.json")
        if settings_path.exists():
            previous = json.loads(settings_path.read_text(encoding="utf-8"))
            if previous.get("settings") != settings:
                raise ValueError("Inställningar skiljer sig från sparad körning. Välj en ny --db för ett nytt experiment.")
        payload = {"settings": settings, "sha256": hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()}
        atomic_json(settings_path, payload)
        source_root = Path(__file__).resolve().parent
        code_hashes = {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
                       for name in ("godlike2026.py", "paper_runtime.py", "market_discovery.py", "rug_schema.py", "public_http.py", "console_dashboard.py", "telemetry.py",
                                    "quote_provider.py", "order_lifecycle.py", "execution_engine.py", "execution_ledger.py", "quote_broker.py")}
        # Retain every restart and code version alongside the unchanged trade ledger.
        with self.db_path.with_suffix(".runs.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(json.dumps({"started_at": self.snapshot["started_at"], "pid": os.getpid(),
                                      "settings_sha256": payload["sha256"], "code_sha256": code_hashes,
                                      "cash_at_start": str(self.bot.state.get_wallet()["cash_usd"]),
                                      "actual_money_used": False}, ensure_ascii=False) + "\n")

    def cycle(self):
        if self.stop.is_set():
            return
        cycle_start = time.monotonic()
        self.cycle_evaluated = 0
        self.stage("Kontrollerar stoppar och moonbag")
        for sold in self.bot.monitor.check_and_exit_positions(True):
            self.event(f"SÅLT {sold['symbol']} / {sold['reason']} / netto ${sold['net_usd']:.2f}")
        if time.monotonic() - self.last_scan >= 60 or not self.watchlist:
            self.stage("Hämtar kandidater från flera flöden och nya Solana-pooler")
            discovered = self.bot.dex.scan_trending_mints(
                should_stop=lambda: self.stop.is_set() or self.stop_path.exists())
            self.discovery_stats = getattr(self.bot.dex, "discovery_stats", {})
            self.last_scan = time.monotonic()
            if discovered:
                # Retain recent candidates long enough for multiple observations.
                self.watchlist = list(dict.fromkeys(discovered + self.watchlist))[:200]
            elif not self.watchlist:
                self.event("Inga kandidater hämtades. " + getattr(self.bot.dex, "last_error", ""))
        wallet = self.bot.state.get_wallet()
        if not self.entries_paused.is_set() and wallet["cash_usd"] >= self.bot.cfg["TRADE_BUDGET_USD"]:
            batch_size = max(1, min(60, int(self.bot.cfg.get("SCAN_BATCH_SIZE", 60))))
            batch_count = min(batch_size, len(self.watchlist))
            batch = [self.watchlist[(self.watch_index + i) % len(self.watchlist)] for i in range(batch_count)]
            for index, mint in enumerate(batch):
                if self.stop.is_set() or self.entries_paused.is_set() or self.stop_path.exists() or len(self.bot.state.get_open_positions(True)) >= self.bot.cfg["MAX_OPEN_POSITIONS"]:
                    break
                if self.bot.state.get_wallet()["cash_usd"] < self.bot.cfg["TRADE_BUDGET_USD"]:
                    break
                # One documented request supplies up to 30 current token quotes.
                # Evaluate this chunk immediately; expired quotes are refreshed by the client.
                if index % 30 == 0:
                    prefetch = getattr(self.bot.dex, "prefetch_tokens", None)
                    if callable(prefetch):
                        self.stage(f"Hämtar priser i grupp: {index + 1}–{min(index + 30, len(batch))} av {len(batch)}")
                        prefetch(batch[index:index + 30])
                        if self.stop.is_set() or self.entries_paused.is_set() or self.stop_path.exists():
                            break
                self.watch_index += 1
                self.evaluated_mints.add(mint)
                self.cycle_evaluated += 1
                self.stage("Utvärderar " + mint[:12] + "...")
                evaluation_started = time.perf_counter()
                try:
                    result = self.bot.evaluate_and_buy(mint, is_paper=True)
                    self.latency.record("candidate_evaluation", (time.perf_counter()-evaluation_started)*1000)
                    status = result.get("status", "ERROR")
                    reason = result.get("reason", "")
                    self.counts[status] += 1
                    if status != "BOUGHT":
                        self.reasons[reason] += 1
                    cached = getattr(self.bot.dex, "_cache", {}).get(mint)
                    quote = cached[1] if cached else {}
                    symbol = quote.get("symbol") or mint[:10]
                    if quote:
                        self.quotes.pop(mint, None)
                        self.quotes[mint] = {"mint": mint, "symbol": symbol, "price_usd": quote.get("price_usd"),
                                             "liquidity_usd": quote.get("liquidity_usd"), "observed_at": quote.get("observed_at"),
                                             "decision": status, "reason": reason}
                        if len(self.quotes) > 30:
                            self.quotes.pop(next(iter(self.quotes)))
                    self.event(f"{status} {symbol}: {reason}")
                except Exception as exc:
                    self.latency.record("candidate_evaluation", (time.perf_counter()-evaluation_started)*1000, False, str(exc))
                    self.counts["ERROR"] += 1
                    self.event(f"Kandidatfel {mint[:12]}: {type(exc).__name__}: {exc}")
                # A scanner batch must not delay exit checks for an entire token list.
                self.bot.monitor.check_and_exit_positions(True)
        self.cycle_seconds = time.monotonic() - cycle_start
        self.latency.record("scan_cycle", self.cycle_seconds*1000)
        self.publish()

    def run_worker(self):
        getcontext().prec = 50  # Decimal context is thread-local.
        started = time.monotonic()
        try:
            self._save_run_settings()
            self.publish("RUNNING")
            while not self.stop.is_set():
                if self.stop_path.exists() or (self.duration_seconds and time.monotonic()-started >= self.duration_seconds):
                    self.stop.set()
                    break
                try:
                    self.cycle()
                except Exception as exc:
                    self.event(f"Cykelfel, nya försök pausas: {type(exc).__name__}: {exc}")
                    self.counts["CYCLE_ERROR"] += 1
                    with self.guard:
                        self.snapshot.update(status="DEGRADED", updated_at=utc_now(), events=list(self.events))
                self.stage("Väntar på nästa observation")
                self.stop.wait(self.poll_interval)
            self.publish("STOPPED")
            self.stage("Stoppad; innehav och historik är sparade. Inga tvångsförsäljningar.")
        except Exception as exc:
            self.event(f"SESSION STOPPAD: {type(exc).__name__}: {exc}")
            with self.guard:
                self.snapshot.update(status="ERROR", error=str(exc), events=list(self.events), updated_at=utc_now())
        finally:
            self.stop.set()


def _money(value, signed=False):
    if value is None:
        return "OKÄNT"
    number = Decimal(str(value))
    return f"${number:+,.2f}" if signed else f"${number:,.2f}"


def render_dashboard(snapshot):
    report = snapshot.get("report", {})
    lines = ["=" * 100, "GODLIKE | PAPER MOT AKTUELL MARKNAD | INGA RIKTIGA ORDER",
        f"Status: {snapshot['status']} | Uppdaterad (UTC): {snapshot['updated_at']} | PID: {snapshot['pid']}",
        f"Arbete: {snapshot['stage']}",
        f"Senaste portföljvärdering (UTC): {snapshot.get('report_as_of', 'saknas')}",
        f"Start: {_money(report.get('initial_cash'))} | Kassa: {_money(report.get('cash'))} | Order: {_money(snapshot['trade_budget_usd'])}",
        f"NETTORESULTAT: {_money(report.get('net_pnl'), True)} | Realiserat: {_money(report.get('realized_pnl'), True)} | Orealiserat: {_money(report.get('unrealized_pnl'), True)}",
        f"Portfölj efter säljkostnader: {_money(report.get('total_port'))} | Avgifter: {_money(report.get('fees'))}",
        f"Öppna positioner: {len(snapshot.get('positions', []))} | Avslutade: {report.get('total_trades', 0)} | Vinster: {report.get('winning_trades', 0)} | Förluster: {report.get('losing_trades', 0)}",
        f"Bevakar: {snapshot.get('candidates', 0)} tokens | Unika utvärderade sedan start: {snapshot.get('unique_evaluated', 0)}",
        f"Senaste cykel: {snapshot.get('cycle_evaluated', 0)} utvärderingar på {snapshot.get('cycle_seconds', 0)}s | Beslut: {snapshot.get('decisions', {})}"]
    discovery = snapshot.get("discovery", {})
    if discovery:
        source_counts = {item["source"]: item.get("count", 0) for item in discovery.get("sources", [])}
        lines.append("Kandidatflöden (överlapp kan finnas): " + str(source_counts))
        if discovery.get("errors"):
            lines.append("DELVIS KANDIDATDATA: " + str(discovery["errors"])[:240])
    if not report.get("total_trades") and not snapshot.get("positions"):
        lines.append("Ingen genomförd affär ännu. $0 betyder ingen observerad handelsvinst, inte bevisad lönsamhet.")
    valuation_time = snapshot.get("report_as_of")
    if snapshot["status"] == "DEGRADED" or (valuation_time and (datetime.now(timezone.utc) - datetime.fromisoformat(valuation_time)).total_seconds() > 30):
        lines.append("OBS: ovanstående PnL är senaste sparade värdering; ny värdering inväntas.")
    if snapshot.get("market_error"):
        lines.append("MARKNADSFEL: " + snapshot["market_error"][:200])
    if snapshot.get("rugcheck_error"):
        lines.append("RISKDATA: " + snapshot["rugcheck_error"][:200])
    if snapshot.get("rejection_reasons"):
        lines.append("Vanligaste orsaker sedan start: " + "; ".join(
            f"{reason}: {count}" for reason, count in list(snapshot["rejection_reasons"].items())[:3]))
    lines.append("Visar de 6 senaste observerade priserna / besluten (inte hela bevakningslistan):")
    for quote in snapshot.get("quotes", [])[-6:]:
        age = max(0, time.time() - float(quote.get("observed_at") or 0))
        price = quote.get("price_usd")
        price = f"${Decimal(str(price)):.8g}" if price is not None else "?"
        symbol = "".join(c for c in str(quote["symbol"]) if c.isprintable())[:14]
        lines.append(f"  {symbol:<14} {price:<16} likviditet {_money(quote.get('liquidity_usd')):<16} {age:5.0f}s | {quote['decision']}")
    for position in snapshot.get("positions", []):
        symbol = "".join(c for c in str(position["symbol"]) if c.isprintable())[:14]
        lines.append(f"  INNEHAV {symbol}: raw {position['remaining_raw']}, TP1 {position['tp1_executed']}, TP2 {position['tp2_executed']}, stop {position['stop_loss_price']}")
    lines.append("Senaste händelser:")
    lines += ["  " + event["time"][11:19] + " " + event["text"] for event in snapshot.get("events", [])[-4:]]
    lines.append("Ctrl+C stoppar. Data sparas automatiskt. Resultatet är simulerat, inte utbetald vinst.")
    return "\n".join(lines)


def run_paper_session(bot, duration_seconds=0, poll_interval=5, dashboard_interval=1):
    session = PaperSession(bot, duration_seconds, poll_interval)
    with SessionLock(session.db_path):
        if session.stop_path.exists():
            session.stop_path.unlink()
        logger = logging.getLogger("godlike2026")
        handler = logging.FileHandler(session.log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        previous_propagate = logger.propagate
        previous_level = logger.level
        logger.propagate = False
        logger.setLevel(logging.INFO)
        worker = threading.Thread(target=session.run_worker, name="godlike-paper", daemon=False)
        display = ConsoleDashboard()
        worker.start()
        try:
            next_draw = 0
            while worker.is_alive():
                key = display.read_key()
                if key in ("q", "\x03"):
                    session.stop.set()
                elif key == "p":
                    if session.entries_paused.is_set():
                        session.entries_paused.clear()
                    else:
                        session.entries_paused.set()
                    session.event("Nya köp pausade; exitkontroller fortsätter" if session.entries_paused.is_set() else "Nya köp återupptagna")
                if time.monotonic() >= next_draw:
                    snapshot = session.snapshot_copy()
                    snapshot["entries_paused"] = session.entries_paused.is_set()
                    atomic_json(session.status_path, snapshot)
                    display.draw(snapshot)
                    next_draw = time.monotonic() + dashboard_interval
                worker.join(timeout=0.2)
        except KeyboardInterrupt:
            session.stop.set()
            while worker.is_alive():
                worker.join(timeout=0.25)
        finally:
            session.stop.set()
            worker.join()
            snapshot = session.snapshot_copy()
            atomic_json(session.status_path, snapshot)
            display.close()
            print(render_dashboard(snapshot), flush=True)
            print(f"\nStatus: {session.status_path}\nLogg: {session.log_path}", flush=True)
            logger.removeHandler(handler)
            handler.close()
            logger.propagate = previous_propagate
            logger.setLevel(previous_level)
        return snapshot
