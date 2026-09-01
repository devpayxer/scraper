@echo off
REM ---------------------------------------------------------------------------
REM Scheduled run for Windows Task Scheduler.
REM
REM Point a Basic Task at this file. "Start in" must be the repo folder, or the
REM relative paths below resolve against C:\Windows\System32.
REM
REM   Program/script : C:\Users\you\scraper\scripts\run_windows.bat
REM   Start in       : C:\Users\you\scraper
REM
REM Tick "Run task as soon as possible after a scheduled start is missed" so a
REM run still happens when the PC was asleep. Missing days costs you nothing --
REM eBay serves the full 90-day history on every request.
REM ---------------------------------------------------------------------------

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo No virtualenv found. Run this first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

set PYTHONUTF8=1
set LOG=scrape.log

echo. >> "%LOG%"
echo ===== %DATE% %TIME% scrape ===== >> "%LOG%"
".venv\Scripts\python.exe" -m ebayparts scrape >> "%LOG%" 2>&1
if errorlevel 1 (
    echo scrape failed, see %LOG%
    exit /b 1
)

echo ===== %DATE% %TIME% report ===== >> "%LOG%"
".venv\Scripts\python.exe" -m ebayparts report --csv >> "%LOG%" 2>&1

echo Done. Open out\report.html
exit /b 0
