$ErrorActionPreference = 'Stop'

$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create .venv with Python 3.12 and install backend/requirements-lock.txt first.'
}

$pnpm = . (Join-Path $PSScriptRoot 'resolve-node.ps1')
Push-Location (Join-Path $PSScriptRoot '..')
try {
    & $python -m pytest -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
} finally {
    Pop-Location
}

Push-Location (Join-Path $PSScriptRoot '..\frontend')
try {
    & $pnpm exec vue-tsc --noEmit
    if ($LASTEXITCODE -ne 0) { throw 'Frontend type check failed.' }
    & $pnpm run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally {
    Pop-Location
}

Write-Host 'Backend tests, frontend type check, and frontend build passed.'
