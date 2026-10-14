# Fullständigt mål – krav, implementation och bevis

Granskningspunkt: 13 oktober 2026, efter testkörningen med 157 godkända tester och före godkännande/integration av efterföljande exekverings- och quotemoduler. Kravkälla: användarens `pasted-text-1.txt` i `C:/Users/krist/.codex/attachments/77ae7187-be95-4ff3-8afd-fe7bb1c572ad/`. Denna fil bevarar hela uppdraget; den begränsar det inte till en paperscanner eller snyggare terminal.

**Slutmålet är inte uppnått.** Den aktuella leveransen har fungerande offentlig marknadsinhämtning, en experimentell entryregel, modellbaserad paperbokföring, riskfilter och en terminalöversikt. Den saknar bland annat ett färdigt gemensamt paper/live-exekveringssystem, verklig transaktionsavstämning, utvecklar-/wallet intelligence och belagd strategiavkastning. Live är låst. En godkänd testsuite eller en växande tokenlista är inte bevis på vinst.

Senare verifierad delprogress, **225 godkända tester**: offertbaserad paper är nu standard i huvudboten. `quote_broker.py` kopplar befintliga signaler, köp, TP1/TP2, moonbag och stopp till `execution_engine.py`, beständig orderlivscykel och atomisk råsaldobokföring. `test_quote_broker.py` täcker hela denna kedja och runtimecykel, med AMM-funktionerna blockerade för att upptäcka otillåten fallback. USD-resultat använder sparad FX-evidens; liquidationvärde bygger på min-output för hela kvarvarande mängden och finansierade SOL-kostnader. Kapitalets USDC/SOL-fördelning ingår i samma 100 USD. Publik FX-/quote-värdering har också kontrollerats utan order i `execution_research/quote_portfolio_read_smoke.json`. Se `EXECUTION_PROGRESS_SV.md`. **Fullmålet är fortsatt inte uppnått**: liveadapter, kedjeavstämning, exekveringskalibrering, intelligens, flera validerade strategier och långvarig utvärdering återstår. Tabellerna nedan bevarar den tidigare granskningspunkten och ska läsas tillsammans med detta aktuella tillägg.

Aktuell nästa kontrollpunkt, **267 godkända tester**: `swap_build.py` konstruerar osignerade v0-transaktioner från riktig Jupiter V2-data. `solana_rpc.py` har endast läsning/simulation. `transaction_reconciliation.py` verifierar förberedd signatur/message-hash mot rå transaktion och kedjans beloppsmetadata. `reconciliation_service.py` kopplar detta till beständig återstart och atomiska orderutfall; separat failed-fee-applier kan bokföra en bekräftad kedjefailure exakt en gång. Tester använder syntetiska signerade fixtures, aldrig broadcast. Ett publikt mainnet-test 21:32:11 UTC byggde unsigned bytes och fick avgiftsuppskattning; simulationen gav `AccountNotFound` för en ny tom adress. **Live-signering är fortsatt spärrad** eftersom aktuella binära routevillkor och lookup-tabeller inte är färdigverifierade. Liveadapter, finansierad portfölj/reservationer, rent-/feebokföring och verklig kalibrering återstår. Detta förbättrar delarna A07/A11/E04/E09/E10 utan att göra något av de fullständiga slutkraven färdigt.

Status används strikt:

- **IMPLEMENTERAT**: den avgränsade funktionen finns med identifierbart kod- och/eller körbevis. Betyder inte ekonomisk eller fullständig produktionsvalidering.
- **DELVIS**: viss funktion finns, men minst en uttrycklig del eller nödvändig verifiering återstår.
- **SAKNAS**: ingen fungerande implementation med erforderligt bevis har identifierats. Ett namn, en stub, ett dokument eller en oanvänd klass räknas inte som implementation.

Kodreferenser nedan avser filer i denna mapp och namngivna funktioner, inte externa referensprojekts påståenden. Vid ändringar ska både status och bevis uppdateras; planerat arbete får inte markeras klart i förväg.

## A. Användarens 18 slutkriterier

