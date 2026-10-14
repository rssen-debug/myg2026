# GODLIKE2026 – audit snapshot (2026-10-14)

This branch is an **isolated import of the 38-file public source** from [smr293/godlike2026-fixed](https://github.com/smr293/godlike2026-fixed) at commit `fb33d045dcce04bc92de37c9b317bf9688ebf9a3`. It is held in `rssen-debug/myg2026` on branch `godlike2026-paper-audit`; **the repository's default branch was not modified**. This is a source-copy, not a recreation of the original git commit history. The upstream SHA256 manifest is preserved as `SOURCE_MANIFEST_SHA256.json`; it describes the original files, **not** this modified branch.

## What is changed
1. Imported all 38 source files for inspection and future review.
2. `godlike2026.py --unit-tests` now fails if zero tests are discovered, instead of emitting a misleading green result.
3. Added `START_PAPER.cmd` (explicitly PAPER quote execution only) and a `.gitignore` exception so the documented launcher can be checked in.
4. Added checked-in offline tests for order idempotency, invalid transitions, discovery filtering, rug-vault corroboration, execution policies, and basic release constraints.
5. Added GitHub Actions CI that compiles the code, installs requirements, runs tests, checks the live CLI stays blocked, and runs a synthetic replay smoke check.

## Important evidence constraints
- The upstream documentation says **267 tests previously passed**, but the original `test_*.py` test sources were **not** included in the public 38-file upload. That historical claim cannot be independently reproduced from this source as given.
- A passing CI checks only the tests in **this** branch; it is not evidence of 267 original tests, financial profitability, a valid strategy, or end-to-end live execution.
- The last upstream-described $100 paper session showed **zero fills**, **zero net PnL**, and a scanner. No economic edge is proven.
- Historical CSV replay is a **manually selected entry / exit-rule demonstration**, not a historical backtest of the token selection strategy.
- Jupiter quote-based paper does not simulate chain inclusion, all failure fees, MEV, or real fills. **LIVE MUST REMAIN BLOCKED**.

## Release decision
- **Source review:** in progress.
- **Offline regression smoke:** automated in this branch; inspect GitHub Actions for the actual status.
- **Real-market paper execution:** not run in this environment (no network access to exchange/RPC endpoints).
- **Trading performance:** NOT VALIDATED.
- **Live:** NOT READY, blocked.
- **Profitable:** NOT ESTABLISHED.

## What must be done before considering real money
1. Recover original Astra tests and restore meaningful end-to-end regression coverage.
2. Collect an unbiased, timestamped history of *all* launch candidates including dead/rugged ones, rejected and accepted.
3. Backtest entry selection at historical observation times without lookahead, with execution costs/slippage/failure assumptions, and out-of-sample validation.
4. Forward-test against current market quotes for at least 30 calendar days and a meaningful sample (original project target: 200 closed simulated trades if valid opportunities exist). Report profitability net of expenses, capital tied up, drawdown, missing quotes and rejected trades.
5. Have an independently verified, separately approved execution adapter; never enable live just by deleting an explicit guard.

## Local quick start (paper only)
```powershell
py -3 -m pip install -r requirements.txt
py -3 godlike2026.py --unit-tests
py -3 -X utf8 -u godlike2026.py --realtime --execution quote --duration 0 --cash 100 --trade-budget 20 --db paper_quotes_100.db --report-dir reports
```

Paper data is retained across restarts when the same database path is used.
