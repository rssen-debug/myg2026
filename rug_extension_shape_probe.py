"""Inspect the PUBLIC RugCheck extension field shape for actual selected PAPER candidates.

No swapping, no trading approval, no metadata editing. This only saves the
report-level extension field representation so an allowlist can be implemented
without guessing the RugCheck schema.
"""
from __future__ import annotations
import json
from pathlib import Path
from public_http import get_public_json

MINTS={
 "HUMPH":"7jNwX4Me9zGEjmWY7zARDG2LYjVs1WXD6DZ1JvSfpump",
 "SEALS":"3sKaoPryg4KrXubLWtoqF7MznLT7QNqKRxMMkkxTpump",
 "CRAWLER":"CQCDTp8rsFV3PhaPeYjcCW5LdGTxi6jFx7eC3UXnpump",
}
def observe():
    rows=[]
    for symbol,mint in MINTS.items():
        row={"symbol":symbol,"mint":mint,"mode":"PUBLIC_REPORT_READONLY"}
        try:
            data=get_public_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report",timeout=8)
            for label,obj in (("root",data),("token",data.get("token") if isinstance(data,dict) else None)):
                if not isinstance(obj,dict):
                    row[label+"_data_type"]=type(obj).__name__
                    continue
                row[label+"_extension_field_names"]=[k for k in ("extensions","token_extensions","transferFee","tokenProgram") if k in obj]
                for key in ("extensions","token_extensions","transferFee","tokenProgram"):
                    if key in obj:
                        row[label+"_"+key]=obj[key]
            row["status"]="SCHEMA_RETRIEVED_NOT_TRADE_APPROVED"
        except Exception as exc:
            row["status"]="RUG_API_NOT_AVAILABLE"
            row["error"]=f"{type(exc).__name__}: {str(exc)[:200]}"
        rows.append(row)
    return {"mode":"PUBLIC_RUG_EXTENSION_SCHEMA_ONLY","actual_money_used":False,
            "live_approved":False,"profitability_validated":False,
            "data":[dict(x) for x in rows]}

if __name__=="__main__":
    result=observe()
    Path("rug_extensions_public_shape.json").write_text(json.dumps(result,indent=2,ensure_ascii=False,default=str),encoding="utf-8")
    for x in result["data"]:
        print(json.dumps(x,ensure_ascii=False,default=str)[:4000])
    print("NO ORDERS; source field shapes only; no security waiver.")
