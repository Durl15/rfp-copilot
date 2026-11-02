# RFP Copilot

FastAPI service that ingests government solicitations (pasted text, PDF, or SAM.gov notice id), tags the sections that matter for a bid/no-bid decision, and pulls a firm's daily opportunity shortlist from CivicProcureBot. LLM work runs on Meta's Muse Spark through the Meta Model API.

![Agent console](docs/console.png)

## Try it in 3 minutes

Live: <https://rfp-copilot-uiobenkvzq-ue.a.run.app> (opens the agent console).

1. **Pick the firm** "DJ AI Business Consultant LLC" (left column, firm 1, the one real firm).
2. **Pick the solicitation** "DASNY 7628" (a real, closed NYS RFP; the due date is shifted and the title says so).
3. **Run agent.** The trace streams live: the model reads the profile and the solicitation, searches past performance, and a token guard counts input first. Expect a **`no_bid` by rule** if the Woodfork entry is absent (zero verified matches), or **`needs_review`** if it is present (one verified match). The verdict card shows `RULE` or `MODEL` and the rule reason.
4. **Add the evidence.** Upload or record a voice note in the right column. Muse Voice Transcribe transcribes it, the model extracts one past-performance entry, and every field shows the verbatim quote it came from. Nothing is saved yet.
5. **Confirm.** The entry appears in the firm's past performance as **VERIFIED**. (Reject discards it.)
6. **Run agent again.** With one verified, validated match the verdict is `needs_review`, the score is clamped to 40-65, and the compliance matrix is built (no narrative or capability statement yet, because bid is not supported). Download the matrix as .docx; every file ends with "Draft for human review".

Use **Replay last run** to replay a stored run at 8x with no model spend. For a `bid` outcome, pick the fictional firm 2 with the fictional electrical solicitation: it has three verified entries, so the model's verdict stands and the narrative and capability statement are generated.

