def run_real_solana_replay(bot: GodlikeBot, target_pool: Optional[str] = None, *,
                           csv_path=None, decimals=None, output_dir=None):
    """Compatibility entry point: require real historical liquidity, never invent it."""
    from replay_csv import run_replay
    cfg = bot.cfg
    csv_path = csv_path or cfg.get("REPLAY_CSV_PATH")
    decimals = cfg.get("REPLAY_TOKEN_DECIMALS") if decimals is None else decimals
    if not csv_path or decimals is None:
        print("Replay kräver CSV med historiska OHLC-priser och likviditet vid varje bars öppning, samt tokenens faktiska decimals.")
        print("Använd --replay-csv FIL --decimals N. Enbart GeckoTerminal OHLCV räcker inte för realistiska fills.")
        print("Detta är ett test av en manuell entry och exitregler, inte ett bevis på lönsam tokenurvalsstrategi.")
        return False
    try:
        summary = run_replay(
            csv_path, config=cfg, decimals=decimals, output_dir=output_dir,
            buy_fn=lambda budget, price, liquidity, digits: simulate_paper_buy(budget, price, liquidity, digits, config=cfg),
            sell_fn=lambda raw, digits, price, liquidity: simulate_paper_sell(raw, digits, price, liquidity, config=cfg),
        )
    except (ValueError, OSError) as exc:
        print(f"Replay avbruten: {exc}")
        return False
    print(f"Offline exitreplay klar. Bestående rapport: {summary['report_path']}")
    if summary.get("synthetic_input"):
        print("SYNTETISKA DEMODATA: påhittade priser/likviditet; resultatet visar ingen marknadslönsamhet.")
    print("Strategins lönsamhet är INTE verifierad av ett test med en manuellt vald entry.")
    return summary
