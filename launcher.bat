@echo off
setlocal
cd /d "%~dp0"

rem One window for all the tools. MapleStory runs as administrator, and Windows drops clicks and keys
rem sent to it from programs with fewer rights, so this runs as administrator too.
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

start "" .venv\Scripts\pythonw.exe launcher.py
exit /b

:error
echo.
echo Something went wrong, see the message above.
pause
exit /b 1
