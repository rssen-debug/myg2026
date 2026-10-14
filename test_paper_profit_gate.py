"""Synthetic fixtures for the READ-ONLY paper result auditor, not performance claims."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from paper_profit_gate import evaluate, number

class TestPaperProfitGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "paper.db"
    def tearDown(self): self.tmp.cleanup()
    def schema(self):
        with sqlite3.connect(self.db) as c:
            c.executescript("""
            CREATE TABLE market_observations (timestamp REAL);
            CREATE TABLE positions (id INTEGER, usd_invested TEXT, net_usd_received TEXT, is_paper INTEGER, status TEXT);
            CREATE TABLE trade_history (pos_id INTEGER, usd_amount TEXT, is_paper INTEGER, action TEXT);
            CREATE TABLE equity_history (id INTEGER PRIMARY KEY, timestamp REAL, equity_usd TEXT, complete INTEGER);
            """)
    def test_missing_database_never_approved(self):
        r = evaluate(self.db)
        self.assertFalse(r["profitability_validated"])
        self.assertFalse(r["live_approved"])
        self.assertFalse(self.db.exists())
    def test_missing_tables_never_approved(self):
        with sqlite3.connect(self.db) as c: c.execute("CREATE TABLE dummy(x)")
        self.assertIn("missing", " ".join(evaluate(self.db)["reasons"]))
    def test_financial_nan_rejected(self):
        with self.assertRaises(ValueError): number("NaN")
    def test_empty_paper_history_not_ready(self):
        self.schema()
        r = evaluate(self.db)
        self.assertEqual(r["paper_closed_positions"], 0)
        self.assertIn("Only 0", " ".join(r["reasons"]))
    def test_synthetic_positive_closed_pnl_still_not_validated(self):
        self.schema()
        t = 2000000000
        with sqlite3.connect(self.db) as c:
            c.executemany("INSERT INTO market_observations VALUES (?)",
                          [(t + d*86400,) for d in range(30)])
            c.executemany("INSERT INTO positions VALUES (?,?,?,?,?)",
                          [(i, "1.00", "1.05", 1, "CLOSED") for i in range(1,201)])
            c.executemany("INSERT INTO equity_history VALUES (?,?,?,?)",
                          [(i+1,t+i*86400,"100.00",1) for i in range(30)])
        r = evaluate(self.db)
        self.assertEqual(r["paper_days_observed"],30)
        self.assertEqual(r["paper_closed_positions"],200)
        self.assertEqual(r["paper_realized_net_usd"],"10.00")
        self.assertEqual(r["paper_max_drawdown_pct"],"0.00")
        self.assertFalse(r["profitability_validated"])
        self.assertFalse(r["live_approved"])
        self.assertIn("out-of-sample", " ".join(r["reasons"]))
    def test_missing_valuation_fails_closed(self):
        self.schema()
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO equity_history VALUES (1, 2000000000, NULL, 0)")
        r = evaluate(self.db)
        self.assertIsNone(r["paper_max_drawdown_pct"])
        self.assertIn("Incomplete", " ".join(r["reasons"]))
    def test_gap_day_detected(self):
        self.schema()
        with sqlite3.connect(self.db) as c:
            c.executemany("INSERT INTO market_observations VALUES (?)",
                          [(2000000000,), (2000000000 + 2*86400,)])
        r = evaluate(self.db, min_days=2, min_closed=1)
        self.assertIn("Gap days", " ".join(r["reasons"]))
if __name__ == "__main__": unittest.main()
