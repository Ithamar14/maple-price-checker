@echo off
cd /d "%~dp0"

rem MapleStory runs as administrator, and Windows drops clicks/keys sent to it from
rem programs with fewer rights, so the crawler has to run as administrator too.
net session >nul 2>&1
if errorlevel 1 (
    echo Asking for administrator rights ^(MapleStory runs as admin, so the crawler must too^)...
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up Python environment...
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)

echo Free Market crawler. Hold ESC for 1 second to stop it.
.venv\Scripts\python crawler.py %*
pause
exit /b

:error
echo.
echo Something went wrong, see the message above.
pause
