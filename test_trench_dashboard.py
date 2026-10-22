"""PAPER console must show trench telemetry and moonbag without claiming safety."""
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from console_dashboard import frame, latest_restore_event
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


class TestRestoreEventHUD(unittest.TestCase):
    def test_missing_restore_event_is_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(latest_restore_event(Path(folder) / "missing.jsonl"))

    def test_does_not_mislabel_previous_run_as_current(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "forward_pulse_runs.jsonl"
            log.write_text(json.dumps({"status": "RESTORED", "artifact_id": 7,
                                       "run_id": "old-run"}) + "\n", encoding="utf-8")
            self.assertIsNone(latest_restore_event(log, run_id="new-run"))
            self.assertEqual(latest_restore_event(log, run_id="old-run")["artifact_id"], 7)

    def test_invalid_restored_record_cannot_claim_success(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "forward_pulse_runs.jsonl"
            log.write_text("\n".join(json.dumps(item) for item in [
                {"status": "RESTORED", "artifact_id": 14, "run_id": "42"},
                {"status": "RESTORED", "artifact_id": "forged", "run_id": "42"},
                {"status": "RESTORED", "artifact_id": 0, "run_id": "42"},
            ]) + "\n", encoding="utf-8")
            self.assertEqual(latest_restore_event(log, run_id="42")["artifact_id"], 14)
            log.write_text(json.dumps({"status": "RESTORED", "artifact_id": True,
                                       "run_id": "42"}) + "\n", encoding="utf-8")
            self.assertIsNone(latest_restore_event(log, run_id="42"))

    def test_local_dashboard_never_trusts_historical_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "forward_pulse_runs.jsonl"
            log.write_text(json.dumps({"status": "RESTORED", "artifact_id": 999,
                                       "run_id": "previous"}) + "\n", encoding="utf-8")
            with patch.dict("os.environ", {"GITHUB_RUN_ID": ""}), \
                 patch("console_dashboard.latest_restore_event", return_value={
                     "status": "RESTORED", "artifact_id": 999}) as reader:
                output = frame(example(), width=128, height=80, color=False)
            reader.assert_not_called()
            self.assertIn("RESTORE LOCAL", output)
            self.assertNotIn("RESTORE VERIFIED", output)

    def test_first_run_never_claims_verified_restore(self):
        with patch("console_dashboard.latest_restore_event",
                   return_value={"status": "FIRST_RUN", "artifact_id": None}):
            output = frame(example(), width=128, height=80, color=False)
        self.assertIn("RESTORE FIRST RUN", output)
        self.assertNotIn("RESTORE VERIFIED", output)

    def test_restored_state_is_distinct_from_first_run(self):
        with patch("console_dashboard.latest_restore_event",
                   return_value={"status": "RESTORED", "artifact_id": 42}):
            output = frame(example(), width=128, height=80, color=False)
        self.assertIn("RESTORE VERIFIED", output)
        self.assertIn("artifact 42", output)

    def test_reads_latest_real_restore_event(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "forward_pulse_runs.jsonl"
            log.write_text("\n".join([
                json.dumps({"status": "RESTORED", "artifact_id": 7, "utc": "2026-10-21T12:00:00Z"}),
                json.dumps({"status": "SKIP_REPORT_ONLY_ARTIFACT", "artifact_id": 9}),
                json.dumps({"status": "RESTORED", "artifact_id": 11, "utc": "2026-10-22T12:00:00Z"}),
            ]) + "\n", encoding="utf-8")
            self.assertEqual(latest_restore_event(log)["artifact_id"], 11)

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
        self.assertIn("[AUDIT] ZIP [SHA256/FAIL-CLOSED] · DB [READ-ONLY-CHECK] · PAGES [AUTO] · IDS [UNIQUE]", output)
        self.assertIn("API [EXACT-URL] · AUDIT [SKIPS-LOGGED] · DB [512MiB]  |  PAPER ONLY", output)
        self.assertTrue("RESTORE LOCAL" in output or "RESTORE UNVERIFIED" in output)
        self.assertIn("PAPER ONLY", output)
        self.assertIn("Färska köp- och säljofferter", output)

    def test_without_trench_metrics_hud_does_not_invent_evidence(self):
        data=example()
        data.pop("trench_labels")
        data.pop("trench_flags")
        self.assertNotIn("TRENCH-OBSERVATIONER",render_dashboard(data))
        self.assertNotIn("TRENCH ENDAST MARKNADSOBS",frame(data,width=128,height=80,color=False))


if __name__=="__main__":
    unittest.main()
