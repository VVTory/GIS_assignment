param([switch]$OpenBrowser, [switch]$RefreshLayers)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $project 'backend'
$frontend = Join-Path $project 'frontend'
$services = Join-Path $project 'tmp/services'
$dataDir = Join-Path $services 'geoserver-data'
$logs = Join-Path $project 'tmp/logs'
$python = Join-Path $project '.venv/Scripts/python.exe'
$war = Join-Path $services 'geoserver-2.28.2.war'
$jetty = Join-Path $services 'jetty-runner-9.4.57.jar'
$vite = Join-Path $frontend 'node_modules/vite/bin/vite.js'
$apiUrl = 'http://127.0.0.1:8002'
$geoUrl = 'http://127.0.0.1:8600/geoserver'
$webUrl = 'http://127.0.0.1:5175'

function Test-Url([string]$url) {
    try { $response = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing; return ($response.StatusCode -eq 200) }
    catch { return $false }
}
function Start-Hidden([string]$file, [string[]]$arguments, [string]$workingDirectory, [string]$name) {
    $quotedArguments = $arguments | ForEach-Object { '"' + $_ + '"' }
    $serviceProcess = Start-Process -FilePath $file -ArgumentList $quotedArguments -WorkingDirectory $workingDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs "$name.out.log") -RedirectStandardError (Join-Path $logs "$name.err.log") -PassThru
    $serviceProcess.Id | Set-Content (Join-Path $logs "$name.pid")
}

New-Item -ItemType Directory -Force -Path $logs, $dataDir | Out-Null
foreach ($path in @($python, $vite)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "缺少项目运行文件：$path" }
}

if (-not (Test-Url "$apiUrl/health/live")) {
    Start-Hidden -file $python -arguments @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8002') -workingDirectory $backend -name 'api'
}
$java = if ($env:JAVA_HOME) { Join-Path $env:JAVA_HOME 'bin/java.exe' } else { (Get-Command java -ErrorAction SilentlyContinue).Source }
if (-not $java -or -not (Test-Path -LiteralPath $java)) {
    $java = 'C:/Program Files/Harris/ENVI56/IDL88/bin/bin.x86_64/jre/bin/java.exe'
}
if (-not (Test-Url "$geoUrl/web/") -and (Test-Path -LiteralPath $java) -and (Test-Path -LiteralPath $war) -and (Test-Path -LiteralPath $jetty)) {
    $previousDataDir = $env:GEOSERVER_DATA_DIR
    $env:GEOSERVER_DATA_DIR = $dataDir
    Start-Hidden -file $java -arguments @('-jar', $jetty, '--host', '127.0.0.1', '--port', '8600', '--path', '/geoserver', $war) -workingDirectory $project -name 'geoserver'
    if ($null -eq $previousDataDir) { Remove-Item Env:GEOSERVER_DATA_DIR -ErrorAction SilentlyContinue }
    else { $env:GEOSERVER_DATA_DIR = $previousDataDir }
}

$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) {
    $node = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
}
if (-not (Test-Path -LiteralPath $node)) { throw '未找到 Node.js。请安装 Node.js 20 或更高版本。' }
if (-not (Test-Url $webUrl)) {
    $previousApiTarget = $env:VITE_API_TARGET
    $env:VITE_API_TARGET = $apiUrl
    Start-Hidden -file $node -arguments @($vite, '--host', '127.0.0.1', '--port', '5175', '--strictPort') -workingDirectory $frontend -name 'web'
    if ($null -eq $previousApiTarget) { Remove-Item Env:VITE_API_TARGET -ErrorAction SilentlyContinue }
    else { $env:VITE_API_TARGET = $previousApiTarget }
}

foreach ($check in @("$apiUrl/health/ready", "$webUrl/api/v1/catalog", $webUrl)) {
    $ready = $false
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        if (Test-Url $check) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "服务启动超时：$check。请检查 tmp/logs 中的启动记录。" }
}
if ($RefreshLayers) {
    if (-not (Test-Url "$geoUrl/web/")) { throw '地图服务尚未就绪，请稍后使用 -RefreshLayers 重试。网页仍可通过本地瓦片显示地图。' }
    Push-Location $backend
    try { & $python scripts/publish_geoserver.py; if ($LASTEXITCODE -ne 0) { throw 'GeoServer 图层发布失败。' } }
    finally { Pop-Location }
}
Write-Host "网页已就绪：$webUrl/"
Write-Host "接口健康检查：$apiUrl/health/ready"
Write-Host "GeoServer：$geoUrl/web/"
if ($OpenBrowser) { Start-Process "$webUrl/" -WindowStyle Hidden }
