"""PAPER console must show trench telemetry and moonbag without claiming safety."""
import unittest

from console_dashboard import frame
from paper_runtime import render_dashboard


def example():
    return {
        "mode":"PAPER_REAL_MARKET","status":"RUNNING",
        "stage":"Evaluation","updated_at":"2026-10-14T19:00:00+00:00",
        "pid":123,"trade_budget_usd":"20","candidates":20,"unique_evaluated":10,
        "cycle_evaluated":5,"cycle_seconds":2.2,
        "report":{"initial_cash":"100","cash":"70","net_pnl":"-1.00",
                  "realized_pnl":"0","unrealized_pnl":"-1.00",
                  "total_port":"99","fees":"0","total_trades":0},
        "trench_labels":{"ELEVATED_OBSERVABLE_RISK":4,"INSUFFICIENT_DATA":2},
        "trench_flags":{"PAID_BOOSTS_ARE_NOT_ORGANIC_VALIDATION":4},
        "positions":[{"symbol":"MEME","initial_raw":"100","remaining_raw":"25",
                      "stop_loss_price":"1.90","tp1_executed":1,"tp2_executed":1}],
        "events":[],"quotes":[],"discovery":{},
    }


class TestTrenchMoonbagHUD(unittest.TestCase):
    def test_text_dashboard_marks_trench_as_unverified_observations(self):
        content=render_dashboard(example())
        self.assertIn("TRENCH-OBSERVATIONER",content)
        self.assertIn("EJ wallet/dev-verifiering",content)
        self.assertIn("ELEVATED_OBSERVABLE_RISK",content)
        self.assertIn("TRENCH-FLAGGOR",content)
        self.assertIn("TP2 1",content)

    def test_compact_console_exposes_moonbag_and_unverified_trench(self):
        output=frame(example(),width=128,height=80,color=False)
        self.assertIn("TRENCH ENDAST MARKNADSOBS",output)
        self.assertIn("ingen verifierad whale/dev",output)
        self.assertIn("MOONBAG",output)

    def test_dashboard_persistently_labels_paper_evidence(self):
        output = frame(example(), width=128, height=80, color=False)
        self.assertIn("EVIDENSKONTROLL  PAPER MODELL / INTE LIVE-PnL", output)
        self.assertIn("KAPITALSKYDD", output)
        self.assertIn("FORWARD-DATA", output)
        self.assertIn("HISTORIKSKYDD", output)
        self.assertIn("ZIP: metadata + faktisk datamängd", output)
        self.assertIn("Färska köp- och säljofferter", output)

    def test_without_trench_metrics_hud_does_not_invent_evidence(self):
        data=example()
        data.pop("trench_labels")
        data.pop("trench_flags")
        self.assertNotIn("TRENCH-OBSERVATIONER",render_dashboard(data))
        self.assertNotIn("TRENCH ENDAST MARKNADSOBS",frame(data,width=128,height=80,color=False))


if __name__=="__main__":
    unittest.main()
