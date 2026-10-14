"""Safety and accounting tests for exploratory OHLCV PAPER simulation."""
import math
import unittest

from historical_sol_paper import next_open_entry, run_strategy, simulate_exit, parse_candles, COST


def rising_bars(n=90):
    out=[]
    for i in range(n):
        o=100*(1.005**max(0,i-1))
        c=100*(1.005**i)
        out.append({"ts":1700000000+3600*i,
                    "o":o,"h":max(o,c)*1.001,"l":min(o,c)*.999,
                    "c":c,"v":500000.0})
    return out


class TestHistoricalSolPaper(unittest.TestCase):
    def test_real_entry_rules_create_candidate_without_current_bar_peek(self):
        bars=rising_bars()
        asset={"pool":"pool-one","name":"Test","bars":bars}
        self.assertTrue(next_open_entry(asset,30))
        # Future bar's close, high/low, volume never affects entry.
        bars[30].update(c=0.00001,h=999999,l=0.00001,v=0)
        self.assertTrue(next_open_entry(asset,30))

    def test_constant_price_roundtrip_loses_after_costs(self):
        cost=COST
        spent=20
        quantity=(spent-cost["network_fee_usd_each_side"])*(1-cost["trade_fee_pct"])*(1-cost["slippage_pct"])/100
        received=simulate_exit(100,quantity)
        self.assertLess(received,spent)

    def test_paper_opens_and_closes_without_free_capital(self):
        asset={"pool":"pool-one","name":"Test","bars":rising_bars()}
        report=run_strategy([asset])
        self.assertGreater(report["entry_signals"],0)
        self.assertGreater(report["closed_trades"],0)
        self.assertLessEqual(report["open_positions"],1)
        for trade in report["trades"]:
            self.assertEqual(trade["entry_usd"],20.0)
            self.assertGreater(trade["exit_ts"],trade["entry_ts"])
            self.assertEqual(trade["net_pnl_usd"],round(trade["exit_usd"]-20.0,6))

    def test_conservative_stop_first_on_ambiguous_candle(self):
        asset={"pool":"pool-one","name":"Test","bars":rising_bars()}
        # First entry at idx26. Next candle crosses both +30% and -14%.
        # Its OPEN stays near previous price: stop must get priority.
        first_entry=26
        buy_price=asset["bars"][first_entry]["o"]
        following=asset["bars"][first_entry+1]
        following["h"]=buy_price*1.5
        following["l"]=buy_price*0.5
        report=run_strategy([asset])
        matching=[t for t in report["trades"] if t["entry_ts"]==asset["bars"][first_entry]["ts"]]
        self.assertTrue(matching, "Test fixture must trigger a real PAPER entry")
        self.assertEqual(matching[0]["reason"],"STOP")
        self.assertLess(matching[0]["net_pnl_usd"],0)

    def test_entry_bar_stop_is_recognized_not_ignored(self):
        asset={"pool":"pool-one","name":"Test","bars":rising_bars()}
        entry_index=26
        entry=asset["bars"][entry_index]
        entry["l"]=entry["o"]*0.65
        # Prior hourly closes remain unchanged, so the signal is still valid.
        report=run_strategy([asset])
        first=[t for t in report["trades"] if t["entry_ts"]==entry["ts"]]
        self.assertTrue(first)
        self.assertEqual(first[0]["exit_ts"],entry["ts"])
        self.assertEqual(first[0]["reason"],"ENTRY_BAR_STOP")
        self.assertLess(first[0]["net_pnl_usd"],0)

    def test_order_size_cannot_exceed_previous_bar_volume_cap(self):
        bars=rising_bars()
        for bar in bars:bar["v"]=1000
        asset={"pool":"pool-one","name":"Test","bars":bars}
        report=run_strategy([asset])
        self.assertEqual(report["closed_trades"],0)

    def test_broken_hourly_history_prevents_false_signal(self):
        bars=rising_bars()
        bars[22]["ts"]+=30
        asset={"pool":"pool-one","bars":bars}
        self.assertFalse(next_open_entry(asset,30))

    def test_invalid_ohlcv_data_rejected(self):
        data={"data":{"attributes":{"ohlcv_list":[
            [1700000000,100,102,98,101,10000],
            [1700003600,100,102,99,float("nan"),10000],
            [1700007200,100,110,90,101,10000],
        ]}}}
        parsed=parse_candles(data)
        self.assertEqual(len(parsed),2)

    def test_no_pools_no_fabricated_results(self):
        with self.assertRaises(ValueError):
            run_strategy([])

if __name__=="__main__":
    unittest.main()
