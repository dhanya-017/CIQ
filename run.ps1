# Start CIQ Data Intelligence (backend + UI) at http://localhost:8000
# Optional overrides: $env:AWS_PROFILE, $env:AWS_REGION, $env:CIQ_MODEL, $env:CIQ_EFFORT
param([int]$Port = 8000)
Set-Location $PSScriptRoot
if (-not (Test-Path "Data\warehouse\ciq.duckdb")) { python -m ciq.build.build_warehouse }
python -m uvicorn app.server:app --host 127.0.0.1 --port $Port
