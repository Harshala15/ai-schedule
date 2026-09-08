param(
  [string]$Profile = "testing-new3",
  [string]$Region = "ap-south-1",
  [string]$Bucket = "vedanjay-schedules-test-608744602858",
  [string]$RoleName = "global1-lambda-role",
  [string]$RepoName = "intellis-ai-scheduler",
  [string]$Tag = "intellis-ai-scheduler-20260907",
  [string]$OpenMeteoApiKey = "PASTE_OPENMETEO_PROFESSIONAL_KEY_HERE"
)

function awscli { py -m awscli @args }

$ErrorActionPreference = "Stop"
$Sites = @("BAMKHAL", "BALAKWADA", "CME", "ANDAD", "SAWDA", "SIRMOUR", "KASIPET", "BHUPALPALLY", "KOTHAGUDEM", "OSEPL", "ANJANGOAN")

# Put site-specific OpenRouter keys here before deploying.
$OpenRouterKeys = @{
  BAMKHAL      = "PASTE_OPENROUTER_KEY_BAMKHAL"
  BALAKWADA    = "PASTE_OPENROUTER_KEY_BALAKWADA"
  CME          = "PASTE_OPENROUTER_KEY_CME"
  ANDAD        = "PASTE_OPENROUTER_KEY_ANDAD"
  SAWDA        = "PASTE_OPENROUTER_KEY_SAWDA"
  SIRMOUR      = "PASTE_OPENROUTER_KEY_SIRMOUR"
  KASIPET      = "PASTE_OPENROUTER_KEY_KASIPET"
  BHUPALPALLY  = "PASTE_OPENROUTER_KEY_BHUPALPALLY"
  KOTHAGUDEM   = "PASTE_OPENROUTER_KEY_KOTHAGUDEM"
  OSEPL        = "PASTE_OPENROUTER_KEY_OSEPL"
  ANJANGOAN    = "PASTE_OPENROUTER_KEY_ANJANGOAN"
}

$AccountId = (awscli sts get-caller-identity --profile $Profile --query Account --output text).Trim()
$Ecr = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$ImageUri = "$Ecr/$RepoName`:$Tag"
$RoleArn = "arn:aws:iam::$AccountId`:role/$RoleName"

try {
  awscli ecr describe-repositories --profile $Profile --region $Region --repository-names $RepoName | Out-Null
} catch {
  awscli ecr create-repository --profile $Profile --region $Region --repository-name $RepoName | Out-Null
}

awscli ecr get-login-password --profile $Profile --region $Region | docker login --username AWS --password-stdin $Ecr

docker build --platform linux/amd64 --provenance=false --sbom=false `
  -f ".\Dockerfile.intellis-ai-lambda" `
  -t "$RepoName`:$Tag" .

docker tag "$RepoName`:$Tag" $ImageUri
docker push $ImageUri

foreach ($Site in $Sites) {
  $Fn = "$Site-ai-intellis-scheduler"
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
    OPENROUTER_API_KEY = $OpenRouterKeys[$Site]
    LLM_PROVIDER = "openrouter"
  }

  $EnvFile = ".\$Fn-env.json"
  @{ Variables = $EnvVars } | ConvertTo-Json -Depth 10 | Set-Content -Encoding ascii $EnvFile

  $Exists = $true
  try {
    awscli lambda get-function --profile $Profile --region $Region --function-name $Fn | Out-Null
  } catch {
    $Exists = $false
  }

  if ($Exists) {
    awscli lambda update-function-code `
      --profile $Profile `
      --region $Region `
      --function-name $Fn `
      --image-uri $ImageUri | Out-Null

    awscli lambda wait function-updated --profile $Profile --region $Region --function-name $Fn

    awscli lambda update-function-configuration `
      --profile $Profile `
      --region $Region `
      --function-name $Fn `
      --timeout 900 `
      --memory-size 512 `
      --environment "file://$EnvFile" | Out-Null
  } else {
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
  }

  awscli lambda wait function-updated --profile $Profile --region $Region --function-name $Fn
  Remove-Item $EnvFile -Force
  Write-Host "Ready: $Fn"
}

Write-Host "Deployment image: $ImageUri"
Write-Host "Manual validation first; attach EventBridge only after outputs and idempotency are confirmed."
