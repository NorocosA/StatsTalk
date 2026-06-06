@echo off
setlocal enabledelayedexpansion
title StatsTalk Setup

echo.
echo ========================================
echo    StatsTalk - Setup
echo ========================================
echo.

:: ---- Step 1: Check Python ----
echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+
    echo          Download: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version
echo        OK

:: ---- Step 2: Create venv ----
echo.
echo [2/4] Creating virtual environment...
if exist "venv\" (
    echo        venv already exists, skipping
) else (
    python -m venv venv
    echo        venv created
)

:: ---- Step 3: Install dependencies ----
echo.
echo [3/4] Installing dependencies...
call venv\Scripts\activate.bat
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [WARNING] Some packages failed to install
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

start "" venv\Scripts\python.exe launcher.py

echo   Browser: http://localhost:8501
echo.
pause
