@echo off
setlocal
cd /d "%~dp0"
title MapleStory crafting calculator

rem first run: sets up its own Python and packages in this folder (see setup.bat)
call "%~dp0setup.bat" || goto :error
set "PY=.venv\Scripts\python.exe"

if not exist "data\recipes.json" (
    echo No recipes yet: downloading item data, this takes about 25 minutes...
    "%PY%" fetch_prices.py || goto :error
)

rem looks up market prices, then opens craft_calculator.html in your browser
"%PY%" crafting.py %* || goto :error
exit /b

:error
echo.
echo Something went wrong, see the message above.
pause
exit /b 1
