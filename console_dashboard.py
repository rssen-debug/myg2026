"""One in-place console screen. No terminal escapes from external token names."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
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


def fit_ellipsis(value, width):
    """Show a truncation marker without exceeding terminal cell width."""
    if width <= 0:
        return ""
    full = clean(value)
    rendered = fit(full, width)
    # Avoid silently cropping important long stage/status descriptions.
    if rendered.rstrip() == full:
        return rendered
    if width == 1:
        return "…"
    return fit(full, width - 1).rstrip() + "…" + " " * max(
        0, width - 1 - sum(0 if unicodedata.combining(c) else
                          2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
                          for c in fit(full, width - 1).rstrip())
    )


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


def missing_metrics(report):
    """Distinguish unavailable metrics from malformed numeric metrics."""
    return [key for key in ("net_pnl", "realized_pnl", "unrealized_pnl", "win_rate",
                            "max_drawdown_pct", "cash", "total_port", "fees",
                            "profit_factor", "initial_cash") if report.get(key) is None]


def metric_health(report):
    """Explicitly surface invalid numeric inputs instead of silently hiding them."""
    invalid = []
    for key in ("net_pnl", "realized_pnl", "unrealized_pnl", "win_rate", "max_drawdown_pct",
                "cash", "total_port", "fees", "profit_factor", "initial_cash"):
        value = report.get(key)
        if value is None:
            continue
        try:
            if not Decimal(str(value)).is_finite():
                invalid.append(key)
        except (ValueError, ArithmeticError):
            invalid.append(key)
    return invalid


def ratio(value):
    try:
        number = Decimal(str(value)) if value is not None else None
        return f"{number:.2f}" if number is not None and number.is_finite() else "--"
    except (ValueError, ArithmeticError):
        return "--"


def percentage(value):
    try:
        number = Decimal(str(value)) if value is not None else None
        return f"{number:.1f}%" if number is not None and number.is_finite() else "--"
    except (ValueError, ArithmeticError):
        return "--"


def valuation_freshness(as_of, now=None):
    """Treat missing, invalid, naive, and future timestamps as unverified."""
    if not isinstance(as_of, str) or not as_of.strip():
        return "MISSING"
    try:
        timestamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return "INVALID"
        age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
        if age < -5:
            return "FUTURE"
        return "STALE" if age > 30 else "FRESH"
    except (ValueError, TypeError, OverflowError):
        return "INVALID"


def latest_restore_event(path=Path("forward_pulse_runs.jsonl"), run_id=None):
    """Read only the tail of the audit log; never imply a restore succeeded."""
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - 8192)
            stream.seek(start)
            raw = stream.read()
            if start:
                boundary = raw.find(b"\n")
                raw = raw[boundary + 1:] if boundary >= 0 else b""
            tail = raw.decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(item, dict) or item.get("status") not in {"RESTORED", "FIRST_RUN"}:
                continue
            if run_id is not None and str(item.get("run_id")) != str(run_id):
                continue
            if item["status"] == "RESTORED" and (type(item.get("artifact_id")) is not int or item["artifact_id"] <= 0):
                continue
            return item
    except (OSError, ValueError):
        pass
    return None


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
    row(f"● PAPER ONLY  /  LIVE LOCKED  |  {snapshot.get('status', 'STARTING')}  |  Köp {'PAUSADE' if paused else 'AKTIVA'}  |  {snapshot.get('updated_at', '--')}  |  PID {snapshot.get('pid', '--')}")
    row(fit_ellipsis(snapshot.get("stage", "Startar..."), width-4), "2")
    # Pin safety-critical telemetry above the height crop, inspired by
    # Rich's visible-region approach; no extra dependency or refresh thread.
    summary_invalid = metric_health(report)
    summary_missing = missing_metrics(report)
    summary_freshness = valuation_freshness(snapshot.get("report_as_of"))
    row(f"◆ STATUS  DATA {summary_freshness}  |  FEL {len(summary_invalid)}  |  SAKNAS {len(summary_missing)}  |  PAPER ONLY",
        "1;31" if summary_invalid or summary_freshness != "FRESH" else "1;33" if summary_missing else "1;36")
    rows.append(rule)
    pnl = report.get("net_pnl")
    try:
        pnl_number = Decimal(str(pnl)) if pnl is not None else None
        if pnl_number is not None and not pnl_number.is_finite():
            pnl_number = None
    except (ValueError, ArithmeticError):
        pnl_number = None
    pnl_style = "1;33" if pnl_number is None else "1;32" if pnl_number > 0 else "1;31" if pnl_number < 0 else "1;37"
    row(f"NETTO {money(pnl, True)}    REALISERAT {money(report.get('realized_pnl'), True)}    OREALISERAT {money(report.get('unrealized_pnl'), True)}", pnl_style)
    if not report.get("total_trades") and not snapshot.get("positions"):
        row("INGA TRADES: modellfriktion/likvidationsvarde av start-USDC/SOL ar inte handels-PnL.", "33")
    row(f"Start {money(report.get('initial_cash'))}   Kassa {money(report.get('cash'))}   Likvidationsmodell {money(report.get('total_port'))}   Order {money(snapshot.get('trade_budget_usd'))}")
    row(f"Avgifter {money(report.get('fees'))}   Avslutade {report.get('total_trades', 0)}   Win {percentage(report.get('win_rate'))}   PF {ratio(report.get('profit_factor'))}   Max DD {percentage(report.get('max_drawdown_pct'))}")
    row("◇ METRIC LEGEND  -- / OKÄNT = SAKNAT ELLER OGILTIGT VÄRDE · INTE NOLL | PAPER ONLY", "2")
    invalid_metrics = summary_invalid
    missing = summary_missing
    if invalid_metrics:
        row("◆ DATA QUALITY  INVALID METRICS: " + ", ".join(invalid_metrics[:5]) +
            (f" (+{len(invalid_metrics)-5} MORE)" if len(invalid_metrics) > 5 else "") + " | PAPER ONLY", "1;31")
    if missing:
        row(f"◇ DATA QUALITY  {len(missing)} MISSING: " + ", ".join(missing[:3]) +
            (f" (+{len(missing)-3} MORE)" if len(missing) > 3 else "") + " | PAPER ONLY", "1;33")
    if not invalid_metrics and not missing:
        row("◆ DATA QUALITY  ALL TRACKED METRICS FINITE | PAPER ONLY", "1;32")
    row("◆ EVIDENSKONTROLL  PAPER MODELL / INTE LIVE-PnL  |  Färska köp- och säljofferter krävs", "1;33")
    row("◆ KAPITALSKYDD  Inga verkliga order / inga privata nycklar / LIVE LÅST", "1;36")
    row("◆ FORWARD-DATA  Historisk replay och kodtester bevisar INTE framtida vinst", "33")
    row("◇ HISTORIKSKYDD  [AUDIT] ZIP [SHA256/FAIL-CLOSED] · DB [READ-ONLY-CHECK] · PAGES [AUTO] · IDS [UNIQUE]", "3")
    row("  API [EXACT-URL] · AUDIT [SKIPS-LOGGED] · DB [512MiB]  |  PAPER ONLY", "3")
    current_run_id = os.getenv("GITHUB_RUN_ID")
    restore = latest_restore_event(run_id=current_run_id) if current_run_id else None
    if restore and restore["status"] == "RESTORED":
        row(f"◆ RESTORE VERIFIED  {restore['status']} | artifact {restore.get('artifact_id') or '--'} | {restore.get('utc', '--')} | EJ KONTINUERLIG HISTORIK", "1;36")
    elif restore and restore["status"] == "FIRST_RUN":
        row("◇ RESTORE FIRST RUN  NY HISTORIK · INGEN TIDIGARE SQLITE · EJ KONTINUERLIG DATA | PAPER ONLY", "1;33")
    elif not current_run_id:
        row("◇ RESTORE LOCAL  ○ INGEN AKTUELL RUN-ID · HISTORIK EJ VERIFIERAD | PAPER ONLY", "1;33")
    else:
        row("◇ RESTORE UNVERIFIED  ○ SAKNAR BEVIS FÖR AKTUELL RUN-ID | PAPER ONLY", "1;33")
    as_of = snapshot.get("report_as_of")
    freshness = summary_freshness
    if freshness == "FRESH":
        row("◆ VÄRDERING FÄRSK  " + str(as_of) + " | Lönsamhet EJ verifierad", "1;36")
    else:
        row("◇ VÄRDERING " + freshness + "  " + str(as_of or "saknas") +
            " | DATA EJ VERIFIERAD · Lönsamhet EJ verifierad", "1;33")
    exits = snapshot.get("exit_rules", {})
    row(f"SL -{exits.get('INITIAL_STOP_LOSS_PCT', '--')}% | TP1 +{exits.get('TAKE_PROFIT_1_PCT', '--')}%: sälj 40% | TP2 +{exits.get('TAKE_PROFIT_2_PCT', '--')}%: sälj 35% | Trailing {exits.get('MOONBAG_TRAILING_PCT', '--')}%")
    rows.append(rule)
    row(f"DISCOVERY  {snapshot.get('candidates', 0)} bevakade  /  {snapshot.get('unique_evaluated', 0)} unika utvärderade  /  cykel {snapshot.get('cycle_evaluated', 0)} på {snapshot.get('cycle_seconds', 0)}s", "1;36")
    trench = snapshot.get("trench_labels", {})
    if trench:
        row("TRENCH ENDAST MARKNADSOBS (ingen verifierad whale/dev): " +
            "  ".join(f"{k}={v}" for k,v in trench.items()), "33")
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
        moonbag = " MOONBAG" if pos.get("tp2_executed") else ""
        row(f"{pos['symbol']}  raw {pos['remaining_raw']}  SL {pos['stop_loss_price']}  TP1 {pos['tp1_executed']}  TP2 {pos['tp2_executed']}{moonbag}")
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
            report = snapshot.get("report") or {}
            invalid = metric_health(report)
            missing = missing_metrics(report)
            freshness = valuation_freshness(snapshot.get("report_as_of"))
            # Detect changed invalid numeric inputs without logging their raw values.
            invalid_values = tuple((key, str(report.get(key))[:80]) for key in invalid)
            # Track key portfolio values even when their validity does not change.
            # Only bounded summaries are emitted, never raw untrusted values.
            portfolio = (money(report.get("cash")), money(report.get("total_port")))
            quality = "ALERT" if invalid or freshness != "FRESH" else "PARTIAL" if missing else "OK"
            quality_detail = ("INVALID DATA" if invalid else "VALUATION " + freshness) if quality == "ALERT" else ("MISSING FIELDS" if missing else "VERIFIED FIELDS")
            # Make safety-relevant changes visible without claiming the trading engine is paused.
            status_marker = "[!]" if quality == "ALERT" else "[?]" if quality == "PARTIAL" else "[+]"
            signature = (snapshot.get("status"), snapshot.get("entries_paused"),
                         str(report.get("net_pnl")), len(snapshot.get("positions", [])),
                         tuple(invalid), tuple(missing), freshness, invalid_values, portfolio,
                         snapshot.get("candidates", 0))
            if signature != self.last_plain_status:
                # Put actionable counts first and preserve the tail even at 220 columns.
                # Reserve room for positions and discovery instead of truncating them.
                prefix = (
                    f"{fit(snapshot.get('updated_at', '--'), 10).strip()} PAPER "
                    f"{status_marker} QUALITY {quality} ({quality_detail}) | "
                    f"DATA {freshness} | "
                    f"INVALID {len(invalid)} [{','.join(invalid[:3]) or '-'}] | "
                    f"MISSING {len(missing)} [{','.join(missing[:3]) or '-'}] | "
                    f"PnL {money(report.get('net_pnl'), True)} | "
                    f"CASH {portfolio[0]} | EQUITY {portfolio[1]} | "
                )
                suffix = (
                    f"ENTRIES {'PAUSED' if snapshot.get('entries_paused') else 'ACTIVE'} | "
                    f"POSITIONS {len(snapshot.get('positions') or [])} | "
                    f"{fit(snapshot.get('candidates', 0), 5).strip()} tok"
                )
                budget = max(0, 220 - len(clean(suffix)) - 1)
                self.stream.write(fit_ellipsis(prefix, budget).rstrip() + " " + clean(suffix) + "\n")
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
