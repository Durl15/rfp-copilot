import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ingest, routes
from app.llm.meta_client import LLMResult
from app.main import app
from tests.conftest import SAMPLE_TEXT, make_pdf, rows


class FakeLLM:
    def __init__(self, payload=None, fail=False):
        self.payload, self.fail, self.calls = payload or {}, fail, []

    def complete(self, messages, **kw):
        self.calls.append((messages, kw))
        if self.fail:
            raise RuntimeError("llm down")
        return LLMResult(json.dumps(self.payload), 1, 1)


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(routes, "get_llm", lambda: fake)
    return fake


@pytest.fixture
def client(tmp_db):
    with TestClient(app) as c:
        yield c


# ---- tagger


def test_regex_tagger_extracts_everything_without_llm_call():
    llm = FakeLLM()
    tags, filled = ingest.tag_solicitation(SAMPLE_TEXT, llm)
    assert tags["due_date"] == "2026-10-30"
    assert tags["naics"] == "541512"
    assert tags["set_aside"] == "HUBZone"
    assert tags["place_of_performance"] == "Syracuse, NY"
    assert tags["section_c"].startswith("SECTION C") and "software modernization" in tags["section_c"]
    assert "price volume" in tags["section_l"] and "past performance" in tags["section_m"]
    assert filled == [] and llm.calls == []


def test_toc_hit_does_not_shadow_real_section():
    text = "SECTION C\nSECTION L\nSECTION M\n" + SAMPLE_TEXT
    assert "software modernization" in ingest.regex_tag(text)["section_c"]


def test_llm_fills_only_gaps_in_one_call():
    llm = FakeLLM(
        {
            "due_date": "November 5, 2026",
            "naics": "999999",
            "set_aside": None,
            "place_of_performance": None,
            "section_c": None,
            "section_l": None,
            "section_m": None,
        }
    )
    tags, filled = ingest.tag_solicitation("NAICS 541512. Proposals are due soon.", llm)
    assert len(llm.calls) == 1
    assert llm.calls[0][1]["json_schema"] and llm.calls[0][1]["feature"] == "solicitation_tagger"
    assert tags["naics"] == "541512"  # regex value is never overwritten
    assert tags["due_date"] == "2026-11-05" and filled == ["due_date"]


def test_llm_failure_keeps_regex_tags():
    tags, filled = ingest.tag_solicitation("NAICS 541512", FakeLLM(fail=True))
    assert tags["naics"] == "541512" and filled == []


# ---- three input types via the API


def test_post_text(client, llm):
    r = client.post("/solicitations", json={"text": SAMPLE_TEXT})
    assert r.status_code == 201
    body = r.json()
    assert body["source"] == "text" and body["naics"] == "541512" and "raw_text" not in body
    assert client.get(f"/solicitations/{body['id']}").json()["due_date"] == "2026-10-30"
    assert len(rows("SELECT * FROM solicitations")) == 1


def test_post_pdf_uploads_and_extracts(client, llm, monkeypatch):
    uploaded = {}
    monkeypatch.setattr(ingest, "upload_pdf", lambda data, name, s: uploaded.update(name=name) or "file_abc")
    pdf = make_pdf(SAMPLE_TEXT.splitlines())
    r = client.post("/solicitations", files={"file": ("rfp.pdf", pdf, "application/pdf")})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"] == "pdf" and body["file_id"] == "file_abc" and uploaded["name"] == "rfp.pdf"
    assert body["naics"] == "541512" and body["due_date"] == "2026-10-30"


def test_post_pdf_survives_files_api_failure(client, llm, monkeypatch):
    monkeypatch.setattr(ingest, "upload_pdf", lambda *a: None)
    r = client.post("/solicitations", files={"file": ("rfp.pdf", make_pdf(["NAICS 541512"]), "application/pdf")})
    assert r.status_code == 201 and r.json()["file_id"] is None


def test_post_non_pdf_rejected(client, llm):
    r = client.post("/solicitations", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 415


def test_post_sam_notice(client, llm, monkeypatch):
    monkeypatch.setattr(
        ingest,
        "fetch_sam_notice",
        lambda nid, s: {
            "title": "Network Upgrade",
            "text": "Network Upgrade\n\nBody with no tags.",
            "hints": {
                "due_date": "2026-11-12",
                "naics": "517311",
                "set_aside": "HUBZone",
                "place_of_performance": "Utica, NY",
            },
        },
    )
    r = client.post("/solicitations", json={"sam_notice_id": "abc123"})
    assert r.status_code == 201
    body = r.json()
    assert body["source"] == "sam" and body["sam_notice_id"] == "abc123" and body["title"] == "Network Upgrade"
    assert body["naics"] == "517311" and body["due_date"] == "2026-11-12"


@pytest.mark.parametrize("payload", [{}, {"text": "  "}, {"text": "x", "sam_notice_id": "y"}])
def test_post_requires_exactly_one_input(client, llm, payload):
    assert client.post("/solicitations", json=payload).status_code == 422


def test_sam_requires_key(monkeypatch):
    monkeypatch.delenv("SAM_API_KEY", raising=False)
    with pytest.raises(ValueError):
        ingest.ingest_sam("abc", FakeLLM())


# ---- real fixtures (skipped when tests/fixtures has no PDFs)

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.pdf"))


@pytest.mark.skipif(not FIXTURES, reason="no PDFs in tests/fixtures")
@pytest.mark.parametrize("pdf", FIXTURES, ids=lambda p: p.name)
def test_tagger_on_fixture_pdf(pdf):
    text = ingest.extract_pdf_text(pdf.read_bytes())
    assert text
    tags = ingest.regex_tag(text)
    assert set(tags) == set(ingest.FIELDS)
    assert any(tags.values()), f"regex pass found nothing in {pdf.name}"