| ID | Slutkriterium | Status | Befintligt bevis | Kvar för fullständigt godkännande |
|---|---|---|---|---|
| A01 | Existing codebase audited and improved | IMPLEMENTERAT, avgränsat | `godlike2026_original.py` bevarar originalet. Rättningar i `godlike2026.py`, `test_accounting.py`, `test_moonbag.py`, `test_execution.py`; `RESEARCH_SV.md`, `VALIDATION.md`. | Fortsatt granskning av ny kod, beroenden, död kod och kvarvarande legacy-RPC/Jupiter-funktioner. Ingen total säkerhetscertifiering påstås. |
| A02 | Real-time Solana token discovery operational | IMPLEMENTERAT för publika pollingflöden | `market_discovery.py::discover_mints`; `DexScreenerClient::scan_trending_mints/prefetch_tokens`; `test_discovery.py`, `test_dex_batch.py`; nätkontroll i `NETWORK_VALIDATION_SV.md`. | Direkt launch-/graduation-eventtäckning, mätning av bortfall och upptäcktsfördröjning är separata, ännu saknade krav. |
| A03 | Developer intelligence and rug-risk detection operational | DELVIS | `RugCheckValidator`, `rug_schema.py`, `test_rug.py`, `test_rug_schema.py`, `test_risk_runtime.py`. | Utvecklarhistorik, funding/kluster, bundles, wash trading, beteendesignaler och kalibrerad rug-sannolikhet. |
| A04 | Multiple clearly defined strategies available | SAKNAS | En experimentell momentumregel finns i `GodlikeBot::evaluate_and_buy`; `test_signals.py`. | Flera utbytbara, versionsatta strategier med fullständig specifikation och jämförande out-of-sample-resultat. Se S01–S13. |
| A05 | Reliable live market data ingestion | DELVIS | `public_http.py`, batch- och poolspecificerad cache i `DexScreenerClient`; certifikatkontroll, felstatus, stale-data-avslag; `telemetry.py`. | Långtids-SLO, verkligt källticklag, robust RPC/streaming, full data lineage och representativ bortfallsanalys. HTTP-svarstid är inte feedlag. |
| A06 | Realistic execution-aware paper trading | DELVIS | `simulate_paper_buy/sell`, konservativa reservtak, `get_position_market_data`, `replay_csv.py`; kostnads-, gap-, likviditets- och stale-data-tester. | Verkliga routes/offerter, gemensam orderlivscykel, tx-latens/failure/expiry, fees för misslyckanden och kalibrering. En AMM-modell är ingen verifierad fill. |
| A07 | Real transaction-building and submission capability implemented | SAKNAS | `JupiterEngine::execute_live_swap` returnerar avslag; tidigare osäker broadcastkod finns endast i originalet. | Verifierade aktuella API:er, transaktionsbyggande, simulation, signering, kontrollerad submission och kedjespecifikt kvitto. |
| A08 | Secure, disabled-by-default LIVE MODE | DELVIS | `LIVE_BLOCK_REASON`, `_load_keypair` laddar ingen nyckel, `--live` avvisas; no-network-gatetester i `test_accounting.py`. | Fungerande liveadapter bakom explicit konfiguration/bekräftelse, säker secret storage, minst privilegium och policy för tillåtna transaktioner. Ett permanent avslag uppfyller bara spärrdelen. |
| A09 | Robust portfolio risk controls | DELVIS | `StateManager::can_open_position`, atomisk bokföring, max positioner, drawdown, förlustsvit, stale/okänd likviditet; `test_accounting.py`, `test_risk_runtime.py`. | Daglig förlustgräns, hårda oberoende allokerings-/exponeringsgränser, volatilitetsanpassning, verklig fee-reserv, infrastrukturspärr och manuell liquidation. Se R01–R16. |
| A10 | Accurate realized and liquidation-adjusted PnL | DELVIS | `StateManager::performance_report/paper_equity`, proportionell kostnadsbas efter raw-mängd, fees, TP och null vid okänd värdering. | Säljroutebaserad liquidation, kedjebekräftade avgifter/utfall och korrekt hantering av osäkra/delvisa faktiska exits. Nuvarande PnL är simulerad. |
| A11 | Working transaction reconciliation and restart recovery | DELVIS | Paperpositioner/ledger består i SQLite; `SessionLock`, settingskontroll, bestående likviditetstak; reset/duplicate/restart-tester. | Transaktionsspecifik reconciliation efter timeout/broadcast/crash, signerad identitet före sändning, atomiskt kvitto+ledger och återställning av osäkra tx. `reconcile_pending_transactions` är en varningsstub. |
| A12 | Real execution latency measurements | DELVIS | `telemetry.py::Telemetry`, transportmätning i `public_http.py`, kandidat-/cykelmätning i `paper_runtime.py`; p50/p95/p99 i status/TUI. | Hela kedjan event→inclusion/confirmation, RPC-percentiler, källticklag, quote age/expiry, missade möjligheter och latensförluster. Inga verkliga execution-latensresultat finns. |
| A13 | Conservative MEV, slippage, and failed-fill modeling | DELVIS | Konstant AMM-impact, antagen adverse movement, stale/invalid-data-avslag, kostnadsstress och nästa-bar-fills. | Mätt MEV/adverse ordering, contention/congestion, priority/Jito, failurekostnader och empirisk sannolikhets-/latensmodell. |
| A14 | End-to-end automated tests passing | DELVIS | `validation_tests.txt`: 157 tester godkända på granskningspunkten; offline CLI/replay-integration och runtime/transporttester. | Fullt orderlivscykel-E2E, paper/live-beslutsparitet, tx-recovery, adversarial kongestion/RPC-failure och regressioner för de saknade modulerna. 157 tester bevisar inte kraven som ännu saknas. |
| A15 | Reproducible forward-testing reports | DELVIS | SQLite-observationer/beslut/affärer/equity; `paper_runtime.py` settings + kodhashar + runmanifest; `replay_csv.py` inputhash och parametrar. | Komplett quote/order-eventlogg, strategi/version per beslut, daily/cohort-rapporter, frysta holdouts och maskinell 30-dagars-/200-affärsgrind. |
| A16 | Live calibration procedure implemented | SAKNAS | Begränsningar och behov beskrivs i `RESEARCH_SV.md`; all live är låst. | Ett fungerande manuellt godkänt, hårt beloppsbegränsat kalibreringsflöde med buy/sell, verklig fill/fee/latens och jämförelse mot modell. Dokument är inte procedurimplementation. |
| A17 | Documented limitations and remaining risks | IMPLEMENTERAT | `README_SV.md`, `NETWORK_VALIDATION_SV.md`, `RESEARCH_SV.md`, `VALIDATION.md` och denna spårning. | Håll dokumenten synkroniserade med ny kod, driftbevis och återstående luckor. |
| A18 | No fake profitability or simulated success claims | IMPLEMENTERAT i rapporterade resultat | `profitability_validated: false`, `actual_money_used: false`, uttrycklig modell-PnL, null vid okänd värdering; syntetisk replay märks. | Samma krav gäller varje framtida adapter/strategi och presentation. Uppdiktade affärer eller default-FILLED är aldrig godtagbara. |

