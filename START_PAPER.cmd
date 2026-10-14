@echo off
setlocal
cd /d "%~dp0"
title GODLIKE2026 - PAPER ONLY - no real orders
where py >nul 2>&1
if errorlevel 1 (
 echo Install Python 3.11+ first.
 pause
 exit /b 1
)
echo.
echo Experimental paper trading. No real money. Profitability NOT validated.
echo Install dependencies first: py -3 -m pip install -r requirements.txt
echo Press Ctrl+C to stop. Reuses paper_quotes_100.db after restart.
echo.
py -3 -X utf8 -u godlike2026.py --realtime --execution quote --duration 0 --cash 100 --trade-budget 20 --db paper_quotes_100.db --report-dir reports
set RET=%errorlevel%
echo.
echo Process exited with code %RET%. Database was not reset.
pause
exit /b %RET%
