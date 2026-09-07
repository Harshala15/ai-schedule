param(
    [string]$AwsAccountId = "429694361053",
    [string]$Region = "ap-south-1",
    [string]$ImageTag = "latest"
)

$ErrorActionPreference = "Stop"

$plants = @("BHUPALPALLY", "KASIPET", "SIRMOUR")

Write-Host "============================================================"
Write-Host "Building and Deploying All 3 Plant Forecast Scheduler Lambdas"
Write-Host "Plants: $($plants -join ', ')"
Write-Host "AWS Account: $AwsAccountId | Region: $Region"
Write-Host "============================================================"

# 1. Login to ECR
Write-Host "`nLogging in to AWS ECR..."
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin "$AwsAccountId.dkr.ecr.$Region.amazonaws.com"

$summary = @()

foreach ($plant in $plants) {
    Write-Host "`n------------------------------------------------------------"
    Write-Host "Starting Build & Deployment for: $plant"
    Write-Host "------------------------------------------------------------"
    
    try {
        & .\deploy-forecast-scheduler.ps1 -Plant $plant -AwsAccountId $AwsAccountId -Region $Region -ImageTag $ImageTag
        $summary += [PSCustomObject]@{
            Plant = $plant
            Status = "SUCCESS"
            Message = "Deployed to Lambda successfully"
        }
    } catch {
        Write-Host "   [ERROR] Failed to deploy ${plant}: $_"
        $summary += [PSCustomObject]@{
            Plant = $plant
            Status = "FAILED"
            Message = "$_"
        }
    }

}

Write-Host "`n============================================================"
Write-Host "Deployment Summary for All 3 Plants:"
Write-Host "============================================================"
$summary | Format-Table -AutoSize
