# Kontroller av publika marknads-API:er

Kontrollerat **13 oktober 2026** med publika HTTPS GET-anrop. Inga verkliga order eller plånboksanrop gjordes. Dessa kontroller visar anslutning och datahantering, inte lönsamhet eller garanterad API-tillgänglighet.

## Observerade resultat

- DexScreener-profilflödet svarade HTTP 200 med 30 profiler. RugCheck gav HTTP 200 med token-, marknads- och holder-data.
- En senare kontroll av den utökade scannern gav **175 unika Solana-kandidater**. Grupphämtning gav pris-/pooldata för **26 av 30 efterfrågade tokens**. Saknade tokens räknades inte som lyckade svar. Detta är marknadsobservationer, inte bindande köp-/säljofferter.
- Upptäckt använder fem DexScreener-flöden samt normalt högst fem sidor från GeckoTerminals nya Solana-pooler. Bevakningslistan rymmer högst 200 kandidater, med upp till 60 utvärderingar per cykel och högst 30 tokens per prisanrop.
- Snabba anrop till fler GeckoTerminal-sidor gav HTTP 429 vid kontroll. Delvisa fel sparas per källa och visas som `PARTIAL`/`ERROR`; tidigare giltiga kandidater får behållas. Det finns ingen garanti att alla flöden eller tokens är tillgängliga vid varje cykel.

## Verifierad HTTPS

Python 3.11:s vanliga OpenSSL-kedjeverifiering gav ursprungligen certifikatfel för RugCheck. Windows verifierade kedjan via både `truststore` och systemets `curl.exe`; den aktuella nättransporten använder dessa verifierande vägar när det behövs.

`public_http.py` tillåter bara HTTPS GET mot `api.dexscreener.com`, `api.rugcheck.xyz` och `api.geckoterminal.com`. Den begränsar svarsstorlek och tidsgräns, följer inga omdirigeringar och stänger aldrig av certifikatkontroller. TLS-fel kan utlösa fallback till systemets verifierande curl; andra fel blir diagnostik. HTTP-fel och ogiltig JSON är aldrig godkända riskrapporter.

## RugCheck och poolmodell

De inspekterade rapporterna använder `markets[].liquidityA`/`liquidityB` med avkodade `liquidityAAccount`/`liquidityBAccount`; de tidigare antagna `baseVault`/`quoteVault` fanns inte i dessa svar. `rug_schema.py` kräver samstämmig mint, direkt poolägande, kontostatus och holder-belopp innan ett poolkonto undantas från koncentrationsberäkningen. Detta är samstämmighetskontroll inom rapporten, inte oberoende verifiering direkt på kedjan. Okända protokollauktoriteter räknas fortsatt som koncentration.

Rapportens `mint` måste matcha den begärda minten och rätt målpool måste finnas. Klassisk SPL med `token_extensions: null` stöds. Token-2022-rapporten hade verkliga extension-fält och förblir blockerad. Paper-entry tillåter bara `pump_fun_amm`, `raydium` och `raydium_cpmm`; andra eller okända pooltyper avvisas av constant-product-modellen.

Ett granskat publikt resultat med symbolen Bonk redovisade cirka 50 miljoner USD i poollikviditet men bara cirka **10,9107 USD i quote-reserv** och flera kritiska risker. Den explicita quote-mängden värderas med `priceUsd / priceNative`; modellen antar inte att quote-token alltid är värd en dollar. Denna kontroll begränsar modellens likviditet till **högst cirka 21,8214 USD**, i stället för att lita på avrundad total TVL minus base-värde. RugCheck-reserver för rätt pool kan sänka taket ytterligare. Tokenen ska avvisas. Total likviditet ensam bevisar inte fyllbarhet, och det konservativa taket är fortfarande ingen verklig offert.

## Vad körningen hittills visade

Den sparade papersessionen **22:31:59 svensk tid (20:31:59 UTC)** hade **100 USD kassa, 0 USD nettoresultat och inga genomförda köp eller försäljningar**. Den stoppades kontrollerat för uppgradering med bokföringen kvar. Detta historiska kontrollresultat är inget påstående om senare körstatus: se `paper_live_100.status.json` och `paper_live_100.log`.

Deterministiska offline-tester täcker transport, certifikatbevarande fallback, schemafiltrering, upptäckt och runtime. De ersätter inte körningens faktiska handelsresultat. Sparade publika rapportexempel är `network_sample_report.json` och `network_sample_classic_report.json`; de är inga köprekommendationer.

Källor: [DexScreener API](https://docs.dexscreener.com/api/reference), [GeckoTerminal API-schema](https://api.geckoterminal.com/docs/v2/swagger.json), [RugCheck API-schema](https://api.rugcheck.xyz/swagger/doc.json), [granskad RugCheck-rapport](https://api.rugcheck.xyz/v1/tokens/64DatjYomSAdf12hcTPWemxEgrWuVKWD3YwLjykJJiug/report).
