@echo off
cd /d "%~dp0"

rem Test of the walking: to the Henesys weapon store, talk to the NPC, back to the Free Market Entrance.
rem MapleStory runs as administrator, so this has to as well, or Windows drops its keys and clicks.
net session >nul 2>&1
if errorlevel 1 (
    echo Asking for administrator rights...
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)

echo Walking test. Hold ESC for 1 second to stop it.
.venv\Scripts\python walker.py %*
pause
