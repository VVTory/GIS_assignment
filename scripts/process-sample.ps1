param([switch]$HashFiles, [int]$Year = 0, [switch]$PublishMapServices)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create .venv with Python 3.12 and install backend/requirements-lock.txt first.'
}

Push-Location $projectRoot
try {
    $dataFolder = [IO.Path]::GetFullPath((Join-Path $projectRoot 'data'))
    $active = Join-Path $dataFolder 'sample'
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $staging = Join-Path $dataFolder ("staging-$stamp-" + [guid]::NewGuid().ToString('N').Substring(0,8))
    $backup = Join-Path $dataFolder "releases/previous-$stamp"
    foreach ($taskPath in @($active, $staging, $backup)) {
        if (-not [IO.Path]::GetFullPath($taskPath).StartsWith($dataFolder + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Release path leaves the project data folder.' }
    }
    if (Test-Path -LiteralPath $backup) { throw "Backup already exists: $backup" }
    $pipelineArgs = @('--output', $staging)
    if ($HashFiles) { $pipelineArgs += '--hash-files' }
    if ($Year -gt 0) { $pipelineArgs += @('--year', $Year) }
    & $python -m processing.sample_pipeline @pipelineArgs
    if ($LASTEXITCODE -ne 0) { throw 'Sample data processing failed.' }
    $previousDataRoot = $env:EF_DATA_ROOT
    try {
        $env:EF_DATA_ROOT = $staging
        & $python -m scripts.verify_sample_release
        if ($LASTEXITCODE -ne 0) { throw "Staged data verification failed; active release is unchanged. See $staging" }
    } finally {
        if ($null -eq $previousDataRoot) { Remove-Item Env:EF_DATA_ROOT -ErrorAction SilentlyContinue }
        else { $env:EF_DATA_ROOT = $previousDataRoot }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backup) | Out-Null
    $hadActive = Test-Path -LiteralPath $active
    if ($hadActive) { Move-Item -LiteralPath $active -Destination $backup }
    try { Move-Item -LiteralPath $staging -Destination $active }
    catch {
        if ($hadActive -and -not (Test-Path -LiteralPath $active)) { Move-Item -LiteralPath $backup -Destination $active }
        throw
    }
    Write-Host "Published validated data: $active"
    if ($hadActive) { Write-Host "Previous release retained: $backup" }
    if ($PublishMapServices) {
        Push-Location (Join-Path $projectRoot 'backend')
        try {
            & $python scripts/publish_geoserver.py
            if ($LASTEXITCODE -ne 0) { throw 'Map-service upload failed; validated local raster tiles remain available.' }
        } finally { Pop-Location }
    }
} finally {
    Pop-Location
}
