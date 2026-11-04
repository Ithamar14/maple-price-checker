@echo off
cd /d "%~dp0"

rem Takes the screenshots the store bot is built from, step by step: the step shows over the game.
rem F9 screenshot, F10 next step, F11 back. Screenshots go to printscreens\store_setup.
rem MapleStory runs as administrator, and Windows hides its key presses from programs with fewer rights.
net session >nul 2>&1
if errorlevel 1 (
    echo Asking for administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

.venv\Scripts\python store_capture.py %*
pause
