$ErrorActionPreference = "Stop"

$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$junction = Join-Path $env:TEMP "bags-workspace-pg"
if (-not (Test-Path -LiteralPath $junction)) {
    Write-Output "Bags PostgreSQL is already stopped."
    exit 0
}
$target = ((Get-Item -LiteralPath $junction -Force).Target | Select-Object -First 1)
if ($target -ne $workspace) {
    throw "The existing Bags PostgreSQL junction points to a different workspace."
}

$bin = Join-Path $junction ".runtime\postgresql-16.15\pgsql\bin"
$data = Join-Path $junction ".runtime\postgres-data"
if (-not (Test-Path -LiteralPath "$bin\pg_ctl.exe") -or -not (Test-Path -LiteralPath "$data\PG_VERSION")) {
    Write-Output "Bags PostgreSQL is already stopped or not installed."
    exit 0
}

& "$bin\pg_ctl.exe" -D $data status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Output "Bags PostgreSQL is already stopped."
    exit 0
}
& "$bin\pg_ctl.exe" -D $data -m fast -w stop
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "Bags PostgreSQL stopped cleanly."
