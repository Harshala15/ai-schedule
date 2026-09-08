param(
    [ValidateSet("BHUPALPALLY", "KASIPET", "SIRMOUR", "KOTHAGUDEM", "OSEPL")]
    [string]$Plant = "BHUPALPALLY",
    [string]$AwsAccountId = "429694361053",
    [string]$Region = "ap-south-1",
    [string]$ImageTag = "latest"
)

$ErrorActionPreference = "Stop"

$plantMap = @{
    "BHUPALPALLY" = @{
        "Repo" = "bhupalpally-forecast-scheduler"
        "Function" = "bhupalpally-forecast-scheduler"
        "Dockerfile" = "bhupalpally_forecast_scheduler/Dockerfile"
    }
    "KASIPET" = @{
        "Repo" = "kasipet-forecast-scheduler"
        "Function" = "kasipet-forecast-scheduler-lambda"
        "Dockerfile" = "kasipet_forecast_scheduler/Dockerfile"
    }
    "SIRMOUR" = @{
        "Repo" = "simour-forecast-scheduler"
        "Function" = "simour-forecast-scheduler"
        "Dockerfile" = "simour_forecast_scheduler/Dockerfile"
    }
    "KOTHAGUDEM" = @{
        "Repo" = "kothagudem-forecast-scheduler"
        "Function" = "kothagudem-forecast-scheduler"
        "Dockerfile" = "kothagudem_forecast_scheduler/Dockerfile"
    }
    "OSEPL" = @{
        "Repo" = "osepl-forecast-scheduler"
        "Function" = "osepl-forecast-scheduler"
        "Dockerfile" = "Dockerfile.osepl"
    }
}


$info = $plantMap[$Plant]
$repositoryName = $info["Repo"]
$functionName = $info["Function"]
$dockerfile = $info["Dockerfile"]


if (-not (Test-Path $dockerfile)) {
    Write-Error "Dockerfile not found at $dockerfile"
}

$ecrUri = "$AwsAccountId.dkr.ecr.$Region.amazonaws.com/$repositoryName"
$imageUri = "${ecrUri}:$ImageTag"

Write-Host "============================================================"
Write-Host "Deploying Forecast Scheduler for $Plant"
Write-Host "ECR Repository: $repositoryName"
Write-Host "Lambda Function: $functionName"
Write-Host "Region: $Region"
Write-Host "============================================================"

Write-Host "1. Logging in to ECR..."
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin "$AwsAccountId.dkr.ecr.$Region.amazonaws.com"

Write-Host "2. Ensuring ECR repository exists..."
$repoExists = $true
try {
    aws ecr describe-repositories --region $Region --repository-names $repositoryName | Out-Null
} catch {
    $repoExists = $false
}

if (-not $repoExists) {
    Write-Host "   Creating ECR repository $repositoryName..."
    aws ecr create-repository --region $Region --repository-name $repositoryName | Out-Null
}

Write-Host "3. Building Docker image using $dockerfile..."
docker build --provenance=false -f $dockerfile -t $repositoryName .

Write-Host "4. Tagging image to $imageUri..."
docker tag "${repositoryName}:latest" $imageUri

Write-Host "5. Pushing image to ECR..."
docker push $imageUri

Write-Host "6. Updating Lambda function code for $functionName..."
aws lambda update-function-code `
    --region $Region `
    --function-name $functionName `
    --image-uri $imageUri | Out-Null
Write-Host "   Successfully updated Lambda function code."


Write-Host "============================================================"
Write-Host "Deployment complete for $Plant forecast scheduler!"
Write-Host "============================================================"

