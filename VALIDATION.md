# Verifiering – 13 oktober 2026

**Senast körda resultat: 267 automatiska tester godkända**, enligt `validation_tests.txt`: `Ran 267 tests in 17.621s`, `OK`. Python 3.11 på Windows. Tiden gäller kodtester, inte marknadshistorik. `test_quote_broker.py` verifierar huvudbotens signal → offertorder → atomisk bokföring → TP1/TP2/moonbag → offertvärdering samt en runtimecykel. Poolmodellfunktionerna är blockerade i dessa tester, så ett oavsiktligt fallback-anrop ger testfel. Nya tester omfattar osignerad transaktionskonstruktion, RPC-läsningar, signerade syntetiska fixtures, saldometadata och avstämning efter avbrott. Se `EXECUTION_PROGRESS_SV.md` för kvarvarande arbete.

| Område | Exempel på verifierade egenskaper |
| --- | --- |
| Bokföring | Kassa/PnL bevaras, exakta mängder, samtidiga dubbelstängningar, okänd värdering, drawdown och återställning |
| Kostnadsmodell | Analytisk constant-product-beräkning, heltal, kostnadsstress, stor order i liten pool avvisas |
| Moonbag/replay | 40/35/rest, stop-prioritet, gap, stigande trailing, nästa-bar-fills, varaktiga rapporter och faktisk CLI |
| Risk och handelsdjup | Rätt mint/pool, authorities, holderdata, decimals, pooltyp och reservtak; entrytak bevaras till värdering/exit efter omstart |
| Entryhypotes | Warmup, återanvänd cachetick ger ingen ny observation, boosts är inte signal, prisdrift/byte av pool avvisas |
| HTTP/discovery | Verifierad TLS/fallback, begränsade hosts, redirectavslag, partiella flödesfel, deduplicering och prisbatcher |
| Runtime/TUI/telemetri | Databaslås, sparad status/inställning, kontrollerat stopp, terminalsanering och begränsade latensprover |
| Offertmotor och råsaldo | Offertidentitet, bevarad min-output, risk efter requote, atomiskt orderägarskap, native SOL skild från wrapped SOL, exakt kostnadsbas och full rollback vid databasfel |
| Konstruktion/RPC | Sparade riktiga `/build`-bytes kompileras till osignerad v0; signerarspridning, fel ATA, tips, för stor priority fee och gammalt underlag avvisas; transporten tillåter inte broadcast |
| Kedjeavstämning | Syntetiska signerade bytes, prepared-hash/signatur, exakta råbelopp över 2^53, WSOL kontra rent, input/min-output, saknad metadata och on-chain-failure utan fill |
| Avstämning/återstart | Sparade villkor före SUBMITTING, avbruten sändning förblir osäker utan resend; confirmed/failure och bokföringscallback i samma SQL-transaktion; failure-fee bokas högst en gång även vid samtidiga retries |

## Tidigare körda CLI- och replaykontroller

```powershell
py -3 -m py_compile godlike2026.py replay_csv.py
py -3 -X utf8 godlike2026.py --unit-tests
py -3 -X utf8 godlike2026.py --replay-csv sample_synthetic.csv --decimals 6 --cash 1000000 --trade-budget 1000 --stress --report-dir validation_final
py -3 -X utf8 godlike2026.py --live
```

Syntaxkontrollen lyckades. Test-CLI:n avslutades med kod 0. Replay och kostnadsstress kördes med riktiga funktioner från den rättade modulen. Båda fullföljde BUY → TP1 → TP2 → MOONBAG_TRAILING_STOP på **påhittade data** och skrev rapporter. Nätverksblockerade integrationstester kontrollerar att offline-replay inte anropar nätet. `--live` avvisades med avsedd avslutningskod 2 utan order eller nyckelladdning.

Rapporterna i `validation_final` innehåller demoscenarier, inte marknadsprestation. Deras syntetiska ursprung framgår både i konsolen och i JSON. De ska inte användas i en avkastningsprognos.

## Inte verifierat

- Strategins verkliga lönsamhet, prognosvärde eller förmåga att hantera en miljon dollar live.
- Faktiska swappar, säker policykontrollerad live-signering, orderlandning eller avstämning av botens egna sända affärer. Signaturkontroll och återstart är testade med syntetiska fixtures; inga testtransaktioner skickades. Fullständiga route-binära villkor och lookup-tabeller är ännu inte godkända för signering.
- En kedjeexekverad Jupiter-affär. Offertintegration och huvudbotens FX-/likvidationsvärdering är verifierade med aktuella publika läsanrop; `execution_research/quote_portfolio_read_smoke.json` visar 0 skapade order. Ett separat osignerat RPC-test fick korrekt mainnet-genesis och avgiftsuppskattning men simulationen avvisades med `AccountNotFound` för den nya tomma testadressen. Ingen finansierad swap simulerades eller genomfördes. Publika DexScreener-, GeckoTerminal- och RugCheck-anrop har också kontrollerats med TLS-verifiering.
- Verklig historisk entrystrategi på representativa tokenuniversum, inklusive döda/ruggade tokens. Ingen sådan datamängd följde med filen.
- Alla pooltyper, verklig gas/rent per order, MEV, misslyckade nättransaktionsavgifter eller kontinuerlig tickbaserad drawdown.

Godkända tester visar att de angivna kodinvarianterna håller i de testade fallen. De är inte en ekonomisk eller säkerhetsmässig certifiering.

`GOAL_REQUIREMENTS.md` följer samtliga 18 slutkriterier och deras delkrav. Gemensam paper/live-orderlivscykel, live-reconciliation, developer/wallet intelligence, flera validerade strategier, livekalibrering och 30 dagar/200 avslutade trades är inte färdiga. Den dokumenterade driftkontrollen 20:31:59 UTC hade 100 USD i paperkassa, inga affärer och 0 USD PnL.
