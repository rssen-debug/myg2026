"""Read-only, reproducible funnel report from timestamped paper decisions.

Separates repeated evaluation attempts from distinct tokens; NEVER claims alpha,
forward edge, executable fill availability or safe live trading.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3


def analyze(db_path):
    path = Path(db_path).expanduser().resolve()
    report = {
        "status": "NOT_READY",
        "mode": "PAPER_FUNNEL_ONLY",
        "profitability_validated": False,
        "live_approved": False,
        "database": str(path),
        "total_evaluations": 0,
        "unique_mints_evaluated": 0,
        "distinct_observed_minutes": 0,
        "decisions": [],
        "reasons": [],
        "warnings": [
            "Counts are repeated polling decisions, not unique trades.",
            "Missing/excluded future rug outcomes and survivorship-bias control; no proven trading edge.",
        ],
    }
    if not path.is_file():
        report["warnings"].append("No existing paper SQLite file")
        return report
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as con:
            con.row_factory = sqlite3.Row
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "market_observations" not in tables:
                report["warnings"].append("No market_observations table")
                return report
            columns = {row[1] for row in con.execute("PRAGMA table_info(market_observations)")}
            if not {"timestamp", "mint", "decision_json"}.issubset(columns):
                report["warnings"].append("Required observation columns missing")
                return report
            row = con.execute(
                "SELECT COUNT(*) count, COUNT(DISTINCT mint) tokens, "
                "COUNT(DISTINCT CAST(timestamp/60 AS INTEGER)) minutes "
                "FROM market_observations").fetchone()
            report["total_evaluations"] = row["count"]
            report["unique_mints_evaluated"] = row["tokens"]
            report["distinct_observed_minutes"] = row["minutes"]
            for row in con.execute("""
                SELECT json_extract(decision_json,'$.status') AS state,
                       COUNT(*) AS observations,
                       COUNT(DISTINCT mint) AS unique_tokens
                FROM market_observations
                GROUP BY state ORDER BY observations DESC, state
            """):
                report["decisions"].append(dict(row))
            for row in con.execute("""
                SELECT json_extract(decision_json,'$.reason') AS reason,
                       COUNT(*) AS observations,
                       COUNT(DISTINCT mint) AS unique_tokens
                FROM market_observations
                WHERE json_extract(decision_json,'$.status') NOT IN ('BOUGHT')
                GROUP BY reason ORDER BY observations DESC, reason
            """):
                report["reasons"].append(dict(row))
            # Even if BOUGHT is present, it is a paper decision, not proof
            # of a later fill, sell, positive expectancy, or real execution.
            report["warnings"].append(
                "BOUGHT entries, if present, are simulated and require independent order-ledger reconciliation")
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        report["warnings"].append(f"Could not evaluate paper funnel: {type(exc).__name__}: {exc}")
    return report


def main():
    p = argparse.ArgumentParser(description="Unique-token/repeated-decision paper funnel report")
    p.add_argument("--db", default="paper_forward.db")
    p.add_argument("--output")
    args = p.parse_args()
    report = analyze(args.db)
    encoded = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False)
    print(encoded)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded + "\n", encoding="utf-8")
    raise SystemExit(0 if report["total_evaluations"] else 2)


if __name__ == "__main__":
    main()
