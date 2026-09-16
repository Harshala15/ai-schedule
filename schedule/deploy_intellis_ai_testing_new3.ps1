param(
  [string]$Profile = "intellis-608",
  [string]$Region = "ap-south-1",
  [string]$Bucket = "vedanjay-schedules-test-608744602858",
  [string]$RoleName = "global1-lambda-role",
  [string]$RepoName = "intellis-ai-scheduler",
  [string]$Tag = "intellis-ai-$(Get-Date -Format 'yyyyMMdd-HHmmss')",
  [string]$OpenMeteoApiKey = "jbThkFlLZSXZE3CU"
)

function awscli {
  if (Get-Command aws -ErrorAction SilentlyContinue) {
    aws @args
  } else {
    py -m awscli @args
  }
}

$ErrorActionPreference = "Stop"

$Sites = @(
  "SIRMOUR",
  "BHUPALPALLY",
  "KASIPET",
  "KOTHAGUDEM",
  "OSEPL",
  "ANJANGOAN",
  "BAMKHAL",
  "BALAKWADA",
  "CME",
  "ANDAD",
  "SAWDA",
  "GUGARIYAKHEDI",
  "NANDGAON",
  "GSNP",
  "ZTRIC"
)

$AccountId = (awscli sts get-caller-identity --profile $Profile --query Account --output text).Trim()
Write-Host "Deploying to AWS Account: $AccountId with Profile: $Profile"

if ($AccountId -ne "608744602858") {
  Write-Warning "Target account is $AccountId (expected 608744602858)."
}

$Ecr = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$ImageUri = "$Ecr/$RepoName`:$Tag"
$RoleArn = "arn:aws:iam::$AccountId`:role/$RoleName"

Write-Host "Ensuring ECR repository $RepoName exists..."
try {
  awscli ecr describe-repositories --profile $Profile --region $Region --repository-names $RepoName | Out-Null
} catch {
  awscli ecr create-repository --profile $Profile --region $Region --repository-name $RepoName | Out-Null
}

Write-Host "Authenticating Docker to ECR..."
awscli ecr get-login-password --profile $Profile --region $Region | docker login --username AWS --password-stdin $Ecr

Write-Host "Building Docker image $RepoName`:$Tag..."
docker build --platform linux/amd64 --provenance=false --sbom=false `
  -f ".\Dockerfile.intellis-ai-lambda" `
  -t "$RepoName`:$Tag" .

Write-Host "Tagging and pushing image to $ImageUri..."
docker tag "$RepoName`:$Tag" $ImageUri
docker push $ImageUri

$summary = @()

foreach ($Site in $Sites) {
  $Fn = "$Site-ai-intellis-scheduler"
  Write-Host "`nUpdating Lambda: $Fn..."

  $Exists = $true
  try {
    awscli lambda get-function --profile $Profile --region $Region --function-name $Fn | Out-Null
  } catch {
    $Exists = $false
  }

  if ($Exists) {
    try {
      awscli lambda update-function-code `
        --profile $Profile `
        --region $Region `
        --function-name $Fn `
        --image-uri $ImageUri | Out-Null

      awscli lambda wait function-updated --profile $Profile --region $Region --function-name $Fn
      Write-Host "[SUCCESS] $Fn code updated"
      $summary += [PSCustomObject]@{ Site = $Site; Function = $Fn; Status = "UPDATED" }
    } catch {
      Write-Host "[ERROR] Failed to update ${Fn}: $_"
      $summary += [PSCustomObject]@{ Site = $Site; Function = $Fn; Status = "FAILED ($($_))" }
    }
  } else {
    try {
      $EnvVars = @{
        SITE_ID = $Site
        PLANT_NAME = $Site
        S3_BUCKET = $Bucket
        BUCKET = $Bucket
        S3_RAW_OWNER = "vedanjay"
        S3_OUTPUT_PREFIX = "generated/vedanjay_ai_intellis"
        SIMOUR_STORAGE_ROOT = "/tmp/intellis_ai_scheduler"
        INTELLIS_IDEMPOTENCY_ENABLED = "true"
        OPENMETEO_API_KEY = $OpenMeteoApiKey
      }

      $EnvFile = ".\$Fn-env.json"
      @{ Variables = $EnvVars } | ConvertTo-Json -Depth 10 | Set-Content -Encoding ascii $EnvFile

      awscli lambda create-function `
        --profile $Profile `
        --region $Region `
        --function-name $Fn `
        --package-type Image `
        --code ImageUri=$ImageUri `
        --role $RoleArn `
        --timeout 900 `
        --memory-size 512 `
        --environment "file://$EnvFile" | Out-Null

      awscli lambda wait function-updated --profile $Profile --region $Region --function-name $Fn
      Remove-Item $EnvFile -Force -ErrorAction SilentlyContinue
      Write-Host "[SUCCESS] $Fn created"
      $summary += [PSCustomObject]@{ Site = $Site; Function = $Fn; Status = "CREATED" }
    } catch {
      Write-Host "[ERROR] Failed to create ${Fn}: $_"
      $summary += [PSCustomObject]@{ Site = $Site; Function = $Fn; Status = "FAILED ($($_))" }
    }
  }
}

Write-Host "`n============================================================"
Write-Host "Deployment Summary (Account $AccountId)"
Write-Host "Image: $ImageUri"
Write-Host "============================================================"
$summary | Format-Table -AutoSize

