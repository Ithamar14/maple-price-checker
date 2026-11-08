@echo off
setlocal
cd /d "%~dp0"

rem The crafting calculator, in its own small window. It doesn't touch the game, so no administrator rights.

rem first run: sets up its own Python and packages in this folder (see setup.bat)
call "%~dp0setup.bat" || goto :error

start "" .venv\Scripts\pythonw.exe launcher.py craft
exit /b

:error
echo.
echo Something went wrong, see the message above.
pause
exit /b 1
