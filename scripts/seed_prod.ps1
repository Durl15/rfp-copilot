# Uploads the local database to gs://rfp-copilot-data so production starts with its solicitations and confirmed
# past-performance entries. Run ONCE, before the first deploy. Refuses to overwrite an existing bucket copy unless -Force.
param([switch]$Force)
$ErrorActionPreference = "Stop"
$Project = "dj-ai-grant-consultant"
$Region = "us-east1"
$Bucket = "rfp-copilot-data"
$ObjectName = "rfp_copilot.sqlite3"
$Root = Split-Path -Parent $PSScriptRoot
$Local = Join-Path $Root "data\rfp_copilot.sqlite3"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Local)) { throw "no local database at $Local" }

# The bucket must exist before the first deploy; creating it here is idempotent with deploy.ps1.
& gcloud storage buckets describe "gs://$Bucket" --project $Project --quiet *> $null
if ($LASTEXITCODE -ne 0) {
    & gcloud storage buckets create "gs://$Bucket" --location $Region --uniform-bucket-level-access --project $Project --quiet
    if ($LASTEXITCODE -ne 0) { throw "could not create gs://$Bucket" }
}

& gcloud storage ls "gs://$Bucket/$ObjectName" --project $Project --quiet *> $null
if ($LASTEXITCODE -eq 0 -and -not $Force) {
    throw "gs://$Bucket/$ObjectName already exists. Production data would be overwritten; re-run with -Force only if that is intended."
}

# Consistent snapshot via the sqlite backup API (the live file may have uncheckpointed WAL content).
$snapshot = Join-Path ([System.IO.Path]::GetTempPath()) "rfp_copilot_seed_$([guid]::NewGuid().ToString('N')).sqlite3"
try {
    & $Python -I -c "import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $Local $snapshot
    if ($LASTEXITCODE -ne 0) { throw "snapshot failed" }
    & gcloud storage cp $snapshot "gs://$Bucket/$ObjectName" --project $Project --quiet
    if ($LASTEXITCODE -ne 0) { throw "upload failed" }
    Write-Host ("Seeded gs://{0}/{1} ({2:N0} bytes)" -f $Bucket, $ObjectName, (Get-Item $snapshot).Length)
} finally {
    Remove-Item $snapshot -Force -ErrorAction SilentlyContinue
}

