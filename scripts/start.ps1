param(
    [ValidateSet('sample', 'full')]
    [string]$Mode = 'sample'
)

$ErrorActionPreference = 'Stop'

if ($Mode -eq 'full') {
    throw 'Full mode is not available: no validated full-data release is installed.'
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker Desktop is required. Install/start Docker Desktop and retry.'
}

docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose is unavailable. Update Docker Desktop and retry.'
}

docker compose up --build -d
if ($LASTEXITCODE -ne 0) {
    throw 'Services did not start. Inspect Docker Compose logs for the failing service.'
}

Write-Host 'Frontend: http://127.0.0.1:5173'
Write-Host 'API docs: http://127.0.0.1:8000/api/docs'
Write-Host 'Mode: processed sample release (run scripts/process-sample.ps1 first)'
