import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import firms, voice
from app.agent import matrix
from app.main import app
from app.routes import get_llm
from app.seed import seed_firms
from tests.conftest import rows

REAL_TRANSCRIBE = voice.transcribe
TRANSCRIPT = (
    "This is a note about work for Onondaga County. We did help desk support and ITSM migration for the county. "
    "I was the prime contractor. It was a two point four million dollar contract and the county was very happy."
)
ENTRY = {"client": "Onondaga County", "scope": "help desk support and ITSM migration", "value_range": "two point four million dollar",
         "dates": None, "role": "prime contractor", "outcome": "the county was very happy", "reference": None}
QUOTES = {"client": "work for Onondaga County", "scope": "help desk support and ITSM migration", "value_range": "two point four million dollar",
          "dates": None, "role": "prime contractor", "outcome": "the county was very happy", "reference": None}


class FakeLLM:
    """Scripted extraction answers; scope and match calls answer for the past-performance assessment."""

    run_id = None

    def __init__(self, *extractions):
        self.extractions = list(extractions)
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append(kw)
        if kw["feature"] == "voice_extract":
            return SimpleNamespace(parsed=self.extractions.pop(0), parse_error=None)
        if kw["feature"] == "pp_scope":
            return SimpleNamespace(parsed={"terms": ["help desk", "ITSM migration"], "size_indicator": ""}, parse_error=None)
        entry = json.loads(messages[0]["content"].split("entry:\n", 1)[1])
        hit = "ITSM migration" in entry.get("scope", "")
        return SimpleNamespace(parsed={"relevant": hit, "matched_terms": ["help desk", "ITSM migration"] if hit else [],
                                       "quote": "help desk support and ITSM migration" if hit else ""}, parse_error=None)


@pytest.fixture(autouse=True)
def fake_asr(monkeypatch):
    monkeypatch.setattr(voice, "transcribe", lambda *a, **k: {"text": TRANSCRIPT, "audio_duration_ms": 12500, "raw": {"transcript": TRANSCRIPT, "audioDurationMs": 12500}, "wav_bytes": 1})
    seed_firms()


def upload(llm, firm_id=1):
    app.dependency_overrides[get_llm] = lambda: llm
    try:
        return TestClient(app).post("/voice/past-performance", files={"audio": ("note.wav", b"RIFFdata", "audio/wav")}, data={"firm_id": firm_id})
    finally:
        app.dependency_overrides.clear()


def good(entry=ENTRY, evidence=QUOTES):
    return {"entry": entry, "evidence": evidence}


def verified_matches(llm):
    return len(matrix.assess_past_performance(firms.get_firm(1), {"section_c": "help desk and ITSM migration", "title": "t"}, llm)["verified"])


def test_transcript_to_entry_with_valid_quotes():
    llm = FakeLLM(good())
    r = upload(llm)
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == TRANSCRIPT and body["entry"]["client"] == "Onondaga County" and body["entry"]["verified"] is False
    assert all(q is None or q in TRANSCRIPT for q in body["evidence"].values())
    assert llm.calls[0]["reasoning_effort"] == "low"
    assert rows("SELECT verified FROM pending_entries") == [{"verified": 0}]


def test_fabricated_quote_rejected_after_one_retry():
    fake = {**QUOTES, "reference": "J. Smith, Director"}
    llm = FakeLLM(good({**ENTRY, "reference": "J. Smith"}, fake), good({**ENTRY, "reference": "J. Smith"}, fake))
    r = upload(llm)
    assert r.status_code == 502 and "reference" in r.json()["detail"]
    assert len(llm.calls) == 2 and rows("SELECT * FROM pending_entries") == []


def test_retry_recovers():
    bad = good({**ENTRY, "dates": "2021"}, {**QUOTES, "dates": "in 2021"})
    llm = FakeLLM(bad, good())
    r = upload(llm)
    assert r.status_code == 200 and r.json()["entry"]["dates"] is None


