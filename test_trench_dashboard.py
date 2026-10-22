"""PAPER console must show trench telemetry and moonbag without claiming safety."""
import unittest
import json
import io
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from console_dashboard import ConsoleDashboard, frame, latest_restore_event, percentage, metric_health, missing_metrics, valuation_freshness, fit_ellipsis
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


class TestMissingReportHUD(unittest.TestCase):
    def test_missing_or_malformed_report_is_visible_and_never_crashes(self):
        for value in (None, [], "broken", 123):
            with self.subTest(value=value):
                data = example()
                data["report"] = value
                display = frame(data, width=118, height=30, color=False)
                self.assertIn("REPORT MISSING", display)
                self.assertIn("NETTO OKÄNT", display)
                output = io.StringIO()
                dashboard = ConsoleDashboard(stream=output)
                dashboard.draw(data)
                self.assertIn("QUALITY ALERT", output.getvalue())
                self.assertIn("REPORT MISSING", output.getvalue())

class TestEllipsisTrailingWhitespace(unittest.TestCase):
    def test_trailing_spaces_are_not_false_truncation(self):
        self.assertEqual(fit_ellipsis("OK  ", 6), "OK    ")
        self.assertEqual(fit_ellipsis("OK  ", 4), "OK  ")
        self.assertEqual(fit_ellipsis("OVERFLOW  ", 5).rstrip(), "OVER…")

