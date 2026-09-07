$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonCandidate = Get-Command python -ErrorAction SilentlyContinue
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundledPython) { $pythonCommand=$bundledPython }
elseif ($pythonCandidate) { $pythonCommand=$pythonCandidate.Source }
else { throw 'Install Python 3.11+ first.' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $pythonCommand -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r backend/requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
$pnpmCandidate = Get-Command pnpm -ErrorAction SilentlyContinue
$bundledPnpm = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd'
if ($pnpmCandidate) { $pnpmCommand=$pnpmCandidate.Source }
elseif (Test-Path -LiteralPath $bundledPnpm) { $pnpmCommand=$bundledPnpm }
else { throw 'Install Node.js 22+ and pnpm first.' }
& $pnpmCommand install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
& $pnpmCommand build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
if (-not (Test-Path -LiteralPath 'data\access-control.json')) {
    New-Item -ItemType Directory -Force -Path 'data' | Out-Null
    Copy-Item -LiteralPath 'deploy\access-default.json' -Destination 'data\access-control.json'
}
Write-Output 'Setup complete. Run start.ps1 to open Grid Studio.'