## B. Högst prioriterat: gemensamt beslutssystem och två adaptrar

Detta är användarens högst prioriterade arkitekturkrav. Det uppfylls **inte** genom att ta bort spärren i `execute_live_swap`.

| ID | Krav | Status och konkret kvarstående verifiering |
|---|---|---|
| E01 | En gemensam beslutsmotor, PAPER- och LIVE-adapter | SAKNAS som komplett arkitektur. Nuvarande `evaluate_and_buy` och `PositionMonitor` anropar papersimulering direkt; live avvisas. Samma beslut ska kunna skickas till valfri adapter. |
| E02 | Identiska data, discovery, signaler, entry/exit, risk och bokföringsregler | DELVIS: funktioner kan återanvändas, men två fungerande adaptrar och automatisk paritetstest saknas. Identiskt marknads-/kontotillstånd måste ge identiska orderbeslut. |
| E03 | Gemensamma quote-, routing-, slippage- och transaktionsvillkor | SAKNAS. Papersimulering saknar exekverbar route. Bevisa samma min-output, decimals, belopp, routningsbegränsningar och expiry i båda adaptrarna. |
| E04 | Gemensam beständig orderlivscykel/state machine | SAKNAS. Ska särskilja intent, quote, validering, simulation, signering, submission, confirmation, failure, expired, uncertain och reconciled utan dubbla fills. Historiktabellens action är inte en sådan state machine. |
| E05 | Route/quote för varje köp och sälj när stödd | SAKNAS. Validera input/output mint, raw amounts, fees, route, context/ålder och minsta utfall; avvisa fel/saknad route; bevara tidsstämplad råquote. Legacy `get_quote` räcker inte som integrerad adapter. |
| E06 | Verkligt fillutfall i paper | DELVIS: ogiltiga/olönsamma modellorder kan avvisas, men modellerade giltiga order fylls utan empiriskt submission-/inclusionutfall. Ingen implicit framgång får bokföras när utfallet är okänt. |
| E07 | Spread, impact, reserver, storlek, DEX/routing-fees och slippage | DELVIS: modellerad impact, poolavgift och adverse movement finns; exakt route-fee/spread, faktisk poolbalans och fler poolmodeller saknas. `test_execution.py`, `test_risk_runtime.py`, tester för bestående entry-reservtak ger nuvarande bevis. |
| E08 | Propagation, RPC-/confirmationlatens, blockhash/quote-expiry, congestion, contention | SAKNAS i fillmodellen. HTTP-mätningar får inte sättas lika med transaktionslatens. |
| E09 | Tx-simuleringsfel, failurekostnad, priority fee, Jito-tip, försvunnen likviditet | DELVIS för likviditetsavslag; övrig tx-kostnad/utfallsmodell saknas. Misslyckad order får varken skapa tillgångar eller intäkter. |
| E10 | Osäkert utfall förblir osäkert | DELVIS: okänt portföljvärde blir null och gammal pending lämnas orörd. Beständig orderstatus UNCERTAIN med säker reconciliation och spärr för ny dublett saknas. |
| E11 | Sellbarhet och full orderstorlek vid liquidation | DELVIS: `paper_equity` modellerar återstående raw-mängd med kostnader och reservtak. Verklig säljroute, transferrestriktioners aktuella status, exit-latens och partiella/reverterade försäljningar återstår. |
| E12 | Jämför vanlig RPC och specialiserad/private submission | SAKNAS. Mät reliability, kostnad, inclusion och adverse ordering. Private route eller hög tip får aldrig betraktas som garanti. |

