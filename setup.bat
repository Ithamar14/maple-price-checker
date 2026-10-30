@echo off
rem Makes sure everything the tools need is set up inside this folder (internet needed once):
rem   tools\uv.exe      a small installer (uv, by Astral) that fetches Python and the packages
rem   tools\python\     its own copy of Python, so you don't need to install Python yourself
rem   .venv\            the packages (OCR, OpenCV, ...)
rem Delete those to start over; delete the whole folder to remove everything.
rem Called by price_checker.bat and craft_calculator.bat; exits with an error code if it fails.
setlocal
cd /d "%~dp0"

set "UV_VERSION=0.12.24"
set "UV_SHA256=7c38608c8a18ee137d748a1773053b07ec8f3a30fab49aebaa6f4e4efeceb019"
set "UV=%~dp0tools\uv.exe"
set "UV_PYTHON_INSTALL_DIR=%~dp0tools\python"
set "UV_CACHE_DIR=%~dp0tools\cache"
set "UV_LINK_MODE=copy"
set "PY=.venv\Scripts\python.exe"

if exist "%PY%" (
    "%PY%" -c "import tkinter, cv2, mss, rapidocr, onnxruntime, rapidfuzz" >nul 2>&1 && exit /b 0
)

echo Setting up (first run only, takes a few minutes and about 400 MB)...
echo.
if not exist "%UV%" (
    echo [1/3] Downloading the installer...
    if not exist tools mkdir tools
    curl.exe -fL --progress-bar -o tools\uv.zip "https://github.com/astral-sh/uv/releases/download/%UV_VERSION%/uv-x86_64-pc-windows-msvc.zip" || (
        echo.
        echo Couldn't download the installer. Check your internet connection and try again.
        exit /b 1
    )
    certutil -hashfile tools\uv.zip SHA256 | findstr /i /x "%UV_SHA256%" >nul || (
        echo The download doesn't match what was expected, so it won't be run.
        del tools\uv.zip
        exit /b 1
    )
    tar -xf tools\uv.zip -C tools || exit /b 1
    del tools\uv.zip
)

echo [2/3] Getting Python...
rem a setup that's missing something (or made by an older version) is replaced, not patched
if exist .venv rmdir /s /q .venv
"%UV%" venv --python 3.12 --python-preference only-managed .venv || exit /b 1

echo [3/3] Installing packages...
"%UV%" pip install --python "%PY%" -r requirements.txt || exit /b 1
"%UV%" cache clean >nul 2>&1
echo.
echo Setup done.
echo.
exit /b 0
