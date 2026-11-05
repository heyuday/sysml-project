# Explicit provisioning only. No execution phase. Refuse to load over existing data.
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot)
try {
    docker compose up -d --wait postgres redis
    if ($LASTEXITCODE) { throw 'Service startup failed' }
    $count = docker compose exec -T postgres psql -U bench -d chbenchmark -Atc "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
    if ($LASTEXITCODE -or [int]$count -ne 0) { throw 'The CH template must be empty before initial provisioning.' }
    docker compose --profile tools build loader
    if ($LASTEXITCODE) { throw 'Loader build failed' }
    docker compose --profile tools run --rm loader --create=true --load=true --execute=false
    if ($LASTEXITCODE) { throw 'BenchBase load failed' }
    $env:SYSML_DSN='postgresql://bench:localbench@127.0.0.1:55432/chbenchmark'
    & .\.venv\Scripts\python.exe -m sysml.cli install-cdc
    if ($LASTEXITCODE) { throw 'CDC installation failed' }
} finally { Pop-Location }