## C. Marknads-, utvecklar- och wallet intelligence

| ID | Omfattning som ska täckas | Status / evidens |
|---|---|---|
| I01 | Pump.fun launches; bonding curve state/beteende; graduation och migration | SAKNAS. Nya pooler upptäcks via aggregatorer, men direkta launch-/kurv-/graduationhändelser modelleras inte. `pump_fun` bonding-curve-pooler avvisas för nuvarande modell. |
| I02 | Nya pooler och token discovery | IMPLEMENTERAT för bounded polling: `market_discovery.py`; ingen garanti för alla launches, memecoinklassning eller realtid på slotnivå. |
| I03 | Tokenålder, launchtiming, tidigt köptryck, marknadsvärdesförändring, likviditetsutveckling | DELVIS: h1-volym/buys/sells, prisobservationer och likviditetsstabilitet finns i `evaluate_and_buy`. Launch-age, tidsserier för market cap och eventbaserat köptryck saknas. |
| I04 | Developer wallet, tidigare launches och misstänkta rugs | SAKNAS: ingen persistens, attribution, historik eller empiriskt validerad bedömning. |
| I05 | Developer funding sources, fundingrelationer, kopplade insiderwallets och kluster | SAKNAS: holderkoncentration är inte klusteranalys. Kräver point-in-time graf, attribution/konfidens och false-positive-kontroll. |
| I06 | Bundlade köp/launches, samordning, developer selling och misstänkt distribution | SAKNAS: inga on-chain sekvens-/bundle-/flödesfeatures eller historisk evaluering. |
| I07 | Profitable wallets: historisk realiserad PnL och win rate efter alla kostnader | SAKNAS. Legacy `SmartWalletTracker` kan lagra inmatade signaler men är ingen fungerande lönsamhetsanalys. Tokeninnehavets värdestegring får inte användas som realiserad wallet-vinst. |
| I08 | Wallet entry/exit-timing, smart money, whales och copy trading efter realistisk delay | SAKNAS: inga actual-wallet-dataset eller kostnads-/latensjusterade resultat. |
| I09 | Mint/freeze authority, transferrestriktioner, Token-2022 fees/extensions | DELVIS: rapportfilter kräver revokerade authorities och blockerar Token-2022/okända program; `RugCheckValidator`. Direkt aktuellt kedjestatus- och säljsimuleringstest saknas. |
| I10 | LP-withdrawalrisk, holderkoncentration och verifierade vaults | DELVIS: rätt pool, LP-andelar, holderprocent, reservtak och `rug_schema.py`. LP-report är inget oberoende bevis för att likviditeten består eller kan exekveras. |
| I11 | Fake volume, wash trading och hidden wallet clustering | SAKNAS. Volym-/turnovergränser är grova filter, inte detektorer för dessa fenomen. |
| I12 | Honeypot-liknande säljhinder och rugpull probability | DELVIS som statiskt riskfilter, SAKNAS som verifierad säljbarhet och kalibrerad sannolikhet. RugCheck-score får inte presenteras som modellens egen rug-sannolikhet. |

