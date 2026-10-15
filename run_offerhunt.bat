@echo off
setlocal enabledelayedexpansion
title OFFERHUNT - Autonomous Internship Intelligence Engine

echo ========================================================
echo         OFFERHUNT LOCAL SETUP AND LAUNCHER
echo ========================================================
echo.

cd /d "%~dp0"

:: 1. Check Python installation
where py >nul 2>nul
if %errorlevel% neq 0 (
    where python >nul 2>nul
    if %errorlevel% neq 0 (
        echo [ERROR] Python is not found on your system PATH!
        echo Please install Python 3.11+ from https://python.org
        pause
        exit /b 1
    )
    set PY_CMD=python
) else (
    set PY_CMD=py -3.13
)

:: 2. Check virtual environment
if not exist ".venv\Scripts\activate.bat" (
    echo [SETUP] Creating Python virtual environment in .venv...
    %PY_CMD% -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment!
        pause
        exit /b 1
    )
    echo [SETUP] Installing locked dependencies from requirements-lock.txt...
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements-lock.txt
) else (
    call .venv\Scripts\activate.bat
)

:: 3. Check and initialize demo database and seed spreadsheet if needed
if not exist "artifacts\offerhunt.db" (
    echo [SETUP] Initializing clean database and seed fixtures...
    python scripts\reset_demo_data.py
)

:: 4. Check for .env file
if not exist ".env" (
    if exist ".env.example" (
        copy /Y .env.example .env >nul
        echo [NOTICE] Created default .env from .env.example.
    )
)

echo.
echo ========================================================
echo [SUCCESS] OFFERHUNT is ready!
echo Opening application at http://localhost:8501 ...
echo Press Ctrl+C in this terminal window to stop the server.
echo ========================================================
echo.

:: 5. Launch Streamlit app
streamlit run app.py --server.port 8501

pause
