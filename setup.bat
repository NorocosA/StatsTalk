@echo off
setlocal enabledelayedexpansion
title StatsTalk Setup

echo.
echo ========================================
echo    StatsTalk - Setup
echo ========================================
echo.

:: ---- Step 1: Check Python 3.12 ----
echo [1/4] Checking Python 3.12...
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.12 x64 not found. Please install Python 3.12 x64.
    echo          Download: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version
echo        OK

:: ---- Step 2: Install uv and create venv ----
echo.
echo [2/4] Preparing reproducible environment...
python -m pip install uv==0.10.10 -q
if errorlevel 1 exit /b 1
uv venv --python 3.12 .venv
if errorlevel 1 exit /b 1

:: ---- Step 3: Install dependencies ----
echo.
echo [3/4] Installing dependencies...
uv pip sync --python .venv\Scripts\python.exe --require-hashes requirements.lock
if errorlevel 1 (
    echo [ERROR] Locked dependency installation failed
    exit /b 1
) else (
    echo        Dependencies installed
)

:: ---- Step 4: Configure .env ----
echo.
echo [4/4] Configuring environment...
if not exist ".env" (
    copy .env.example .env >nul
    echo        .env created (Demo mode, no API Key needed)
    echo.
    echo   ----------------------------------------
    echo   Demo mode is ON (LLM_MOCK=true)
    echo   No API Key or SPSS required!
    echo.
    echo   For real LLM analysis:
    echo   1. Edit .env file
    echo   2. Set LLM_API_KEY=your-key
    echo   3. Set LLM_MOCK=false
    echo   ----------------------------------------
) else (
    echo        .env already exists, skipping
)

:: ---- Launch ----
echo.
echo ========================================
echo   Setup complete! Starting StatsTalk...
echo ========================================
echo.

start "" .venv\Scripts\python.exe launcher.py

echo   StatsTalk will open on a secure random local port.
echo.
pause
