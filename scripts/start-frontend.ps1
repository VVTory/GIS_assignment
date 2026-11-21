$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$pnpm = . (Join-Path $PSScriptRoot 'resolve-node.ps1')
Push-Location (Join-Path $projectRoot 'frontend')
try {
    & $pnpm exec vite --host 127.0.0.1 --port 5173 --strictPort
    if ($LASTEXITCODE -ne 0) { throw 'Frontend failed to start.' }
} finally {
    Pop-Location
}
