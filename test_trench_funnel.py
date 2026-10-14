"""Deterministic trench-funnel tests for distinct tokens and false verified signals."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from trench_funnel import audit
from trench_evidence import assess_pair


class TestTrenchFunnel(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/"paper.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE market_observations(timestamp REAL,mint TEXT,decision_json TEXT)")
    def tearDown(self):
        self.temp.cleanup()
    def insert(self,mint,decision):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO market_observations VALUES(?,?,?)",
                         (1700000000,mint,json.dumps(decision)))
    def evidence(self,label="OBSERVABLE_MARKET_FLOW_ONLY",flags=()):
        obj=assess_pair({"observed_at":1700000000,
                         "buys_h1":50,"sells_h1":25,
                         "volume_h1":100000,"liquidity_usd":200000,
                         "price_change_h1":2},now=1700000000)
        obj["label"]=label
        obj["flags"]=list(flags)
        return obj

    def test_no_data_not_a_positive_conclusion(self):
        report=audit(self.db)
        self.assertEqual(report["status"],"NO_EVIDENCE")
        self.assertFalse(report["profitability_validated"])
        self.assertFalse(report["live_approved"])

    def test_repeated_decisions_do_not_inflate_unique_tokens(self):
        trench=self.evidence(flags=["PAID_BOOSTS_ARE_NOT_ORGANIC_VALIDATION"])
        self.insert("mint-A",{"status":"WAITING","trench_evidence":trench})
        self.insert("mint-A",{"status":"REJECTED","trench_evidence":trench})
        self.insert("mint-B",{"status":"REJECTED","trench_evidence":self.evidence("ELEVATED_OBSERVABLE_RISK",
                                                       ["SELL_DOMINANCE_REPORTED"])})
        r=audit(self.db)
        self.assertEqual(r["status"],"RESEARCH_TELEMETRY_ONLY")
        self.assertEqual(r["total_decisions"],3)
        self.assertEqual(r["unique_mints_observed"],2)
        self.assertEqual(r["unique_mints_with_evidence"],2)
        self.assertEqual(r["observation_labels"]["OBSERVABLE_MARKET_FLOW_ONLY"],2)
        self.assertEqual(r["label_unique_mints"]["OBSERVABLE_MARKET_FLOW_ONLY"],1)
        self.assertEqual(r["flag_unique_mints"]["PAID_BOOSTS_ARE_NOT_ORGANIC_VALIDATION"],1)
        self.assertFalse(r["trench_security_verified"])

    def test_imported_pretrench_rows_are_not_silently_labeled_safe(self):
        self.insert("old",{"status":"REJECTED","reason":"Missing"})
        r=audit(self.db)
        self.assertEqual(r["status"],"NO_EVIDENCE")
        self.assertEqual(r["unique_mints_observed"],1)
        self.assertEqual(r["unique_mints_with_evidence"],0)

    def test_fake_trading_approval_is_a_hard_error(self):
        suspicious=self.evidence()
        suspicious["onchain_wallet_confluence_verified"]=True
        self.insert("mint-A",{"status":"BOUGHT","trench_evidence":suspicious})
        report=audit(self.db)
        self.assertEqual(report["status"],"INVALID_EVIDENCE")
        self.assertTrue(report["errors"])
        self.assertFalse(report["live_approved"])

    def test_wrong_or_corrupt_schema_not_passed_as_clean(self):
        self.insert("mint-A",{"status":"SKIPPED","trench_evidence":{"version":"future"}})
        report=audit(self.db)
        self.assertEqual(report["status"],"INVALID_EVIDENCE")


if __name__=="__main__":
    unittest.main()
