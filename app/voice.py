"""Voice-note past-performance intake: transcribe, extract with verbatim-quote checks, then confirm or reject.

Nothing reaches the firm's past-performance list until /voice/confirm; pending rows live in the database only.
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app import db, firms
from app.agent.events import Emitter
from app.agent.run import _persist
from app.config import get_settings
from app.llm.meta_client import MetaClient
from app.routes import get_llm

log = logging.getLogger(__name__)
router = APIRouter()

ASR_MODEL = "muse-voice-transcribe-1.0"
MAX_AUDIO_BYTES = 25 * 1024 * 1024
ASR_TIMEOUT_S = 120
EXTRACT_TOKENS = 1500
FIELDS = ("client", "scope", "value_range", "dates", "role", "outcome", "reference")
EXTRACT_PROMPT = (
    "You turn the transcript of a voice note into ONE past-performance entry. Fields: client, scope, value_range, dates, "
    "role, outcome, reference. For every field the speaker actually stated, give the value and, in evidence, one quote "
    "copied character for character from the transcript that supports it. A field the speaker did not state is null, "
    "with a null evidence quote. Never guess, infer or fill in. Reply with the JSON only."
)
_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_FIELD_OBJECT = {"type": "object", "properties": {f: _NULLABLE_STR for f in FIELDS}, "required": list(FIELDS), "additionalProperties": False}
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"entry": _FIELD_OBJECT, "evidence": _FIELD_OBJECT},
    "required": ["entry", "evidence"],
    "additionalProperties": False,
}


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def check_evidence(transcript: str, entry: dict, evidence: dict) -> list[str]:
    """Every non-null entry field needs a non-empty quote that appears verbatim (whitespace/case-insensitive) in the transcript."""
    hay = _norm(transcript)
    problems = []
    for f in FIELDS:
        value = entry.get(f)
        if value is None or not str(value).strip():
            continue
        quote = evidence.get(f)
        if not quote or not quote.strip():
            problems.append(f"{f}: no supporting quote")
        elif _norm(quote) not in hay:
            problems.append(f"{f}: quote not found verbatim in the transcript")
    return problems


def transcribe(audio: bytes, filename: str, content_type: str | None, run_id: str) -> dict:
    """POST {META_API_BASE}/asr/transcribe (multipart: file, model). Request shape not verified against Meta's docs
    (dev.meta.ai was not reachable); response mismatches are logged."""
    s = get_settings()
    started = time.monotonic()
    seconds, error = None, None
    try:
        r = httpx.post(
            f"{s.meta_api_base}/asr/transcribe",
            headers={"Authorization": f"Bearer {s.model_api_key}"},
            files={"file": (filename, audio, content_type or "application/octet-stream")},
            data={"model": ASR_MODEL},
            timeout=ASR_TIMEOUT_S,
        )
        r.raise_for_status()
        body = r.json()
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            log.warning("asr response mismatch: expected JSON with a string 'text', got %s", list(body) if isinstance(body, dict) else type(body))
            raise ValueError("transcription response had no 'text' field")
        seconds = next((body[k] for k in ("duration", "audio_seconds", "duration_seconds") if isinstance(body.get(k), (int, float))), None)
        if seconds is None:
            log.warning("asr response mismatch: no audio duration field in keys %s", list(body))
        return {"text": text.strip(), "audio_seconds": seconds}
    except Exception as exc:
        error = exc
        raise
    finally:
        with db.session() as conn:
            conn.execute(
                "INSERT INTO llm_calls (ts, model, feature, latency_ms, ok, error, run_id, audio_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), ASR_MODEL, "voice_transcribe", int((time.monotonic() - started) * 1000),
                 0 if error else 1, f"{type(error).__name__}: {error}"[:500] if error else None, run_id, seconds),
            )


def extract_entry(transcript: str, llm: MetaClient) -> tuple[dict, dict]:
    """One structured call (effort low); one retry with the failed checks fed back, then ValueError."""
    first = {"role": "user", "content": f"transcript:\n{transcript}"}
    messages = [first]
    problems: list[str] = []
    for _ in range(2):
        res = llm.complete(messages, system=EXTRACT_PROMPT, json_schema=EXTRACT_SCHEMA, schema_name="voice_entry",
                           reasoning_effort="low", feature="voice_extract", max_tokens=EXTRACT_TOKENS)
        parsed = res.parsed
        if not (isinstance(parsed, dict) and isinstance(parsed.get("entry"), dict) and isinstance(parsed.get("evidence"), dict)):
            problems = [res.parse_error or "no valid JSON answer returned"]
        else:
            entry = {f: parsed["entry"].get(f) for f in FIELDS}
            problems = check_evidence(transcript, entry, parsed["evidence"])
            if not problems:
                return entry, {f: parsed["evidence"].get(f) if entry[f] is not None else None for f in FIELDS}
        messages = [first, {"role": "user", "content": "Your previous answer failed these checks: " + "; ".join(problems)
                            + ". Null every field you cannot support with a verbatim quote."}]
    raise ValueError("extraction failed verification: " + "; ".join(problems))


def _emitter(firm_id: int, run_id: str | None = None) -> Emitter:
    """Resumes the stored log for run_id so a confirm lands in the same run as its transcription."""
    events = None
    if run_id:
        with db.session() as conn:
            r = conn.execute("SELECT events FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        events = json.loads(r["events"]) if r else None
    em = Emitter(lambda ev: _persist(em, firm_id, "voice"), run_id, events)
    return em


@router.post("/voice/past-performance")
async def voice_past_performance(audio: UploadFile = File(...), firm_id: int = Form(...), llm: MetaClient = Depends(get_llm)):
    if not firms.get_firm(firm_id):
        raise HTTPException(404, f"firm {firm_id} not found")
    data = await audio.read()
    if not data:
        raise HTTPException(422, "audio file is empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio exceeds 25 MB")
    em = _emitter(firm_id)
    llm.run_id = em.run_id
    try:
        t = await run_in_threadpool(transcribe, data, audio.filename or "voice-note", audio.content_type, em.run_id)
    except Exception as exc:
        raise HTTPException(502, f"transcription failed: {type(exc).__name__}: {exc}"[:500])
    em("voice_transcribed", chars=len(t["text"]), audio_seconds=t["audio_seconds"])
    if not t["text"]:
        raise HTTPException(422, "no speech found in the audio")
    try:
        entry, evidence = await run_in_threadpool(extract_entry, t["text"], llm)
    except Exception as exc:
        raise HTTPException(502, f"extraction failed: {type(exc).__name__}: {exc}"[:500])
    pending_id = uuid.uuid4().hex
    with db.session() as conn:
        conn.execute(
            "INSERT INTO pending_entries (id, created_at, firm_id, run_id, transcript, entry, evidence, verified) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (pending_id, datetime.now(timezone.utc).isoformat(), firm_id, em.run_id, t["text"], json.dumps(entry), json.dumps(evidence)),
        )
    em("voice_extracted", pending_id=pending_id, entry=entry, evidence=evidence)
    return {"pending_id": pending_id, "run_id": em.run_id, "transcript": t["text"], "entry": {**entry, "verified": False}, "evidence": evidence}


class ConfirmIn(BaseModel):
    pending_id: str
    edits: dict[str, str | None] | None = None


class RejectIn(BaseModel):
    pending_id: str


@router.post("/voice/confirm")
def voice_confirm(body: ConfirmIn):
    edits = body.edits or {}
    if bad := sorted(set(edits) - set(FIELDS)):
        raise HTTPException(422, f"unknown edit field(s): {', '.join(bad)}")
    with db.session() as conn:  # one transaction: the pending row is deleted only if the firm update commits
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM pending_entries WHERE id = ?", (body.pending_id,)).fetchone()
        if not row:
            raise HTTPException(404, "pending entry not found")
        entry = {**json.loads(row["entry"]), **edits}
        entry = {f: (v.strip() or None) if isinstance(v, str) else v for f, v in entry.items()}
        if not any(entry.values()):
            raise HTTPException(422, "entry has no content to confirm")
        entry["verified"] = True
        current = json.loads(conn.execute("SELECT past_performance FROM firms WHERE id = ?", (row["firm_id"],)).fetchone()["past_performance"])
        conn.execute("UPDATE firms SET past_performance = ? WHERE id = ?", (json.dumps([*current, entry]), row["firm_id"]))
        conn.execute("DELETE FROM pending_entries WHERE id = ?", (body.pending_id,))
    em = _emitter(row["firm_id"], row["run_id"])
    em("voice_confirmed", pending_id=body.pending_id, entry=entry, edited=sorted(edits))
    return {"firm_id": row["firm_id"], "entry": entry}


@router.post("/voice/reject")
def voice_reject(body: RejectIn):
    with db.session() as conn:
        if conn.execute("DELETE FROM pending_entries WHERE id = ?", (body.pending_id,)).rowcount != 1:
            raise HTTPException(404, "pending entry not found")
    return {"rejected": body.pending_id}
