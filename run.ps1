# Start CIQ Data Intelligence (backend + UI) at http://localhost:8000
# Rebuilds the warehouse first if the claims file, reference files, rules or build code changed.
# Settings come from .env (see .env.example). Usage: .\run.ps1 [-Port 8000] [-Reload]
param([int]$Port = 8000, [switch]$Reload)
Set-Location $PSScriptRoot
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $py -m ciq.build.build_warehouse --if-changed
if ($LASTEXITCODE -ne 0) {
    if (Test-Path "Data\warehouse\ciq.duckdb") {
        Write-Host "Build stopped (see above) - starting with the existing warehouse" -ForegroundColor Yellow
    } else {
        Write-Host "No warehouse to serve - fix the input (python scripts\doctor.py) and try again" -ForegroundColor Red
        exit 1
    }
}
$extra = if ($Reload) { @("--reload", "--reload-dir", "app", "--reload-dir", "ciq") } else { @() }
& $py -m uvicorn app.server:app --host 127.0.0.1 --port $Port @extra
