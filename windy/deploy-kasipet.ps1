param(
    [string]$AwsAccountId = "429694361053",
    [string]$Region = "ap-south-1",
    [string]$RepositoryName = "kasipet-windy-capture",
    [string]$FunctionName = "KASIPET-windy-capture",
    [string]$ImageTag = "latest"
)

$ErrorActionPreference = "Stop"

$ecrUri = "$AwsAccountId.dkr.ecr.$Region.amazonaws.com/$RepositoryName"
$imageUri = "$ecrUri:$ImageTag"

Write-Host "Logging in to ECR..."
aws ecr get-login-password --region $Region |
    docker login --username AWS --password-stdin "$AwsAccountId.dkr.ecr.$Region.amazonaws.com"

Write-Host "Ensuring ECR repository exists..."
$repoExists = $true
try {
    aws ecr describe-repositories --region $Region --repository-names $RepositoryName | Out-Null
} catch {
    $repoExists = $false
}

if (-not $repoExists) {
    aws ecr create-repository --region $Region --repository-name $RepositoryName | Out-Null
}

Write-Host "Building image..."
docker build -f Dockerfile.windy-capture-lambda -t $RepositoryName .

Write-Host "Tagging image..."
docker tag "$RepositoryName`:latest" $imageUri

Write-Host "Pushing image..."
docker push $imageUri

Write-Host "Updating Lambda function code..."
aws lambda update-function-code `
    --region $Region `
    --function-name $FunctionName `
    --image-uri $imageUri | Out-Null

Write-Host "Pinning Lambda command to kasipet_lambda.lambda_handler..."
$imageConfigPath = Join-Path $env:TEMP "kasipet-image-config.json"
Set-Content -LiteralPath $imageConfigPath -Value '{"Command":["kasipet_lambda.lambda_handler"]}' -Encoding ascii
aws lambda update-function-configuration `
    --region $Region `
    --function-name $FunctionName `
    --image-config file://$imageConfigPath | Out-Null

Write-Host "Deployment complete for $FunctionName using $imageUri"
