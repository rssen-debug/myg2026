"""Moonbag historical replay safety tests. Synthetic prices never imply real profit."""
import unittest
from moonbag_historical_replay import run_moonbag


def rising(n=90, vol=300000.0):
    bars=[]
    for i in range(n):
        o=100*1.005**max(0,i-1)
        c=100*1.005**i
        bars.append({"ts":1700000000+3600*i,
                     "o":o,"h":max(o,c)*1.001,"l":min(o,c)*0.999,
                     "c":c,"v":vol})
    return bars


def asset(bars):
    return {"pool":"fake-synthetic","name":"SYNTHETIC","bars":bars}


class TestFixedMoonbagHistoricalResearch(unittest.TestCase):
    def test_tp1_tp2_25percent_trailing_exit_preserves_original_quantities(self):
        bars=rising()
        entry=bars[26]["o"]
        bars[27].update(o=entry*1.30,l=entry*1.20,h=entry*1.50,c=entry*1.45)
        bars[28].update(o=entry*1.65,l=entry*1.40,h=entry*1.91,c=entry*1.85)
        bars[29].update(o=entry*1.85,l=entry*1.75,h=entry*2.10,c=entry*2.05)
        bars[30].update(o=entry*1.88,l=entry*1.75,h=entry*1.90,c=entry*1.82)
        result=run_moonbag([asset(bars)],end=bars[31]["ts"])
        events=[e for e in result["partial_sells"] if e["entry_ts"]==bars[26]["ts"]]
        self.assertEqual([e["event"] for e in events],
                         ["TP1","TP2","MOONBAG_TRAILING_STOP"])
        quantities=[e["quantity"] for e in events]
        self.assertAlmostEqual(quantities[0]/sum(quantities),0.40,places=8)
        self.assertAlmostEqual(quantities[1]/sum(quantities),0.35,places=8)
        self.assertAlmostEqual(quantities[2]/sum(quantities),0.25,places=8)
        self.assertEqual(result["tp1_partial_sells"],1)
        self.assertEqual(result["tp2_partial_sells"],1)
        self.assertEqual(result["moonbag_trailing_stops"],1)
        self.assertTrue(any(x["tp1_executed"] and x["tp2_executed"]
                            for x in result["closed_positions"]))

    def test_stop_beats_take_profit_if_same_bar_contains_both(self):
        bars=rising()
        entry=bars[26]["o"]
        bars[26].update(h=entry*1.8,l=entry*0.50)
        result=run_moonbag([asset(bars)],end=bars[28]["ts"])
        events=[e for e in result["partial_sells"] if e["entry_ts"]==bars[26]["ts"]]
        self.assertEqual([e["event"] for e in events],["STOP_LOSS"])
        self.assertEqual(result["tp1_partial_sells"],0)

    def test_no_signals_do_not_erase_or_invent_money(self):
        bars=rising(vol=100)
        result=run_moonbag([asset(bars)])
        self.assertEqual(result["closed_trades"],0)
        self.assertEqual(result["entries"],0)
        self.assertEqual(result["net_return_pct"],0)

    def test_each_bought_position_charged_entry_and_exit_fees(self):
        bars=rising()
        result=run_moonbag([asset(bars)])
        self.assertGreater(result["entries"],0)
        for closed in result["closed_positions"]:
            self.assertEqual(closed["invested_usd"],20.0)
            self.assertAlmostEqual(
                closed["received_usd"]-closed["invested_usd"],
                closed["net_pnl_usd"],places=5)

    def test_unusable_empty_history_never_claims_paper_trades(self):
        with self.assertRaises(ValueError):
            run_moonbag([])
        with self.assertRaises(ValueError):
            run_moonbag([{"pool":"x","name":"TEST","bars":[]}])

    def test_duplicate_pool_does_not_double_count_liquidity(self):
        b=asset(rising())
        with self.assertRaisesRegex(ValueError,"Duplicate pool"):
            run_moonbag([b,b])


if __name__=="__main__":
    unittest.main()
