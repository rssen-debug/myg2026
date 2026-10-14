# GODLIKE – varför 0 paperköp och vad som är verifierat (14 oktober 2026)

**Ingen verklig vinst har verifierats. Livehandel är fortfarande hårt spärrad.**

## Faktiska observationer

- Den vanliga quote-PAPER-körningen [38627005257](https://github.com/rssen-debug/myg2026/actions/runs/38627005257) innehöll i den ackumulerade SQL-historiken **9 240 utvärderingar / 849 unika mints / 0 avslutade paperaffärer**.
- Ny, separat strikt marknads-/RugCheck-probe [38630021001](https://github.com/rssen-debug/myg2026/actions/runs/38630021001): **128** mints, **14** med godkänt första marknadsfilter, **12** undersökta RugCheck-rapporter (gräns i scripten). Av dessa **0** godkända: **11** på Token-2022-program, **1** för ej stödd poolmodell. Två andra preliminära kandidater undersöktes inte i RugCheck på grund av forskningsbudget.
- Oberoende read-only RPC-probe [38630573793](https://github.com/rssen-debug/myg2026/actions/runs/38630573793) gav kontosvar för **alla 11** blockerade Token-2022-mints. Ägande: Token-2022-programmet. På mint-kontona syntes enbart **MetadataPointer** och **TokenMetadata**. Mint-/freeze-authority var avstängda i det observerade tillståndet. Detta är *inte* bevis på hållarfördelning, LP-säkerhet, transferbarhet eller lyckad exekvering.

## Implementerat

1. `token2022_onchain_inspector.py` parse:ar mint-program, authority-struktur och Token-2022 TLV-tillägg från det riktiga mint-kontot på Solana. Dolda, okända, dubbla och för korta data blockeras. Koden skickar inga transaktioner och ger aldrig tradinggodkännande.
2. `pre_entry_liquidity_rug_survey.py` skiljer initial marknadskvalificering, RugCheck och senare ej utförd orderrouting åt med spårbara avvisningsorsaker.
3. `godlike2026.py` har **AV** som standard för `TOKEN2022_METADATA_PAPER_EXPERIMENT`. En frivillig flagga `--token2022-metadata-paper-experiment` fungerar endast med `--realtime --execution quote` och behåller LIVE spärrat.
4. För den särskilda paper-varianten krävs on-chain Token-2022-ägare, exakt de två metadata-tilläggen, icke-aktiva mint/freeze-authorities, inga osäkra/okända tillägg, identiska decimals och **alla** tidigare RugCheck-risk-, LP-, holder- och poolkontroller. Dessutom krävs verifierad exakt Jupiter-rutt, köp- och säljtestofferter, kapital- och kostnadskontroller innan en *simulerad* position kan bokföras.
5. Experimentinställningen versionskontrolleras mot sparad paperdatabas, så en gammal eller en vanlig forskningssession inte kan blandas med en ändrad Token-2022-modell.
6. Regressionstester: **134/134 gröna** i [kodverifiering 38631187373](https://github.com/rssen-debug/myg2026/actions/runs/38631187373). Kodtester är inte handelsbevis.

## Hur du granskar experimentet

För PAPER, separat ny databas och aldrig riktig wallet:

```powershell
py -3 godlike2026.py --realtime --execution quote --token2022-metadata-paper-experiment --cash 100 --trade-budget 20 --duration 330 --db token2022_metadata_experiment.db
```

Experimentjobbet på GitHub: [Token-2022 paper market trial](https://github.com/rssen-debug/myg2026/actions/workflows/godlike-token2022-paper-market-trial.yml).

Flaggan **är inte påslagen i vanliga paperflödet**, och ska inte användas för verkliga pengar. En metadata-only mint kan fortfarande vara en rug via annan mekanism och Jupiter-offerter kan avvika från verkliga fills. Båda pool- och programkontroller måste fortsätta vara exakta.

## Fortsatta hinder

- Bevisa faktiskt färska riktiga påverkansjusterade köp- och säljsignaler i quote-PAPER med SPARAD SQL-ledger; det finns ännu inga sådana lönsamhetsresultat.
- RugCheck-rapporters tokenProgram kan inte ensam ersätta oberoende on-chain program/extension-kontroll.
- På on-chain account-nivå kan extra extensionkrav finnas; riktiga transfer-hindren behöver simuleras/kalibreras separat. Metadata-only mint kontroll kan aldrig i sig bevisa säljbarhet.
- Target AMM måste vara samma som granskade målpoolen, annars ny fullständig riskvalidering; förbigå inte quote_route-regler.
- Solana-nätavgifter, rent/ATA, sell-quote, price impact, failure rates och kapitalbegränsningarna är inte livevaliderade.
- Tidigare historical OHLCV-replays gav starkt negativa resultat på ett selektionsbiasat redan sett urval. Inga ytterligare fittade parameterförsök på den perioden får kallas oberoende out-of-sample.

**Fullmålet "garanterat profitabel" kan inte ges och är inte uppnått.**


## Uppföljning: report-only Token-2022-schema och exakt köp-/säljpool

- Fullt paperexperiment före report-schemafixen: [38631333178](https://github.com/rssen-debug/myg2026/actions/runs/38631333178), 322 unika tokens, 2 400 bedömningar, **0 simulerade köp**, **0 avslutade order**. Även där flaggade 79 utvärderingar / 8 unika tokens på grund av `token_extensions` som den tidigare generiska regeln inte kunde tolka.
- Offentliga RugCheck-extensionstrukturer från tre av dessa token visade `token_extensions` med **MetadataPointer** och **TokenMetadata**, medan transferHook, permanentDelegate, transferFeeConfig, nonTransferable, pausable med flera specifika riskfält var null/false. Läsprov: [38632410904](https://github.com/rssen-debug/myg2026/actions/runs/38632410904).
- `rug2022_report_schema.py` validerar **exakt de fältnamn som RugCheck returnerade** och matchar metadata-mint/adress mot den oberoende på kedjan observerade minten. Okända/nya nycklar, aktiv transfer hook, transferFeeConfig, metadata-auktoritet eller extra metadata avvisas. Den är endast aktiv tillsammans med opt-in **paper** och färsk on-chain attestering; standard/live är fortsatt spärrad.
- Read-only followthrough med tre förvalda tidigare blockerade mints: [38632911282](https://github.com/rssen-debug/myg2026/actions/runs/38632911282). Alla tre passerade därefter hela RugCheck-rapportfiltret **men detta betyder inte säljbarhet**. HUMPH hade matchande köprutt men en overifierad mellanliggande mint i säljrutten; SEALS hade overifierad mellanliggande mint redan i köprutten; CRAWLER fick ett offertrelaterat fel. **Ingen bör köpas på dessa offerter.**
- Nytt isolerat paperexperiment efter report-schemaändringen: [38633096342](https://github.com/rssen-debug/myg2026/actions/runs/38633096342). Även där krävs tidssärskilda signalkrav, hela RugCheck, on-chain extensiongranskning, exakt Jupiter-rutt för kontrollerad pool och konservativ försäljningskontroll. Håll slutsatsen NOT_READY tills orderbokföringen verifierat verkligt citerade, simulerade BUY+SELL efter avgifter.

**Token-2022-metadata-only på kedjan är inte en universell säkerhetsklassning.** Ingen ska släppa på pool-/ruttkraven för att få artificiella affärer.
