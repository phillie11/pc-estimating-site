# Shared helpers for the Siteline PowerShell scripts (deploy.ps1, run-command.ps1, load-data.ps1).
# Works in Windows PowerShell 5.1 and PowerShell 7. Not run on its own.

$ErrorActionPreference = 'Stop'
$env:AWS_REGION = 'eu-west-2'
$env:AWS_DEFAULT_REGION = 'eu-west-2'
$env:AWS_PAGER = ''

function Say([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Assert-Tool([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { Fail "$Name is not installed (see infra/README.md step 1)." }
}

# Run the AWS CLI; stop with a clear message if it fails. Returns its output as one string.
function Invoke-Aws {
    $output = & aws @args
    if ($LASTEXITCODE -ne 0) { Fail "this AWS command failed: aws $($args -join ' ')" }
    return ($output -join "`n")
}

# Write JSON as UTF-8 without a byte-order mark (the AWS CLI cannot read a BOM) and return a file:// URL.
# JSON always goes through a file: Windows PowerShell 5.1 mangles quotes in JSON passed as an argument.
function Write-JsonFile([string]$Path, $Object) {
    $json = ConvertTo-Json -InputObject $Object -Depth 50 -Compress
    [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding $false))
    return "file://$Path"
}

function New-TempFolder {
    $path = Join-Path ([System.IO.Path]::GetTempPath()) ("siteline-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $path | Out-Null
    return $path
}

function Get-DeployConfig([string]$EnvName) {
    $value = & aws ssm get-parameter --name "/siteline/$EnvName/deploy" --query Parameter.Value --output text
    if ($LASTEXITCODE -ne 0) { Fail "could not read /siteline/$EnvName/deploy. Has terraform apply been run for $EnvName, and are you signed in (aws sso login)?" }
    return (($value -join "`n") | ConvertFrom-Json)
}

function Get-NetworkFile($Config, [string]$Folder) {
    $network = @{ awsvpcConfiguration = @{
            subnets        = @($Config.subnets)
            securityGroups = @($Config.security_groups)
            assignPublicIp = 'ENABLED'
        }
    }
    return (Write-JsonFile (Join-Path $Folder 'network.json') $network)
}

# Wait for a one-off task to stop (up to 20 minutes); returns the app container's exit code.
function Wait-Task($Config, [string]$TaskArn) {
    & aws ecs wait tasks-stopped --cluster $Config.cluster --tasks $TaskArn
    if ($LASTEXITCODE -ne 0) { & aws ecs wait tasks-stopped --cluster $Config.cluster --tasks $TaskArn }
    $task = (Invoke-Aws ecs describe-tasks --cluster $Config.cluster --tasks $TaskArn --output json | ConvertFrom-Json).tasks[0]
    $container = $task.containers | Where-Object { $_.name -eq $Config.container }
    if ($null -eq $container.exitCode) { return @{ Code = -1; Reason = $task.stoppedReason } }
    return @{ Code = [int]$container.exitCode; Reason = $task.stoppedReason }
}

# Everything the task printed, line by line.
function Get-TaskOutput($Config, [string]$Prefix, [string]$TaskArn) {
    Start-Sleep -Seconds 5 # the last lines can take a few seconds to reach CloudWatch
    $taskId = $TaskArn.Split('/')[-1]
    $raw = & aws logs get-log-events --log-group-name $Config.log_group `
        --log-stream-name "$Prefix/$($Config.container)/$taskId" --start-from-head --output json
    if ($LASTEXITCODE -ne 0) { return @() }
    return @((($raw -join "`n") | ConvertFrom-Json).events | ForEach-Object { $_.message })
}
