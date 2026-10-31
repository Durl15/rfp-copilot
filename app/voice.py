"""Voice-note past-performance intake: transcribe, extract with verbatim-quote checks, then confirm or reject.

Nothing reaches the firm's past-performance list until /voice/confirm; pending rows live in the database only.
"""
import json
import logging
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import imageio_ffmpeg
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
ASR_TIMEOUT_S = 180
FFMPEG_TIMEOUT_S = 120
EXTRACT_TOKENS = 1500
FIELDS = ("client", "scope", "value_range", "dates", "role", "outcome", "reference")
EXTRACT_PROMPT = (
    "You turn the transcript of a voice note into ONE past-performance entry. Fields: client, scope, value_range, dates, "
    "role, outcome, reference. For every field the speaker actually stated, give the value and, in evidence, one quote "
    "copied character for character from the transcript that supports it. A field the speaker did not state is null, "
    "with a null evidence quote. Never guess, infer or fill in. The first sentence is often a preamble such as \"this is a past "
    "performance note for <firm>\": that firm is the speaker, never the client. Reply with the JSON only."
)
HINTS_RULE = (
    " The user message lists known proper nouns (hints). If a name in the transcript is a close phonetic match to a hint, use the "
    "hint's spelling in the field value; the evidence quote must still be copied verbatim from the transcript."
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


class VoiceError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def to_wav(audio: bytes, filename: str) -> bytes:
    """Any upload -> 16 kHz mono 16-bit PCM WAV (the only encoding the ASR endpoint accepts), via the bundled ffmpeg."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / f"in{Path(filename).suffix[:10]}", Path(tmp) / "out.wav"
        src.write_bytes(audio)
        try:
            p = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(src), "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-f", "wav", str(out)],
                               capture_output=True, timeout=FFMPEG_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise VoiceError(400, "unsupported or corrupt audio: ffmpeg timed out")
        if p.returncode != 0 or not out.exists():
            raise VoiceError(400, "unsupported or corrupt audio: " + p.stderr.decode("utf-8", "replace")[:200])
        return out.read_bytes()


def _post_asr(wav: bytes) -> dict:
    """POST the two required parts; one retry on 5xx/timeout. Raises VoiceError(502) on failure."""
    s = get_settings()
    last = "no response"
    for _ in range(2):
        try:
            r = httpx.post(
                f"{s.meta_api_base}/asr/transcribe",
                headers={"Authorization": f"Bearer {s.model_api_key}"},
                files={
                    "request": (None, json.dumps({"model": ASR_MODEL, "audioEncoding": "WAV"}), "application/json"),
                    "audio": ("audio.wav", wav, "audio/wav"),
                },
                timeout=ASR_TIMEOUT_S,
            )
        except httpx.TransportError as exc:  # includes timeouts
            last = f"{type(exc).__name__}: {exc}"
            continue
        if r.status_code >= 500:
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            continue
        if r.status_code != 200:
            raise VoiceError(502, f"transcription failed: HTTP {r.status_code}: {r.text[:300]}")
        try:
            body = r.json()
        except ValueError:
            raise VoiceError(502, f"transcription response was not JSON: {r.text[:300]}")
        if not isinstance(body, dict) or not isinstance(body.get("transcript"), str):
            raise VoiceError(502, f"transcription response had no 'transcript': {r.text[:300]}")
        return body
    raise VoiceError(502, f"transcription failed: {last}"[:500])


def transcribe(audio: bytes, filename: str, content_type: str | None, run_id: str) -> dict:
    """Convert to WAV, POST {META_API_BASE}/asr/transcribe, log the call. Returns text, audio_duration_ms, raw response, wav size."""
    started = time.monotonic()
    duration_ms, error = None, None
    try:
        wav = to_wav(audio, filename)
        body = _post_asr(wav)
        d = body.get("audioDurationMs")
        duration_ms = int(d) if isinstance(d, (int, float)) else None
        return {"text": body["transcript"].strip(), "audio_duration_ms": duration_ms, "raw": body, "wav_bytes": len(wav)}
    except Exception as exc:
        error = exc
        raise
    finally:
        with db.session() as conn:
            conn.execute(
                "INSERT INTO llm_calls (ts, model, feature, latency_ms, ok, error, run_id, audio_duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), ASR_MODEL, "voice_transcribe", int((time.monotonic() - started) * 1000),
                 0 if error else 1, f"{type(error).__name__}: {error}"[:500] if error else None, run_id, duration_ms),
            )


def extract_entry(transcript: str, llm: MetaClient, hints: str | None = None) -> tuple[dict, dict]:
    """One structured call (effort low); one retry with the failed checks fed back, then ValueError."""
    hints = (hints or "").strip()
    system = EXTRACT_PROMPT + (HINTS_RULE if hints else "")
    first = {"role": "user", "content": f"transcript:\n{transcript}" + (f"\n\nhints: {hints}" if hints else "")}
    messages = [first]
    problems: list[str] = []
    for _ in range(2):
        res = llm.complete(messages, system=system, json_schema=EXTRACT_SCHEMA, schema_name="voice_entry",
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
async def voice_past_performance(audio: UploadFile = File(...), firm_id: int = Form(...), hints: str | None = Form(None), llm: MetaClient = Depends(get_llm)):
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
    except VoiceError as exc:
        raise HTTPException(exc.status, exc.detail)
    except Exception as exc:
        raise HTTPException(502, f"transcription failed: {type(exc).__name__}: {exc}"[:500])
    em("voice_transcribed", chars=len(t["text"]), audio_duration_ms=t["audio_duration_ms"])
    if not t["text"]:
        raise HTTPException(422, "no speech found in the audio")
    try:
        entry, evidence = await run_in_threadpool(extract_entry, t["text"], llm, hints)
    except Exception as exc:
        raise HTTPException(502, f"extraction failed: {type(exc).__name__}: {exc}"[:500])
    pending_id = uuid.uuid4().hex
    with db.session() as conn:
        conn.execute(
            "INSERT INTO pending_entries (id, created_at, firm_id, run_id, transcript, entry, evidence, verified, raw_response) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (pending_id, datetime.now(timezone.utc).isoformat(), firm_id, em.run_id, t["text"], json.dumps(entry), json.dumps(evidence), json.dumps(t["raw"])),
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
