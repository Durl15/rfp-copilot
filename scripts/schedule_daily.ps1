# Creates or updates the Cloud Scheduler job that triggers the daily run at 7:30 AM Eastern.
# The admin key is read from Secret Manager at run time and is never printed.
param([int]$FirmId = 1)
$ErrorActionPreference = "Continue"
$Project = "dj-ai-grant-consultant"
$Region = "us-east1"
$Service = "rfp-copilot"
$Job = "rfp-copilot-daily"

$url = (& gcloud run services describe $Service --region $Region --project $Project --format "value(status.url)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $url) { throw "service $Service not found; run scripts\deploy.ps1 first" }

$key = (& gcloud secrets versions access latest --secret COPILOT_ADMIN_KEY --project $Project).Trim()
if ($LASTEXITCODE -ne 0 -or -not $key) { throw "could not read COPILOT_ADMIN_KEY from Secret Manager" }
if ($key -match ",") { throw "COPILOT_ADMIN_KEY contains a comma, which gcloud's --headers syntax cannot carry" }

& gcloud scheduler jobs describe $Job --location $Region --project $Project --quiet *> $null
$verb = if ($LASTEXITCODE -eq 0) { "update" } else { "create" }
$bodyFile = Join-Path $PSScriptRoot "daily_body.json"
Set-Content -Path $bodyFile -Value "{""firm_id"": $FirmId}" -NoNewline -Encoding ascii

& gcloud scheduler jobs $verb http $Job --location $Region --project $Project --quiet `
    --schedule "30 7 * * *" --time-zone "America/New_York" `
    --uri "$url/agent/run-daily" --http-method POST `
    --headers "X-Admin-Key=$key,Content-Type=application/json" `
    --message-body-from-file $bodyFile `
    --attempt-deadline 900s | Out-Null
if ($LASTEXITCODE -ne 0) { throw "gcloud scheduler jobs $verb failed" }
Write-Host "Scheduler job $Job ${verb}d: 07:30 America/New_York -> POST $url/agent/run-daily (firm_id $FirmId)"




