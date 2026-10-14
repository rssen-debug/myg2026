"""Regressions for actual scanner entry gating; mocked routing NEVER proves fills.

Three *distinct time-stamped price observations* are required before one
eligible paper buy request. Fee-sensitive paper quotes, RugCheck and wallet
bookkeeping are independently covered elsewhere; this suite never sends orders.
"""
from __future__ import annotations
from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from godlike2026 import GodlikeBot, CONFIG, RugCheckValidator

MINT="DezXAZ8z7PnrnRJjz3wXBoRgixCaKGiyVFs2RmmJ6Bq"
CLASSIC=RugCheckValidator.CLASSIC_TOKEN_PROGRAM
TOKEN_2022="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class FakeState:
    def __init__(self,db):
        self.db=db
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE market_observations (timestamp REAL,mint TEXT,market_json TEXT,decision_json TEXT)")
        self.can_open_calls=0
    @contextmanager
    def _get_connection(self):
        con=sqlite3.connect(self.db)
        try:
            yield con
            con.commit()
        finally:
            con.close()
    def can_open_position(self,mint,trade_usd,dex_client=None):
        self.can_open_calls+=1
        return True,"OK"


class FakeDex:
    def __init__(self):self.price=Decimal("1")
    def get_token_data(self,mint):
        assert mint==MINT
        import time
        return {"pair_address":"mock-pool", "symbol":"MOCK","name":"Research Only",
                "price_usd":self.price,"liquidity_usd":Decimal("100000"),
                "buys_h1":100,"sells_h1":35,"volume_h1":Decimal("90000"),
                "price_change_h1":Decimal("5"),"observed_at":time.time(),
                "boosts":0}


class FakeRug:
    def __init__(self):self.calls=0
    def validate(self,mint,target_pair_address=None):
        self.calls+=1
        assert target_pair_address=="mock-pool"
        return True,"FAKE_RESEARCH_ONLY", {"decimals":6,"paper_liquidity_cap_usd":"100000"}


class FakeBroker:
    def __init__(self):self.buys=0
    def buy(self,mint,fresh,meta,cap):
        self.buys+=1
        assert mint==MINT
        assert fresh["pair_address"]=="mock-pool"
        return {"status":"BOUGHT","reason":"MOCK PAPER ROUTING ONLY; NO EXCHANGE FILL"}


class TestActualScannerFlow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/"scanner.db"
        self.bot=GodlikeBot.__new__(GodlikeBot)
        self.bot.cfg=dict(CONFIG)
        self.bot.state=FakeState(self.db)
        self.bot.dex=FakeDex()
        self.bot.rug=FakeRug()
        self.bot.quote_broker=FakeBroker()
        self.bot._signals={}
        self.base=1700000000.0
    def tearDown(self):
        self.tmp.cleanup()
    def decision(self,seconds,price):
        self.bot.dex.price=Decimal(price)
        with patch("godlike2026.time.time",return_value=self.base+seconds):
            return self.bot.evaluate_and_buy(MINT,is_paper=True)

    def test_eligible_three_observation_path_reaches_paper_buy_intent(self):
        self.assertEqual(self.decision(0,"1.00")["status"],"WAITING")
        self.assertEqual(self.decision(16,"1.015")["status"],"WAITING")
        result=self.decision(36,"1.040")
        self.assertEqual(result["status"],"BOUGHT")
        self.assertIn("MOCK",result["reason"])
        self.assertEqual(self.bot.state.can_open_calls,1)
        self.assertEqual(self.bot.rug.calls,1)
        self.assertEqual(self.bot.quote_broker.buys,1)
        with sqlite3.connect(self.db) as c:
            statuses=[json.loads(r[0])["status"] for r in c.execute(
                "SELECT decision_json FROM market_observations ORDER BY rowid")]
        self.assertEqual(statuses,["WAITING","WAITING","BOUGHT"])

    def test_flat_prices_cannot_become_fake_momentum_entry(self):
        self.decision(0,"1.00")
        self.decision(16,"1.00")
        result=self.decision(36,"1.00")
        self.assertEqual(result["status"],"REJECTED")
        self.assertEqual(self.bot.quote_broker.buys,0)

    def test_one_api_receipt_cannot_manufacture_three_price_observations(self):
        self.decision(0,"1.00")
        self.decision(0,"1.02")
        result=self.decision(0,"1.04")
        self.assertEqual(result["status"],"WAITING")
        self.assertEqual(self.bot.quote_broker.buys,0)

    def test_live_remains_blocked_even_with_perfect_fake_observations(self):
        response=self.bot.evaluate_and_buy(MINT,is_paper=False)
        self.assertEqual(response["status"],"BLOCKED")
        self.assertEqual(self.bot.quote_broker.buys,0)


class TestRugTokenProgramDiagnostics(unittest.TestCase):
    def setUp(self):
        self.validator=RugCheckValidator()
        self.report={"mint":MINT,"rugged":False,"token":{
            "mintAuthority":None, "freezeAuthority":None, "decimals":6}}
    def check(self):
        return self.validator._validate_report(self.report,"mock-pool",expected_mint=MINT)

    def test_missing_program_is_diagnosed_not_accidentally_approved(self):
        allowed,reason,_=self.check()
        self.assertFalse(allowed)
        self.assertIn("saknas",reason)

    def test_actual_token2022_is_distinguished_and_blocked(self):
        self.report["tokenProgram"]=TOKEN_2022
        allowed,reason,_=self.check()
        self.assertFalse(allowed)
        self.assertIn("icke-klassiskt",reason)

    def test_conflicting_programs_are_not_treated_as_mere_token2022(self):
        self.report["tokenProgram"]=CLASSIC
        self.report["token"]["tokenProgram"]=TOKEN_2022
        allowed,reason,_=self.check()
        self.assertFalse(allowed)
        self.assertIn("motsägelsefulla",reason)

    def test_classic_program_does_not_bypass_remaining_missing_risk_checks(self):
        self.report["tokenProgram"]=CLASSIC
        allowed,reason,_=self.check()
        self.assertFalse(allowed)
        self.assertNotIn("icke-klassiskt",reason)


if __name__=="__main__":
    unittest.main()
