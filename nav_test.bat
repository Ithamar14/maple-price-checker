@echo off
cd /d "%~dp0"

rem One room on each floor, back to the main portal after each, over and over. It never sells.
rem Each time round it takes the next room of each floor, so in time every door gets tried.
rem Log: logs\walk_<date>.log  Marked screenshots: debug\nav_steps  Failures: debug\nav_failed_<n>.png

echo Navigation test: one room on each floor, back to the main portal after each, and again.
echo Start in a Free Market room or on the Entrance, with the minimap showing the map's name.
echo Hold ESC for 1 second to stop it.
echo.
call walk_test.bat --nav-test %*
