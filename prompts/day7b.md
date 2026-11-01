Day 7b: deploy RFP Copilot to Cloud Run with persistent state, plus two small product changes. GCP project dj-ai-grant-consultant, region us-east1, service name rfp-copilot. gcloud is installed and authenticated on this machine.



PRODUCT CHANGES FIRST

1\. Run the compliance matrix for needs\_review verdicts as well as bid. Narrative and capability statement remain bid-only. The run trace and the console tabs show the matrix for needs\_review; export kind=matrix works for it. Update tests.

2\. POST /agent/run-daily: when the CivicProcureBot shortlist endpoint is unreachable or returns an error, fall back to all solicitations with demo=true plus any ingested in the last 14 days, log a shortlist\_fallback event with the reason, and continue. Never fail the daily run because the shortlist is down. Test it.



DEPLOY

3\. Dockerfile: python:3.12-slim, install from requirements.lock.txt, copy app, run uvicorn on $PORT with --proxy-headers. No apt ffmpeg (imageio-ffmpeg bundles the binary; verify the import works in the image by running a one-line check during build). .dockerignore excludes .env, data/, logs/, tests/, prompts/, .git.

4\. Persistence: SQLite lives at DB\_PATH (default data/rfp\_copilot.sqlite3 locally, /tmp/rfp\_copilot.sqlite3 in Cloud Run). Add app/storage.py with whole-file sync to a GCS bucket named by env GCS\_BUCKET (bucket rfp-copilot-data): on startup, if the bucket has the file, download it before migrations; after any write transaction (ingest, decision, confirm, reject, remove, run events), schedule an upload debounced 5 seconds; also upload on shutdown. Use google-cloud-storage; add to requirements and lock. When GCS\_BUCKET is unset, storage is a no-op (local dev and tests). Log each sync at INFO with byte size.

5\. Startup seeding must be idempotent: seed firms and the demo solicitation only if absent, never duplicate on restart.

6\. Abuse limits, because the URL will be public: in-memory per-IP limits of 6 agent runs per hour and 10 voice uploads per hour on POST /agent/run, /agent/go-no-go, /agent/matrix, /voice/past-performance; a global daily cap MAX\_DAILY\_RUNS (env, default 150) across all agent runs; both return 429 with a plain message. Admin-key requests bypass per-IP limits. Max upload size for voice 25 MB (413). Tests for each.

7\. SSE through Cloud Run: confirm the run endpoint streams without buffering (headers Cache-Control: no-cache, X-Accel-Buffering: no, and a keepalive comment every 15 seconds) so a 2-minute run survives the proxy.

8\. scripts/deploy.ps1 (PowerShell, idempotent):

&#x20;  - enable run, cloudbuild, secretmanager, storage, cloudscheduler APIs

&#x20;  - create bucket gs://rfp-copilot-data (us-east1, uniform access) if missing

&#x20;  - create service account rfp-copilot-sa if missing; grant roles/storage.objectAdmin on the bucket only and roles/secretmanager.secretAccessor

&#x20;  - create secrets MODEL\_API\_KEY and COPILOT\_ADMIN\_KEY if missing, reading values from .env (never echo them); add a new version if the secret exists

&#x20;  - gcloud run deploy rfp-copilot --source . --region us-east1 --service-account rfp-copilot-sa --allow-unauthenticated --min-instances 1 --max-instances 1 --concurrency 8 --memory 1Gi --cpu 1 --timeout 900 --set-secrets MODEL\_API\_KEY=MODEL\_API\_KEY:latest,COPILOT\_ADMIN\_KEY=COPILOT\_ADMIN\_KEY:latest --set-env-vars META\_API\_BASE=https://api.meta.ai/v1,META\_MODEL=muse-spark-1.3,DB\_PATH=/tmp/rfp\_copilot.sqlite3,GCS\_BUCKET=rfp-copilot-data,LOG\_LEVEL=INFO,MAX\_DAILY\_RUNS=150

&#x20;  - print the service URL and run GET /health and GET /solicitations against it

&#x20;  - min-instances 1 and max-instances 1 are deliberate: one writer for SQLite, no cold start during the demo video

9\. scripts/seed\_prod.ps1: uploads the local data/rfp\_copilot.sqlite3 to gs://rfp-copilot-data/rfp\_copilot.sqlite3 so production starts with the DASNY 7628 solicitation and the confirmed Woodfork entry; refuses to run if the bucket already has the file unless -Force. Document that this is run once before the first deploy.

10\. Cloud Scheduler: scripts/schedule\_daily.ps1 creates or updates job rfp-copilot-daily, 7:30 AM America/New\_York, POST {SERVICE\_URL}/agent/run-daily with header X-Admin-Key; the script reads the value from Secret Manager at creation time and never prints it.

11\. README: "Deploy" section with the three scripts in order (seed\_prod, deploy, schedule\_daily), the env table, the persistence model and its single-instance constraint, and the abuse limits. Note that the service URL is the live product link for the hackathon submission.

12\. Full suite passes. Commit: "Day 7b: Cloud Run deploy, GCS-backed SQLite, abuse limits, matrix on needs\_review, shortlist fallback". Push to origin main. Do NOT run the deploy scripts yourself; I run them.



Output: changed files, test count, and the exact three commands to run, in order. No secrets.

