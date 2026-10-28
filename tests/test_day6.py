import json
from types import SimpleNamespace

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
    monkeypatch.setattr(voice, "transcribe", lambda *a, **k: {"text": TRANSCRIPT, "audio_seconds": 12.5})
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


def test_transcribe_posts_to_asr_and_logs_call(monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"text": " hello ", "duration": 3.5})

    monkeypatch.setattr(voice.httpx, "post", fake_post)
    out = REAL_TRANSCRIBE(b"abc", "n.m4a", "audio/mp4", "run1")
    assert out == {"text": "hello", "audio_seconds": 3.5}
    assert seen["url"].endswith("/asr/transcribe") and seen["data"] == {"model": "muse-voice-transcribe-1.0"}
    assert rows("SELECT feature, audio_seconds, ok FROM llm_calls WHERE run_id = 'run1'") == [{"feature": "voice_transcribe", "audio_seconds": 3.5, "ok": 1}]
