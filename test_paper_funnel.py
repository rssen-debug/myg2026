"""Synthetic unit checks for paper funnel; no forward-profit claims."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from paper_funnel import analyze


class TestPaperFunnel(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "paper.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_db_does_not_create_or_approve(self):
        report = analyze(self.db)
        self.assertEqual(report["status"], "NOT_READY")
        self.assertFalse(report["live_approved"])
        self.assertFalse(self.db.exists())

    def test_repeated_evaluations_are_not_unique_tokens(self):
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE market_observations (timestamp REAL, mint TEXT, decision_json TEXT)")
            rows=[
                (2000000000, "A", json.dumps({"status":"REJECTED","reason":"No liquidity"})),
                (2000000004, "A", json.dumps({"status":"REJECTED","reason":"No liquidity"})),
                (2000000080, "B", json.dumps({"status":"WAITING","reason":"More observations"})),
            ]
            con.executemany("INSERT INTO market_observations VALUES (?,?,?)", rows)
        r=analyze(self.db)
        self.assertEqual(r["total_evaluations"],3)
        self.assertEqual(r["unique_mints_evaluated"],2)
        self.assertEqual(r["distinct_observed_minutes"],2)
        self.assertEqual(next(x for x in r["reasons"] if x["reason"]=="No liquidity")["unique_tokens"],1)
        self.assertEqual(next(x for x in r["reasons"] if x["reason"]=="No liquidity")["observations"],2)
        self.assertFalse(r["profitability_validated"])

    def test_invalid_schema_fails_closed(self):
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE market_observations (wrong TEXT)")
        r=analyze(self.db)
        self.assertEqual(r["total_evaluations"],0)
        self.assertTrue(any("columns" in w for w in r["warnings"]))


if __name__=="__main__":
    unittest.main()
