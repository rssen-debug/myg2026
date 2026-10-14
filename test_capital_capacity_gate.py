"""Offline capacity checks; synthetic input never implies a safe executable quote."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from capital_capacity_gate import evaluate, positive_decimal

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)


def allocation(notional="100000", liquidity="10000000", pool="pool-one"):
    recent = (NOW - timedelta(seconds=1)).isoformat()
    return {
        "pool_id": pool, "mint": "synthetic-token",
        "planned_buy_usd": notional, "pool_liquidity_usd": liquidity,
        "buy_quote_input_usd": notional, "buy_quote_output_token_raw": "12000000000",
        "sell_quote_input_token_raw": "12000000000",
        "sell_quote_output_usd": "98500", "roundtrip_network_fees_usd": "100",
        "buy_impact_pct": "1.2", "sell_impact_pct": "1.1",
        "buy_quote_at_utc": recent, "sell_quote_at_utc": recent,
    }


class TestCapitalCapacityGate(unittest.TestCase):
    def assess(self, rows, **kwargs):
        return evaluate({"allocations":rows}, now=NOW, **kwargs)

    def test_without_quote_data_fail_closed(self):
        self.assertEqual(evaluate({}, now=NOW)["status"], "NOT_READY")
        self.assertFalse(evaluate({}, now=NOW)["live_approved"])

    def test_ten_million_requires_large_distinct_pool_liquidity(self):
        result = evaluate({}, now=NOW)
        self.assertEqual(Decimal(result["required_deployment_usd"]), Decimal("8000000"))
        self.assertEqual(Decimal(result["conservative_minimum_distinct_pool_liquidity_usd"]), Decimal("400000000"))

    def test_million_order_into_small_pool_rejected(self):
        item = allocation("1000000", "10000000")
        result = self.assess([item], aum_usd="1250000")
        self.assertFalse(result["capacity_model_passed"])
        self.assertIn("exceeds 2%", " ".join(result["reasons"]))

    def test_reused_pool_not_double_counted(self):
        first=allocation(pool="same")
        second=allocation(pool="same")
        result=self.assess([first,second], aum_usd="250000")
        self.assertIn("pool reused", " ".join(result["reasons"]))
        self.assertFalse(result["capacity_model_passed"])

    def test_wrong_sell_units_rejected(self):
        item=allocation()
        item["sell_quote_input_token_raw"]="11999999999"
        result=self.assess([item],aum_usd="125000")
        self.assertIn("exact acquired raw tokens", " ".join(result["reasons"]))

    def test_stale_quote_rejected(self):
        item=allocation()
        item["buy_quote_at_utc"]=(NOW-timedelta(minutes=1)).isoformat()
        result=self.assess([item],aum_usd="125000")
        self.assertIn("stale", " ".join(result["reasons"]))

    def test_price_impact_rejected(self):
        item=allocation()
        item["sell_impact_pct"]="3"
        result=self.assess([item],aum_usd="125000")
        self.assertIn("price impact", " ".join(result["reasons"]))

    def test_bad_quote_size_rejected(self):
        item=allocation()
        item["buy_quote_input_usd"]="25000"
        result=self.assess([item],aum_usd="125000")
        self.assertIn("quoted buy size", " ".join(result["reasons"]))

    def test_huge_roundtrip_loss_rejected(self):
        item=allocation()
        item["sell_quote_output_usd"]="90000"
        result=self.assess([item],aum_usd="125000")
        self.assertIn("roundtrip loss", " ".join(result["reasons"]))

    def test_even_conservative_synthetic_quotes_never_enable_live(self):
        result=self.assess([allocation()],aum_usd="125000")
        self.assertTrue(result["capacity_model_passed"])
        self.assertFalse(result["live_approved"])
        self.assertFalse(result["profitability_validated"])
        self.assertEqual(result["status"],"NOT_READY")

    def test_nan_and_negative_cannot_be_passed_to_decimal(self):
        for raw in ("NaN", "Infinity", "-5", None, True):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                positive_decimal(raw,"some")
    def test_timezone_required(self):
        item=allocation()
        item["buy_quote_at_utc"]="2026-10-14T17:59:59"
        result=self.assess([item],aum_usd="125000")
        self.assertIn("timezone", " ".join(result["reasons"]))


if __name__=="__main__":
    unittest.main()
