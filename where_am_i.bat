@echo off
cd /d "%~dp0"

rem Shows what the bot reads from the minimap, over the game. It only reads the screen.
rem Hold SPACE for 1 second to save where you're standing, as the next point it asks for (data\nav_points.json).
rem MapleStory runs as administrator, and Windows hides its key presses from programs with fewer rights.
net session >nul 2>&1
if errorlevel 1 (
    echo Asking for administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

.venv\Scripts\python navigation.py %*
pause
