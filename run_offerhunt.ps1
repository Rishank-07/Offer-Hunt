# OFFERHUNT Local Setup & Launcher Script
Set-Location -Path $PSScriptRoot

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "       OFFERHUNT LOCAL SETUP AND LAUNCHER" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# Check .venv
if (-not (Test-Path ".venv\Scripts\activate.ps1")) {
    Write-Host "[SETUP] Creating Python virtual environment in .venv..." -ForegroundColor Yellow
    py -3.13 -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        python -m venv .venv
    }
    Write-Host "[SETUP] Installing locked dependencies..." -ForegroundColor Yellow
    & .\.venv\Scripts\pip.exe install -r requirements-lock.txt
}

# Ensure demo environment is initialized
if (-not (Test-Path "artifacts\offerhunt.db")) {
    Write-Host "[SETUP] Initializing database and demo artifacts..." -ForegroundColor Yellow
    & .\.venv\Scripts\python.exe scripts\reset_demo_data.py
}

# Ensure .env exists
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item .env.example .env
        Write-Host "[NOTICE] Created default .env from .env.example" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "[SUCCESS] Starting OFFERHUNT on http://localhost:8501..." -ForegroundColor Green
Write-Host "Press Ctrl+C in this terminal window to stop the server." -ForegroundColor Gray
Write-Host ""

& .\.venv\Scripts\streamlit.exe run app.py --server.port 8501
