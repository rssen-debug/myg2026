"""Paper buy preflight regression: never use fake 1.1x mark-based token count.

Strict target pool, independent initial/engine quotes, and reversed SELL
quantity are still checked. The test does not fabricate a successful fill.
"""
from decimal import Decimal as D
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from quote_broker import QuoteBroker, USDC
from execution_engine import SOL_MINT

MINT = "CQCDTp8rsFV3PhaPeYjcCW5LdGTxi6jFx7eC3UXnpump"


class TestQuotePreflightUsesTrueExecutableUnits(unittest.TestCase):
    def make_broker(self, quote):
        broker=QuoteBroker.__new__(QuoteBroker)
        broker.ensure_funded=Mock()
        broker._fx=Mock(return_value={
            USDC:{"price_usd":"1","observed_at":100},
            SOL_MINT:{"price_usd":"200","observed_at":100}})
        broker.policy=SimpleNamespace(estimated_network_fee_lamports=3105000)
        broker.cfg={"TRADE_BUDGET_USD":D("20"),"BASE_SLIPPAGE_PCT":D("1")}
        broker.ledger=SimpleNamespace(limits=SimpleNamespace(max_order_raw=20000000))
        broker._quote=quote
        broker._validate_target_route=Mock()
        broker._save_context=Mock()
        broker.engine=SimpleNamespace(execute=Mock(return_value={"state":"FAILED","id":"not-a-real-order"}))
        broker._failure_reason=Mock(return_value="MOCK_ENGINE_FAILURE_NO_FILL")
        broker.store=SimpleNamespace(get_posting=Mock(side_effect=AssertionError("No fill allowed")))
        broker._mark_cache=None
        return broker

    def pair(self):
        return {"price_usd":"0.000005","liquidity_usd":"150000",
                "pair_address":"test-verified-pool",
                "symbol":"MOCK", "observed_at":1111.0}

    def test_reverse_sell_is_for_actual_buy_quote_output_not_mark_times_1_1(self):
        calls=[]
        buy=SimpleNamespace(out_amount_raw=3550000, to_dict=lambda:{"direction":"BUY"})
        sell=SimpleNamespace(out_amount_raw=18900000, to_dict=lambda:{"direction":"SELL"})

        def quoted(input_mint,output_mint,amount,valuation=False):
            calls.append((input_mint,output_mint,amount,valuation))
            return buy if input_mint==USDC else sell
        broker=self.make_broker(quoted)
        response=broker.buy(MINT,self.pair(),{"decimals":6,"validated_at":100},D("150000"))
        self.assertEqual(response["status"],"REJECTED")
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0][0:2],(USDC,MINT))
        self.assertEqual(calls[1][0:2],(MINT,USDC))
        self.assertEqual(calls[1][2],3550000)
        self.assertEqual(calls[1][3],True)
        self.assertEqual(broker._validate_target_route.call_count,2)
        context=broker._save_context.call_args.args[1]
        self.assertEqual(context["sell_probe_raw"],3550000)
        self.assertEqual(context["preflight_buy_probe"]["direction"],"BUY")
        self.assertEqual(context["sell_probe"]["direction"],"SELL")
        broker.store.get_posting.assert_not_called()

    def test_buy_pool_validation_failure_prevents_selling_quote_and_order(self):
        calls=[]
        buy=SimpleNamespace(out_amount_raw=3550000,to_dict=lambda:{})
        def quoted(a,b,amount,valuation=False):
            calls.append((a,b))
            if a==USDC:return buy
            raise AssertionError("Reverse sell cannot be requested without approved BUY pool")
        broker=self.make_broker(quoted)
        broker._validate_target_route.side_effect=ValueError("BUY_ROUTE_OTHER_POOL")
        response=broker.buy(MINT,self.pair(),{"decimals":6,"validated_at":100},D("150000"))
        self.assertEqual(response["status"],"REJECTED")
        self.assertIn("BUY_ROUTE_OTHER_POOL",response["reason"])
        self.assertEqual(calls,[(USDC,MINT)])
        broker._save_context.assert_not_called()
        broker.engine.execute.assert_not_called()

    def test_zero_buy_token_quote_never_issues_an_order(self):
        buy=SimpleNamespace(out_amount_raw=0,to_dict=lambda:{})
        broker=self.make_broker(lambda *args,**kwargs:buy)
        response=broker.buy(MINT,self.pair(),{"decimals":6,"validated_at":100},D("150000"))
        self.assertEqual(response["status"],"REJECTED")
        self.assertIn("tokenmängd",response["reason"])
        broker.engine.execute.assert_not_called()

    def test_reverse_sell_route_mismatch_fails_without_exec(self):
        buy=SimpleNamespace(out_amount_raw=3550000,to_dict=lambda:{})
        sell=SimpleNamespace(out_amount_raw=19100000,to_dict=lambda:{})
        broker=self.make_broker(lambda a,b,*args,**kwargs:buy if a==USDC else sell)
        broker._validate_target_route.side_effect=[None,ValueError("SELL_ROUTE_OTHER_POOL")]
        response=broker.buy(MINT,self.pair(),{"decimals":6,"validated_at":100},D("150000"))
        self.assertEqual(response["status"],"REJECTED")
        self.assertIn("SELL_ROUTE_OTHER_POOL",response["reason"])
        broker.engine.execute.assert_not_called()


if __name__=="__main__":
    unittest.main()
