"""Source-level market/rug survey tests: never call order execution."""
import unittest
from decimal import Decimal

from pre_entry_liquidity_rug_survey import market_gate, survey
from godlike2026 import CONFIG

MINT_A="DezXAZ8z7PnrnRJjz3wXBoRgixCaKGiyVFs2RmmJ6Bq"
MINT_B="JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"


def datum(mint, *, liquidity=100000,observed_at=1000):
    return {
      "symbol":"MOCK", "pair_address":"mock-"+mint[:7],
      "price_usd":Decimal("0.002"),"liquidity_usd":Decimal(liquidity),
      "volume_h1":Decimal(50000),"buys_h1":100,"sells_h1":35,
      "price_change_h1":Decimal(3),"observed_at":observed_at,
    }


class MockDex:
    def __init__(self,results):
        self.results=results
        self.calls=[]
    def prefetch_tokens(self,mints):
        self.calls.append(list(mints))
        return {mint:self.results.get(mint) for mint in mints}


class MockRug:
    def __init__(self,approve=False):
        self.calls=[]
        self.approve=approve
    def validate(self,mint,target_pair_address):
        self.calls.append((mint,target_pair_address))
        return self.approve,"MOCK_NOT_REAL_RUG_REPORT",({"decimals":6,"paper_liquidity_cap_usd":"20000"} if self.approve else {})


class TestPreEntryAudit(unittest.TestCase):
    def test_a_qualifying_market_does_not_mean_a_paper_buy(self):
        d=datum(MINT_A)
        self.assertEqual(market_gate(d,CONFIG,now=1000),"ELIGIBLE_FOR_TIMED_MOMENTUM_ONLY")
        dex=MockDex({MINT_A:d})
        rug=MockRug(approve=True)
        report=survey([MINT_A],dex=dex,rug=rug,clock=lambda:1000)
        self.assertEqual(report["rug_probe_count"],1)
        self.assertEqual(report["rug_funnel"]["REPORT_FILTER_PASSED_NOT_SELLABILITY_PROVEN"],1)
        self.assertEqual(rug.calls,[(MINT_A,d["pair_address"])])
        self.assertTrue(report["not_an_executable_fill"])
        self.assertFalse(report["live_approved"])
        self.assertFalse(report["profitability_validated"])
        self.assertFalse(report["observations"][0]["paper_order_submitted"])
        self.assertFalse(report["observations"][0]["live_order_submitted"])

    def test_market_failure_does_not_consume_rug_calls(self):
        dex=MockDex({MINT_A:datum(MINT_A,liquidity=50),MINT_B:None})
        rug=MockRug()
        r=survey([MINT_A,MINT_B],dex=dex,rug=rug,clock=lambda:1000)
        self.assertEqual(r["rug_probe_count"],0)
        self.assertEqual(r["market_funnel"]["LOW_OR_INVALID_LIQUIDITY_OR_PRICE"],1)
        self.assertEqual(r["market_funnel"]["MISSING_MARKET_DATA"],1)
        self.assertEqual(rug.calls,[])

    def test_stale_price_and_infinite_liquidity_are_blocked(self):
        self.assertEqual(market_gate(datum(MINT_A,observed_at=100),CONFIG,now=1000),
                         "MISSING_OR_STALE_MARKET_TIMESTAMP")
        self.assertEqual(market_gate(datum(MINT_A,liquidity="Infinity"),CONFIG,now=1000),
                         "LOW_OR_INVALID_LIQUIDITY_OR_PRICE")

    def test_rug_block_not_a_market_success(self):
        dex=MockDex({MINT_A:datum(MINT_A)})
        rug=MockRug(approve=False)
        r=survey([MINT_A],dex=dex,rug=rug,clock=lambda:1000)
        self.assertEqual(r["rug_funnel"]["REPORT_FILTER_BLOCKED"],1)
        self.assertEqual(r["observations"][0]["routing_status"],"NOT_CHECKED")

    def test_invalid_base_mint_never_probed(self):
        with self.assertRaises(ValueError):
            survey(["TOKENZ_FAKE"] ,dex=MockDex({}),rug=MockRug())
        with self.assertRaises(ValueError):
            survey([MINT_A]*201,dex=MockDex({}),rug=MockRug())


if __name__=="__main__":
    unittest.main()
