"""Exact RugCheck metadata-only schema on opt-in Token2022 PAPER must remain fail-closed."""
import copy
import unittest

from rug2022_report_schema import verified_metadata_only_report_shape
from test_token2022_optin_paper import full_report,attestation,Token2022PaperGates,MINT,POOL

SYS="11111111111111111111111111111111"


def report_extensions():
    return {
        "nonTransferable":False,
        "transferFeeConfig":None,
        "defaultAccountState":None,
        "permanentDelegate":None,
        "metadataPointer":{"authority":None,"metadataAddress":MINT},
        "groupPointer":None,"groupMemberPointer":None,
        "tokenMetadata":{"authority":None,"mint":MINT,
                         "name":"Mock Coin","symbol":"MOCK",
                         "uri":"https://example.invalid/metadata.json",
                         "additionalMetadata":{}},
        "mintCloseAuthority":None,"confidentialTransferMint":None,
        "interestBearingConfig":None,"transferHook":None,
        "confidentialTransferFeeConfig":None,"tokenGroup":None,
        "tokenGroupMember":None,"scaledUiAmountConfig":None,
        "pausableConfig":None,
    }


def valid_report():
    r=full_report()
    r["token_extensions"]=report_extensions()
    r["transferFee"]={"pct":0,"maxAmount":0,"authority":SYS}
    return r


class TestStrictMetadataReport(unittest.TestCase):
    def setUp(self):
        self.validator=Token2022PaperGates().validator(True)
    def passed(self,r):
        return self.validator._validate_report(r,POOL,expected_mint=MINT,
                                               token2022_attestation=attestation())

    def test_exact_public_metadata_shape_allowed_only_for_attested_paper(self):
        r=valid_report()
        self.assertTrue(verified_metadata_only_report_shape(r["token_extensions"],MINT))
        approved,reason,meta=self.passed(r)
        self.assertTrue(approved,reason)
        self.assertIn("token2022_paper_attestation",meta)

    def test_default_mode_must_still_block_report(self):
        validator=Token2022PaperGates().validator(False)
        ok,reason,_=validator._validate_report(
            valid_report(),POOL,expected_mint=MINT,
            token2022_attestation=attestation())
        self.assertFalse(ok)
        self.assertIn("blockeras",reason)

    def test_no_chain_proof_even_with_exact_report_shape_is_blocked(self):
        approved,_,_=self.validator._validate_report(
            valid_report(),POOL,expected_mint=MINT)
        self.assertFalse(approved)

    def test_evil_or_unknown_extension_fields_are_rejected(self):
        samples=[
            ("nonTransferable",lambda x:x.update(nonTransferable=True)),
            ("transferHook",lambda x:x.update(transferHook={"programId":"malicious"})),
            ("transferFeeConfig",lambda x:x.update(transferFeeConfig={"olderTransferFee":{}})),
            ("permanentDelegate",lambda x:x.update(permanentDelegate="some wallet")),
            ("defaultAccountState",lambda x:x.update(defaultAccountState={"state":"frozen"})),
            ("unknown",lambda x:x.update(futureUnknownExtension={})),
            ("metadataAuthority",lambda x:x["tokenMetadata"].update(authority="mutable-authority")),
            ("metadataMint",lambda x:x["tokenMetadata"].update(mint="wrong")),
            ("metadataAddon",lambda x:x["tokenMetadata"].update(additionalMetadata={"fee":"100%"})),
            ("pointer",lambda x:x["metadataPointer"].update(metadataAddress="another-mint")),
            ("pointerAuthority",lambda x:x["metadataPointer"].update(authority="mutable")),
            ("feeUnknownKey",lambda x:x["tokenMetadata"].update(transferFee=2)),
        ]
        for name,corrupt in samples:
            with self.subTest(case=name):
                row=valid_report()
                corrupt(row["token_extensions"])
                self.assertFalse(verified_metadata_only_report_shape(row["token_extensions"],MINT))
                ok,reason,_=self.passed(row)
                self.assertFalse(ok,reason)

    def test_any_nonzero_transfer_fee_is_blocked(self):
        for key,val in (("pct",0.01),("maxAmount",100),("authority","fee-change-wallet")):
            with self.subTest(key=key):
                r=valid_report()
                r["transferFee"][key]=val
                ok,_,_=self.passed(r)
                self.assertFalse(ok)

    def test_unknown_extensions_in_token_not_whitelisted(self):
        r=valid_report()
        r["token"]["extensions"]={"transferHook":{}}
        ok,_,_=self.passed(r)
        self.assertFalse(ok)

    def test_rug_or_LP_holder_checks_not_relaxed(self):
        for name,apply in (
                ("rugged",lambda d:d.update(rugged=True)),
                ("holders",lambda d:d["topHolders"][0].update(pct=90)),
                ("market",lambda d:d["markets"][0].update(marketType="unsupported")),
                ("risk",lambda d:d.update(risks=[{"level":"danger","name":"scam"}])),
                ("lp",lambda d:d["markets"][0]["lp"].update(lpLockedPct="2")),
        ):
            with self.subTest(name=name):
                data=valid_report()
                apply(data)
                ok,reason,_=self.passed(data)
                self.assertFalse(ok,reason)


if __name__=="__main__":
    unittest.main()
