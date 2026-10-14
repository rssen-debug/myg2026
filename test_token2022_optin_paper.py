"""Strict opt-in PAPER-only Token-2022 regression tests; absolutely no real orders."""
import unittest
from unittest.mock import patch

from godlike2026 import RugCheckValidator
from token2022_onchain_inspector import TOKEN_2022_PROGRAM, attest_metadata_only_for_paper, inspect_mints

MINT="DezXAZ8z7PnrnRJjz3wXBoRgixCaKGiyVFs2RmmJ6Bq"
POOL="synthetic-test-pool"
CLASSIC=RugCheckValidator.CLASSIC_TOKEN_PROGRAM


def full_report(program=TOKEN_2022_PROGRAM):
    return {
        "mint":MINT,"rugged":False,
        "tokenProgram":program,
        "mintAuthority":None,"freezeAuthority":None,
        "token":{"decimals":6,"mintAuthority":None,"freezeAuthority":None},
        "score":0,"risks":[],
        "markets":[{
            "pubkey":POOL,"marketType":"pump_fun_amm",
            "lp":{"baseUSD":"120000","quoteUSD":"100000","lpLockedPct":"99"}}],
        "topHolders":[
            {"address":"holder1","pct":5},
            {"address":"holder2","pct":6}],
    }


def attestation():
    return {
        "mint":MINT,"token_program":TOKEN_2022_PROGRAM,
        "extensions":["MetadataPointer","TokenMetadata"],
        "decimals":6,"onchain_slot":12345,
        "purpose":"PAPER_MINT_INSPECTION_ONLY",
        "rug_safety_verified":False,"sellability_verified":False,
        "live_approved":False,
    }


class FakeLimiter:
    def wait(self):pass


class Token2022PaperGates(unittest.TestCase):
    def validator(self, enabled):
        return RugCheckValidator(limiter=FakeLimiter(),
            config={"TOKEN2022_METADATA_PAPER_EXPERIMENT":enabled})

    def test_disabled_by_default_even_if_metadata_only(self):
        approved,reason,meta=self.validator(False)._validate_report(
            full_report(),POOL,expected_mint=MINT,token2022_attestation=attestation())
        self.assertFalse(approved)
        self.assertIn("blockeras",reason)

    def test_optin_without_independent_chain_proof_still_blocked(self):
        approved,reason,_=self.validator(True)._validate_report(
            full_report(),POOL,expected_mint=MINT)
        self.assertFalse(approved)
        self.assertIn("blockeras",reason)

    def test_only_structurally_attested_metadata_only_token_may_pass_rest_of_rugcheck(self):
        approved,reason,meta=self.validator(True)._validate_report(
            full_report(),POOL,expected_mint=MINT,token2022_attestation=attestation())
        self.assertTrue(approved)
        self.assertEqual(meta["paper_liquidity_cap_usd"],200000)
        self.assertEqual(meta["token2022_paper_attestation"]["purpose"],"PAPER_MINT_INSPECTION_ONLY")
        self.assertIn("ingen säljbarhetsgaranti",reason)

    def test_live_approval_or_false_attestation_cannot_be_taken_as_rug_proof(self):
        for corrupt in (
          {"mint":"wrong"},
          {"purpose":"LIVE_APPROVED"},
          {"extensions":["TransferFeeConfig"]},
          {"token_program":"fake"},
          {"onchain_slot":None},
          {"rug_safety_verified":True},
        ):
            with self.subTest(corrupt=corrupt):
                bad={**attestation(),**corrupt}
                ok,_,_=self.validator(True)._validate_report(
                    full_report(),POOL,expected_mint=MINT,token2022_attestation=bad)
                self.assertFalse(ok)

    def test_existing_token_rug_hard_blocks_still_apply(self):
        cases=[
          ("rugged", lambda r:r.update(rugged=True)),
          ("mint", lambda r:r.update(mintAuthority="funded-authority")),
          ("fee", lambda r:r.update(transferFee={"pct":1})),
          ("program", lambda r:r.update(tokenProgram="unsupported-token-program")),
          ("pool", lambda r:r["markets"][0].update(pubkey="another-pool")),
          ("lp", lambda r:r["markets"][0]["lp"].update(lpLockedPct="1")),
          ("holders", lambda r:r["topHolders"][0].update(pct=25)),
          ("score", lambda r:r.update(score=900)),
          ("danger", lambda r:r.update(risks=[{"name":"fraud","level":"critical"}])),
          ("ext", lambda r:r.update(extensions={"TransferHook":True})),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                data=full_report()
                mutate(data)
                result,reason,_=self.validator(True)._validate_report(
                    data,POOL,expected_mint=MINT,token2022_attestation=attestation())
                self.assertFalse(result,reason)

    def test_actual_validation_invokes_independent_attestation_before_rugcheck(self):
        validator=self.validator(True)
        with patch("godlike2026.get_public_json",return_value=full_report()) as get, \
             patch("token2022_onchain_inspector.attest_metadata_only_for_paper",
                   return_value=(attestation(),"read-only checked")) as att:
            ok,_,data=validator.validate(MINT,POOL)
        self.assertTrue(ok)
        self.assertEqual(att.call_args[0],(MINT,6))
        self.assertEqual(get.call_count,1)
        self.assertIn("token2022_paper_attestation",data)

    def test_rpc_failure_never_leads_to_a_report_pass(self):
        validator=self.validator(True)
        with patch("godlike2026.get_public_json",return_value=full_report()), \
             patch("token2022_onchain_inspector.attest_metadata_only_for_paper",
                   return_value=(None,"RPC 429")):
            ok,reason,_=validator.validate(MINT,POOL)
        self.assertFalse(ok)
        self.assertIn("RPC 429",reason)

    def test_legacy_classic_spl_success_not_mutated_by_research_flag(self):
        classic=full_report(CLASSIC)
        validator=self.validator(True)
        with patch("godlike2026.get_public_json",return_value=classic), \
             patch("token2022_onchain_inspector.attest_metadata_only_for_paper") as att:
            accepted,_,meta=validator.validate(MINT,POOL)
        self.assertTrue(accepted)
        att.assert_not_called()
        self.assertNotIn("token2022_paper_attestation",meta)


if __name__=="__main__":
    unittest.main()