All intelligence måste ha källa, observationstid, historisk tillgänglighet och fel-/osäkerhetsstatus. Privata datatjänster får inte påstås integrerade genom att endast nämnas i dokumentation.

## D. Strategier som uttryckligen ska undersökas, implementeras och jämföras

| ID | Strategi | Nuvarande status |
|---|---|---|
| S01 | Early launch momentum | DELVIS: generellt momentum finns, men launch-event/ålder och tidig entryvalidering saknas. |
| S02 | Post-launch continuation | DELVIS: samma generella momentumhypotes, ingen särskild continuationstrategi eller jämförelse. |
| S03 | Bonding-curve momentum | SAKNAS; pooltypen stöds inte av exekveringsmodellen. |
| S04 | Graduation breakout | SAKNAS; graduation-event och migrationsverifiering saknas. |
| S05 | Post-graduation pullback | SAKNAS. |
| S06 | Volume expansion | DELVIS: minsta h1-volym/turnover är ett filter, inte en versionsatt expansionsstrategi. |
| S07 | Smart-money confirmation | SAKNAS; legacy wallet-signalclass innebär inte fungerande lönsamhetsdata. |
| S08 | Whale-wallet accumulation | SAKNAS. |
| S09 | Momentum reversal | SAKNAS. |
| S10 | Liquidity expansion | DELVIS: fallande likviditet kontrolleras, expansion som signal och strategi saknas. |
| S11 | Breakout and retest | SAKNAS. |
| S12 | Short-duration scalping | SAKNAS; nuvarande TP/SL-regler är inte bevisad kortsiktig scalping. |
| S13 | Market-regime-dependent strategies | SAKNAS; inga regimdefinitioner eller beroende strategival. |

Varje strategi ska ha ett identifierbart, versionsatt kontrakt för **entry, exit, max positionsstorlek, stop-loss, take-profit, trailing stop, tidsbaserad exit, likviditetskrav, förväntade exekveringskostnader och riskjusterad förväntad avkastning**. Nuvarande momentumregel har flera entryfilter och gemensam TP/SL/moonbag, men saknar tidsbaserad exit och empiriskt skattad riskjusterad expectancy. Ett namn som ”AAA+++” är inget mätbart acceptanskriterium.

Tillkommande signaler måste jämföras genom reproducerbar ablation/out-of-sample-testning efter kostnader. Maskininlärning är inte ett självändamål och får bara införas om den reproducerbart förbättrar en enklare baslinje. Ett underkänt strategiresultat ska redovisas, inte döljas eller optimeras bort på samma holdout.

## E. Oberoende riskkontroller

