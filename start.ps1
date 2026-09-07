param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$logDirectory = Join-Path $projectRoot 'logs'
$address = 'http://127.0.0.1:18473'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Missing Python environment. Run setup.ps1 first.'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'dist\index.html'))) {
    throw 'Missing frontend build. Run setup.ps1 first.'
}
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$existing = $null
try { $existing = Invoke-RestMethod -Uri "$address/api/auth/session" -TimeoutSec 2 } catch { }
if ($existing -and $existing.app -eq 'grid-studio') {
    Write-Output "Grid Studio is already running: $address"
} else {
    $pythonProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', '18473', '--workers', '1') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'server.out.log') -RedirectStandardError (Join-Path $logDirectory 'server.err.log')
    @{ pid=$pythonProcess.Id; startedTicks=$pythonProcess.StartTime.ToUniversalTime().Ticks.ToString(); workspace=$projectRoot } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logDirectory 'server.json') -Encoding UTF8
    $ready = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        if ($pythonProcess.HasExited) { throw "Server could not start. See logs/server.err.log." }
        try { $health = Invoke-RestMethod -Uri "$address/api/auth/session" -TimeoutSec 1; if ($health.app -eq 'grid-studio') { $ready=$true; break } } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Server did not become ready. See logs/server.err.log.' }
    Write-Output "Grid Studio is ready: $address"
}
if (-not $NoBrowser) { Start-Process $address }
