# GODLIKE 2026 – paperhandel mot aktuell marknad

Den här versionen använder verkliga Jupiter-offerter för simulerade köp, moonbag, försäljningar och portföljvärdering. **Inga riktiga pengar används, liveorder är spärrade och lönsamhet är inte verifierad.** Originalfilen är orörd. Behåll filerna i denna mapp tillsammans.

Offertmotor, beständig orderlivscykel och atomisk råsaldobokföring är kopplade till huvudbotens beslut och terminal. En andra offert efter modellerad fördröjning kan ge avslag; en saknad route ersätts aldrig med poolmodell. Se `EXECUTION_PROGRESS_SV.md` för verifierade funktioner och kvarvarande live-/kalibreringsarbete.

Senaste verifiering: **267 automatiska tester godkända**. Separat osignerad transaktionskonstruktion och RPC-simulation är provade med en ny tom testadress; simulationen gav `AccountNotFound`, ingen swap. Avstämningskomponenten verifierar transaktionsidentitet, råsaldon och avgifter, och sparar återstartsresultat i orderlivscykeln. Den har testats med syntetiska signerade fixtures. Säker live-signering, sändning och koppling till finansierad liveportfölj är ännu inte färdiga.

## Starta med 100 USD

Dubbelklicka på **`START_PAPER.cmd`**. Den öppnar ett synligt CMD-fönster med **100 USD totalt simulerat startkapital och högst 20 USD per order inklusive schablonkostnad**. Högst 2 USD av kapitalet avsätts till native SOL för avgifter; resten till USDC, med observerade USD-kurser. Körningen fortsätter tills du stoppar den. Python 3.11 användes vid verifieringen. Den tidigare sessionen är stoppad; denna nya kontinuerliga körning har inte startats automatiskt.

Samma körning från PowerShell i denna mapp:

```powershell
py -3 -X utf8 -u godlike2026.py --realtime --execution quote --duration 0 --cash 100 --trade-budget 20 --db paper_quotes_100.db --report-dir reports
```

I interaktiv CMD uppdateras samma terminalyta, utan att hela översikten rullar förbi gång på gång. Skärmen visar kassa, nettoresultat, realiserad/orealiserad PnL, avgifter, positioner, SL/TP, kandidatantal, avslag och uppmätt HTTP-latens. De fem senaste priserna på skärmen är **inte hela bevakningslistan**. Tidsstämplarna på skärmen och i JSON är UTC. Långsamma API-anrop kan göra värderingen äldre; detta markeras. HTTP-percentilerna mäter anrop, inte verkliga transaktioners inclusion eller hela marknadsflödets fördröjning.

**P pausar/återupptar nya köp medan exitkontroller fortsätter. Q eller Ctrl+C stoppar efter pågående dataanrop.** P/Q fungerar i det interaktiva Windows-fönstret; Ctrl+C kan också användas från terminalen. Innehav och historik sparas; stoppet gör inga tvångsförsäljningar. Starta samma fil igen för att fortsätta med samma kassa och bokföring. Ett processlås hindrar två samtidiga körningar mot samma databas. Nya signalobservationer samlas efter omstart.

`--cash` initierar bara en ny databas. Befintligt saldo återställs inte. För ett separat experiment, välj ett nytt `--db`-namn. Ändrade sparade kostnads- eller strategiparametrar kräver också en ny databas.

| Fil | Innehåll |
|---|---|
| `paper_quotes_100.db` | Bestående positioner, beslut, affärer, råsaldo och offertvärderingar |
| `paper_quotes_100.status.json` | Senaste körstatus, rapport, priser och fel |
| `paper_quotes_100.log` | Händelser och diagnostik |
| `paper_quotes_100.settings.json` | Sparade körparametrar och kontrollsumma |
| `paper_quotes_100.runs.jsonl` | Varje start, parameterhash och kodhashar |
| `paper_quotes_100.db.lock` | Operativsystemets lås för databasen |

Filerna skapas när den nya startfilen körs. Äldre `paper_live_100.*` och miljonkontot är bevarade separat. Quote-databasen kan inte öppnas med `--execution pool-model`, och en databas med äldre modellaffärer får inte användas som quote-historik.

Den äldre sessionen `paper_live_100` stoppades av användaren **13 oktober 2026 kl. 22:40:01 svensk tid (20:40:01 UTC)**. Den bevakade 184 tokens, hade 100 USD i paperkassa, 0 USD resultat och inga genomförda affärer. **0 USD utan affärer visar ingen handelsvinst och bevisar inte lönsamhet.** Ny offertbaserad historik börjar i den separata databasen när användaren startar den.

## Marknadsdata och köpkrav

Scannern hämtar fem DexScreener-flöden och upp till fem sidor nya Solana-pooler från GeckoTerminal. Den behåller högst 200 unika kandidater och utvärderar upp till 60 per cykel. DexScreener-priser hämtas i grupper om högst 30. En separat offentlig nätkontroll gav **175 unika kandidater och pris-/pooldata för 26 av 30 efterfrågade tokens**. Antalen varierar; detta är inte exekverbara offerter eller full täckning av marknaden.

