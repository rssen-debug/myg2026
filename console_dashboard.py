"""One in-place console screen. No terminal escapes from external token names."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import os
import shutil
import sys
import time
import unicodedata

ESC = "\x1b["


def clean(value):
    return "".join(c for c in str(value) if c.isprintable() and unicodedata.category(c) not in {"Cf", "Cs"})


def fit(value, width):
    result, used = [], 0
    for char in clean(value):
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result) + " " * max(0, width-used)


def money(value, signed=False):
    if value is None:
        return "OKÄNT"
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "OKÄNT"
        return f"${amount:+,.2f}" if signed else f"${amount:,.2f}"
    except Exception:
        return "OKÄNT"


def percentage(value):
    return "--" if value is None else f"{Decimal(str(value)):.1f}%"


def frame(snapshot, width=118, height=38, color=True):
    """Render a bounded frame; at least one bottom row remains for the cursor."""
    width, height = max(40, min(132, width)), max(12, height)
    report = snapshot.get("report", {})
    rows = []
    def row(text="", style=""):
        body = fit(text, width-4)
        if color and style:
            body = ESC + style + "m" + body + ESC + "0m"
        rows.append("| " + body + " |")
    rule = "+" + "-"*(width-2) + "+"
    rows.append(rule)
    model = "JUPITER-OFFERTER" if snapshot.get("execution_model") == "quote" else "POOLMODELL"
    row(f"GODLIKE  /  SOLANA TRADING DESK       PAPER {model}  |  LIVE LÅST", "1;36")
    paused = snapshot.get("entries_paused", False)
    row(f"{snapshot.get('status', 'STARTING')}  |  Köp {'PAUSADE' if paused else 'AKTIVA'}  |  {snapshot.get('updated_at', '--')}  |  PID {snapshot.get('pid', '--')}")
    row(snapshot.get("stage", "Startar..."), "2")
    rows.append(rule)
    pnl = report.get("net_pnl")
    pnl_style = "1;33" if pnl is None else "1;32" if Decimal(str(pnl)) > 0 else "1;31" if Decimal(str(pnl)) < 0 else "1;37"
    row(f"NETTO {money(pnl, True)}    REALISERAT {money(report.get('realized_pnl'), True)}    OREALISERAT {money(report.get('unrealized_pnl'), True)}", pnl_style)
    row(f"Start {money(report.get('initial_cash'))}   Kassa {money(report.get('cash'))}   Likvidationsmodell {money(report.get('total_port'))}   Order {money(snapshot.get('trade_budget_usd'))}")
    row(f"Avgifter {money(report.get('fees'))}   Avslutade {report.get('total_trades', 0)}   Win {percentage(report.get('win_rate'))}   PF {report.get('profit_factor') or '--'}   Max DD {percentage(report.get('max_drawdown_pct'))}")
    as_of = snapshot.get("report_as_of")
    stale = bool(as_of and (datetime.now(timezone.utc)-datetime.fromisoformat(as_of)).total_seconds() > 30)
    row(("VÄRDERING INAKTUELL. Senast: " if stale else "Värderad: ") + str(as_of or "saknas") + " | Lönsamhet EJ verifierad", "33" if stale else "2")
    exits = snapshot.get("exit_rules", {})
    row(f"SL -{exits.get('INITIAL_STOP_LOSS_PCT', '--')}% | TP1 +{exits.get('TAKE_PROFIT_1_PCT', '--')}%: sälj 40% | TP2 +{exits.get('TAKE_PROFIT_2_PCT', '--')}%: sälj 35% | Trailing {exits.get('MOONBAG_TRAILING_PCT', '--')}%")
    rows.append(rule)
    row(f"DISCOVERY  {snapshot.get('candidates', 0)} bevakade  /  {snapshot.get('unique_evaluated', 0)} unika utvärderade  /  cykel {snapshot.get('cycle_evaluated', 0)} på {snapshot.get('cycle_seconds', 0)}s", "1;36")
    sources = snapshot.get("discovery", {}).get("sources", [])
    for start in range(0, len(sources), 3):
        row("Flöden: " + "  ".join(f"{s.get('source', '').replace('dex_', '').replace('gecko_', '')} {s.get('count', 0)} [{s.get('status', '?')}]" for s in sources[start:start+3]))
    metrics = snapshot.get("network_metrics", {})
    for host, label in (("api.dexscreener.com", "Dex"), ("api.rugcheck.xyz", "Rug"), ("api.geckoterminal.com", "Gecko"), ("api.jup.ag", "Jupiter")):
        item = metrics.get(host)
        if item:
            age = max(0, time.time() - item["last_attempt_at"])
            row(f"{label:<6} {'OK' if item['last_ok'] else 'FEL':<3} {age:.0f}s sedan | HTTP p50 {item['p50_ms']}ms / p95 {item['p95_ms']}ms / p99 {item['p99_ms']}ms | fel {item['failures']}/{item['requests']}", "2" if item["last_ok"] else "33")
    row("PRISER  senaste 5; hela bevakningslistan analyseras separat", "1")
    for quote in snapshot.get("quotes", [])[-5:]:
        observed = quote.get("observed_at")
        age = f"{max(0, time.time()-float(observed)):.0f}s" if observed else "--"
        row(f"{fit(quote.get('symbol', '?'), 14)} {str(quote.get('price_usd', '?')):>13} USD  LQ {money(quote.get('liquidity_usd')):>12}  {age:>5}  {quote.get('decision', '?')}")
    positions = snapshot.get("positions", [])
    row(f"INNEHAV  {len(positions)} öppna", "1;36")
    for pos in positions[:3]:
        row(f"{pos['symbol']}  raw {pos['remaining_raw']}  SL {pos['stop_loss_price']}  TP1 {pos['tp1_executed']}  TP2 {pos['tp2_executed']}")
    if not positions:
        row("Inga öppna positioner. Inga affärer framtvingas.", "2")
    for reason, count in list(snapshot.get("rejection_reasons", {}).items())[:2]:
        row(f"AVSLAG/VÄNTAN {count:>5}  {reason}", "2")
    for event in snapshot.get("events", [])[-2:]:
        row(event["time"][11:19] + "  " + event["text"])
    # Keep the footer visible even on small consoles. Full history stays in log.
    rows = rows[:height-4]
    row("[P] Pausa/fortsätt nya köp  [Q] eller Ctrl+C Stoppa  |  Historik sparas", "1;36")
    row("Simulerat resultat. Uppströms ticklag och verklig exekvering är ännu ej kalibrerade.", "2")
    rows.append(rule)
    return "\n".join(rows)


def _enable_virtual_terminal(stream):
    if not stream.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes, kernel.GetStdHandle.restype = [wintypes.DWORD], wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle, mode = kernel.GetStdHandle(-11), wintypes.DWORD()
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class ConsoleDashboard:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.interactive = _enable_virtual_terminal(self.stream)
        self.started = False
        self.last_plain_status = None

    def draw(self, snapshot):
        if not self.interactive:
            # Redirected output is a concise status stream, never repeated full screens.
            signature = (snapshot.get("status"), snapshot.get("entries_paused"),
                         str(snapshot.get("report", {}).get("net_pnl")), len(snapshot.get("positions", [])))
            if signature != self.last_plain_status:
                self.stream.write(f"{snapshot.get('updated_at')} PAPER {signature[0]} PnL {money(snapshot.get('report', {}).get('net_pnl'), True)} | {snapshot.get('candidates', 0)} tokens\n")
                self.stream.flush()
                self.last_plain_status = signature
            return
        columns, lines = shutil.get_terminal_size((120, 40))
        if not self.started:
            self.stream.write(ESC + "?1049h" + ESC + "?25l" + ESC + "2J")
            self.started = True
        self.stream.write(ESC + "H" + frame(snapshot, min(118, columns-1), lines-1) + ESC + "J")
        self.stream.flush()

    def read_key(self):
        if self.interactive and os.name == "nt":
            import msvcrt
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    return None
                return key.lower()
        return None

    def close(self):
        if self.started:
            self.stream.write(ESC + "?25h" + ESC + "?1049l")
            self.stream.flush()
            self.started = False
