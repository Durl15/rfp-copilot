New project: RFP Copilot, a FastAPI service. Public repo. Python 3.12, SQLite, pydantic v2, anthropic SDK (pointed at Meta Model API), httpx, pypdf, python-docx, pytest.

1. Scaffold: app/main.py, app/config.py (env: MODEL_API_KEY, META_API_BASE default https://api.meta.ai/v1, META_MODEL default muse-spark-1.3, CIVICPROCUREBOT_API_BASE, CIVICPROCUREBOT_API_KEY), app/db.py with migrations, tests/, requirements.txt, README.md. .gitignore and .env.example already exist; do not overwrite them.
2. app/llm/meta_client.py: wrap the anthropic SDK client with base_url and api_key from config. Methods: complete(messages, system, tools=None, json_schema=None, reasoning_effort="medium", feature="") and stream(...). Log every call to SQLite table llm_calls (ts, model, feature, prompt_tokens, completion_tokens, latency_ms, ok, error). Retry 3x with backoff on 429/5xx.
3. app/ingest.py + routes: POST /solicitations accepts {"text"}, multipart PDF, or {"sam_notice_id"}. For PDFs, upload to Meta Files API and store file_id; also extract text with pypdf. Section tagger: regex pass for Sections C/L/M, due date, NAICS, set-aside, place of performance, then one Muse Spark structured-output call to fill gaps. Table solicitations.
4. app/shortlist.py: GET /shortlist/today?firm_id= proxies CivicProcureBot's read-only endpoint with the API key from config.
5. README: sections "What pre-existed" (CivicProcureBot shortlist API, read-only, link to github.com/Durl15/civicprocurebot) and "What was built during the hackathon" (everything in this repo, Oct 26-Nov 4 2026), plus a Meta capabilities table.
6. Tests: client retry (mock), llm_calls logging, ingestion for all three input types, section tagger on tests/fixtures/*.pdf (fixtures may be absent; skip those tests gracefully if the folder is empty).

No secrets committed. Run pytest at the end and report results. Commit with message: "Day 1: scaffold, Meta client, ingestion (hackathon work begins Oct 26)". Do not push.