| ID | Krav | Status / kvarstående |
|---|---|---|
| R01 | Max positionsstorlek, kapital per token och tokenexponering | DELVIS: fast trade budget, kassa och en öppen position per mint; hard cap fristående från strategi/adapter och allokeringsprocent saknas. |
| R02 | Max samtidiga positioner | IMPLEMENTERAT i nuvarande entrypolicy (`MAX_OPEN_POSITIONS`); framtida adapter får inte kunna kringgå den. |
| R03 | Daglig förlustgräns | SAKNAS. Definiera tidszon/dygn, realiserad kontra liquidation-PnL, omstartsbeteende och persistens; inga automatiska kringgåenden via reset. |
| R04 | Portföljdrawdown | IMPLEMENTERAT för modellerade värden från observerad topp. Okänd liquidation gör värdet ofullständigt; stoppar kan misslyckas vid rug. |
| R05 | Likviditetsbaserad storlek | DELVIS: impact/reservtak avvisar för stora order. Ingen validerad adaptiv storleksoptimering eller routebaserad kapacitet. |
| R06 | Volatilitetsbaserad exponeringsminskning | SAKNAS. |
| R07 | Skydd efter konsekutiva förluster | IMPLEMENTERAT för avslutade paperpositioner; `MAX_CONSECUTIVE_LOSSES`, cooldown och tester. |
| R08 | Avstängning vid infrastrukturfel | DELVIS: feldata avvisas och runtime visar DEGRADED; ingen komplett oberoende breaker med beständig retry/backoff/återhämtningspolicy över alla adapters. |
| R09 | Stale data och saknad värdering | IMPLEMENTERAT som paperavslag; upstream feedlag och route-age kräver ytterligare bevis. |
| R10 | Dubbletter | DELVIS: atomiska köp/TP/stängningar och processlås; global idempotens över signerad order/broadcast/restart saknas. |
| R11 | Verklig walletbalans och transaction-fee-reserv | SAKNAS i fungerande liveadapter; paperkassa valideras. En konfigurationsnyckel för MIN_SOL_RESERVE är inte verifierad livekontroll. |
| R12 | Emergency stop | DELVIS: Ctrl+C/Q stoppar runtime och P pausar nya entries medan exits fortsätter. Live kill switch för pending/broadcast-kontroll saknas. |
| R13 | Manuell emergency liquidation när möjlig | SAKNAS. Stopp av process ska inte beskrivas som försäljning. |
| R14 | Ingen martingale/okontrollerad averaging down/riskeskalering efter förlust | IMPLEMENTERAT i nuvarande fasta budgetpolicy; bevara som oberoende invariant i nya strategier. |
| R15 | Oåtkomliga tillgångar/likviditetskollaps | DELVIS: okänd liquidation och avvisade exits ger ingen påhittad cash. Realistiska osäkerhets-/förlustscenarier och verklig exitreconciliation saknas. |
| R16 | Oberoende riskmotor som strategin inte kan kringgå | SAKNAS som komplett adapteroberoende kontrakt. State-validitet finns men täcker inte alla riskkrav. |

## F. Latens, calibration och liveinförande

Den föreskrivna mätkedjan är **token-event upptäckt → data mottagen → analys klar → signal skapad → quote erhållen → transaktion byggd → skickad → bekräftad**. Nuvarande `Telemetry` mäter HTTP-anrop per host, kandidatutvärdering och scanner-cykel med begränsad samplebuffert och p50/p95/p99. `console_dashboard.py` visar nätpercentiler. Det är användbar drifttelemetri men inte denna kompletta kedja.

Kvarstående mätkrav: korrelerade monotona spantider och UTC-eventtider per order, RPC-svar, upstream lag, quote age, inclusion/confirmation delay, missade möjligheter, latensrelaterad kostnad, bortfall och sampleantal. Externa klockor får inte antas vara synkroniserade. Mät först flaskhalsen; streaming/asynkronitet ska användas där den ger verifierbar nytta. En snabb kodloop får inte likställas med tidigare inclusion.

Kalibrering är **SAKNAD**. Den ska först efter separat explicit användargodkännande kunna genomföra mycket små, hårt begränsade liveköp och livesälj. Per försök ska förväntad och verklig fill, slippage, success/failure, confirmationlatens, total fee inklusive priority/tip och modellfel sparas. Paperparametrar får uppdateras genom versionsatt analys, inte genom att efterhandsändra gamla utfall. Utan kalibreringsbevis måste osäkerheten synas.

Staged deployment måste stödja samtliga sex steg utan automatisk förflyttning till riktiga medel:

1. **Read-only monitoring:** implementerat för offentliga källor.
2. **Real-time paper:** implementerat med redovisade modellbegränsningar.
3. **Små manuellt godkända livetester:** saknas; inte godkända att köras av detta dokument.
4. **Kalibrerings- och reconciliation-review:** saknas.
5. **Strikt begränsad livedrift:** saknas och fortsatt låst.
6. **Gradvisa ökningar efter dokumenterad driftstabilitet:** saknas. Ingen automatisk uppskalning till användarens eventuella framtida kapital.

## G. Prestationsmätning och analysmotor

