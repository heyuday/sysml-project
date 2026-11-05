$ErrorActionPreference = 'Stop'
$project = Split-Path $PSScriptRoot
$batch = Join-Path $project 'artifacts\rq1-command-a'
$upstream = Join-Path $project 'artifacts\low-concurrency-20261104'
$bird = Join-Path (Split-Path $project) 'BIRD-Interact\BIRD-Interact-ADK'
$python = Join-Path $bird '.venv\Scripts\python.exe'
$status = Join-Path $batch 'status.json'
$null = New-Item -ItemType Directory -Force -Path $batch
try {
    $lock = [IO.File]::Open((Join-Path $batch 'launcher.lock'),'OpenOrCreate','ReadWrite','None')
} catch [IO.IOException] {
    return
}
try {
    if (Test-Path $status) {
        $old = Get-Content -Raw $status | ConvertFrom-Json
        if ($old.state -in @('complete','failed')) { return }
    }
    while ($true) {
        $s = Get-Content -Raw (Join-Path $upstream 'status.json') | ConvertFrom-Json
        if ($s.state -eq 'complete' -and $s.total -eq 216 -and $s.completed -eq 216) { break }
        Start-Sleep -Seconds 30
    }
    Set-Location $bird
    $env:PYTHONUTF8='1'
    $proc=Start-Process -FilePath $python -ArgumentList @('-B',(Join-Path $PSScriptRoot 'rq1_repeats.py')) -WorkingDirectory $bird -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $batch 'launcher.log') -RedirectStandardError (Join-Path $batch 'launcher.err.log')
    if ($proc.ExitCode -ne 0) {exit $proc.ExitCode}
} finally {
    $lock.Dispose()
}
