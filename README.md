# RFP Copilot

FastAPI service that ingests government solicitations (pasted text, PDF, or SAM.gov notice id), tags the sections that matter for a bid/no-bid decision, and pulls a firm's daily opportunity shortlist from CivicProcureBot. LLM work runs on Meta's Muse Spark through the Meta Model API.

## What pre-existed

- **CivicProcureBot shortlist API** — a separate, already-running service (<https://github.com/Durl15/civicprocurebot>). Its shortlist endpoint is **read-only**; RFP Copilot only proxies it (`GET /shortlist/today`) and never writes to it. Nothing in this repo modifies CivicProcureBot.

## What was built during the hackathon

Everything in this repository, built Oct 26 – Nov 4, 2026:

| Piece | Location |
|---|---|
| Config from env, SQLite schema + versioned migrations | `app/config.py`, `app/db.py` |
| Meta Model API client: retry (3x, exponential backoff on 429/5xx), per-call logging to `llm_calls`, streaming | `app/llm/meta_client.py` |
| Solicitation ingestion (text / PDF / SAM notice), section + field tagger, `solicitations` table | `app/ingest.py`, `app/routes.py` |
| CivicProcureBot shortlist proxy | `app/shortlist.py` |
| Test suite | `tests/` |

### Meta capabilities used

| Capability | Where | Status |
|---|---|---|
| Messages API via the Anthropic SDK (`base_url` = Meta endpoint) | `MetaClient.complete` | Used |
| Structured output (JSON schema) | Tagger gap-fill, `json_schema=` | Used |
| `reasoning_effort` (low / medium / high) | Every call; tagger uses `low` | Used |
| Files API | PDF upload on ingest; `file_id` stored on the solicitation | Used |
| Streaming | `MetaClient.stream` | Implemented, not yet called by a route |
| Tool use | `tools=` passthrough on `complete` / `stream` | Implemented, not yet called by a route |

Meta-specific parameters (`reasoning_effort`, `response_format`, Files API `purpose`) are sent through `extra_body` / plain HTTP using the assumed wire format documented at the top of `app/llm/meta_client.py` and in `app/ingest.py`. Confirm them against the Meta Model API reference; each is a one-line change.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # then fill in the keys
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8003
```

Port 8003 avoids the usual 8000–8002 collisions on the dev machine. Migrations run automatically at startup.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_API_KEY` | – | Meta Model API key |
| `META_API_BASE` | `https://api.meta.ai/v1` | Meta endpoint |
| `META_MODEL` | `muse-spark-1.3` | Model id |
| `CIVICPROCUREBOT_API_BASE` | – | CivicProcureBot base URL |
| `CIVICPROCUREBOT_API_KEY` | – | Sent as `X-API-Key` (assumed header; see `app/shortlist.py`) |
| `SAM_API_KEY` | – | Only for `sam_notice_id` ingestion |
| `DB_PATH` | `data/rfp_copilot.sqlite3` | SQLite file (local dev) |

Do not wrap values in quotes in Railway or `.env`.

## API

- `POST /solicitations` — one of:
  - JSON `{"text": "..."}`
  - multipart with a PDF in field `file`
  - JSON `{"sam_notice_id": "..."}`

  Returns the tagged record (`due_date`, `naics`, `set_aside`, `place_of_performance`, `section_c/l/m`, `file_id`, and `llm_filled`, the list of fields the model had to fill because regex missed them).
- `GET /solicitations/{id}`
- `GET /shortlist/today?firm_id=` — CivicProcureBot proxy
- `GET /health`

Tagging is regex first (Sections C/L/M, due date, NAICS, set-aside, place of performance); a single structured-output Muse Spark call fills only the gaps and never overwrites a regex hit. If that call fails, the regex result is stored as-is.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Drop sample solicitations in `tests/fixtures/*.pdf` to enable the real-PDF tagger test; it skips when the folder is empty.

## Known limits

- SQLite lives on local disk. Fine for the hackathon demo; on Railway the filesystem is ephemeral, so move to Postgres before real data lands there.
- Scanned (image-only) PDFs are rejected: no OCR yet.
- Only Sections C, L, and M are captured.
