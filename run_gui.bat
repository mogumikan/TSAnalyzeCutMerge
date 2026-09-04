@echo off
setlocal
set "PYTHON="

where py >nul 2>&1
if not errorlevel 1 set "PYTHON=py"

if not defined PYTHON (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON=python"
)

if not defined PYTHON (
    echo Python not found. Please install it from https://www.python.org/
    pause
    exit /b 1
)

cd /d "%~dp0"
"%PYTHON%" -c "import tkinterdnd2" >nul 2>&1
if errorlevel 1 "%PYTHON%" -m pip install --quiet tkinterdnd2
"%PYTHON%" main.py
if errorlevel 1 pause
