<#
.SYNOPSIS
  Build, push and roll out Siteline to AWS (Windows). Same steps as deploy.sh.

.EXAMPLE
  .\infra\scripts\deploy.ps1 staging
.EXAMPLE
  .\infra\scripts\deploy.ps1 prod
.EXAMPLE
  .\infra\scripts\deploy.ps1 prod -ImageTag 1a2b3c4d5e6f    # redeploy an image already in ECR (rollback)

.DESCRIPTION
  1. Builds the Docker image from siteline/ (Docker Desktop must be running) and pushes it to ECR.
  2. Registers a new "migrate" task definition with that image and runs it. If migrations fail, it
     stops there and the site stays on the old version.
  3. Records the image tag in /siteline/<env>/image_tag (Terraform reads it).
  4. Registers the other task definitions, updates the web and worker services and waits until the
     new version is healthy. If the new version keeps failing, ECS puts the old one back and this says so.
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('staging', 'prod')][string]$Environment,
    [string]$ImageTag = ''
)

. (Join-Path $PSScriptRoot 'common.ps1')

Assert-Tool aws
Assert-Tool docker
Assert-Tool git

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$tmp = New-TempFolder
try {
    Say "Reading the $Environment deploy settings"
    $config = Get-DeployConfig $Environment
    $repoUrl = $config.repository_url
    $registry = $repoUrl.Split('/')[0]
    $repoName = $repoUrl.Substring($registry.Length + 1)
    $platform = if ($config.cpu_arch -eq 'ARM64') { 'linux/arm64' } else { 'linux/amd64' }
    $networkFile = Get-NetworkFile $config $tmp

    function Test-Image([string]$Tag) {
        $ErrorActionPreference = 'Continue' # "not found" is printed on stderr; that is an answer here, not an error
        & aws ecr describe-images --repository-name $repoName --image-ids "imageTag=$Tag" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }

    # --- 1. Image ---------------------------------------------------------------
    if (-not $ImageTag) {
        Push-Location $repoRoot
        try {
            $dirty = (& git status --porcelain) -join "`n"
            if ($LASTEXITCODE -ne 0) { Fail 'git status failed: run this from inside the pc-estimating-site folder.' }
            if ($dirty -and $Environment -eq 'prod') {
                Fail 'you have uncommitted changes. Commit them first: prod only gets images built from a commit.'
            }
            $ImageTag = ((& git rev-parse --short=12 HEAD) -join '').Trim()
            if ($dirty) { $ImageTag = "$ImageTag-wip-" + (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss') }
        }
        finally { Pop-Location }

        Say 'Logging in to ECR'
        $password = Invoke-Aws ecr get-login-password
        $password | & docker login --username AWS --password-stdin $registry | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'docker login failed. Is Docker Desktop running?' }

        if (Test-Image $ImageTag) {
            Say "Image $ImageTag is already in ECR, not rebuilding"
        }
        else {
            Say "Building $ImageTag for $platform (the first build takes a few minutes)"
            & docker build --platform $platform -t "${repoUrl}:$ImageTag" (Join-Path $repoRoot 'siteline')
            if ($LASTEXITCODE -ne 0) { Fail 'docker build failed (see above).' }
            Say 'Pushing'
            & docker push "${repoUrl}:$ImageTag"
            if ($LASTEXITCODE -ne 0) { Fail 'docker push failed (see above).' }
        }
    }
    elseif (-not (Test-Image $ImageTag)) {
        Fail "image $ImageTag is not in $repoUrl"
    }
    $image = "${repoUrl}:$ImageTag"

    # --- 2. Task definitions ---------------------------------------------------------
    # Copy the latest revision of a family, swap in the new image, register it; returns the new ARN.
    function Register-Revision([string]$Family) {
        $current = Invoke-Aws ecs describe-task-definition --task-definition $Family --include TAGS --output json | ConvertFrom-Json
        $def = $current.taskDefinition
        foreach ($container in $def.containerDefinitions) {
            if ($container.name -eq $config.container) { $container.image = $image }
        }
        foreach ($field in 'taskDefinitionArn', 'revision', 'status', 'requiresAttributes', 'compatibilities',
            'registeredAt', 'registeredBy', 'deregisteredAt') {
            $def.PSObject.Properties.Remove($field)
        }
        if ($current.tags -and @($current.tags).Count -gt 0) {
            $def | Add-Member -NotePropertyName tags -NotePropertyValue @($current.tags) -Force
        }
        $file = Write-JsonFile (Join-Path $tmp "$Family.json") $def
        return (Invoke-Aws ecs register-task-definition --cli-input-json $file --query taskDefinition.taskDefinitionArn --output text).Trim()
    }

    # --- 3. Migrations (before anything starts running the new code) --------------------
    Say 'Running database migrations'
    $migrateArn = Register-Revision $config.families.migrate
    $taskArn = (Invoke-Aws ecs run-task --cluster $config.cluster --task-definition $migrateArn --launch-type FARGATE `
            --network-configuration $networkFile --started-by "deploy-$ImageTag" --query 'tasks[0].taskArn' --output text).Trim()
    if (-not $taskArn.StartsWith('arn:')) { Fail 'the migrate task did not start.' }
    $result = Wait-Task $config $taskArn
    Get-TaskOutput $config 'migrate' $taskArn | ForEach-Object { Write-Host $_ }
    if ($result.Code -ne 0) {
        Fail "migrations failed (exit code $($result.Code): $($result.Reason)). Nothing else was changed; the site is still on the old version."
    }

    # From here Terraform also uses the new image (it reads this parameter)
    Invoke-Aws ssm put-parameter --name "/siteline/$Environment/image_tag" --value $ImageTag --type String --overwrite | Out-Null

    # --- 4. Everything else ----------------------------------------------------------------
    Say 'Registering the other task definitions'
    $arns = @{}
    foreach ($key in ($config.families.PSObject.Properties.Name | Where-Object { $_ -ne 'migrate' })) {
        $arns[$key] = Register-Revision $config.families.$key
    }

    Say 'Updating the web and worker services'
    Invoke-Aws ecs update-service --cluster $config.cluster --service $config.services.web --task-definition $arns['web'] | Out-Null
    Invoke-Aws ecs update-service --cluster $config.cluster --service $config.services.worker --task-definition $arns['worker'] | Out-Null

    Say 'Waiting for the new version to be healthy (usually 2-5 minutes)'
    & aws ecs wait services-stable --cluster $config.cluster --services $config.services.web $config.services.worker
    if ($LASTEXITCODE -ne 0) { & aws ecs wait services-stable --cluster $config.cluster --services $config.services.web $config.services.worker }

    # If the new tasks kept failing, ECS rolls back to the previous version and still reports "stable"
    $service = (Invoke-Aws ecs describe-services --cluster $config.cluster --services $config.services.web --output json | ConvertFrom-Json).services[0]
    $primary = $service.deployments | Where-Object { $_.status -eq 'PRIMARY' } | Select-Object -First 1
    if ($primary.taskDefinition -ne $arns['web'] -or $primary.rolloutState -ne 'COMPLETED') {
        Fail "web did not switch to the new version ($($primary.taskDefinition) $($primary.rolloutState)). ECS kept the previous version running. Logs: aws logs tail $($config.log_group) --since 30m"
    }

    Say "Deployed $ImageTag to $Environment"
}
finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
