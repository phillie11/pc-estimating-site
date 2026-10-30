<#
.SYNOPSIS
  Compare two `manage.py model_counts` outputs (PythonAnywhere vs AWS). Same as compare-counts.sh.
.EXAMPLE
  .\infra\scripts\compare-counts.ps1 counts-pythonanywhere.txt counts-aws.txt
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Before,
    [Parameter(Mandatory = $true, Position = 1)][string]$After
)
$ErrorActionPreference = 'Stop'

# Keep only "app.Model 123" and "TOTAL 123" lines (drops log lines such as "AXES: BEGIN ...")
function Get-Counts([string]$Path) {
    Get-Content -LiteralPath $Path | ForEach-Object { $_.Trim() } |
        Where-Object { $_ -cmatch '^([A-Za-z_]+\.[A-Za-z0-9_]+|TOTAL) [0-9]+$' } | Sort-Object
}

$a = @(Get-Counts $Before)
$b = @(Get-Counts $After)
if ($a.Count -eq 0) { Write-Host "No counts found in $Before" -ForegroundColor Red; exit 1 }
$differences = @(Compare-Object -ReferenceObject $a -DifferenceObject $b)
if ($differences.Count -eq 0) {
    Write-Host "MATCH: every table has the same number of rows ($($a.Count) lines compared)." -ForegroundColor Green
}
else {
    $differences | ForEach-Object {
        $side = if ($_.SideIndicator -eq '<=') { "only in $Before" } else { "only in $After" }
        Write-Host ("{0,-50} {1}" -f $_.InputObject, $side)
    }
    Write-Host 'DIFFERENT: do not switch over; see infra/scripts/migrate-data.md.' -ForegroundColor Red
    exit 1
}
