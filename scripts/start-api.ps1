$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create .venv with Python 3.12 and install backend/requirements-lock.txt first.'
}
Push-Location $projectRoot
try {
    & $python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
    if ($LASTEXITCODE -ne 0) { throw 'API failed to start.' }
} finally {
    Pop-Location
}