| Kravgrupp | Vad som finns | Vad som saknas |
|---|---|---|
| PnL och liquidation | Net, realiserad, orealiserad, modellerad portfölj, sammanlagda fees i `StateManager::performance_report`. | Full gross→net-brygga per kostnadstyp, verklig sellquote/chainutfall och osäkerhetsintervall. |
| Handelsstatistik | Win rate, vinnare/förlorare/breakeven, profit factor, expectancy, observerad max drawdown. | Loss rate som explicit mått, genomsnittlig vinnare/förlorare, risk/reward, statistiskt meningsfull Sharpe med korrekt sampling. |
| Exekveringskostnad | Modellerad gas/poolavgift/slippage samt teknisk nätfelstatistik. | Transaction success/failure-rate, faktisk slippage, priority fees/tips, latency losses och paper/live-jämförelse per order. HTTP-felprocent är inte tx-felprocent. |
| Exponering | Öppna innehav, kassa och trade budget. | Exponering per strategi/token, kapitalutnyttjande, daglig och kumulativ attribution. |
| Automatisk efteranalys | Sparade beslut, avslag och orderhistorik finns som underlag. | En fungerande analysmotor som förklarar vinnare/förlorare, felaktiga godkännanden, developer-/smart money-signaler, entry-/exitförbättringar och kostnadsutplånad alpha per regim. |
| Promotion och rollback | Settingshash, kodhashar/runmanifest och källkod kan identifiera ett experiment. | Reproducerbara strategiversioner, automatisk holdoutkontroll, shadow-evaluering, uttrycklig promotionspolicy och testad rollback. Produktion får inte automatiskt bytas mot ett oprövat förslag. |

Varje trade ska gå att inspektera med **varför entry, varför exit, vilka signaler, förväntad kostnad, vad som verkligen hände och möjliga förbättringar**. Nuvarande reason/TP/SL-historik täcker bara en del; inga fullständiga quote-/latens-/signalattributioner finns.

## H. Testdesign och obligatorisk forward-utvärdering

Tekniska tester måste hållas åtskilda från strategiresultat och anslutningssmoke. `replay_csv.py` testar manuellt vald entry och exitregler; det är inte ett historiskt test av hela discovery-/entrysystemet. Den syntetiska CSV:n är endast en testfixture.

Följande forskningskrav är fortsatt öppna: representativa döda/ruggade tokens; tidsriktig risk-/likviditetsdata; kronologi utan look-ahead eller läckage; skydd mot survivorship bias; frysta train/validation/holdout-perioder; strategi- och regimjämförelse; känslighetsanalys; multipeltestningskorrigering; kompletta avslag och uteblivna fills. Att många avslag lagras löser inte automatiskt samplingbias i promotade tokenflöden.

**Maskinell utvärderingsgrind saknas.** Den ska pröva samtliga krav nedan, utan att någonsin själv aktivera live:

| Grind | Användarens uttryckliga miniminivå | Nuvarande belägg/status |
|---|---|---|
| G01 Tid | Minst 30 kalenderdagar verklig forward-observation | EJ UPPFYLLT. En kort driftkontroll är inte 30 dagar; historisk replay får inte räknas som passerad forwardtid. Definiera uptime/gap och första giltiga observation. |
| G02 Affärer | Minst 200 avslutade simulerade positioner om giltiga möjligheter finns | EJ UPPFYLLT. Kontrollsnapshot 20:31:59 UTC: 0 avslutade. TP-deluttag ska inte räknas som flera kompletta trades. Förläng testet vid få möjligheter; framtvinga inte trades. |
| G03 Netto | Positiv net PnL efter samtliga modellerade/observerade kostnader | EJ VISAT. Snapshot hade 0 USD, inte positiv avkastning. Illikvida/okända innehav får inte döljas. |
| G04 Generalisering | Positiv out-of-sample expectancy | SAKNAS. Ingen representativ fryst utvärdering är genomförd. |
| G05 Risk | Acceptabel drawdown | EJ GODKÄNT. Riskbudget och kriterium ska fastställas före utvärderingen; teknisk 20 %-breaker är inte bevis på acceptabel uppmätt risk. |
| G06 Exekvering | Stabila exekveringsantaganden | EJ VISAT. Kräver versionsspårning, driftmått och kalibreringsosäkerhet; ändringar ska skiljas från tidigare cohort. |
| G07 Kritiska fel | Inga olösta kritiska tradingbuggar | EJ UPPFYLLT för livedrift: adapter, reconciliation och exekveringsmodell saknar väsentliga delar. Gröna unit tests kan inte frikänna funktioner som inte finns. |
| G08 Regimer | Utvärdera flera marknadsregimer där möjligt | SAKNAS; ska redovisa täckning och beroenden mellan trades. |

