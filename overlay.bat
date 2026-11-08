@echo off
setlocal
cd /d "%~dp0"

rem The price checker overlay, in its own small window. MapleStory runs as administrator, and Windows
rem keeps its keys (F8, to pause) from programs with fewer rights, so this runs as administrator too.
net session >nul 2>&1
if errorlevel 1 (
    echo Asking for administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem first run: sets up its own Python and packages in this folder (see setup.bat)
call "%~dp0setup.bat" || goto :error

if not exist "data\prices.json" (
    echo No price data yet: downloading NPC prices, this takes about 25 minutes...
    .venv\Scripts\python.exe fetch_prices.py || goto :error
)

start "" .venv\Scripts\pythonw.exe launcher.py overlay
exit /b

:error
echo.
echo Something went wrong, see the message above.
pause
exit /b 1
