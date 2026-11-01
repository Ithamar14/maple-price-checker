@echo off
cd /d "%~dp0"

rem The full loop: all 40 channels in a Free Market room, walk to the NPC and sell, then the next room.
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

echo Full loop: every channel of each room, going to sell at the NPC when the money runs low.
echo It buys NPC deals without asking, and custom list items by the rules set for each in the launcher.
echo It SELLS EVERYTHING ON THE EQUIP, USE AND ETC TABS at the NPC, except what's on the custom list.
echo Stand in a Free Market room with the minimap showing the map's name.
echo Hold ESC for 1 second to stop it.
echo.
.venv\Scripts\python full_sell_loop.py %*
pause
