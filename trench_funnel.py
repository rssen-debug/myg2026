"""Read-only trench signal audit of the *real* paper scanner decision ledger.

No fake developer clustering, whale confluence, holder attribution or trading
approval. Repeated polls and unique token counts are reported separately.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3

VERSION = "trench-evidence-v1"

def audit(db_path):
    source = Path(db_path).expanduser().resolve()
    result = {
        "mode": "PAPER_TRENCH_EVIDENCE_ONLY",
        "source": str(source),
        "status": "NO_EVIDENCE",
        "live_approved": False,
        "profitability_validated": False,
        "trench_security_verified": False,
        "total_decisions": 0,
        "decisions_with_trench_evidence": 0,
        "unique_mints_observed": 0,
        "unique_mints_with_evidence": 0,
        "observation_labels": {},
        "label_unique_mints": {},
        "flag_observations": {},
        "flag_unique_mints": {},
        "decision_statuses": {},
        "errors": [],
        "warnings": [
            "Aggregated DexScreener flow is not developer history, bundles, independent wallet clusters, or rug-safety proof.",
            "Repeated scanner polls are not independent trades, fills, or statistically independent market observations.",
        ],
    }
    if not source.is_file():
        result["warnings"].append("Paper SQLite database not found")
        return result
    labels=Counter()
    flags=Counter()
    statuses=Counter()
    label_mints=defaultdict(set)
    flag_mints=defaultdict(set)
    all_mints=set()
    covered_mints=set()
    try:
        with sqlite3.connect(source.as_uri()+"?mode=ro",uri=True,timeout=8) as con:
            tables={row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "market_observations" not in tables:
                result["warnings"].append("No market_observations table")
                return result
            cols={row[1] for row in con.execute("PRAGMA table_info(market_observations)")}
            if not {"mint","decision_json"}.issubset(cols):
                result["errors"].append("Invalid observation schema")
                result["status"]="INVALID_EVIDENCE"
                return result
            for mint, raw in con.execute("SELECT mint,decision_json FROM market_observations"):
                result["total_decisions"]+=1
                if isinstance(mint,str) and mint:
                    all_mints.add(mint)
                try:
                    decision=json.loads(raw)
                except (TypeError,ValueError):
                    result["errors"].append("Unreadable decision JSON")
                    continue
                if not isinstance(decision,dict):
                    result["errors"].append("Decision JSON must be an object")
                    continue
                state=decision.get("status")
                statuses[str(state)]+=1
                trench=decision.get("trench_evidence")
                if trench is None:
                    continue
                if not isinstance(trench,dict) or trench.get("version")!=VERSION:
                    result["errors"].append("Unknown/malformed trench evidence schema")
                    continue
                # Hard invariant: research telemetry must NEVER carry a buy,
                # wallet-verification or rug-safety approval bit.
                mandatory_false=("trading_approval","rug_safety_verified",
                                 "developer_history_verified",
                                 "holder_wallet_clusters_verified",
                                 "bundled_launch_verified","wash_trading_verified",
                                 "onchain_wallet_confluence_verified")
                if any(trench.get(key) is not False for key in mandatory_false):
                    result["errors"].append("Trench evidence improperly claims trade or security verification")
                    continue
                label=trench.get("label")
                if label not in {"INSUFFICIENT_DATA","ELEVATED_OBSERVABLE_RISK",
                                 "OBSERVABLE_MARKET_FLOW_ONLY"}:
                    result["errors"].append("Unrecognized trench evidence label")
                    continue
                mark=trench.get("flags")
                if not isinstance(mark,list) or any(not isinstance(v,str) for v in mark):
                    result["errors"].append("Malformed trench flags")
                    continue
                result["decisions_with_trench_evidence"]+=1
                if isinstance(mint,str) and mint:
                    covered_mints.add(mint)
                    label_mints[label].add(mint)
                labels[label]+=1
                for flag in set(mark):
                    flags[flag]+=1
                    if isinstance(mint,str) and mint:
                        flag_mints[flag].add(mint)
        result["unique_mints_observed"]=len(all_mints)
        result["unique_mints_with_evidence"]=len(covered_mints)
        result["observation_labels"]=dict(sorted(labels.items()))
        result["label_unique_mints"]={k:len(v) for k,v in sorted(label_mints.items())}
        result["flag_observations"]=dict(sorted(flags.items()))
        result["flag_unique_mints"]={k:len(v) for k,v in sorted(flag_mints.items())}
        result["decision_statuses"]=dict(sorted(statuses.items()))
        if result["errors"]:
            result["status"]="INVALID_EVIDENCE"
        elif result["decisions_with_trench_evidence"]>0:
            result["status"]="RESEARCH_TELEMETRY_ONLY"
        else:
            result["warnings"].append("No trench evidence collected; new PAPER scans are required")
    except (sqlite3.DatabaseError,OSError,ValueError) as exc:
        result["status"]="INVALID_EVIDENCE"
        result["errors"].append(f"Paper DB failed to read: {type(exc).__name__}: {exc}")
    return result


def main():
    p=argparse.ArgumentParser(description="Read-only trench evidence funnel; never trade")
    p.add_argument("--db",default="paper_forward.db")
    p.add_argument("--output",help="Optional JSON report output")
    args=p.parse_args()
    report=audit(args.db)
    encoded=json.dumps(report,sort_keys=True,indent=2,ensure_ascii=False)
    print(encoded)
    if args.output:
        output=Path(args.output)
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(encoded+"\n",encoding="utf-8")
    raise SystemExit(2 if report["status"]=="INVALID_EVIDENCE" else 0)


if __name__=="__main__":
    main()