class TestNoFalseVerificationLabel(unittest.TestCase):
    def test_finite_metrics_are_not_described_as_verified_trading(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["report_as_of"] = datetime.now(timezone.utc).isoformat()
        data["report"].update(win_rate=50, max_drawdown_pct=1, profit_factor=1.2)
        dashboard.draw(data)
        line = output.getvalue()
        self.assertIn("FINITE METRICS ONLY", line)
        self.assertNotIn("VERIFIED FIELDS", line)
        self.assertIn("PAPER", line)

class TestLiveLockedTailHUD(unittest.TestCase):
    def test_safety_label_survives_truncation(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["status"] = "X" * 500
        data["report"]["net_pnl"] = "NaN"
        dashboard.draw(data)
        line = output.getvalue().rstrip()
        self.assertTrue(line.endswith("LIVE LOCKED"))
        self.assertIn("[!] QUALITY ALERT", line)
        self.assertLessEqual(len(line), 220)

class TestPriorityLogOrder(unittest.TestCase):
    def test_data_quality_precedes_portfolio_in_bounded_log(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["report"]["net_pnl"] = "NaN"
        data["report"]["win_rate"] = None
        dashboard.draw(data)
        line = output.getvalue()
        self.assertLess(line.index("DATA MISSING"), line.index("PnL"))
        self.assertLess(line.index("INVALID 1"), line.index("CASH"))
        self.assertIn("net_pnl", line)
        self.assertLessEqual(len(line.rstrip()), 220)

class TestAlertPriorityHUD(unittest.TestCase):
    def test_quality_warning_survives_long_truncated_plain_line(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["status"] = "R" * 200
        data["report"]["net_pnl"] = "NaN"
        dashboard.draw(data)
        line = output.getvalue()
        self.assertIn("[!] QUALITY ALERT", line)
        self.assertIn("DATA MISSING", line)
        self.assertIn("INVALID 1", line)
        self.assertLessEqual(len(line), 221)

class TestEllipsisHUD(unittest.TestCase):
    def test_truncated_stage_has_visible_marker(self):
        data = example()
        data["stage"] = "VERY LONG STAGE " * 40
        output = frame(data, width=60, height=25, color=False)
        self.assertIn("…", output)
        self.assertTrue(all(len(line) <= 60 for line in output.splitlines()))

    def test_ellipsis_width_is_bounded_with_wide_characters(self):
        self.assertEqual(fit_ellipsis("abcdef", 4), "abc…")
        self.assertEqual(fit_ellipsis("abc", 4), "abc ")
        self.assertEqual(fit_ellipsis("漢字漢字", 5), "漢字…")
        self.assertEqual(fit_ellipsis("abc", 0), "")

class TestQualitySeverityMarkers(unittest.TestCase):
    def test_plain_status_markers_reflect_evidence_not_trading_permission(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["report_as_of"] = datetime.now(timezone.utc).isoformat()
        dashboard.draw(data)
        self.assertIn("[?] QUALITY PARTIAL", output.getvalue())
        data["report"]["win_rate"] = 50
        data["report"]["max_drawdown_pct"] = 1
        data["report"]["profit_factor"] = 1.2
        dashboard.draw(data)
        self.assertIn("[+] QUALITY OK", output.getvalue().splitlines()[-1])
        data["report"]["net_pnl"] = "NaN"
        dashboard.draw(data)
        self.assertIn("[!] QUALITY ALERT", output.getvalue().splitlines()[-1])
        self.assertIn("ENTRIES ACTIVE", output.getvalue().splitlines()[-1])

class TestSafePlainLogFields(unittest.TestCase):
    def test_untrusted_log_fields_are_single_line_and_bounded(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["status"] = "RUNNING" + chr(27) + "[31m" + "X" * 200 + "\nFAKE ALERT"
        data["updated_at"] = "NOW" + "\nFORGED" + "X" * 100
        data["candidates"] = "5" + "\nFAKE SUCCESS"
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertNotIn(chr(27), output.getvalue())
        self.assertNotIn("FAKE ALERT", output.getvalue())
        self.assertNotIn("FAKE SUCCESS", output.getvalue())
        self.assertIn("QUALITY ALERT", output.getvalue())
        self.assertLess(len(output.getvalue()), 230)

class TestCompactPositionCount(unittest.TestCase):
    def test_position_count_changes_emit_bounded_status(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        dashboard.draw(data)
        self.assertIn("POSITIONS 1", output.getvalue())
        data["positions"] = []
        dashboard.draw(data)
        self.assertIn("POSITIONS 0", output.getvalue().splitlines()[-1])
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 2)

class TestCandidateCountLogRefresh(unittest.TestCase):
    def test_discovery_count_change_updates_plain_status(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        dashboard.draw(data)
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        data["candidates"] = 21
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertIn("21 tok", output.getvalue().splitlines()[-1])

class TestQualityReasonLabel(unittest.TestCase):
    def test_plain_quality_label_explains_warning_cause(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["report_as_of"] = "bad-timestamp"
        dashboard.draw(data)
        self.assertIn("QUALITY ALERT | DATA INVALID", output.getvalue())
        self.assertIn("VALUATION INVALID", output.getvalue())
        data["report"]["net_pnl"] = "NaN"
        dashboard.draw(data)
        self.assertIn("QUALITY ALERT", output.getvalue().splitlines()[-1])
        self.assertIn("INVALID DATA", output.getvalue().splitlines()[-1])
        data["report"]["net_pnl"] = "-1.00"
        data["report_as_of"] = datetime.now(timezone.utc).isoformat()
        dashboard.draw(data)
        self.assertIn("QUALITY PARTIAL", output.getvalue().splitlines()[-1])
        self.assertIn("MISSING FIELDS", output.getvalue().splitlines()[-1])

class TestPausedEntryBanner(unittest.TestCase):
    def test_paused_entries_prominent_but_positions_not_claimed_closed(self):
        data = example()
        data["entries_paused"] = True
        output = frame(data, width=118, height=14, color=False)
        self.assertIn("ENTRY PAUSE ACTIVE", output)
        self.assertIn("ÖPPNA POSITIONER KAN FINNAS", output)
        data["entries_paused"] = False
        self.assertNotIn("ENTRY PAUSE ACTIVE", frame(data, width=118, height=14, color=False))

class TestCompactPausedStatus(unittest.TestCase):
    def test_paused_entries_are_visible_at_start_of_plain_log(self):
        import io
        from console_dashboard import ConsoleDashboard
        data = example()
        data["entries_paused"] = True
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        dashboard.draw(data)
        self.assertIn("[PAUSED] QUALITY", output.getvalue())
        self.assertIn("ENTRIES PAUSED", output.getvalue())
        self.assertLessEqual(len(output.getvalue().splitlines()[0]), 220)

    def test_missing_positions_does_not_crash_plain_log(self):
        import io
        from console_dashboard import ConsoleDashboard
        data = example()
        data["positions"] = None
        output = io.StringIO()
        ConsoleDashboard(stream=output).draw(data)
        self.assertIn("POSITIONS 0", output.getvalue())

class TestAuditTimestampVisibility(unittest.TestCase):
    def test_plain_log_includes_time_not_only_date(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["updated_at"] = "2026-10-22T11:43:27+00:00"
        dashboard.draw(data)
        self.assertIn("2026-10-22T11:43:27 PAPER", output.getvalue())
        self.assertIn("LIVE LOCKED", output.getvalue())

class TestHealthCounterHUD(unittest.TestCase):
    def test_counter_matches_finite_metric_count(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        dashboard.draw(data)
        invalid = metric_health(data["report"])
        missing = missing_metrics(data["report"])
        self.assertIn(f"HEALTH {10-len(invalid)-len(missing)}/10", output.getvalue())
        data["report"]["cash"] = "NaN"
        dashboard.draw(data)
        self.assertIn(f"HEALTH {10-len(metric_health(data['report']))-len(missing_metrics(data['report']))}/10",
                      output.getvalue().splitlines()[-1])
        self.assertEqual(len(output.getvalue().splitlines()), 2)

class TestNonInteractiveQualityStream(unittest.TestCase):
    def test_quality_change_emits_new_line_without_duplicate_spam(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["report_as_of"] = datetime.now(timezone.utc).isoformat()
        dashboard.draw(data)
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("DATA FRESH", output.getvalue())
        data["report"]["net_pnl"] = "NaN"
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertIn("INVALID 1", output.getvalue().splitlines()[-1])
        data["report_as_of"] = "not-a-date"
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertIn("DATA INVALID", output.getvalue().splitlines()[-1])

    def test_cash_and_equity_changes_emit_status_without_duplicate_spam(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        dashboard.draw(data)
        self.assertIn("CASH $70.00", output.getvalue())
        self.assertIn("EQUITY $99.00", output.getvalue())
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        data["report"]["cash"] = "71"
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertIn("CASH $71.00", output.getvalue().splitlines()[-1])
        data["report"]["total_port"] = "100"
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertIn("EQUITY $100.00", output.getvalue().splitlines()[-1])

    def test_invalid_value_change_and_pause_state_emit_status(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["report"]["fees"] = "NaN"
        dashboard.draw(data)
        self.assertIn("ENTRIES ACTIVE", output.getvalue())
        data["report"]["fees"] = "Infinity"
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertNotIn("Infinity", output.getvalue())
        data["entries_paused"] = True
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertIn("ENTRIES PAUSED", output.getvalue().splitlines()[-1])
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 3)

    def test_log_names_fields_and_limits_output(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["report"]["net_pnl"] = "NaN"
        data["report"]["cash"] = "Infinity"
        data["report"]["fees"] = "broken"
        data["report"]["initial_cash"] = "NaN"
        data["report"]["win_rate"] = None
        dashboard.draw(data)
        line = output.getvalue()
        self.assertIn("INVALID 4", line)
        self.assertIn("BAD [net_pnl,cash,fees]", line)
        self.assertIn("MISSING", line)
        self.assertIn("MISSING 3", line)
        self.assertIn("…", line)
        self.assertIn("…", line)  # bounded detail truncates before full field name
        self.assertNotIn("initial_cash]", line)
        dashboard.draw(data)
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_plain_stream_never_contains_external_escape_sequences(self):
        output = io.StringIO()
        dashboard = ConsoleDashboard(stream=output)
        data = example()
        data["status"] = chr(27) + "[31mRUNNING"
        dashboard.draw(data)
        self.assertNotIn(chr(27), output.getvalue())
        self.assertIn("PAPER", output.getvalue())

class TestCompactStatusHUD(unittest.TestCase):
    def test_critical_quality_status_visible_in_short_terminal(self):
        data = example()
        data["report"]["net_pnl"] = "NaN"
        data["report"]["win_rate"] = None
        data["report_as_of"] = "not-a-timestamp"
        output = frame(data, width=90, height=12, color=False)
        self.assertIn("STATUS  DATA INVALID", output)
        self.assertIn("FEL 1", output)
        self.assertIn("SAKNAS", output)
        self.assertIn("PAPER ONLY", output)

    def test_short_terminal_summary_shows_freshness(self):
        data = example()
        data["report_as_of"] = datetime.now(timezone.utc).isoformat()
        output = frame(data, width=90, height=12, color=False)
        self.assertIn("STATUS  DATA FRESH", output)

class TestValuationFreshnessHUD(unittest.TestCase):
    def test_timestamp_freshness_fail_closed(self):
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(valuation_freshness(None, now), "MISSING")
        self.assertEqual(valuation_freshness("not-a-date", now), "INVALID")
        self.assertEqual(valuation_freshness("2026-10-22T12:00:00", now), "INVALID")
        self.assertEqual(valuation_freshness("2026-10-22T12:00:10Z", now), "FUTURE")
        self.assertEqual(valuation_freshness("2026-10-22T11:59:00Z", now), "STALE")
        self.assertEqual(valuation_freshness("2026-10-22T11:59:50Z", now), "FRESH")

    def test_bad_timestamp_never_crashes_or_appears_fresh(self):
        for bad in (None, "invalid", "2026-10-22T12:00:00"):
            with self.subTest(bad=bad):
                data = example()
                data["report_as_of"] = bad
                output = frame(data, width=128, height=80, color=False)
                self.assertIn("DATA EJ VERIFIERAD", output)
                self.assertNotIn("VÄRDERING FÄRSK", output)

class TestMetricLegendHUD(unittest.TestCase):
    def test_missing_metrics_are_not_presented_as_zero(self):
        data = example()
        data["report"]["net_pnl"] = None
        data["report"]["profit_factor"] = None
        output = frame(data, width=128, height=80, color=False)
        self.assertIn("METRIC LEGEND", output)
        self.assertIn("INTE NOLL", output)
        self.assertIn("NETTO OKÄNT", output)
        self.assertIn("PF --", output)

class TestMixedMetricQualityHUD(unittest.TestCase):
    def test_missing_values_still_visible_when_other_metrics_invalid(self):
        data = example()
        data["report"]["net_pnl"] = "NaN"
        data["report"]["win_rate"] = None
        output = frame(data, width=128, height=80, color=False)
        self.assertIn("INVALID METRICS: net_pnl", output)
        self.assertIn("MISSING: win_rate", output)
        self.assertNotIn("ALL TRACKED METRICS FINITE", output)

class TestMissingMetricHUD(unittest.TestCase):
    def test_missing_and_invalid_metrics_are_distinct(self):
        data = example()
        data["report"]["net_pnl"] = None
        data["report"]["win_rate"] = None
        self.assertIn("net_pnl", missing_metrics(data["report"]))
        output = frame(data, width=128, height=80, color=False)
        self.assertIn("DATA QUALITY  ", output)
        self.assertIn("MISSING:", output)
        self.assertNotIn("INVALID METRICS:", output)

class TestMetricQualityHUD(unittest.TestCase):
    def test_profit_factor_never_renders_raw_nonfinite_value(self):
        for value in ("NaN", "Infinity", "garbage"):
            with self.subTest(value=value):
                data = example()
                data["report"]["profit_factor"] = value
                output = frame(data, width=128, height=80, color=False)
                self.assertIn("PF --", output)
                self.assertIn("INVALID METRICS:", output)
        data = example()
        data["report"]["profit_factor"] = 1.2345
        self.assertIn("PF 1.23", frame(data, width=128, height=80, color=False))

    def test_invalid_cash_fees_and_profit_factor_are_visible(self):
        data = example()
        for field in ("cash", "fees", "profit_factor"):
            data["report"][field] = "Infinity"
        self.assertEqual(metric_health(data["report"]), ["cash", "fees", "profit_factor"])
        output = frame(data, width=128, height=80, color=False)
        self.assertIn("INVALID METRICS: cash, fees, profit_factor", output)

    def test_dynamic_data_quality_indicator(self):
        healthy = frame(example(), width=128, height=80, color=False)
        self.assertIn("DATA QUALITY  ", healthy)
        self.assertTrue("MISSING:" in healthy or "ALL TRACKED METRICS FINITE" in healthy)
        bad = example()
        bad["report"]["net_pnl"] = "NaN"
        bad["report"]["win_rate"] = "broken"
        self.assertEqual(metric_health(bad["report"]), ["net_pnl", "win_rate"])
        output = frame(bad, width=128, height=80, color=False)
        self.assertIn("DATA QUALITY  INVALID METRICS: net_pnl, win_rate", output)

class TestInvalidMetricsHUD(unittest.TestCase):
    def test_nonfinite_and_malformed_metrics_never_crash_dashboard(self):
        for bad in ("NaN", "Infinity", "not-a-number"):
            with self.subTest(value=bad):
                data = example()
                data["report"]["net_pnl"] = bad
                data["report"]["win_rate"] = bad
                data["report"]["max_drawdown_pct"] = bad
                output = frame(data, width=128, height=80, color=False)
                self.assertIn("NETTO OKÄNT", output)
                self.assertIn("Win --", output)
                self.assertEqual(percentage(bad), "--")

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

    def test_large_log_tail_discards_partial_jsonl_line(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "forward_pulse_runs.jsonl"
            log.write_text(json.dumps({"status": "RESTORED", "artifact_id": 17,
                                       "run_id": "old", "detail": "x" * 9000}) +
                           "\n" + json.dumps({"status": "RESTORED", "artifact_id": 19,
                                               "run_id": "new"}) + "\n",
                           encoding="utf-8")
            self.assertIsNone(latest_restore_event(log, run_id="old"))
            self.assertEqual(latest_restore_event(log, run_id="new")["artifact_id"], 19)

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
        self.assertIn("EJ KONTINUERLIG HISTORIK", output)

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
