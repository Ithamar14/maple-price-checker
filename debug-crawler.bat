@echo off
cd /d "%~dp0"

rem Short crawler test run for tracking down problems:
rem   --debug              saves a screenshot of every click to debug\clicks (where it aimed vs where
rem                        the game's cursor really is), plus the usual debug screenshots on failures
rem   --no-channel-change  only the shops in the current room
rem   --budget 10k         spends at most 10,000 mesos
rem   --minutes 5          stops by itself after 5 minutes
rem Hold ESC for 1 second to stop it sooner.

echo Debug test run: current room only, 10k budget, 5 minutes max.
echo Stand next to a few shops. Hold ESC for 1 second to stop it.
echo.
call crawler.bat --debug --no-channel-change --budget 10k --minutes 5 %*
