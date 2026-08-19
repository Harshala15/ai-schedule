param(
    [string]$Bucket = "ai-forecasting-storage-429694361053",
    [string]$LambdaName = "simour-forecast-scheduler",
    [string]$StatePrefix = "state/vedanjay/SIRMOUR/prediction_context",
    [string]$ContextFile = "SIRMOUR_context.json",
    [string]$TargetDate = "2026-08-07",
    [string]$TargetTime = "11:15"
)

$ErrorActionPreference = "Stop"

$key = "$StatePrefix/$ContextFile"
$payloadFile = Join-Path $PSScriptRoot "payload.json"
$responseFile = Join-Path $PSScriptRoot "response.json"
$beforeFile = Join-Path $PSScriptRoot "context_before.json"
$afterFile = Join-Path $PSScriptRoot "context_after.json"

Write-Host "Testing historical context sync for $TargetDate $TargetTime" -ForegroundColor Cyan
Write-Host "Bucket: $Bucket"
Write-Host "Lambda: $LambdaName"
Write-Host "Key:    $key"
Write-Host ""

Write-Host "1) Snapshot current context..." -ForegroundColor Cyan
aws s3 cp "s3://$Bucket/$key" $beforeFile
Write-Host ""

Write-Host "2) Build Lambda payload..." -ForegroundColor Cyan
@"
{"target_date":"$TargetDate","target_time":"$TargetTime"}
"@ | Set-Content -Encoding ascii $payloadFile
Get-Content $payloadFile
Write-Host ""

Write-Host "3) Invoke Lambda..." -ForegroundColor Cyan
aws lambda invoke `
  --function-name $LambdaName `
  --cli-binary-format raw-in-base64-out `
  --payload file://$payloadFile `
  $responseFile
Write-Host "Lambda response:"
Get-Content $responseFile
Write-Host ""

Write-Host "4) Tail recent CloudWatch logs..." -ForegroundColor Cyan
aws logs tail "/aws/lambda/$LambdaName" --since 30m
Write-Host ""

Write-Host "5) Re-download context and compare..." -ForegroundColor Cyan
aws s3 cp "s3://$Bucket/$key" $afterFile

$before = Get-Content $beforeFile -Raw
$after = Get-Content $afterFile -Raw

if ($before -eq $after) {
    Write-Host "Context JSON did not change for this run." -ForegroundColor Yellow
} else {
    Write-Host "Context JSON changed for this run." -ForegroundColor Green
}

Write-Host ""
Write-Host "Before dates:" -ForegroundColor Cyan
if ($before.Trim().StartsWith("[")) {
    ($before | ConvertFrom-Json | ForEach-Object { $_.date }) -join ", "
}

Write-Host "After dates:" -ForegroundColor Cyan
if ($after.Trim().StartsWith("[")) {
    ($after | ConvertFrom-Json | ForEach-Object { $_.date }) -join ", "
}
