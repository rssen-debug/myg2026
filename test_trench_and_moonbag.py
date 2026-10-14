"""Offline trench intelligence and real moonbag state-machine regression tests.

These exercise the ACTUAL strategy monitor without a network or wallet.
They do not imply profitable signals, on-chain execution, or safe live trading.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from trench_evidence import assess_pair
from godlike2026 import CONFIG, GodlikeBot, PositionMonitor, TrenchIQAnalyzer, SmartWalletTracker

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCaKGiyVFs2RmmJ6Bq"


def pair(**overrides):
    data = {
        "symbol": "EXAMPLE", "name": "Example Token", "pair_address": "pool-test",
        "price_usd": Decimal("1"), "liquidity_usd": Decimal("200000"),
        "buys_h1": 75, "sells_h1": 32, "volume_h1": Decimal("80000"),
        "price_change_h1": Decimal("5"), "boosts": 0,
        "observed_at": time.time(),
    }
    data.update(overrides)
    return data


class TestTrenchEvidence(unittest.TestCase):
    def test_promoted_token_never_receives_safety_approval(self):
        observed = assess_pair(pair(boosts=1000, symbol="AI PEPE AGENT"))
        self.assertEqual(observed["label"], "OBSERVABLE_MARKET_FLOW_ONLY")
        self.assertFalse(observed["trading_approval"])
        self.assertFalse(observed["rug_safety_verified"])
        self.assertFalse(observed["onchain_wallet_confluence_verified"])
        self.assertFalse(observed["bundled_launch_verified"])
        self.assertFalse(observed["developer_history_verified"])
        self.assertIn("PAID_BOOSTS_ARE_NOT_ORGANIC_VALIDATION", observed["flags"])
        self.assertEqual(TrenchIQAnalyzer().analyze(pair(boosts=1000))[0], None)

    def test_suspicious_reported_flow_is_a_warning_not_an_auto_trade(self):
        observed = assess_pair(pair(buys_h1=8, sells_h1=110, volume_h1="2000000",
                                    liquidity_usd="20000", price_change_h1="-55"))
        self.assertEqual(observed["label"], "ELEVATED_OBSERVABLE_RISK")
        for flag in ("SELL_DOMINANCE_REPORTED", "EXCESSIVE_VOLUME_TO_LIQUIDITY",
                     "SHARP_REPORTED_HOURLY_DROP"):
            self.assertIn(flag, observed["flags"])
        self.assertFalse(observed["trading_approval"])

    def test_missing_and_nan_values_are_not_safe(self):
        for candidate in [None, {}, pair(buys_h1="NaN"), pair(liquidity_usd="Infinity"),
                          pair(observed_at=time.time()-90)]:
            with self.subTest(candidate=candidate):
                observed = assess_pair(candidate)
                self.assertEqual(observed["label"], "INSUFFICIENT_DATA")
                self.assertFalse(observed["rug_safety_verified"])

    def test_two_wallets_in_db_are_not_verified_onchain_confluence(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/"signals.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE wallet_signals(wallet_address TEXT,mint TEXT,amount_sol REAL,tx_signature TEXT,timestamp REAL)")
                con.execute("CREATE TABLE smart_wallets(address TEXT PRIMARY KEY,label TEXT)")
                con.executemany(
                    "INSERT INTO wallet_signals VALUES(?,?,?,?,?)",
                    [("wallet-a",MINT,1.0,"unverified-signature-a",time.time()),
                     ("wallet-b",MINT,1.0,"unverified-signature-b",time.time())])
            class State:
                @contextmanager
                def _get_connection(self):
                    con=sqlite3.connect(db)
                    con.row_factory=sqlite3.Row
                    try:yield con
                    finally:con.close()
            verified,labels,mult=SmartWalletTracker(State(),"unused").check_confluence(MINT)
            self.assertFalse(verified)
            self.assertEqual(len(labels),2)
            self.assertTrue(all(x.startswith("UNVERIFIED:") for x in labels))
            self.assertEqual(mult,1.0)

    def test_rejected_paper_market_decision_still_persists_trench_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/"observation.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE market_observations(id INTEGER PRIMARY KEY,timestamp REAL,mint TEXT,market_json TEXT,decision_json TEXT)")
            class State:
                @contextmanager
                def _get_connection(self):
                    con=sqlite3.connect(db)
                    try:
                        yield con
                        con.commit()
                    finally:
                        con.close()
            class Dex:
                def get_token_data(self,mint):
                    return pair(liquidity_usd=Decimal("250"),buys_h1=1,sells_h1=0)
            bot=GodlikeBot.__new__(GodlikeBot)
            bot.state,bot.dex,bot.cfg=State(),Dex(),dict(CONFIG)
            result=bot.evaluate_and_buy(MINT,is_paper=True)
            self.assertEqual(result["status"],"REJECTED")
            self.assertEqual(result["trench_evidence"]["label"],"ELEVATED_OBSERVABLE_RISK")
            with sqlite3.connect(db) as con:
                evidence=json.loads(con.execute("SELECT decision_json FROM market_observations").fetchone()[0])
            self.assertEqual(evidence["trench_evidence"]["version"],"trench-evidence-v1")
            self.assertFalse(evidence["trench_evidence"]["trading_approval"])


class PositionState:
    def __init__(self):
        self.cfg=dict(CONFIG)
        self.position={
            "id":101, "mint":MINT,"symbol":"EXAMPLE", "pair_address":"pool-test",
            "decimals":6, "initial_raw":"100", "remaining_raw":"100",
            "entry_price_usd":"1.00","peak_price_usd":"1.00",
            "stop_loss_price":"0.86", "trailing_active":0,
            "tp1_executed":0,"tp2_executed":0,"status":"OPEN",
            "entry_liquidity_cap_usd":"150000"}
        self.sales=[]
    def get_open_positions(self,is_paper=True):
        if self.position["status"]!="OPEN":return []
        return [dict(self.position)]
    def update_position_trailing(self,pos_id,peak_price,stop_loss,trailing_active):
        p=self.position
        assert pos_id==p["id"]
        p["peak_price_usd"]=str(max(Decimal(p["peak_price_usd"]),Decimal(peak_price)))
        p["stop_loss_price"]=str(max(Decimal(p["stop_loss_price"]),Decimal(stop_loss)))
        p["trailing_active"]=max(p["trailing_active"],int(trailing_active))
    def apply_partial_sell(self,pos_id,tp_level,remaining_raw,sold_ratio,gross_usd,
                           fee_usd,net_usd,exit_price,is_paper=True):
        p=self.position
        assert p["id"]==pos_id and p["status"]=="OPEN"
        prior=int(p["remaining_raw"])
        sell_qty=prior-int(remaining_raw)
        assert 0<sell_qty<prior and tp_level in (1,2)
        assert not p[f"tp{tp_level}_executed"]
        if tp_level==2:assert p["tp1_executed"]
        p["remaining_raw"]=str(remaining_raw)
        p[f"tp{tp_level}_executed"]=1
        self.sales.append((f"TP{tp_level}",sell_qty))
        return True
    def close_position(self,pos_id,exit_price,reason,net_usd,fee_usd,tx_hash,is_paper=True):
        p=self.position
        assert p["id"]==pos_id and p["status"]=="OPEN"
        self.sales.append((reason,int(p["remaining_raw"])))
        p["remaining_raw"]="0"
        p["status"]="CLOSED"
        return True


class DummyDex:
    def __init__(self): self.price=None
    def get_pair_data(self,mint,pair_address):
        if self.price is None:return None
        return pair(price_usd=Decimal(self.price),observed_at=time.time())


class TestActualMoonbagMonitor(unittest.TestCase):
    def setUp(self):
        self.state=PositionState()
        self.dex=DummyDex()
        self.monitor=PositionMonitor(self.state,self.dex,None)
        self.execution=[]
        def paper_sell(position, raw_amount, price, liquidity, reason, is_paper):
            assert is_paper and int(raw_amount)<=int(position["remaining_raw"])
            self.execution.append((reason,int(raw_amount)))
            return {"booked":False, "gross_usd":price*raw_amount,
                    "net_usd":price*raw_amount, "fee_usd":Decimal("0"),
                    "execution_price":price, "tx_hash":"synthetic-only"}
        self.monitor._execute_sell=paper_sell

    def tick(self,price):
        self.dex.price=str(price)
        return self.monitor.check_and_exit_positions(True)

    def test_full_original_position_tp1_tp2_moonbag_trailing_stop(self):
        self.tick("1.50")
        self.assertEqual(self.state.sales,[("TP1",40)])
        self.assertEqual(self.state.position["remaining_raw"],"60")
        self.assertEqual(self.state.position["tp1_executed"],1)
        self.assertGreaterEqual(Decimal(self.state.position["stop_loss_price"]),Decimal("1.15"))
        self.tick("2.00")
        self.assertEqual(self.state.sales,[("TP1",40),("TP2",35)])
        self.assertEqual(self.state.position["remaining_raw"],"25")
        self.assertEqual(self.state.position["trailing_active"],2)
        self.tick("2.20")
        self.assertEqual(Decimal(self.state.position["stop_loss_price"]),Decimal("1.936"))
        self.assertEqual(len(self.state.sales),2)
        self.tick("1.90")
        self.assertEqual(self.state.sales,[("TP1",40),("TP2",35),("MOONBAG_TRAILING_STOP",25)])
        self.assertEqual(self.state.position["status"],"CLOSED")
        self.assertEqual(self.state.position["remaining_raw"],"0")
        self.tick("2.50")
        self.assertEqual(len(self.state.sales),3)
        self.assertEqual(sum(q for _,q in self.state.sales),100)

    def test_missing_exit_market_data_never_confirms_phantom_sell(self):
        self.tick("1.50")
        self.dex.price=None
        self.monitor.check_and_exit_positions(True)
        self.assertEqual(self.state.sales,[("TP1",40)])
        self.assertEqual(self.state.position["remaining_raw"],"60")
        self.assertEqual(self.state.position["status"],"OPEN")

    def test_initial_stop_sells_all_raw_before_profit_levels(self):
        self.tick("0.65")
        self.assertEqual(self.state.sales,[("STOP_LOSS",100)])
        self.assertEqual(self.state.position["status"],"CLOSED")


if __name__=="__main__":
    unittest.main()