Dessa nivåer är **minimimål, inte bevis på framtida lönsamhet**. Underkända eller otillräckliga data ska ge underkänd/otillräcklig grind med skäl, inte en automatisk sänkning av kraven. PnL får aldrig garanteras.

## I. Tester, terminal och driftsbevis

| Efterfrågat testområde | Befintligt bevis | Kvarstående |
|---|---|---|
| Discovery och data | `test_discovery.py`, `test_dex_batch.py`, `test_public_http.py`, `test_rug_schema.py` | Faktisk feedtäckning/lag och långvarig reliability. |
| Signal, risk, quote/slippage/fees | `test_signals.py`, `test_risk_runtime.py`, `test_execution.py`, `test_accounting.py` | Exekverbara routekontroller och fristående riskmotor. |
| Moonbag, rugs, saknad likviditet, stale data | `test_moonbag.py`, `test_rug.py`, `test_risk_runtime.py`, `test_replay.py` | Verkliga tx-failures och transferrestriktioner under execution. |
| Dubbletter, ledger, restart, runtime | `test_accounting.py`, `test_runtime.py`, reservtakstester i `test_risk_runtime.py`, dashboard/telemetry i `test_dashboard.py` | Broadcast/crash/expiry/reconciliation och alla orderstate transitions. |
| RPC-outage, congestion, paper/live-paritet | Livegate testas; HTTP-fel täcks. | SAKNAS som full execution-E2E. Identiska input måste ge samma beslut i båda adaptrarna; olika fills ska bokföras korrekt. |

Baslinjen för tabellerna ovan var **157 tester, OK, 2,898 s**. Den senaste körningen i `validation_tests.txt` visar **225 tester, OK, 10,144 s**, inklusive huvudbotens offertintegration, FX, runtime, deluttag, bokföringsavbrott och omstart. Tiden avser kodtester, inte marknadens historik eller latens för verkliga transaktioner. Loggen och implementationsfilernas hash är bevis; testantalet är ingen färdigställandegrad.

`console_dashboard.py` uppdaterar samma terminalyta i interaktiv CMD/VT, sanerar externa texter och undviker upprepade fullskärmsutskrifter vid redirect. `P` pausar nya köp medan exits fortsätter; `Q`/Ctrl+C stoppar kontrollerat. Skärmen visar läge, hälsa, upptäckt, några senaste priser, positions-/PnL-/PF-/drawdown-data, HTTP-latens och avslag. Den återstående dashboardomfattningen är aktiv strategiöversikt för flera strategier, wallet intelligence, full rugrisk, trade-inspektör, faktisk RPC-/executionhälsa, paper/live-jämförelse och daily/cumulative performance. Den snyggare CMD-rutan uppfyller inte dessa datakrav i sig.

## J. Bevisregister och uppdateringsregel

| Bevis | Vad det styrker | Vad det inte styrker |
|---|---|---|
| `validation_tests.txt` | Den namngivna testkörningens resultat | Framtida version, okodade krav eller vinst |
| `NETWORK_VALIDATION_SV.md`, offentliga sample-JSON | Faktiska HTTP/schemakontroller och observerade discovery-/prisantal | Full datatäckning, bindande quote eller tradefill |
| `paper_live_100.status.json`, `.db`, `.log`, `.settings.json`, runmanifest | Faktisk paperdrift, bokföring, konfiguration och källversion för respektive körning | Liveutfall eller 30 dagar/200 trades innan dessa verkligen observerats |
| `validation_final/*.json`, `sample_synthetic.csv` | Reproducerbara syntetiska exit-/kostnadsscenarier | Historisk marknadsvinst |
| `godlike2026_original.py`, aktuella `.py`, testfiler | Spårbara implementerade rättningar och begränsningar | Att legacy- eller oanropad kod är fungerande integration |
| `RESEARCH_SV.md` och primärkällor | Teknisk motivering och dokumenterade API-egenskaper vid kontrollen | Uppmätt prestanda eller strategy alpha |

Framtida ändringar måste ange **krav-ID, ändrad fil/funktion, utförd test/körning, kvarstående begränsning och versions-/tidsbevis**. Ett externt referensrepo kan informera designen men dess README eller testsiffror är inte bevis för vår implementation. Användarens tillstånd att bygga och testa systemet är inte tillstånd att sända en verklig transaktion. Slutmålet ska förbli öppet tills både implementation och de uttryckliga utvärderingskraven är hanterade utan fabricerade resultat.
