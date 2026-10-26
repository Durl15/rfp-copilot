import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import db, ingest
from app.agent import go_no_go, tools
from app.llm.meta_client import LLMResult
from app.main import app
from app.routes import get_llm
from app.seed import seed_firms
from tests.conftest import SAMPLE_TEXT, rows


class FakeLLM:
    """Replays scripted LLMResults and records the kwargs of each call."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        return self.results.pop(0)


def _tool_turn(rid, *calls):
    return LLMResult("", 1, 1, None, None, [{"call_id": c, "name": n, "arguments": json.dumps(a)} for c, n, a in calls], None, rid)


FINAL = {
    "score": 82,
    "verdict": "bid",
    "top_reasons": ["HUBZone set-aside matches", "NAICS 541512 in firm codes", "Relevant AI past performance", "extra"],
    "blockers": [],
    "estimated_effort_hours": 56,
    "set_aside_match": True,
    "naics_match": True,
}


def _solicitation() -> str:
    sol_id = str(uuid.uuid4())
    tags = ingest.regex_tag(SAMPLE_TEXT)
    row = {"id": sol_id, "created_at": "2026-10-26", "source": "text", "raw_text": SAMPLE_TEXT, **tags}
    with db.session() as conn:
        conn.execute(f"INSERT INTO solicitations ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    return sol_id


def test_seed_is_idempotent():
    assert seed_firms() == 3
    assert seed_firms() == 0
    assert [r["id"] for r in rows("SELECT id FROM firms ORDER BY id")] == [1, 2, 3]


def test_lookup_naics():
    assert tools.lookup_naics("541512")["title"] == "Computer Systems Design Services"
    assert "error" in tools.lookup_naics("999999")
    assert len(tools.NAICS) == 30


def test_dispatcher_validates_args():
    seed_firms()
    assert tools.execute_tool("get_firm_profile", {"firm_id": 1})["name"].startswith("DJ AI")
    assert tools.execute_tool("lookup_naics", '{"code": "238210"}')["code"] == "238210"
    for name, args in [
        ("get_firm_profile", {"firm_id": "abc"}),
        ("get_firm_profile", {"firm_id": 1, "extra": 1}),
        ("lookup_naics", {"code": "54"}),
        ("search_past_performance", {"firm_id": 1, "keywords": []}),
        ("record_decision", {"solicitation_id": "x", "firm_id": 1, "verdict": "maybe", "score": 5, "reasons": []}),
        ("nope", {}),
    ]:
        with pytest.raises(tools.ToolError):
            tools.execute_tool(name, args)


def test_tool_definitions_shape():
    assert {d["name"] for d in tools.TOOL_DEFINITIONS} == set(tools.TOOLS)
    for d in tools.TOOL_DEFINITIONS:
        assert d["type"] == "function" and d["parameters"]["type"] == "object"


def test_search_past_performance_and_effort_and_record():
    seed_firms()
    hits = tools.search_past_performance(1, ["grant"])["matches"]
    assert len(hits) == 1 and hits[0]["matched_keywords"] == ["grant"]
    assert tools.estimate_effort({"past_performance": 1, "unknown_gap": 2})["hours"] == 40 + 16 + 16
    tools.record_decision("s1", 1, "bid", 80, ["r"])
    assert rows("SELECT verdict, score FROM decisions") == [{"verdict": "bid", "score": 80}]


def test_go_no_go_loop_two_tool_calls_then_final():
    seed_firms()
    sol_id = _solicitation()
    llm = FakeLLM(
        [
            _tool_turn("r1", ("c1", "get_firm_profile", {"firm_id": 1}), ("c2", "get_solicitation", {"solicitation_id": sol_id})),
            _tool_turn("r2", ("c3", "lookup_naics", {"code": "541512"})),
            LLMResult(json.dumps(FINAL), 1, 1, None, None, [], None, "r3"),
        ]
    )
    out = go_no_go.run_go_no_go(1, sol_id, llm)

    assert out["decision"]["verdict"] == "bid" and out["decision"]["top_reasons"] == FINAL["top_reasons"][:3]
    assert [t["tool"] for t in out["trace"]] == ["get_firm_profile", "get_solicitation", "lookup_naics"]
    assert all(len(t["result_summary"]) <= go_no_go.SUMMARY_CHARS for t in out["trace"])
    assert [c.get("previous_response_id") for c in llm.calls] == [None, "r1", "r2"]
    assert all(c["reasoning_effort"] == "high" and c["max_tokens"] == 4000 and c["feature"] == "go_no_go" for c in llm.calls)
    assert llm.calls[1]["messages"][0] == {"type": "function_call_output", "call_id": "c1", "output": llm.calls[1]["messages"][0]["output"]}
    assert len(llm.calls[1]["messages"]) == 2
    assert "record_decision" not in {t["name"] for t in llm.calls[0]["tools"]}
    (saved,) = rows("SELECT * FROM decisions")
    assert (saved["id"], saved["firm_id"], saved["solicitation_id"], saved["score"]) == (out["decision_id"], 1, sol_id, 82)


def test_go_no_go_feeds_tool_errors_back_to_model():
    seed_firms()
    sol_id = _solicitation()
    llm = FakeLLM(
        [
            _tool_turn("r1", ("c1", "lookup_naics", {"code": "bad"})),
            LLMResult(json.dumps(FINAL), 1, 1, None, None, [], None, "r2"),
        ]
    )
    out = go_no_go.run_go_no_go(1, sol_id, llm)
    assert "invalid arguments" in out["trace"][0]["result_summary"]


def test_go_no_go_endpoint():
    seed_firms()
    sol_id = _solicitation()
    llm = FakeLLM([LLMResult(json.dumps(FINAL), 1, 1, None, None, [], None, "r1")])
    app.dependency_overrides[get_llm] = lambda: llm
    try:
        client = TestClient(app)
        r = client.post("/agent/go-no-go", json={"firm_id": 1, "solicitation_id": sol_id})
        assert r.status_code == 200 and r.json()["decision"]["score"] == 82
        assert client.post("/agent/go-no-go", json={"firm_id": 99, "solicitation_id": sol_id}).status_code == 404
        assert client.get("/firms").json()[1]["certifications"] == ["HUBZone", "SDVOSB"]
        assert client.get("/firms/3").json()["naics_codes"] == ["541330"]
        assert client.get("/firms/99").status_code == 404
    finally:
        app.dependency_overrides.clear()
