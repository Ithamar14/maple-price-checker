@echo off
cd /d "%~dp0"

rem Test of your own store: lists Iron Ore, Mithril Ore, Pig's Ribbon and Lemon, all of each, as if
rem the bot had bought them 20% under their price's reference. It places them at its own prices,
rem then asks before it opens the store. Log: logs\store_<time>.txt

rem MapleStory runs as administrator, and Windows drops clicks/keys sent to it from
rem programs with fewer rights, so this has to run as administrator too.
net session >nul 2>&1
if errorlevel 1 (
    echo Asking for administrator rights ^(MapleStory runs as admin, so this must too^)...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up Python environment...
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)

echo Store test: set up your store first, with your inventory window open beside it.
echo Hold ESC for 1 second to stop it.
echo.
.venv\Scripts\python store.py --item "Iron Ore" --item "Mithril Ore" --item "Pig's Ribbon" --item "Lemon" --assume-paid-under 20
pause
exit /b

:error
echo.
echo Something went wrong, see the message above.
pause