Why the verdicts come out the way they do: see [Honesty guards](#honesty-guards).

## Architecture

```
 Browser console (static HTML/JS, no build)
        |  POST /agent/run  (Server-Sent Events back)
        v
 FastAPI (app/main.py, Cloud Run, 1 instance)
        |
        v
 Agent loop (app/agent/run.py)
   token guard -> go/no-go (tool loop) -> floor -> matrix -> narrative -> capability
        |                     ^
        |                     |  read-only tools: firm profile, solicitation,
        |                     |  set-aside/NAICS check, past-performance search
        v
 Meta Model API (api.meta.ai/v1)
   - Muse Spark 1.3, Responses API (tools, json_schema, reasoning, store)
   - Muse Voice Transcribe (/asr/transcribe, WAV only)
        |
        v
 SQLite on the container disk ---- debounced whole-file sync ----> GCS bucket
        ^
        |  POST /agent/run-daily (X-Admin-Key)
 Cloud Scheduler, 07:30 America/New_York  ---- reads ----> CivicProcureBot shortlist (optional)
```

### Meta capabilities

Each row was checked against the code.

| Capability | Where | Why it matters |
|---|---|---|
| Tool calling in the agent loop | `app/agent/go_no_go.py` (`MODEL_TOOLS`, `run_go_no_go`), `app/agent/tools.py` | The model reads the firm and solicitation through read-only tools instead of being handed a prompt blob, and the trace shows each call |
| Structured outputs validated by pydantic | Go/no-go: `GO_NO_GO_SCHEMA` + `GoNoGo` in `app/agent/go_no_go.py`; matrix: `MATRIX_SCHEMA` in `app/agent/matrix.py`; voice: `EXTRACT_SCHEMA` in `app/voice.py`. Also capability statement and tagger gap-fill | Strict JSON schemas via `text.format`; answers are re-validated in code, so a malformed or out-of-contract answer fails loudly rather than reaching a document |
| Reasoning effort dial per task | `reasoning_effort=` in `go_no_go.py` (`high`), `matrix.py` (`medium`; `minimal` for sub-calls), `narrative.py` and `voice.py` (`minimal`), `capability.py` and `ingest.py` (`low`) | Spends thinking only where the decision is; the client lowers effort one step when a call hits `max_output_tokens` |
| Prompt caching on the firm/rubric prefix, `cached_tokens` logged | Prefix built in `app/agent/matrix.py` (`_system_prompt`: fixed rubric + key-sorted firm profile, byte-identical across runs); `cached_tokens` read from usage in `app/llm/meta_client.py`, stored in `llm_calls`, summed in `GET /runs/{id}` | Repeat runs for a firm share a stable prefix. The code does not set a cache flag; it keeps the prefix stable and records what the API reports as cached |
| Token counting as a guard before each run | `app/agent/run.py` (calls `MetaClient.count_input_tokens`; limit `MAX_RUN_INPUT_TOKENS`) | An oversized solicitation stops with `step_failed` before any generation is paid for |
| Stored responses with `previous_response_id`, stateless fallback | `app/llm/conversation.py`, `MetaClient._create_response` in `app/llm/meta_client.py` (`store=True`) | Reasoning carries across tool turns; if an id is missing or expired the full item list is resent without it, and an incomplete response id is never reused |
| SSE streaming of the run | `POST /agent/run` in `app/agent/run.py` (`text/event-stream`, 15 s keepalive) | The console shows each tool call, decision and matrix row as it happens; events are stored for replay even if the client disconnects |
| Muse Voice Transcribe with the WAV contract | `app/voice.py`, `POST /voice/past-performance` | Turns a spoken engagement into a pending, quote-backed entry. Audio is converted to 16 kHz mono 16-bit PCM WAV and sent as two multipart parts (`request`, `audio`) |

Also used: the Files API for PDF upload on ingest (`app/ingest.py`).

## Honesty guards

- **Verbatim evidence quotes.** The matrix keeps a quote copied character for character from the firm profile for each match; voice extraction must give a verbatim transcript quote for every field it fills. Quotes that are not in the source are rejected.
- **Verified flag and claim-word validator.** Only `verified=true` entries may back dates, values, retention, references or production claims; unverified entries expose just client and scope (`app/claims.py`). Matrix and narrative text is checked against this and rejected on violation.
- **`verified_matches` validated in code.** The model lists the past-performance entries that overlap the solicitation; code keeps only entries whose index exists, is unique and is `verified=true`. The rest go to `rejected_matches` with a reason. Both lists are stored on the decision.
- **Deterministic verdict floor.** The direction of the verdict is decided by code, not by the model's mood on a given run. The model's `top_reasons` and `rationale_for_verdict` are never changed; the rule changes only verdict and score.

  | Hard blockers | Validated verified matches | Verdict | Score | `verdict_source` |
  |---|---|---|---|---|
  | any | any | `no_bid` | capped at 35 | `rule`, reason names the blocker |
  | none | 0 | `no_bid` | capped at 35 | `rule` |
  | none | 1-2 | `needs_review` (a model `bid` or `no_bid` is overridden) | clamped to 40-65 | `rule` when the verdict changed; `model` when the model already said `needs_review` |
  | none | 3+ | the model's verdict | unchanged | `model` |

  The override reason is "one or two verified scope matches: review required, bid not yet supported".
- **"Draft for human review" footer** on every .docx export.
- **Confirm-before-save on voice.** Pending rows live in the database only; an entry reaches the firm's past performance after `POST /voice/confirm`.

## What pre-existed vs what was built Oct 26 to Nov 4

**Pre-existing:** CivicProcureBot, a separate private service (<https://github.com/Durl15/civicprocurebot>). RFP Copilot depends on its shortlist endpoint **read-only** (`GET /shortlist/today` is a proxy; nothing here writes to it). The dependency is optional: without it the daily run falls back to demo solicitations and anything ingested in the last 14 days. **Everything in this repository is new.**

Commit summary (from `git log`):

| Date | Commits |
|---|---|
| Oct 26 | Day 1 scaffold, Meta client, ingestion; Day 1b Responses API with structured output and reasoning effort; Day 2 firm profiles, agent tools, go/no-go loop; Day 2b function-call handling; Day 3 compliance matrix and title extraction |
| Oct 27 | Day 4a matrix honesty rules, `requirement_type`, unquoted-claim guard; retry on incomplete output; Day 4b narrative, capability statement, .docx export |
| Oct 28 | Day 4c verified past-performance matching; Day 4d evaluation row type and compact .docx; stateless-safe loops; Day 4f verified-claims guard; Day 4g narrative rules and the first verdict floor; Day 5 streaming run, token guard, daily run, replay, demo data; Day 6 voice intake with confirm step |
| Oct 30 | Day 6c voice against live Muse Voice Transcribe (WAV conversion, request/audio parts, hints) |
| Nov 1 | Day 6d `verified_matches` floor, tagger rules, demo-label route; Day 7a agent console, `GET /solicitations`, admin remove; Day 7b Cloud Run deploy with GCS-synced SQLite, abuse limits, matrix for `needs_review`, run-daily fallback; deploy script fixes |
| Day 8 | Symmetric verdict floor, solicitation cleanup routes, this README |

## Demo data disclosure

- Demo firms 2 and 3 and the seeded county facilities electrical maintenance solicitation (NAICS 238210, HUBZone, Syracuse) are **fictional** and labeled `DEMO`; none of their clients, contracts, values or references are real.
- **DASNY RFP 7628** (Small Business Capacity Building Services) is a **real, closed NYS RFP** with its due date shifted forward; the title says so.
- **DJ AI Business Consultant LLC (firm 1) is a real firm**, and the **Woodfork entry is a real engagement**, confirmed by voice note through the confirm step.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # then fill in the keys
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8003
.\.venv\Scripts\python.exe -m pytest
```

Port 8003 avoids the usual 8000-8002 collisions. Migrations run at startup. Drop sample PDFs in `tests/fixtures/*.pdf` to enable the real-PDF tagger test.

## Deploy (Google Cloud Run)

Project `dj-ai-grant-consultant`, region `us-east1`, service `rfp-copilot`. Run from the repo root in PowerShell, `gcloud` authenticated, `MODEL_API_KEY` and `COPILOT_ADMIN_KEY` set in `.env`:

```powershell
.\scripts\seed_prod.ps1       # once, before the first deploy: uploads the local database to gs://rfp-copilot-data (refuses if it exists; -Force overrides)
.\scripts\deploy.ps1          # idempotent: APIs, bucket, service account, secrets, Cloud Build + deploy, then GET /health and /solicitations
.\scripts\schedule_daily.ps1  # Cloud Scheduler job rfp-copilot-daily: 07:30 America/New_York, POST /agent/run-daily
```

Secret values are read from `.env` (deploy) or Secret Manager (schedule) and never printed.

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_API_KEY` | - | Meta Model API key (Secret Manager on Cloud Run) |
| `META_API_BASE` | `https://api.meta.ai/v1` | Meta endpoint |
| `META_MODEL` | `muse-spark-1.3` | Model id |
| `COPILOT_ADMIN_KEY` | - | `X-Admin-Key` for `POST /agent/run-daily`, demo-label, past-performance remove, solicitation delete and dedupe; also bypasses per-IP abuse limits. Blank means those endpoints reject everything (Secret Manager on Cloud Run) |
| `CIVICPROCUREBOT_API_BASE` / `CIVICPROCUREBOT_API_KEY` | - | Optional shortlist source; not set by `deploy.ps1`, so the daily run uses its fallback until they are |
| `SAM_API_KEY` | - | Only for `sam_notice_id` ingestion |
| `DB_PATH` | `data/rfp_copilot.sqlite3` | SQLite file (`/tmp/rfp_copilot.sqlite3` on Cloud Run) |
| `GCS_BUCKET` | - | Bucket for database persistence; unset means no sync (local dev, tests) |
| `MAX_DAILY_RUNS` | `150` | Global cap on agent runs per rolling 24 hours (429 beyond it) |
| `MAX_RUN_INPUT_TOKENS` | `60000` | Token guard: a run whose first-call input exceeds this stops with `step_failed` |
| `PRICE_IN_PER_MTOK` / `PRICE_OUT_PER_MTOK` | `0.50` / `2.00` | USD per million tokens for `est_cost_usd` |
| `LOG_LEVEL` | `INFO` | Python log level |

Do not wrap values in quotes in `.env` or Secret Manager.

### Abuse limits

In memory, per process (correct because the service runs one instance):

- Per IP: 6 agent runs per hour across `POST /agent/run`, `/agent/go-no-go` and `/agent/matrix`; 10 voice uploads per hour. A valid `X-Admin-Key` bypasses the per-IP limits. The IP is the last `X-Forwarded-For` entry (the address Cloud Run appends).
- Global: `MAX_DAILY_RUNS` agent runs per rolling 24 hours, admin included. `POST /agent/run-daily` is not counted.
- Both return `429`. Voice uploads over 25 MB return `413`.

### Costs

`est_cost_usd` per run is tokens times `PRICE_IN_PER_MTOK` / `PRICE_OUT_PER_MTOK`. **Those prices are placeholders until Meta publishes rates in the console**; treat the estimate as a relative figure, not a bill. Cost is logged per run, and every `llm_calls` row carries its `run_id`.

### Persistence

SQLite on the container's ephemeral disk, mirrored as a whole file to `gs://$GCS_BUCKET/rfp_copilot.sqlite3` (`app/storage.py`). On startup the bucket copy is downloaded before migrations and seeding (a storage error aborts startup so an empty database never overwrites the real one). After a committed write an upload is scheduled 5 seconds out (WAL-safe backup snapshot); shutdown uploads once more. The service runs `--min-instances 1 --max-instances 1` with `--no-cpu-throttling`; writes in the last 5 seconds before a crash can be lost.

## API

- `POST /solicitations`: JSON `{"text"}`, multipart PDF in `file`, or JSON `{"sam_notice_id"}`. Returns the tagged record (`due_date`, `naics`, `set_aside`, `participation_goals`, `place_of_performance`, `section_c/l/m`, `file_id`, `llm_filled`).
- `GET /solicitations`, `GET /solicitations/{id}`, `GET /demo/solicitations`
- `DELETE /solicitations/{id}` (`X-Admin-Key`): deletes the solicitation and its decisions, matrix rows, artifacts and runs; returns the counts.
- `POST /solicitations/dedupe[?dry_run=true]` (`X-Admin-Key`): removes rows with an empty or null title, and for rows sharing a title and source keeps the newest. `dry_run=true` returns the plan without deleting.
- `POST /solicitations/{id}/demo-label` `{demo, title?, due_date?, set_aside?, participation_goals?}` (`X-Admin-Key`): an omitted field is untouched, a field sent as `null` is cleared.
- `POST /agent/run` `{firm_id, solicitation_id, override_no_bid}`: SSE. `no_bid` stops after go/no-go unless `override_no_bid`; `needs_review` runs the matrix only; `bid` runs the full chain. Events: `run_started`, `step_started`, `tool_call`, `tool_result`, `decision`, `matrix_row`, `narrative`, `capability_statement`, `step_failed`, `run_complete`.
- `GET /runs/{run_id}`: stored event log and usage, for replay.
- `POST /agent/run-daily` `{firm_id}` (`X-Admin-Key`): shortlist top 3, ingest, run the chain. On shortlist failure it falls back to demo and recent solicitations and reports the reason. It never sends email.
- `POST /voice/past-performance`, `POST /voice/confirm`, `POST /voice/reject`; `DELETE /firms/{firm_id}/past-performance/{entry_index}` (`X-Admin-Key`); `GET /shortlist/today?firm_id=`; `GET /health`.

Tagging is regex first (Sections C/L/M, due date, NAICS, set-aside, place of performance); one structured-output call fills only the gaps and never overwrites a regex hit. State and local solicitations have no federal C/L/M labels, so the model maps scope, submission instructions and evaluation criteria onto C, L and M and `llm_filled` records it.

Voice contract (`POST {META_API_BASE}/asr/transcribe`, bearer `MODEL_API_KEY`): two multipart parts, `request` (`{"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV"}`) and `audio` (`audio/wav`); mono 16-bit PCM at 16 or 24 kHz. Conversion uses ffmpeg from `imageio-ffmpeg`. Smoke test: `python scripts/voice_smoke.py <audio> [firm_id] [--hints "a, b"]`.

## Known limits and what's next

- SQLite with whole-file GCS sync, single instance only. Move to Postgres before real multi-user data lands.
- `count_input_tokens` uses the Responses API `input_tokens` endpoint and is not verified against Meta's live API; if it fails, the run stops at the guard rather than spending unchecked.
- Scanned (image-only) PDFs are rejected: no OCR. Only Sections C, L and M are captured.
- Cost figures use placeholder prices.

Next: TradesBrain voice intake, an open-weight side-by-side on the same inputs, and a CivicProcureBot Pro tier.
