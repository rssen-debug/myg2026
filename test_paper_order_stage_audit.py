"""Offline paper-order stage audit must never infer trading from mere quotes."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from paper_order_stage_audit import audit


class TestPaperOrderStageAudit(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/"paper.db"
        with sqlite3.connect(self.db) as con:
            con.executescript("""
                CREATE TABLE execution_orders(
                    id TEXT,mode TEXT,state TEXT,intent_json TEXT,quote_json TEXT
                );
                CREATE TABLE execution_order_events(
                    id INTEGER PRIMARY KEY,order_id TEXT,version INTEGER,evidence_json TEXT
                );
                CREATE TABLE positions(is_paper INTEGER,status TEXT);
                CREATE TABLE trade_history(is_paper INTEGER,action TEXT);
            """)
    def tearDown(self):
        self.temp.cleanup()
    def order(self,oid,side,state,*,quote=False,mode="PAPER",reason=None):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO execution_orders VALUES(?,?,?,?,?)",(
                oid,mode,state,json.dumps({"side":side,"mode":mode}),
                json.dumps({"quote":{"quote":{"received_at":1000}}}) if quote else None))
            if reason is not None:
                c.execute("INSERT INTO execution_order_events(order_id,version,evidence_json) VALUES(?,?,?)",
                    (oid,3,json.dumps({"reason":reason})))

    def test_two_failed_quotes_are_not_reported_as_trades(self):
        self.order("buy1","BUY","FAILED",quote=True,reason="Price moved >1%")
        self.order("buy2","BUY","FAILED",quote=True,reason="Pool liquidity drained")
        r=audit(self.db)
        self.assertEqual(r["paper_order_attempts"],2)
        self.assertEqual(r["paper_orders_with_initial_quote"],2)
        self.assertEqual(r["paper_failed_orders_with_initial_quote"],2)
        self.assertEqual(r["paper_strategy_open_positions"],0)
        self.assertEqual(r["paper_strategy_closed_positions"],0)
        self.assertEqual(r["paper_order_states"]["FAILED"],2)
        self.assertFalse(r["profitability_validated"])
        self.assertTrue(r["is_not_live_fill_evidence"])
        self.assertEqual(r["failed_order_reasons"]["Price moved >1%"],1)

    def test_blank_db_means_no_attempts_not_profit(self):
        r=audit(self.db)
        self.assertEqual(r["status"],"NO_PAPER_ORDER_ATTEMPTS")
        self.assertEqual(r["paper_order_attempts"],0)

    def test_simulated_confirmation_not_live_fill_proof(self):
        self.order("simulated","BUY","CONFIRMED",quote=True)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO positions VALUES(1,'OPEN')")
            c.execute("INSERT INTO trade_history VALUES(1,'BUY')")
        r=audit(self.db)
        self.assertEqual(r["paper_order_states"]["CONFIRMED"],1)
        self.assertEqual(r["paper_strategy_open_positions"],1)
        self.assertEqual(r["paper_strategy_closed_positions"],0)
        self.assertFalse(r["live_approved"])
        self.assertFalse(r["profitability_validated"])

    def test_live_order_in_paper_only_database_is_an_integrity_alarm(self):
        self.order("live","BUY","FAILED",mode="LIVE",reason="not supposed to exist")
        r=audit(self.db)
        self.assertEqual(r["status"],"INVALID_EVIDENCE")
        self.assertTrue(any("Non-PAPER" in s for s in r["errors"]))

    def test_corrupted_quote_json_is_not_silently_counted(self):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO execution_orders VALUES(?,?,?,?,?)",
                ("1","PAPER","FAILED",json.dumps({"mode":"PAPER","side":"BUY"}),"{"))
        r=audit(self.db)
        self.assertEqual(r["status"],"INVALID_EVIDENCE")
        self.assertEqual(r["paper_orders_with_initial_quote"],0)


if __name__=="__main__":
    unittest.main()
