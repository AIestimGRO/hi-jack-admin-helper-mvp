#Requires -Version 5.1
param(
    [switch]$Full
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Counts = @(50, 100)
if ($Full) {
    $Counts = @(50, 100, 300, 500)
}

$ReportDir = Join-Path $Root "docs\load-reports"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
$MatrixPath = Join-Path $ReportDir "matrix.md"
$Lines = @(
    "# JACKSIDE load matrix",
    "",
    "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "",
    "| users | avg_ms | p95_ms | p99_ms | errors | db_locked | duplicates | lost_answers | duration_s |",
    "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
)

foreach ($Users in $Counts) {
    $JsonPath = Join-Path $ReportDir "users-$Users.json"
    Write-Host "Running load with --users $Users ..."
    python -m load.jackside_load --users $Users --report $JsonPath
    $Report = Get-Content -Raw -Path $JsonPath | ConvertFrom-Json
    $Lines += (
        "| $($Report.users) | $($Report.avg_ms) | $($Report.p95_ms) | $($Report.p99_ms) | " +
        "$($Report.errors_count) | $($Report.database_locked_count) | " +
        "$($Report.duplicate_submission_count) | $($Report.lost_answers) | $($Report.duration_total_s) |"
    )
    Copy-Item -Force $JsonPath (Join-Path $ReportDir "latest.json")
}

if (-not $Full) {
    $Lines += ""
    $Lines += "Skipped 300/500 in this run. On a server:"
    $Lines += ""
    $Lines += '```powershell'
    $Lines += '.\load\run_matrix.ps1 -Full'
    $Lines += '# or:'
    $Lines += 'python -m load.jackside_load --users 300 --report docs/load-reports/users-300.json'
    $Lines += 'python -m load.jackside_load --users 500 --report docs/load-reports/users-500.json'
    $Lines += '```'
}

$Lines -join "`n" | Set-Content -Encoding utf8 $MatrixPath
Write-Host "Wrote $MatrixPath"
