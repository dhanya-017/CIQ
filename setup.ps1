# One-time developer setup (Windows). Usage: .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "1/5 Creating virtual environment (.venv)" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { python -m venv .venv }
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "2/5 Installing packages" -ForegroundColor Cyan
& $py -m pip install --upgrade pip -q
& $py -m pip install -r requirements.txt -q

Write-Host "3/5 Creating .env from .env.example (if missing)" -ForegroundColor Cyan
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }

Write-Host "4/5 Building the warehouse from the claims file" -ForegroundColor Cyan
& $py -m ciq.build.build_warehouse --if-changed
if ($LASTEXITCODE -eq 0) {
    & $py -m pytest tests -q
} else {
    Write-Host "   Build skipped or stopped - put master_nsclc.csv in Data\real_data\ (see Data\README.md), then rerun .\setup.ps1" -ForegroundColor Yellow
}

Write-Host "5/5 Environment check" -ForegroundColor Cyan
& $py scripts\doctor.py
