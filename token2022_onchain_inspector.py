"""Read-only, conservative Solana Token-2022 MINT extension survey.

Purpose: determine whether candidates blocked by a report-level Token-2022
rule are genuinely Token-2022 on-chain and what security-relevant extensions
are present. This is NOT trading approval, NOT a RugCheck override, and NOT a
mint+pool audit or proof of sellability.

Source encoding: Token-2022 mint base 82 bytes, pad to 165, type byte at
165==1 (Mint), TLV [u16 type][u16 len][len payload] starting at offset 166.
Extension enum numbers follow current Solana spl-token-2022 ExtensionType.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from market_discovery import valid_mint
from solana_rpc import SolanaReadRpc, RpcUnavailable

CLASSIC_PROGRAM="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# https://docs.rs/spl-token-2022/latest/spl_token_2022/extension/enum.ExtensionType.html
EXT_TYPES={
    1:"TransferFeeConfig",2:"TransferFeeAmount",3:"MintCloseAuthority",
    4:"ConfidentialTransferMint",5:"ConfidentialTransferAccount",
    6:"DefaultAccountState",7:"ImmutableOwner",8:"MemoTransfer",
    9:"NonTransferable",10:"InterestBearingConfig",11:"CpiGuard",
    12:"PermanentDelegate",13:"NonTransferableAccount",14:"TransferHook",
    15:"TransferHookAccount",16:"ConfidentialTransferFeeConfig",
    17:"ConfidentialTransferFeeAmount",18:"MetadataPointer",19:"TokenMetadata",
    20:"GroupPointer",21:"TokenGroup",22:"GroupMemberPointer",23:"TokenGroupMember",
    24:"ConfidentialMintBurn",25:"ScaledUiAmount",26:"Pausable",
    27:"PausableAccount",28:"PermissionedBurn",
}
# Flags are conservative. A blank list does not make a token safe.
TRANSFER_OR_AUTHORITY_SENSITIVE={
    1,3,4,6,9,10,12,14,16,24,25,26,28
}


def decode_mint(owner,data):
    """Structure only. Raises when the account is inconsistent or incomplete."""
    if owner not in (CLASSIC_PROGRAM,TOKEN_2022_PROGRAM):
        raise ValueError("Mint is not owned by an audited token program")
    if not isinstance(data,bytes) or len(data)<82:
        raise ValueError("Mint has missing/invalid base bytes")
    def option(offset):
        tag=int.from_bytes(data[offset:offset+4],"little")
        if tag not in (0,1):
            raise ValueError("Malformed COption mint authority")
        return bool(tag)
    mint_authority=option(0)
    freeze_authority=option(46)
    decimals=data[44]
    initialized=data[45]
    if initialized!=1:
        raise ValueError("Token mint is not initialized")
    if decimals>18:
        raise ValueError("Mint decimals exceed modeled range")
    result={
        "onchain_program":("TOKEN_2022" if owner==TOKEN_2022_PROGRAM else "CLASSIC_SPL"),
        "raw_mint_account_bytes":len(data),
        "mint_authority_enabled":mint_authority,
        "freeze_authority_enabled":freeze_authority,
        "decimals":decimals,"initialized":True,
        "extensions":[], "unknown_extension_types":[],
        "risk_flags":[],
        "trading_approval":False,"rug_safety_verified":False,"sellability_verified":False,
    }
    if mint_authority:result["risk_flags"].append("ACTIVE_MINT_AUTHORITY")
    if freeze_authority:result["risk_flags"].append("ACTIVE_FREEZE_AUTHORITY")
    if owner==CLASSIC_PROGRAM:
        if len(data)!=82:
            raise ValueError("Classic SPL mint has unsupported account length")
        return result
    if len(data)==82:
        result["risk_flags"].append("TOKEN_2022_OWNER_NO_EXTENSION_DATA")
        return result
    if len(data)<166:
        raise ValueError("Truncated token-2022 padded mint structure")
    if data[82:165]!=bytes(83):
        raise ValueError("Nonzero padding in token-2022 mint structure")
    if data[165]!=1:
        raise ValueError("Account is not token-2022 Mint type")
    used=set()
    at=166
    while at<len(data):
        if len(data)-at<4:
            if any(data[at:]):
                raise ValueError("Truncated token-2022 TLV header")
            break
        typ=int.from_bytes(data[at:at+2],"little")
        n=int.from_bytes(data[at+2:at+4],"little")
        at+=4
        if typ==0:
            if n!=0 or any(data[at:]):
                raise ValueError("Nonzero uninitialized extension sentinel/padding")
            break
        if typ in used:
            raise ValueError("Duplicate token-2022 extension")
        used.add(typ)
        if n>len(data)-at:
            raise ValueError("Extension length exceeds mint bytes")
        name=EXT_TYPES.get(typ)
        if name is None:
            result["unknown_extension_types"].append(typ)
            result["risk_flags"].append(f"UNKNOWN_EXTENSION_{typ}")
        else:
            result["extensions"].append(name)
            if typ in TRANSFER_OR_AUTHORITY_SENSITIVE:
                result["risk_flags"].append(f"SENSITIVE_EXTENSION_{name}")
        at+=n
    if result["unknown_extension_types"]:
        result["risk_flags"].append("FAIL_CLOSED_UNKNOWN_TOKEN_EXTENSION")
    if not result["extensions"] and not result["unknown_extension_types"]:
        result["risk_flags"].append("TOKEN_2022_NO_PARSED_EXTENSIONS")
    return result


def account_report(mint, account, *, context_slot=None):
    report={"mint":mint,"observed_rpc_slot":context_slot,"status":"UNKNOWN",
            "trading_approval":False,"rug_safety_verified":False,"sellability_verified":False}
    if account is None:
        report.update(status="NO_ONCHAIN_ACCOUNT")
        return report
    try:
        if not isinstance(account,dict):
            raise ValueError("RPC account missing object structure")
        owner=account.get("owner")
        payload=account.get("data")
        if (not isinstance(payload,list) or len(payload)!=2 or
                payload[1]!="base64" or not isinstance(payload[0],str)):
            raise ValueError("Expected exact base64 mint account encoding")
        data=base64.b64decode(payload[0],validate=True)
        if len(data)>50000:
            raise ValueError("Mint too large for bounded diagnostic")
        result=decode_mint(owner,data)
        report.update(result,status="STRUCTURE_PARSED_NOT_TRADE_APPROVED")
    except (binascii.Error,ValueError,TypeError,KeyError) as exc:
        report.update(status="INVALID_OR_UNSUPPORTED_ACCOUNT_FAIL_CLOSED",
                      error=f"{type(exc).__name__}: {str(exc)[:160]}")
    return report


def inspect_mints(mints, rpc=None):
    if not isinstance(mints,list) or not 1<=len(mints)<=20 or len(set(mints))!=len(mints) or any(not valid_mint(m) for m in mints):
        raise ValueError("Mint list must contain 1..20 distinct Solana pubkeys")
    result={
        "mode":"ONCHAIN_READ_ONLY_TOKEN2022_RESEARCH",
        "timestamp_utc":datetime.now(timezone.utc).isoformat(),
        "live_approved":False,"profitability_validated":False,"actual_money_used":False,
        "rug_safety_verified":False,"routing_or_sellability_verified":False,
        "mints_requested":len(mints),"mints_observed":0,
        "summary":{},"records":[],"errors":[],
        "limitations":[
            "Mint-owner and TLV extension presence do not prove safety, holder distribution, unlocked liquidity or sellability.",
            "No mint/pool can be approved by this report. RugCheck and the disabled Token-2022 trading path remain unchanged.",
            "A future Token-2022 implementation must explicitly model extension semantics, token account requirements, transfer fees, pool type, execution and independent reverse-sell quotes.",
        ]
    }
    rpc=rpc or SolanaReadRpc()
    try:
        response=rpc.call("getMultipleAccounts",[mints,{"encoding":"base64","commitment":"confirmed"}])
        if not isinstance(response,dict) or not isinstance(response.get("value"),list) or len(response["value"])!=len(mints):
            raise ValueError("Malformed or incomplete multi-account response")
        slot=response.get("context",{}).get("slot")
        for mint,account in zip(mints,response["value"]):
            result["records"].append(account_report(mint,account,context_slot=slot))
        result["mints_observed"]=len(result["records"])
        result["summary"]=dict(Counter(r["status"]+"|"+r.get("onchain_program","UNKNOWN") for r in result["records"]))
    except (RpcUnavailable,ValueError,KeyError,TypeError) as exc:
        result["errors"].append(f"On-chain RPC survey unavailable: {type(exc).__name__}: {str(exc)[:160]}")
        result["records"]=[]
    return result


def main():
    p=argparse.ArgumentParser(description="Research-only on-chain Token-2022 mint extension survey. NO TRADES.")
    p.add_argument("--input",required=True,help="JSON report from the strict market/Rug survey")
    p.add_argument("--output",default="token2022_onchain_report.json")
    args=p.parse_args()
    data=json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows=data.get("observations",[])
    if not isinstance(rows,list):
        p.error("Missing pre-entry observations")
    blocked=[r["mint"] for r in rows if isinstance(r,dict) and
        r.get("rug_status")=="REPORT_FILTER_BLOCKED" and
        "tokenprogram" in str(r.get("rug_reason","")).lower()]
    blocked=list(dict.fromkeys(blocked))[:20]
    if not blocked:
        report={"mode":"ONCHAIN_READ_ONLY_TOKEN2022_RESEARCH","actual_money_used":False,
                "live_approved":False,"status":"NO_T2022_CANDIDATES","mints_requested":0,
                "records":[]}
    else:
        report=inspect_mints(blocked)
        report["status"]="RESEARCH_ONLY" if not report["errors"] else "RPC_ERROR_NO_APPROVAL"
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="records"},ensure_ascii=False,indent=2))
    for row in report.get("records",[]):
        print(row["mint"][:12],row["status"],row.get("onchain_program"),
              ",".join(row.get("extensions",[])),row.get("risk_flags"))
    print("READ ONLY: ZERO TRADES, ZERO APPROVALS")


if __name__=="__main__":
    main()
