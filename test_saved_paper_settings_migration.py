"""Fail-closed migration of previous PAPER DB settings, not a strategy change."""
import hashlib
import json
import unittest

from paper_runtime import verify_resume_settings


def old_settings():
    return {
        "EXECUTION_MODEL":"quote", "INITIAL_CASH_USD":"100",
        "TRADE_BUDGET_USD":"20", "MIN_LIQUIDITY_USD":"15000.00",
        "BASE_SLIPPAGE_PCT":"1.00", "MAX_DRAWDOWN_PCT":"20.0",
        "PAPER_NETWORK_FEE_LAMPORTS":"3105000",
        "QUOTE_MAX_AGE_MS":"3000",
    }


def envelope(settings):
    return {"settings":settings,
            "sha256":hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()}


class TestSavedPaperDatabaseCompatibility(unittest.TestCase):
    def test_prior_history_without_token_flag_migrates_only_to_false(self):
        prior=old_settings()
        current={**prior,"TOKEN2022_METADATA_PAPER_EXPERIMENT":"False"}
        self.assertEqual(verify_resume_settings(envelope(prior),current),
                         "LEGACY_TOKEN2022_DISABLED_MIGRATION")

    def test_legacy_mint_flag_can_never_be_silently_enabled(self):
        old=old_settings()
        current={**old,"TOKEN2022_METADATA_PAPER_EXPERIMENT":"True"}
        with self.assertRaisesRegex(ValueError,"Inställningar"):
            verify_resume_settings(envelope(old),current)

    def test_fee_changes_also_disallow_resume(self):
        old=old_settings()
        current={**old,"TOKEN2022_METADATA_PAPER_EXPERIMENT":"False"}
        current["PAPER_NETWORK_FEE_LAMPORTS"]="1"
        with self.assertRaisesRegex(ValueError,"Inställningar"):
            verify_resume_settings(envelope(old),current)

    def test_market_entry_parameter_changes_require_new_database(self):
        old=old_settings()
        current={**old,"TOKEN2022_METADATA_PAPER_EXPERIMENT":"False"}
        current["MIN_LIQUIDITY_USD"]="5"
        with self.assertRaisesRegex(ValueError,"Inställningar"):
            verify_resume_settings(envelope(old),current)

    def test_unaltered_new_format_is_accepted(self):
        current={**old_settings(),"TOKEN2022_METADATA_PAPER_EXPERIMENT":"False"}
        self.assertEqual(verify_resume_settings(envelope(current),current),"UNCHANGED")

    def test_sha256_tampering_is_rejected_even_if_settings_seem_equal(self):
        old=old_settings()
        saved=envelope(old)
        saved["sha256"]="0"*64
        with self.assertRaisesRegex(ValueError,"skadats"):
            verify_resume_settings(saved,{**old,"TOKEN2022_METADATA_PAPER_EXPERIMENT":"False"})

    def test_missing_legacy_hash_never_migrates(self):
        with self.assertRaisesRegex(ValueError,"hash"):
            verify_resume_settings({"settings":old_settings()},
                                   {**old_settings(),"TOKEN2022_METADATA_PAPER_EXPERIMENT":"False"})


if __name__=="__main__":
    unittest.main()
