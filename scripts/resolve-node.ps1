$ErrorActionPreference = 'Stop'
$runtime = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies'
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    $nodeBin = Join-Path $runtime 'node\bin'
    if (-not (Test-Path -LiteralPath (Join-Path $nodeBin 'node.exe'))) {
        throw 'Install Node.js 22 and pnpm, or make the bundled runtime available.'
    }
    $env:PATH = $nodeBin + ';' + $env:PATH
}
$pnpmCommand = Get-Command pnpm -ErrorAction SilentlyContinue
if ($pnpmCommand) {
    $pnpmCommand.Source
} else {
    $bundledPnpm = Join-Path $runtime 'bin\fallback\pnpm.cmd'
    if (-not (Test-Path -LiteralPath $bundledPnpm)) { throw 'Install pnpm first.' }
    $bundledPnpm
}