Profil-, boost- och communityflöden är upptäcktskällor, inte köpsignaler. Tokenlistan innehåller inte enbart memecoins. API-fel och delvis tillgängliga flöden syns i status och på skärmen. Saknade eller inaktuella data ersätts inte med påhittade priser.

Entry kräver flera åtskilda observationer, tvåsidig handel, måttligt momentum och stabil likviditet. Därefter måste RugCheck-rapporten avse rätt mint och pool. Riskkontrollen stöder fortfarande bara rapporttyperna `pump_fun_amm`, `raydium` och `raydium_cpmm`. Token-2022, okända program/pooltyper, otillräckliga LP-bevis och ogiltiga riskuppgifter avvisas. Offertens målpool måste stämma med riskkontrollen; andra mellanliggande tokens än SOL/USDC är ännu inte stödda. En säljoffert för minst avsedd köpstorlek krävs. Ett godkänt rapportfilter eller en offert är ingen garanti för säljbarhet vid en senare tidpunkt.

Prisdata begränsas med reservernas USD-värden, inklusive explicit quote-mängd när den finns. RugCheck-data för rätt pool kan sänka modellens likviditet ytterligare. Stor redovisad TVL behandlas därför inte automatiskt som tillgängligt handelsdjup.

Publika HTTPS-anrop fungerar via verifierad certifikathantering i Windows; certifikatkontroller stängs aldrig av. Se `NETWORK_VALIDATION_SV.md` för vad som faktiskt kontrollerats.

## Bokföring och moonbag

- TP1 säljer 40 % av ursprungliga raw tokens, TP2 ytterligare 35 %. Återstoden inklusive avrundningsrester blir moonbag. TP2 kräver TP1; stopnivån sänks aldrig.
- Order, råsaldo, TP-flagga, stopnivå och historik bokförs atomiskt. Native SOL och wrapped SOL är olika tillgångar. Routeravgifter ingår i offerten och dras inte igen. Huvudbotens extra SOL-schablon är 3 105 000 lamports per exekvering, inklusive 3 miljoner i antagen ej återförd konto-/wrapbuffert. Fördröjningen är minst 500 ms och extra ogynnsam rörelse 50 bps. Antagandena är inte livekalibrerade.
- Portföljvärdet använder offerter för hela kvarvarande innehavet och avgiftsfinansierad omvandling av återstående SOL. Min-output används som konservativt offertvärde. Saknad offert/FX eller otillräcklig SOL-reserv ger okänt totalvärde och spärrar nya köp. Drawdown mäts från högsta observerade nettovärde; spärren garanterar ingen maximal förlust.

## Tester och historisk replay

Verifierade beroendeversioner finns i `requirements.txt`. Vid installation på en annan dator, använd en separat Python 3.11-miljö och `python -m pip install -r requirements.txt`. Inga wallet- eller API-nycklar behövs för offline-testerna.

```powershell
py -3 godlike2026.py --unit-tests
py -3 godlike2026.py --replay-csv riktig_historik.csv --decimals 6 --cash 100 --trade-budget 20 --stress --report-dir reports
```

Ange tokenens faktiska decimals. CSV kräver `timestamp,open,high,low,close,liquidity_usd`, med kronologiska, unika och jämnt fördelade bars. Likviditeten måste vara historiskt känd vid varje bars öppning. Valfri `close_liquidity_usd` ger separat slutvärdering. Dagens likviditet eller omsättning får inte ersätta historisk likviditet.

Replay gör en manuellt vald entry efter första baren och testar exitregler. Signaler fylls tidigast vid nästa bars öppning; stops prioriteras vid konflikt. **Det är inget backtest av hela tokenurvalet.** `sample_synthetic.csv` är ett påhittat tekniskt exempel och säger ingenting om verklig avkastning. Offline-tester behöver inga API- eller privatnycklar.

Den historiska CSV-replayen och uttryckligt `--execution pool-model` använder en balanserad constant-product-approximation. De är separata från offertbaserad realtidspaper. Inte heller offertläget modellerar ännu faktisk kedjeinkludering, alla failureavgifter eller MEV. Historiska tester och aktuell paperdata måste utvärderas efter alla kostnader, över breda tokenurval inklusive förlorare och flera marknadsperioder. Användarens miniminivå är 30 kalenderdagar och 200 avslutade simulerade trades om giltiga möjligheter finns, plus positiv netto-PnL/out-of-sample-expectancy, acceptabel drawdown och stabil exekvering. Detta har inte uppnåtts; det finns ännu ingen fullständig automatisk utvärderingsgrind. Positiv simulerad PnL garanterar inte framtida vinst. Se `RESEARCH_SV.md`, `VALIDATION.md` och hela kravspårningen i `GOAL_REQUIREMENTS.md`.
