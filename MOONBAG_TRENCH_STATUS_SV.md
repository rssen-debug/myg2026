# GODLIKE2026 – Moonbag & trenching, 14 oktober 2026

Detta är ett granskningsprotokoll, **inte** ett påstående om lönsamhet eller färdig livehandel.

## Moonbag i den ordinarie boten

- Ursprunglig köpstorlek i raw token-enheter används som bas, inte det senast återstående innehavet.
- Vid första take-profit (+40 % från ingångspris) säljs 40 % av de ursprungliga raw-enheterna, avrundat nedåt.
- Vid nästa take-profit (+80 %, kräver TP1) säljs 35 % av de ursprungliga enheterna, avrundat nedåt.
- Återstoden, normalt cirka 25 %, är moonbag med trailing-stop på 12 % från observerad högsta kurs.
- Vid stop används nya aktuella marknadsdata och en paper-säljoffert/poolmodell; stop-priset är **inte** en garanterad fill.
- Atomiska avräkningar i `quote_broker.py` är separata från den äldre AMM-modellen.
- Regressionstest `test_trench_and_moonbag.py` simulerar 100 ursprungliga enheter och verifierar TP1=40, TP2=35 och slutlig moonbag=25, inklusive ingen dublettförsäljning och ingen försäljning när kursdata saknas.

## Trenching: vad som faktiskt är gjort

- `market_discovery.py` hämtar även GeckoTerminal trendande Solana-pooler, utöver nya pooler och DexScreener-flöden.
- Alla kandidater måste fortfarande klara den befintliga entrylogiken, likviditetskontroll, RugCheck, offert- och kapitalregler innan paperköp.
- `trench_evidence.py` lagrar rapporterad köp/säljaktivitet, omsättning per likviditet, prisförändring, betalboostar, observationens färskhet samt avvikelseflaggor.
- Varje beslut i `GodlikeBot.evaluate_and_buy` sparar `trench_evidence` i `market_observations.decision_json`, även avvisade kandidater.
- `trench_funnel.py` ger rapporter med både **unika tokens** och upprepade observationer (separata mått) och underkänner felaktiga säkerhetspåståenden.
- `SmartWalletTracker` tillåter inte längre att manuellt inlagda signaturer eller flera rapporterade adresser blir ”verifierad whale confluence”. Detta kräver oberoende transaktionsbevis.
- Betalda boosts, namn och sociala länkar får **inte** ge validerad köp-/säkerhetspoäng. Den äldre hype-baserade `TrenchIQAnalyzer` returnerar nu ingen numerisk ”trust score”.

## Trenching: det som fortfarande saknas

1. Verifierad developer funding-historik och wallet-cluster-analys på kedjan.
2. Bundle-/samordnad launch-detektion och faktisk dev-säljsekvens.
3. Reproducerbar point-in-time research över ett brett urval av nya tokens, inklusive döda/ruggade projekt.
4. Mätning av falska positiva riskflaggor och riskjusterad out-of-sample-avkastning.
5. Faktisk kedjeexekvering, nätverkskostnader vid failures, inkludering, front-running och slippagekalibrering.
6. Lång 24/7-paperhistorik med tillräckligt många stängda affärer, ingen avkastningsgaranti.

**De tidigare historiska 66 papertradesen kördes i `historical_sol_paper.py`, en ANNAN fast momentum-/exit-hypotes utan Astras TP1/TP2/moonbag.** Den historiska simuleringen var kraftigt negativ och får inte marknadsföras som ett test av moonbagstrategins lönsamhet.

## Verifieringar

- `GODLIKE paper-only verification`: 91 godkända lokala/offline kontrakts- och regressionstester på GitHub Actions.
- Separat publikt `GODLIKE public Solana trench discovery smoke`: 123 unika kandidater, 19 från trendande Gecko-pooler och 19 från nya Gecko-pooler. Samtliga sju källor rapporterade OK vid den specifika körningen.
- Rapporterna är observationer vid en tidpunkt; nät/API-kvalitet och marknadsurval förändras.
- Live är fortfarande explicit blockerat och inga pengar används av dessa tester.

Kommandon för egen felsökning:

```powershell
py -3 godlike2026.py --unit-tests
py -3 trench_funnel.py --db paper_forward.db --output forward_trench_funnel.json
py -3 paper_funnel.py --db paper_forward.db --output forward_funnel.json
py -3 paper_profit_gate.py --db paper_forward.db --output forward_profit_gate.json
```

Sista grinden returnerar normalt `NOT_READY` och avslutar med kod 2 tills independent out-of-sample och tillräcklig market forward-evidence finns.
