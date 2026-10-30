<#
.SYNOPSIS
  Load a PythonAnywhere database export into the AWS database (Windows). Same as load-data.sh.

.EXAMPLE
  .\infra\scripts\load-data.ps1 staging C:\Users\you\Downloads\siteline-data.json.gz

.DESCRIPTION
  See infra/scripts/migrate-data.md for the whole procedure. Uploads the file to the environment's
  private transfer bucket (everything there is deleted automatically after 7 days), then runs a one-off
  task that downloads it, loads it in one transaction (all or nothing), resets the ID sequences and
  prints the row counts. The target database must be freshly migrated and empty.
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('staging', 'prod')][string]$Environment,
    [Parameter(Mandatory = $true, Position = 1)][string]$File
)

. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Tool aws

if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { Fail "file not found: $File" }
if (-not $File.EndsWith('.json.gz')) { Fail 'expected a .json.gz file made by dumpdata (see migrate-data.md).' }
$File = (Resolve-Path -LiteralPath $File).Path

$config = Get-DeployConfig $Environment
$bucket = $config.transfer_bucket
$key = 'import/' + (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss') + '-' + (Split-Path $File -Leaf)

$answer = Read-Host "Load $File into the $Environment database? Type $Environment to continue"
if ($answer -ne $Environment) { Fail 'cancelled.' }

Say "Uploading to s3://$bucket/$key"
Invoke-Aws s3 cp $File "s3://$bucket/$key" --only-show-errors | Out-Null

$program = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'lib\load_fixture.py'))
& (Join-Path $PSScriptRoot 'run-command.ps1') $Environment 'load export' -RawCommand @('python', '-c', $program, $bucket, $key)