def test_unsaid_fields_are_null():
    r = upload(FakeLLM(good()))
    entry = r.json()["entry"]
    assert entry["dates"] is None and entry["reference"] is None
    assert r.json()["evidence"]["dates"] is None


def test_unconfirmed_entry_does_not_change_matches():
    llm = FakeLLM(good())
    before = firms.get_firm(1)["past_performance"]
    assert upload(llm).status_code == 200
    assert firms.get_firm(1)["past_performance"] == before and verified_matches(llm) == 0


def test_confirm_moves_firm_from_zero_to_one_and_persists():
    llm = FakeLLM(good())
    assert verified_matches(llm) == 0
    body = upload(llm).json()
    c = TestClient(app).post("/voice/confirm", json={"pending_id": body["pending_id"], "edits": {"dates": "2022-2024"}})
    assert c.status_code == 200 and c.json()["entry"]["verified"] is True
    added = firms.get_firm(1)["past_performance"][-1]
    assert added["verified"] is True and added["dates"] == "2022-2024" and added["client"] == "Onondaga County"
    assert verified_matches(llm) == 1
    assert rows("SELECT * FROM pending_entries") == []
    assert TestClient(app).post("/voice/confirm", json={"pending_id": body["pending_id"]}).status_code == 404
    events = [e["event"] for e in json.loads(rows("SELECT events FROM runs WHERE run_id = ?", (body["run_id"],))[0]["events"])]
    assert events == ["voice_transcribed", "voice_extracted", "voice_confirmed"]


def test_reject_removes_pending():
    llm = FakeLLM(good())
    before = firms.get_firm(1)["past_performance"]
    pid = upload(llm).json()["pending_id"]
    assert TestClient(app).post("/voice/reject", json={"pending_id": pid}).status_code == 200
    assert rows("SELECT * FROM pending_entries") == [] and firms.get_firm(1)["past_performance"] == before
    assert TestClient(app).post("/voice/confirm", json={"pending_id": pid}).status_code == 404


def test_check_evidence_is_case_and_whitespace_insensitive():
    assert voice.check_evidence("Hello  World", {"client": "x"}, {"client": "hello\nworld"}) == []


def tiny_wav(seconds=0.2, rate=44100) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * 2 * int(rate * seconds))
    return buf.getvalue()


def capture_post(monkeypatch, *responses):
    calls, queue = [], list(responses)

    def fake_post(url, **kw):
        calls.append({"url": url, **kw})
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(voice.httpx, "post", fake_post)
    return calls


def resp(status=200, body=None, text=""):
    return SimpleNamespace(status_code=status, json=lambda: body, text=text or json.dumps(body))


OK = {"sessionId": "s1", "transcript": " hello ", "audioDurationMs": 60000, "turns": []}


