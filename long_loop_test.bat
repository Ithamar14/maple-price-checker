@echo off
cd /d "%~dp0"

rem Longer test: from Free Market room 6 to the NPC, back to room 5, check 3 shops, change channel, then to the NPC again.
rem It buys nothing in those shops unless you add a budget: long_loop_test.bat --budget 10k
rem Log: logs\walk_<date>.log  Marked screenshots: debug\nav_steps  Failures: debug\nav_failed_<n>.png

echo Long loop test: room 6, NPC, room 5, 3 shops, change channel, NPC.
echo AT THE NPC IT SELLS EVERYTHING ON THE EQUIP, USE AND ETC TABS. Add --no-sell to sell nothing.
echo Stand in room 6 with the minimap showing the map's name.
echo Hold ESC for 1 second to stop it.
echo.
call walk_test.bat --long-loop 6 5 %*
