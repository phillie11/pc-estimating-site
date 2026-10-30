<#
.SYNOPSIS
  Run a manage.py command on AWS (Windows). Same behaviour as run-command.sh.

.EXAMPLE
  .\infra\scripts\run-command.ps1 staging model_counts
.EXAMPLE
  .\infra\scripts\run-command.ps1 prod "check_file_scans --retrigger"
.EXAMPLE
  .\infra\scripts\run-command.ps1 prod createsuperuser -Exec

.DESCRIPTION
  Put the manage.py command and its options in quotes if there is more than one word.
  Without -Exec: starts a one-off task (about a minute), runs the command, prints its output and
  finishes with its exit code. Save the output with:  ... | Out-File counts-aws.txt
  With -Exec: opens an interactive session inside a running web container (needs the AWS Session
  Manager plugin). Use it for commands that ask questions, such as createsuperuser.
  Do not put passwords in the command: one-off task commands are recorded in AWS.
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('staging', 'prod')][string]$Environment,
    [Parameter(Mandatory = $true, Position = 1)][string]$Command,
    [switch]$Exec,
    # Used by load-data.ps1: the whole container command, instead of manage.py arguments
    [string[]]$RawCommand = @()
)

. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Tool aws

$config = Get-DeployConfig $Environment
$words = @($Command.Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries))

if ($Exec) {
    Assert-Tool session-manager-plugin
    $taskArn = (Invoke-Aws ecs list-tasks --cluster $config.cluster --service-name $config.services.web `
            --desired-status RUNNING --query 'taskArns[0]' --output text).Trim()
    if (-not $taskArn.StartsWith('arn:')) { Fail "no web task is running in $Environment." }
    Say "Opening python manage.py $Command in $($taskArn.Split('/')[-1])"
    & aws ecs execute-command --cluster $config.cluster --task $taskArn --container $config.container `
        --interactive --command "python manage.py $Command"
    exit $LASTEXITCODE
}

$tmp = New-TempFolder
try {
    $networkFile = Get-NetworkFile $config $tmp
    if ($RawCommand.Count -gt 0) {
        $containerCommand = $RawCommand
        Say "Starting a one-off task: $Command"
    }
    else {
        $containerCommand = @('python', 'manage.py') + $words
        Say "Starting a one-off task: python manage.py $Command"
    }
    $overrides = @{ containerOverrides = @(@{ name = $config.container; command = @($containerCommand) }) }
    $overridesFile = Write-JsonFile (Join-Path $tmp 'overrides.json') $overrides

    $taskArn = (Invoke-Aws ecs run-task --cluster $config.cluster --task-definition $config.families.manage `
            --launch-type FARGATE --network-configuration $networkFile --overrides $overridesFile `
            --started-by run-command --query 'tasks[0].taskArn' --output text).Trim()
    if (-not $taskArn.StartsWith('arn:')) { Fail 'the task did not start.' }
    Say "Waiting for it to finish (task $($taskArn.Split('/')[-1]))"
    $result = Wait-Task $config $taskArn

    # The command's output goes to the pipeline (not Write-Host), so it can be saved with | Out-File
    Get-TaskOutput $config 'manage' $taskArn

    if ($result.Code -ne 0) { Fail "the command failed (exit code $($result.Code): $($result.Reason))" }
}
finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
