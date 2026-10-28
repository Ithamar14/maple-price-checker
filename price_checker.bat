@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up Python environment...
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)

if not exist "data\prices.json" (
    echo No price data yet: downloading NPC prices, this takes about 15 minutes...
    .venv\Scripts\python fetch_prices.py || goto :error
)

.venv\Scripts\python price_checker.py %*
pause
exit /b

:error
echo.
echo Something went wrong, see the message above.
pause
