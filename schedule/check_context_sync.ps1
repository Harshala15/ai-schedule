param(
    [string]$Bucket = "ai-forecasting-storage-429694361053",
    [string]$LambdaName = "simour-forecast-scheduler",
    [string]$StatePrefix = "state/vedanjay/SIRMOUR/prediction_context",
    [string]$ContextFile = "SIRMOUR_context.json",
    [switch]$InvokeLambda,
    [string]$TargetDate = (Get-Date).ToString("yyyy-MM-dd"),
    [string]$TargetTime = "08:15"
)

$ErrorActionPreference = "Stop"

$key = "$StatePrefix/$ContextFile"
$s3Uri = "s3://$Bucket/$key"

Write-Host "Checking S3 context object:" -ForegroundColor Cyan
Write-Host "  Bucket: $Bucket"
Write-Host "  Key:    $key"
Write-Host ""

Write-Host "1) Listing the state prefix..." -ForegroundColor Cyan
aws s3 ls "s3://$Bucket/$StatePrefix/"
Write-Host ""

Write-Host "2) Inspecting object metadata..." -ForegroundColor Cyan
aws s3api head-object --bucket $Bucket --key $key
Write-Host ""

Write-Host "3) Downloading the file for local inspection..." -ForegroundColor Cyan
$localFile = Join-Path $PSScriptRoot $ContextFile
aws s3 cp $s3Uri $localFile
Get-Content $localFile
Write-Host ""

Write-Host "4) Showing recent Lambda logs..." -ForegroundColor Cyan
aws logs tail "/aws/lambda/$LambdaName" --since 2h
Write-Host ""

if ($InvokeLambda) {
    Write-Host "5) Invoking Lambda to force a fresh run..." -ForegroundColor Cyan
    $payload = "{""target_date"":""$TargetDate"",""target_time"":""$TargetTime""}"
    $responseFile = Join-Path $PSScriptRoot "lambda_response.json"
    $payloadFile = Join-Path $PSScriptRoot "payload.json"
    $payload | Set-Content -Encoding ascii $payloadFile
    aws lambda invoke `
        --function-name $LambdaName `
        --cli-binary-format raw-in-base64-out `
        --payload file://$payloadFile `
        $responseFile
    Write-Host "Lambda response:"
    Get-Content $responseFile
    Write-Host ""

    Write-Host "Re-checking the context object after invocation..." -ForegroundColor Cyan
    aws s3api head-object --bucket $Bucket --key $key
    aws s3 cp $s3Uri $localFile
    Get-Content $localFile
}
