$ErrorActionPreference = "Stop"

$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$junction = Join-Path $env:TEMP "bags-workspace-pg"
if (Test-Path -LiteralPath $junction) {
    $target = ((Get-Item -LiteralPath $junction -Force).Target | Select-Object -First 1)
    if ($target -ne $workspace) {
        throw "The existing Bags PostgreSQL junction points to a different workspace."
    }
} else {
    New-Item -ItemType Junction -Path $junction -Target $workspace | Out-Null
}

$bin = Join-Path $junction ".runtime\postgresql-16.15\pgsql\bin"
$data = Join-Path $junction ".runtime\postgres-data"
$log = Join-Path $junction ".runtime\logs\postgres.log"
foreach ($required in @("$bin\pg_ctl.exe", "$bin\pg_isready.exe", "$data\PG_VERSION")) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Portable PostgreSQL is incomplete: $required"
    }
}

& "$bin\pg_ctl.exe" -D $data status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    & "$bin\pg_ctl.exe" -D $data -l $log -o "-h 127.0.0.1 -p 5432" -w start
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& "$bin\pg_isready.exe" -h 127.0.0.1 -p 5432 -U bags -d bags -t 10
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "Bags PostgreSQL is ready on 127.0.0.1:5432."
