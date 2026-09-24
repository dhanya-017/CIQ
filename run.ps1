# Start CIQ Data Intelligence (backend + UI) at http://localhost:8000
# Settings come from .env (see .env.example). Usage: .\run.ps1 [-Port 8000] [-Reload]
param([int]$Port = 8000, [switch]$Reload)
Set-Location $PSScriptRoot
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
if (-not (Test-Path "Data\warehouse\ciq.duckdb")) { & $py -m ciq.build.build_warehouse }
$extra = if ($Reload) { @("--reload", "--reload-dir", "app", "--reload-dir", "ciq") } else { @() }
& $py -m uvicorn app.server:app --host 127.0.0.1 --port $Port @extra