def test_transcribe_sends_exact_parts_converts_and_logs(monkeypatch):
    calls = capture_post(monkeypatch, resp(200, OK))
    out = REAL_TRANSCRIBE(tiny_wav(), "n.m4a", "audio/mp4", "run1")  # real ffmpeg, 44.1 kHz stereo in
    c = calls[0]
    assert c["url"].endswith("/asr/transcribe") and set(c["files"]) == {"request", "audio"} and "data" not in c
    name, value, ctype = c["files"]["request"]
    assert name is None and ctype == "application/json" and json.loads(value) == {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV"}
    _, wav_bytes, wtype = c["files"]["audio"]
    assert wtype == "audio/wav" and c["timeout"] == 180
    with wave.open(io.BytesIO(wav_bytes)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
    assert out["text"] == "hello" and out["audio_duration_ms"] == 60000 and out["raw"] == OK
    assert rows("SELECT feature, model, audio_duration_ms, ok FROM llm_calls WHERE run_id = 'run1'") == [
        {"feature": "voice_transcribe", "model": "muse-voice-transcribe-1.0", "audio_duration_ms": 60000, "ok": 1}]


def test_ffmpeg_failure_is_400(monkeypatch):
    calls = capture_post(monkeypatch)
    monkeypatch.setattr(voice, "transcribe", REAL_TRANSCRIBE)
    app.dependency_overrides[get_llm] = lambda: FakeLLM()
    try:
        r = TestClient(app).post("/voice/past-performance", files={"audio": ("bad.m4a", b"not audio", "audio/mp4")}, data={"firm_id": 1})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 400 and r.json()["detail"].startswith("unsupported or corrupt audio") and calls == []


def test_asr_retries_once_on_5xx_then_502(monkeypatch):
    calls = capture_post(monkeypatch, resp(503, text="down"), httpx.ReadTimeout("slow"))
    with pytest.raises(voice.VoiceError) as e:
        REAL_TRANSCRIBE(tiny_wav(), "n.wav", "audio/wav", "run2")
    assert e.value.status == 502 and len(calls) == 2
    assert rows("SELECT ok FROM llm_calls WHERE run_id = 'run2'") == [{"ok": 0}]


def test_asr_retry_recovers_and_4xx_not_retried(monkeypatch):
    calls = capture_post(monkeypatch, resp(500, text="x"), resp(200, OK))
    assert REAL_TRANSCRIBE(tiny_wav(), "n.wav", "audio/wav", "run3")["text"] == "hello" and len(calls) == 2
    calls = capture_post(monkeypatch, resp(400, text="Malformed request"))
    with pytest.raises(voice.VoiceError, match="Malformed request"):
        REAL_TRANSCRIBE(tiny_wav(), "n.wav", "audio/wav", "run4")
    assert len(calls) == 1


def test_route_stores_duration_and_raw_response(monkeypatch):
    monkeypatch.setattr(voice, "transcribe", lambda *a, **k: {"text": TRANSCRIPT, "audio_duration_ms": 60000, "raw": OK, "wav_bytes": 1})
    assert upload(FakeLLM(good())).status_code == 200
    assert json.loads(rows("SELECT raw_response FROM pending_entries")[0]["raw_response"]) == OK


WOODFORK = (Path(__file__).parent / "fixtures" / "voice_transcript_woodfork.txt").read_text(encoding="utf-8")
W_ENTRY = {"client": "The Woodfork Group", "scope": "East Adams RFQ project and the City MWBE project", "value_range": None,
           "dates": "four months; late February 2027", "role": "AI platform development and staff training",
           "outcome": "faster bid turnaround", "reference": "Terry Woodfork"}
W_QUOTES = {"client": "The client was the Woodford Group", "scope": "East Adam RFQ project and the City MWBE project", "value_range": None,
            "dates": "Over four months", "role": "AI platform development and and staff training", "outcome": "faster faster bid turnaround",
            "reference": "Terry Woodford president of the Woodford Group"}


def test_hints_reach_prompt_and_value_differs_from_verbatim_quote():
    llm = FakeLLM(good(W_ENTRY, W_QUOTES))
    entry, evidence = voice.extract_entry(WOODFORK, llm, "The Woodfork Group, East Adams, Terry Woodfork")
    assert entry["client"] == "The Woodfork Group" and "Woodford" in evidence["client"]
    assert voice.check_evidence(WOODFORK, entry, evidence) == []
    assert "hint" in llm.calls[0]["system"] and "phonetic" in llm.calls[0]["system"]
    llm2 = FakeLLM(good(W_ENTRY, W_QUOTES))
    voice.extract_entry(WOODFORK, llm2)
    assert "phonetic" not in llm2.calls[0]["system"]


def test_preamble_firm_is_not_the_client():
    assert "speaker, never the client" in voice.EXTRACT_PROMPT
    bad = {**W_ENTRY, "client": "DJ AI business consultant"}
    assert voice.check_evidence(WOODFORK, bad, {**W_QUOTES, "client": "note for DJ AI business consultant"}) == []
    entry, _ = voice.extract_entry(WOODFORK, FakeLLM(good(W_ENTRY, W_QUOTES)))
    assert "DJ AI" not in entry["client"] and entry["value_range"] is None
