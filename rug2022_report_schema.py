"""Strict RugCheck extension JSON whitelist for independently attested PAPER-only mints.

Observed RugCheck structure on October 14, 2026 for 3 real Solana
metadata-only Token-2022 mints. This is NOT a token safety validator by
itself; requires independent read-only onchain mint TLV attestation AND all
other RugCheck, pool reserve/holder, quote-route, fee and paper risk gates.
"""
from __future__ import annotations

NON_METADATA_KEYS=frozenset({
    "transferFeeConfig","defaultAccountState","permanentDelegate",
    "groupPointer","groupMemberPointer","mintCloseAuthority",
    "confidentialTransferMint","interestBearingConfig","transferHook",
    "confidentialTransferFeeConfig","tokenGroup","tokenGroupMember",
    "scaledUiAmountConfig","pausableConfig",
})
ROOT_KEYS=NON_METADATA_KEYS|{"nonTransferable","metadataPointer","tokenMetadata"}
POINTER_KEYS={"authority","metadataAddress"}
METADATA_KEYS={"authority","mint","name","symbol","uri","additionalMetadata"}


def verified_metadata_only_report_shape(payload, expected_mint):
    """Match one exact known RugCheck JSON representation; unknown -> False."""
    if not isinstance(expected_mint,str) or not expected_mint:
        return False
    if not isinstance(payload,dict) or set(payload)!=ROOT_KEYS:
        return False
    if payload.get("nonTransferable") is not False:
        return False
    if any(payload.get(field) is not None for field in NON_METADATA_KEYS):
        return False
    pointer=payload.get("metadataPointer")
    meta=payload.get("tokenMetadata")
    if not isinstance(pointer,dict) or set(pointer)!=POINTER_KEYS:
        return False
    if pointer.get("authority") is not None or pointer.get("metadataAddress")!=expected_mint:
        return False
    if not isinstance(meta,dict) or set(meta)!=METADATA_KEYS:
        return False
    if meta.get("authority") is not None or meta.get("mint")!=expected_mint:
        return False
    if meta.get("additionalMetadata")!={}:
        return False
    for name,limit in (("name",150),("symbol",40),("uri",1500)):
        val=meta.get(name)
        if not isinstance(val,str) or not 0<len(val)<=limit:
            return False
    return True
