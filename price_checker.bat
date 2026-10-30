@echo off
setlocal
cd /d "%~dp0"
title MapleStory price checker

rem first run: sets up its own Python and packages in this folder (see setup.bat)
call "%~dp0setup.bat" || goto :error
set "PY=.venv\Scripts\python.exe"

if not exist "data\prices.json" (
    echo No price data yet: downloading NPC prices, this takes about 25 minutes...
    "%PY%" fetch_prices.py || goto :error
)

"%PY%" price_checker.py %*
pause
exit /b

:error
echo.
echo Something went wrong, see the message above.
pause
exit /b 1
