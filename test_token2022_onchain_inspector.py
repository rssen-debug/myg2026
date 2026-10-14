"""Adversarial offline Token-2022 mint/TLV and RPC evidence tests."""
import base64
from pathlib import Path
import struct
import unittest

from token2022_onchain_inspector import (
    CLASSIC_PROGRAM, TOKEN_2022_PROGRAM,
    account_report, decode_mint, inspect_mints,
)
MINT_A="DezXAZ8z7PnrnRJjz3wXBoRgixCaKGiyVFs2RmmJ6Bq"
MINT_B="JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"


def mint_base(mint_enabled=False,freeze_enabled=False):
    b=bytearray(82)
    b[0:4]=struct.pack("<I",int(mint_enabled))
    b[36:44]=struct.pack("<Q",1000000000)
    b[44]=6
    b[45]=1
    b[46:50]=struct.pack("<I",int(freeze_enabled))
    return bytes(b)


def token2022(*extensions, **base_kwargs):
    b=mint_base(**base_kwargs)+bytes(83)+bytes([1])
    for ext_type,data in extensions:
        b+=struct.pack("<HH",ext_type,len(data))+data
    return b


def account(data,owner=TOKEN_2022_PROGRAM):
    return {"owner":owner,"data":[base64.b64encode(data).decode(),"base64"]}


class FakeRpc:
    def __init__(self,records):
        self.records=records
        self.calls=[]
    def call(self,method,params):
        self.calls.append((method,params))
        return {"context":{"slot":12345},
                "value":self.records}


class TestToken2022MintEvidence(unittest.TestCase):
    def test_classic_token_mint_is_parsed_but_never_trade_approved(self):
        r=decode_mint(CLASSIC_PROGRAM,mint_base())
        self.assertEqual(r["onchain_program"],"CLASSIC_SPL")
        self.assertFalse(r["trading_approval"])
        self.assertFalse(r["sellability_verified"])

    def test_safe_looking_metadata_does_not_authorize_trades(self):
        # Metadata pointer and token metadata do not themselves prove rug safety.
        r=decode_mint(TOKEN_2022_PROGRAM,token2022((18,bytes(64)),(19,b"example")))
        self.assertEqual(r["extensions"],["MetadataPointer","TokenMetadata"])
        self.assertFalse(r["trading_approval"])
        self.assertFalse(r["rug_safety_verified"])
        self.assertEqual(r["unknown_extension_types"],[])

    def test_transfer_hook_delegate_and_fee_are_flagged(self):
        r=decode_mint(TOKEN_2022_PROGRAM,token2022(
            (1,bytes(16)),(12,bytes(32)),(14,bytes(64)),
            mint_enabled=True,freeze_enabled=True))
        self.assertIn("ACTIVE_MINT_AUTHORITY",r["risk_flags"])
        self.assertIn("ACTIVE_FREEZE_AUTHORITY",r["risk_flags"])
        for name in ["TransferFeeConfig","PermanentDelegate","TransferHook"]:
            self.assertIn("SENSITIVE_EXTENSION_"+name,r["risk_flags"])

    def test_unknown_extension_fails_closed(self):
        r=decode_mint(TOKEN_2022_PROGRAM,token2022((201,bytes(5))))
        self.assertEqual(r["unknown_extension_types"],[201])
        self.assertIn("FAIL_CLOSED_UNKNOWN_TOKEN_EXTENSION",r["risk_flags"])
        self.assertFalse(r["trading_approval"])

    def test_duplicate_or_truncated_tlv_raises(self):
        with self.assertRaisesRegex(ValueError,"Duplicate"):
            decode_mint(TOKEN_2022_PROGRAM,token2022((18,b"a"),(18,b"b")))
        b=token2022((14,bytes(12)))
        with self.assertRaisesRegex(ValueError,"length"):
            decode_mint(TOKEN_2022_PROGRAM,b[:-4])

    def test_account_not_owned_by_token_program_is_invalid(self):
        r=account_report(MINT_A,account(token2022(),"unknown_owner"))
        self.assertEqual(r["status"],"INVALID_OR_UNSUPPORTED_ACCOUNT_FAIL_CLOSED")
        self.assertFalse(r["trading_approval"])

    def test_batch_evidence_never_approves_any_candidate(self):
        rpc=FakeRpc([account(token2022((18,bytes(5)))),
                     account(token2022((14,bytes(1))))])
        r=inspect_mints([MINT_A,MINT_B],rpc=rpc)
        self.assertEqual(r["mints_observed"],2)
        self.assertEqual(r["errors"],[])
        self.assertEqual(rpc.calls[0][0],"getMultipleAccounts")
        self.assertEqual(rpc.calls[0][1][1]["encoding"],"base64")
        self.assertTrue(all(not x["trading_approval"] for x in r["records"]))
        self.assertFalse(r["live_approved"])
        self.assertFalse(r["profitability_validated"])

    def test_invalid_mint_list_rejected_before_any_request(self):
        rpc=FakeRpc([])
        with self.assertRaises(ValueError):
            inspect_mints(["not-a-mint"],rpc=rpc)
        self.assertEqual(rpc.calls,[])


if __name__=="__main__":
    unittest.main()
