"""Offline source-contract tests. Passing these does not prove profitability."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from execution_engine import ExecutionPolicy
from market_discovery import valid_mint, _dex_mints, _gecko_mints, _interleave, discover_mints
from order_lifecycle import OrderIntent, OrderStore, OrderError, IdempotencyConflict, IllegalTransition, ConcurrentOrderUpdate
from rug_schema import verified_token_vaults

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCaKGiyVFs2RmmJ6Bq"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

def intent(key="paper-001", amount=1000000, mode="PAPER", side="BUY"):
    return OrderIntent(key, mode, side, USDC, MINT, amount, 100, "ci-paper-v1")

class TestOrderIntent(unittest.TestCase):
    def test_mode(self):
        with self.assertRaises(OrderError): intent(mode="REAL")
    def test_side(self):
        with self.assertRaises(OrderError): intent(side="SHORT")
    def test_zero_amount(self):
        with self.assertRaises(OrderError): intent(amount=0)
    def test_boolean_amount(self):
        with self.assertRaises(OrderError): intent(amount=True)
    def test_same_mint(self):
        with self.assertRaises(OrderError): OrderIntent("x", "PAPER", "BUY", MINT, MINT, 12, 10, "test")
    def test_frozen(self):
        x = intent()
        with self.assertRaises((AttributeError, TypeError)): x.amount_raw = 2

class TestOrders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OrderStore(Path(self.tmp.name) / "ledger.db", clock=lambda: 1000.0)
    def tearDown(self): self.tmp.cleanup()
    def test_created(self):
        row = self.store.create(intent())
        self.assertEqual((row["state"], row["version"]), ("CREATED", 0))
    def test_idempotency(self):
        a = self.store.create(intent())
        b = self.store.create(intent())
        self.assertEqual(a["id"], b["id"])
    def test_idempotency_conflict(self):
        self.store.create(intent())
        with self.assertRaises(IdempotencyConflict): self.store.create(intent(amount=3))
    def test_no_direct_confirmation(self):
        row = self.store.create(intent())
        with self.assertRaises(IllegalTransition):
            self.store.transition(row["id"], "CONFIRMED", expected_version=0, evidence={"reason":"fake"})
    def test_cancel_is_terminal(self):
        row = self.store.create(intent())
        result = self.store.transition(row["id"], "CANCELLED", expected_version=0, evidence={"reason":"ci"})
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(len(self.store.events(row["id"])), 2)
        with self.assertRaises(IllegalTransition):
            self.store.transition(row["id"], "CREATED", expected_version=1, evidence={"reason":"no reopen"})
    def test_stale_version(self):
        row = self.store.create(intent())
        self.store.transition(row["id"], "CANCELLED", expected_version=0, evidence={"reason":"ci"})
        with self.assertRaises(ConcurrentOrderUpdate):
            self.store.transition(row["id"], "CANCELLED", expected_version=0, evidence={"reason":"duplicate"})
    def test_no_pending(self):
        self.assertEqual(self.store.pending_for_reconciliation(), [])

class TestDiscovery(unittest.TestCase):
    def test_valid_mint(self): self.assertTrue(valid_mint(MINT))
    def test_base_assets_not_candidates(self):
        self.assertFalse(valid_mint(SOL))
        self.assertFalse(valid_mint(USDC))
    def test_invalid_base58(self): self.assertFalse(valid_mint("0" * 44))
    def test_dex_dedupe(self):
        vals, invalid = _dex_mints([
            {"chainId":"solana","tokenAddress":MINT},
            {"chainId":"solana","tokenAddress":MINT},
            {"chainId":"ethereum","tokenAddress":MINT},
            {"chainId":"solana","tokenAddress":"bad"}])
        self.assertEqual(vals, [MINT])
        self.assertEqual(invalid, 1)
    def test_gecko_parse(self):
        vals, invalid = _gecko_mints({"data":[
            {"relationships":{"base_token":{"data":{"id":"solana_"+MINT}}}},
            {"relationships":{"base_token":{"data":{"id":"ethereum_"+MINT}}}}]})
        self.assertEqual(vals, [MINT])
        self.assertEqual(invalid, 1)
    def test_interleave(self):
        self.assertEqual(_interleave([["a","b"],["a","c"]],3), ["a","b","c"])
    def test_bounded_discovery(self):
        calls = []
        def fetch(url, timeout):
            calls.append(url)
            return [{"chainId":"solana","tokenAddress":MINT}]
        candidates, _ = discover_mints(fetch=fetch, gecko_pages=0, max_mints=2)
        self.assertEqual(candidates, [MINT])
        self.assertEqual(len(calls), 5)

class TestPolicy(unittest.TestCase):
    def test_defaults(self): self.assertGreater(ExecutionPolicy().paper_delay_ms, 0)
    def test_negative_delay(self):
        with self.assertRaises(ValueError): ExecutionPolicy(paper_delay_ms=-1)
    def test_nan_impact(self):
        with self.assertRaises(ValueError): ExecutionPolicy(max_price_impact_pct=Decimal("NaN"))
    def test_boolean_age(self):
        with self.assertRaises(ValueError): ExecutionPolicy(quote_max_age_ms=True)

class TestRugCheck(unittest.TestCase):
    def test_missing_report(self): self.assertEqual(verified_token_vaults(None,MINT),set())
    def test_wrong_mint(self):
        self.assertEqual(verified_token_vaults({"mint":"wrong","markets":[],"topHolders":[]}, MINT),set())
    def test_matching_pool(self):
        pool, vault = "pool-1", "vault-1"
        account = {"mint":MINT,"owner":pool,"state":1,"delegate":None,"closeAuthority":None,"amount":900}
        report = {"mint":MINT,"markets":[{"pubkey":pool,"mintA":MINT,"liquidityA":vault,"liquidityAAccount":account}],
                  "topHolders":[{"address":vault,"owner":pool,"amount":900}]}
        self.assertEqual(verified_token_vaults(report,MINT),{vault})
        report["topHolders"][0]["amount"] = 901
        self.assertEqual(verified_token_vaults(report,MINT),set())

if __name__ == "__main__":
    unittest.main()
