@echo off
cd /d "%~dp0"

rem Test of the sell loop, walking only: from Free Market room 6 to the Henesys weapon store, find and
rem double-click the NPC, close its window, then back to room 5. It doesn't sell or change channel.
rem Log: logs\walk_<date>.log  Failure screenshots: debug\nav_failed_<n>.png

echo Sell loop test: room 6 to the NPC and back to room 5.
echo Stand in channel 40, room 6, with the minimap showing the map's name.
echo Hold ESC for 1 second to stop it.
echo.
call walk_test.bat --sell-loop 6 5 %*
