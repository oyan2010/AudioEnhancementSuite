@echo off
title Audio Enhancement Suite
echo.
echo  ╔══════════════════════════════════════════════╗
echo  ║      Audio Enhancement Automation Suite      ║
echo  ╚══════════════════════════════════════════════╝
echo.

set PYTHON=A:\Python-MainEXE\python.exe

if not exist "%PYTHON%" (
    echo [ERROR] Python not found at: %PYTHON%
    echo         Please check your Python installation path.
    pause
    exit /b 1
)

echo [INFO] Installing / verifying dependencies...
"%PYTHON%" -m pip install -r "%~dp0requirements.txt" --quiet --disable-pip-version-check

echo [INFO] Launching application...
"%PYTHON%" "%~dp0main.py"

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Application exited with error code %ERRORLEVEL%
    pause
)
