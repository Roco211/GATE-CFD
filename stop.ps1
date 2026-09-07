$ErrorActionPreference = 'Stop'
$recordPath = Join-Path $PSScriptRoot 'logs\server.json'
if (-not (Test-Path -LiteralPath $recordPath)) { Write-Output 'No server started by this launcher.'; exit 0 }
$record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
if ($record.workspace -ne $PSScriptRoot) { throw 'Workspace identity mismatch.' }
$serverProcess = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
if (-not $serverProcess) { Write-Output 'Server is already stopped.'; exit 0 }
$savedTicks = if ($record.startedTicks) { [long]$record.startedTicks } else { ([datetime]$record.started).ToUniversalTime().Ticks }
if ($serverProcess.StartTime.ToUniversalTime().Ticks -ne $savedTicks) { throw 'Process identity changed; refusing to stop an unrelated process.' }
$commandInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)"
if ($commandInfo.CommandLine -notmatch 'uvicorn\s+app.main:app' -or $commandInfo.CommandLine -notmatch [regex]::Escape((Join-Path $PSScriptRoot '.venv\Scripts\python.exe'))) { throw 'Server identity mismatch.' }
# Windows venv Python can own a child interpreter. Stop only descendants of the
# verified server process, and then the verified launcher itself.
$descendants = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($record.pid)"
foreach ($child in $descendants) {
    if ($child.CommandLine -match 'uvicorn\s+app.main:app') { Stop-Process -Id $child.ProcessId -Force }
}
Stop-Process -Id $record.pid -Force -ErrorAction SilentlyContinue
Write-Output 'Grid Studio stopped. Live execution records are saved in data/live.sqlite3.'
