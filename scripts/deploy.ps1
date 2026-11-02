# Deploys RFP Copilot to Cloud Run. Idempotent: safe to re-run; each run adds a new secret version and a new revision.
# Prerequisite: scripts\seed_prod.ps1 has been run once. Secret values are read from .env and never printed.
$ErrorActionPreference = "Stop"
$Project = "dj-ai-grant-consultant"
$Region = "us-east1"
$Service = "rfp-copilot"
$Bucket = "rfp-copilot-data"
$Sa = "rfp-copilot-sa"
$SaEmail = "$Sa@$Project.iam.gserviceaccount.com"
$Root = Split-Path -Parent $PSScriptRoot

function Gcloud {
    $ErrorActionPreference = "Continue"
    & gcloud.cmd @args --project $Project --quiet 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args[0..2] -join ' ') failed (exit $LASTEXITCODE)" }
}
function Gcloud-Ok {  # true when the command succeeds; output suppressed
    $ErrorActionPreference = "Continue"
    & gcloud.cmd @args --project $Project --quiet *> $null
    return ($LASTEXITCODE -eq 0)
}
function Read-EnvValue([string]$Name) {
    $line = Get-Content (Join-Path $Root ".env") | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -Last 1
    if (-not $line) { throw "$Name is not set in .env" }
    $value = ($line -replace "^\s*$Name\s*=", "").Trim().Trim('"').Trim("'")
    if (-not $value) { throw "$Name is blank in .env" }
    return $value
}

Write-Host "Enabling APIs"
Gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com storage.googleapis.com cloudscheduler.googleapis.com

Write-Host "Bucket gs://$Bucket"
if (-not (Gcloud-Ok storage buckets describe "gs://$Bucket")) {
    Gcloud storage buckets create "gs://$Bucket" --location $Region --uniform-bucket-level-access
}

Write-Host "Service account $SaEmail"
if (-not (Gcloud-Ok iam service-accounts describe $SaEmail)) {
    Gcloud iam service-accounts create $Sa --display-name "RFP Copilot runtime"
    Start-Sleep -Seconds 10  # IAM propagation before the grants below
}
Gcloud storage buckets add-iam-policy-binding "gs://$Bucket" --member "serviceAccount:$SaEmail" --role roles/storage.objectAdmin | Out-Null

foreach ($name in "MODEL_API_KEY", "COPILOT_ADMIN_KEY") {
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tmp, (Read-EnvValue $name), [System.Text.UTF8Encoding]::new($false))
        if (Gcloud-Ok secrets describe $name) {
            Write-Host "Secret $name exists: adding a new version"
            Gcloud secrets versions add $name --data-file $tmp | Out-Null
        } else {
            Write-Host "Creating secret $name"
            Gcloud secrets create $name --replication-policy automatic --data-file $tmp | Out-Null
        }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
    # Accessor on these two secrets only, not project-wide.
    Gcloud secrets add-iam-policy-binding $name --member "serviceAccount:$SaEmail" --role roles/secretmanager.secretAccessor | Out-Null
}

# min/max instances 1: SQLite has one writer and there is no cold start during the demo.
# --no-cpu-throttling: the debounced GCS upload and in-flight runs execute outside a request, so CPU must stay allocated.
Write-Host "Deploying $Service (Cloud Build from source)"
Push-Location $Root
try {
    Gcloud run deploy $Service --source . --region $Region --service-account $SaEmail --allow-unauthenticated `
        --min-instances 1 --max-instances 1 --concurrency 8 --memory 1Gi --cpu 1 --no-cpu-throttling --timeout 900 `
        --set-secrets "MODEL_API_KEY=MODEL_API_KEY:latest,COPILOT_ADMIN_KEY=COPILOT_ADMIN_KEY:latest" `
        --set-env-vars "META_API_BASE=https://api.meta.ai/v1,META_MODEL=muse-spark-1.3,DB_PATH=/tmp/rfp_copilot.sqlite3,GCS_BUCKET=$Bucket,LOG_LEVEL=INFO,MAX_DAILY_RUNS=150"
} finally {
    Pop-Location
}

$url = (& gcloud run services describe $Service --region $Region --project $Project --format "value(status.url)").Trim()
if (-not $url) { throw "could not read the service URL" }
Write-Host "Service URL: $url"
Write-Host "GET /health"
Invoke-RestMethod "$url/health" | ConvertTo-Json -Compress
Write-Host "GET /solicitations"
$sols = Invoke-RestMethod "$url/solicitations"
Write-Host ("{0} solicitation(s): {1}" -f @($sols).Count, ((@($sols) | ForEach-Object { $_.title }) -join "; "))

