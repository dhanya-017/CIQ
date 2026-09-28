# Start CIQ Data Intelligence (backend + UI) at http://localhost:8000
#
# All a new user needs:  1) put master_nsclc.csv in Data\real_data\   2) run .\run.ps1
# First run: creates .venv, installs packages and creates .env (one-time, a few minutes).
# Every run: re-installs packages if requirements.txt changed, rebuilds the warehouse only if the claims file,
# reference files, rules or build code changed, then starts the app.
# Usage: .\run.ps1 [-Port 8000] [-Reload]
param([int]$Port = 8000, [switch]$Reload)
Set-Location $PSScriptRoot
$py = ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "First run: creating the Python environment (.venv)" -ForegroundColor Cyan
    python -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) {
        Write-Host "Python 3.10+ is required (https://www.python.org/downloads/). Install it, then run .\run.ps1 again." -ForegroundColor Red
        exit 1
    }
}
$stamp = ".venv\requirements.installed.txt"
if (-not (Test-Path $stamp) -or ((Get-Content requirements.txt -Raw) -ne (Get-Content $stamp -Raw))) {
    Write-Host "Installing packages (requirements.txt)" -ForegroundColor Cyan
    & $py -m pip install -q --upgrade pip
    & $py -m pip install -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Write-Host "Package install failed - see the messages above." -ForegroundColor Red; exit 1 }
    Copy-Item requirements.txt $stamp
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example (the defaults work; edit it to change settings)" -ForegroundColor Cyan
}

& $py -m ciq.build.build_warehouse --if-changed
if ($LASTEXITCODE -ne 0) {
    if (Test-Path "Data\warehouse\ciq.duckdb") {
        Write-Host "Build stopped (see above) - starting with the existing warehouse" -ForegroundColor Yellow
    } else {
        Write-Host "Nothing to serve yet: put master_nsclc.csv in Data\real_data\ and run .\run.ps1 again." -ForegroundColor Red
        exit 1
    }
}
$extra = if ($Reload) { @("--reload", "--reload-dir", "app", "--reload-dir", "ciq") } else { @() }
Write-Host "Starting CIQ at http://localhost:$Port" -ForegroundColor Green
& $py -m uvicorn app.server:app --host 127.0.0.1 --port $Port @extra
