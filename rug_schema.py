"""Conservative mapping of public RugCheck report token-vault fields.

The public schema uses liquidityA/liquidityB and decoded account objects.
Unknown authorities or incomplete account evidence are never excluded from
holder concentration. This is report corroboration, not on-chain verification.
"""


def verified_token_vaults(report, requested_mint):
    """Return token accounts demonstrably assigned to a reported pool.

    Only direct pool-owned accounts are supported here. Shared protocol
    authorities require additional protocol-specific verification, so their
    balances remain in the concentration calculation.
    """
    if not isinstance(report, dict) or not isinstance(requested_mint, str) or not requested_mint:
        return set()
    if report.get("mint") != requested_mint:
        return set()
    markets = report.get("markets")
    holders = report.get("topHolders")
    if not isinstance(markets, list) or not isinstance(holders, list):
        return set()
    by_address = {}
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        address = holder.get("address")
        if isinstance(address, str) and address:
            by_address.setdefault(address, []).append(holder)
    excluded = set()
    for market in markets:
        if not isinstance(market, dict):
            continue
        pool = market.get("pubkey")
        if not isinstance(pool, str) or not pool:
            continue
        for side in ("A", "B"):
            address = market.get("liquidity" + side)
            account = market.get("liquidity" + side + "Account")
            mint = market.get("mint" + side)
            if (not isinstance(address, str) or not address or address in (requested_mint, pool)
                    or mint != requested_mint or not isinstance(account, dict)
                    or account.get("mint") != requested_mint or account.get("owner") != pool
                    or account.get("state") != 1 or isinstance(account.get("state"), bool)
                    or "delegate" not in account or "closeAuthority" not in account
                    or account["delegate"] is not None or account["closeAuthority"] is not None):
                continue
            rows = by_address.get(address, [])
            if len(rows) != 1 or rows[0].get("owner") != pool:
                continue
            # The decoded token-account amount must agree with its holder row.
            amount = account.get("amount")
            holder_amount = rows[0].get("amount")
            if (not isinstance(amount, int) or isinstance(amount, bool) or amount < 0
                    or not isinstance(holder_amount, int) or isinstance(holder_amount, bool)
                    or amount != holder_amount):
                continue
            excluded.add(address)
    return excluded
