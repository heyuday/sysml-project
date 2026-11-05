$ErrorActionPreference = 'Stop'
$project = Split-Path $PSScriptRoot
$batch = Join-Path $project 'artifacts\rq1-command-a-full-lite'
$bird = Join-Path (Split-Path $project) 'BIRD-Interact\BIRD-Interact-ADK'
$python = Join-Path $bird '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'rq1_repeats.py'
$null = New-Item -ItemType Directory -Force -Path $batch
$lock = [IO.File]::Open((Join-Path $batch 'launcher.lock'),'OpenOrCreate','ReadWrite','None')
try {
    Set-Location $bird
    $env:PYTHONUTF8 = '1'
    $status = Join-Path $batch 'status.json'
    if ((Test-Path -LiteralPath $status) -and (Get-Content -Raw $status | ConvertFrom-Json).state -eq 'complete') { return }
    for ($attempt = 0; $attempt -lt 4; $attempt++) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $proc = Start-Process -FilePath $python -ArgumentList @('-B',$runner,'--full-lite') -WorkingDirectory $bird -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $batch "launcher-$stamp.log") -RedirectStandardError (Join-Path $batch "launcher-$stamp.err.log")
        if ($proc.ExitCode -eq 0) { return }
        if ($attempt -lt 3) { Start-Sleep -Seconds (60 * ($attempt + 1)) }
    }
    exit 1
} finally {
    $lock.Dispose()
}
