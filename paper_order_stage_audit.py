"""Read-only order-stage report for real-quote PAPER attempts in GODLIKE SQLite.

Distinguishes:
  - BOUGHT strategy positions (simulated only),
  - actual quote-only validation,
  - failed initial/delayed/re-risk orders,
  - closed positions and evidence-backed exits.

A SAVED QUOTED order is NOT an executed buy. No live adapter or wallet used.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

REQUIRED_TABLES={"execution_orders","execution_order_events",
                 "positions","trade_history"}

def audit(db_path):
    path=Path(db_path).resolve()
    report={
        "mode":"READ_ONLY_QUOTE_PAPER_ORDER_AUDIT",
        "database":str(path),
        "created_at_utc":datetime.now(timezone.utc).isoformat(),
        "status":"NO_RECORDS",
        "live_approved":False,
        "profitability_validated":False,
        "actual_money_used":False,
        "is_not_live_fill_evidence":True,
        "paper_order_attempts":0,
        "paper_order_states":{},
        "paper_order_sides":{},
        "paper_orders_with_initial_quote":0,
        "paper_failed_orders_with_initial_quote":0,
        "failed_order_reasons":{},
        "paper_strategy_open_positions":0,
        "paper_strategy_closed_positions":0,
        "paper_strategy_buy_ledger_records":0,
        "paper_strategy_sell_ledger_records":0,
        "integrity":"NOT_CHECKED",
        "errors":[],
        "limitations":[
            "All paper CONFIRMED states are modeled, not on-chain confirmations.",
            "A QUOTED or FAILED order is not a buy, and no return can be inferred from quote counts.",
            "Token-2022 buy/sell preflight quotes can pass independently but still fail strategy or real execution-risk checks.",
        ],
    }
    if not path.is_file():
        report["errors"].append("No paper SQLite file")
        return report
    try:
        with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=8) as c:
            c.row_factory=sqlite3.Row
            integrity=c.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0]!="ok":
                report["integrity"]="FAILED"
                report["errors"].append("SQLite quick_check failed")
                return report
            report["integrity"]="OK"
            tables={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not REQUIRED_TABLES.issubset(tables):
                report["errors"].append("Missing canonical execution/strategy table")
                return report
            counts=Counter()
            sides=Counter()
            failed=Counter()
            for row in c.execute("SELECT id,mode,state,intent_json,quote_json FROM execution_orders"):
                if row["mode"]!="PAPER":
                    report["errors"].append("Non-PAPER order appeared in paper-only database")
                    continue
                try:
                    intent=json.loads(row["intent_json"])
                    if not isinstance(intent,dict) or intent.get("mode")!="PAPER":
                        raise ValueError("Paper intent body mismatched")
                    side=intent["side"]
                    if side not in {"BUY","SELL"}:
                        raise ValueError("Unknown order side")
                except (TypeError,ValueError,KeyError):
                    report["errors"].append("Malformed paper order intent")
                    continue
                counts[row["state"]]+=1
                sides[side]+=1
                report["paper_order_attempts"]+=1
                try:
                    quote=json.loads(row["quote_json"]) if row["quote_json"] else None
                except (TypeError,ValueError):
                    quote=None
                    report["errors"].append("Invalid recorded initial quote JSON")
                has_quote=isinstance(quote,dict) and isinstance(quote.get("quote"),dict)
                if has_quote:report["paper_orders_with_initial_quote"]+=1
                if row["state"]=="FAILED":
                    if has_quote:report["paper_failed_orders_with_initial_quote"]+=1
                    last=c.execute("SELECT evidence_json FROM execution_order_events WHERE order_id=? "
                                   "ORDER BY version DESC,id DESC LIMIT 1",(row["id"],)).fetchone()
                    if last is None:
                        failed["NO_FINAL_EVENT"]+=1
                    else:
                        try:
                            evidence=json.loads(last["evidence_json"])
                            reason=evidence.get("reason","UNKNOWN")
                            if not isinstance(reason,str):
                                reason="NON_TEXT_REASON"
                            failed[reason[:180]]+=1
                        except (TypeError,ValueError,KeyError):
                            failed["INVALID_LAST_EVENT"]+=1
            report["paper_order_states"]=dict(sorted(counts.items()))
            report["paper_order_sides"]=dict(sorted(sides.items()))
            report["failed_order_reasons"]=dict(sorted(failed.items()))
            report["paper_strategy_open_positions"]=c.execute(
                "SELECT COUNT(*) FROM positions WHERE is_paper=1 AND status='OPEN'").fetchone()[0]
            report["paper_strategy_closed_positions"]=c.execute(
                "SELECT COUNT(*) FROM positions WHERE is_paper=1 AND status='CLOSED'").fetchone()[0]
            report["paper_strategy_buy_ledger_records"]=c.execute(
                "SELECT COUNT(*) FROM trade_history WHERE is_paper=1 AND action='BUY'").fetchone()[0]
            report["paper_strategy_sell_ledger_records"]=c.execute(
                "SELECT COUNT(*) FROM trade_history WHERE is_paper=1 AND action IN ('SELL','STOP_LOSS','TP1','TP2','MOONBAG_TRAILING_STOP')").fetchone()[0]
            report["status"]=("INVALID_EVIDENCE" if report["errors"]
                              else "PAPER_ORDER_ATTEMPTS" if report["paper_order_attempts"]
                              else "NO_PAPER_ORDER_ATTEMPTS")
    except (sqlite3.DatabaseError,ValueError,OSError) as exc:
        report["status"]="INVALID_EVIDENCE"
        report["errors"].append(f"{type(exc).__name__}: {str(exc)[:200]}")
    return report


def main():
    p=argparse.ArgumentParser(description="Read-only real-quote PAPER order stage audit")
    p.add_argument("--db",default="paper_forward.db")
    p.add_argument("--output",default="paper_order_stage_audit.json")
    args=p.parse_args()
    result=audit(args.db)
    value=json.dumps(result,sort_keys=True,indent=2,ensure_ascii=False)
    print(value)
    path=Path(args.output)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(value+"\n",encoding="utf-8")
    raise SystemExit(2 if result["errors"] else 0)


if __name__=="__main__":
    main()
