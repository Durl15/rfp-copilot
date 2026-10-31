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
| Responses API via the OpenAI SDK (`base_url` = Meta endpoint) | `MetaClient.complete` | Used |
| Structured output (`text.format` JSON schema) | Tagger gap-fill, `json_schema=` | Used |
| `reasoning.effort` (minimal / low / medium / high) | Every call; tagger uses `low` | Used |
| Files API | PDF upload on ingest; `file_id` stored on the solicitation | Used |
| Streaming | `MetaClient.stream` | Implemented, not yet called by a route |
| Tool use | `tools=` passthrough on `complete` / `stream` | Implemented, not yet called by a route |

Reasoning and structured output use native Responses API parameters (`reasoning`, `text.format`, `previous_response_id`; see the top of `app/llm/meta_client.py`). The Files API `purpose` is still plain HTTP using the assumed wire format in `app/ingest.py`; confirm it against the Meta Model API reference.

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
| `COPILOT_ADMIN_KEY` | – | Required `X-API-Key` for `POST /agent/run-daily`; blank means the endpoint rejects everything |
| `MAX_RUN_INPUT_TOKENS` | `60000` | Token guard: a run whose first-call input exceeds this stops with `step_failed` |
| `PRICE_IN_PER_MTOK` / `PRICE_OUT_PER_MTOK` | `0.50` / `2.00` | USD per million tokens for `est_cost_usd`. Assumed rates: set Meta's actual pricing |

Do not wrap values in quotes in Railway or `.env`.

## API

- `POST /solicitations` — one of:
  - JSON `{"text": "..."}`
  - multipart with a PDF in field `file`
  - JSON `{"sam_notice_id": "..."}`

  Returns the tagged record (`due_date`, `naics`, `set_aside`, `place_of_performance`, `section_c/l/m`, `file_id`, and `llm_filled`, the list of fields the model had to fill because regex missed them).
- `GET /solicitations/{id}`
- `GET /shortlist/today?firm_id=` — CivicProcureBot proxy
- `POST /agent/run` `{firm_id, solicitation_id, override_no_bid: false}` — Server-Sent Events: go/no-go, then matrix, narrative, capability statement. A `no_bid` verdict stops after go/no-go (reasons in `decision` and `run_complete`) unless `override_no_bid` is true. Events: `run_started`, `step_started`, `tool_call`, `tool_result`, `decision`, `matrix_row`, `narrative`, `capability_statement`, `step_failed`, `run_complete`; each carries `seq` and `timestamp`. The token guard counts input tokens first and stops with `step_failed` above `MAX_RUN_INPUT_TOKENS`.
- `GET /runs/{run_id}` — the stored event log of a run, for replay with no model spend. Every `llm_calls` row carries its `run_id`; estimated cost is logged per run.
- `POST /agent/run-daily` `{firm_id}` with header `X-API-Key` — pulls the CivicProcureBot shortlist, ingests the top 3 by score, runs the full chain on each, and returns a summary list. It never sends email.
- `GET /demo/solicitations`
- `GET /health`

Tagging is regex first (Sections C/L/M, due date, NAICS, set-aside, place of performance); a single structured-output Muse Spark call fills only the gaps and never overwrites a regex hit. If that call fails, the regex result is stored as-is.

## Voice intake

`POST /voice/past-performance` (multipart: `audio`, `firm_id`, optional `hints`) accepts any audio upload (m4a, mp3, wav, ogg, webm, aac). It is converted with ffmpeg to 16 kHz mono 16-bit PCM WAV, transcribed, then extracted into a pending entry that only reaches the firm's past performance after `POST /voice/confirm`. `hints` is a comma-separated list of proper nouns (e.g. `The Woodfork Group, East Adams`); a transcribed name that is a close phonetic match is written with the hint spelling in the field value while the evidence quote stays verbatim. Unreadable audio returns 400 `unsupported or corrupt audio`; ASR failures return 502 after one retry on 5xx/timeout.

Muse Voice Transcribe contract (`POST {META_API_BASE}/asr/transcribe`, bearer `MODEL_API_KEY`):

- Multipart with exactly two parts: `request` (`application/json`: `{"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV"}`) and `audio` (`audio/wav`). Other part names return 400.
- WAV only, mono 16-bit PCM at 16 or 24 kHz. m4a, mp3 and other encodings are rejected.
- 200 response: `{"sessionId", "transcript", "audioDurationMs", "turns": []}`.

Conversion uses ffmpeg from the `imageio-ffmpeg` package (no system ffmpeg). The `imageio-ffmpeg` 0.7.0 `manylinux2014_x86_64` wheel bundles a linux x86_64 ffmpeg binary, so the Railway image needs no change.

Smoke test (one real call): `python scripts/voice_smoke.py <audio> [firm_id] [--hints "a, b"]`.

## Demo data

**Demo firms and demo solicitations are fictional.** Firms 2 and 3 (`demo=true`) and the seeded county facilities electrical maintenance solicitation (NAICS 238210, HUBZone set-aside, Syracuse) are invented for demonstration; none of their clients, contracts, values or references are real. Firm 2 carries three `verified=true` past-performance entries that match the demo solicitation, so a run on it reaches a `bid` or `needs_review` verdict. Firm 1 is the only real firm.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Drop sample solicitations in `tests/fixtures/*.pdf` to enable the real-PDF tagger test; it skips when the folder is empty.

## Known limits

- SQLite lives on local disk. Fine for the hackathon demo; on Railway the filesystem is ephemeral, so move to Postgres before real data lands there.
- Token counting uses the Responses API `input_tokens` endpoint (`MetaClient.count_input_tokens`); it is not verified against Meta's live API. If it fails, the run stops at the token guard rather than spending unchecked.
- Scanned (image-only) PDFs are rejected: no OCR yet.
- Only Sections C, L, and M are captured.
