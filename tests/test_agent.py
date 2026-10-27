import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, ingest
from app.agent import go_no_go, tools
from app.llm.meta_client import MetaClient
from app.main import app
from app.routes import get_llm
from app.seed import seed_firms
from tests.conftest import SAMPLE_TEXT, rows


def _fn_call(call_id, name, args):
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=json.dumps(args))


def _message(text):
    return SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])


def _response(rid, *output, status="completed", incomplete_reason=None):
    return SimpleNamespace(
        id=rid,
        status=status,
        incomplete_details=SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None,
        output=list(output),
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


class FakeSDK:
    """Replays scripted Responses-API objects and records the kwargs of each responses.create call."""

    def __init__(self, responses):
        self.responses_left = list(responses)
        self.calls = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses_left.pop(0)


def _llm(*responses):
    sdk = FakeSDK(responses)
    return MetaClient(client=sdk), sdk


def _tool_turn(rid, *calls):
    return _response(rid, *[_fn_call(c, n, a) for c, n, a in calls])


def _final(rid="r1", payload=None):
    return _response(rid, _message(json.dumps(payload or FINAL)))


FINAL = {
    "score": 82,
    "verdict": "bid",
    "top_reasons": ["HUBZone set-aside matches", "NAICS 541512 in firm codes", "Relevant AI past performance", "extra"],
    "blockers": [],
    "rationale_for_verdict": "Set-aside and NAICS match and past performance is relevant, so bid.",
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
    llm, sdk = _llm(
        _tool_turn("r1", ("c1", "get_firm_profile", {"firm_id": 1}), ("c2", "get_solicitation", {"solicitation_id": sol_id})),
        _tool_turn("r2", ("c3", "lookup_naics", {"code": "541512"})),
        _final("r3"),
    )
    out = go_no_go.run_go_no_go(1, sol_id, llm)

    assert out["decision"]["verdict"] == "bid" and out["decision"]["top_reasons"] == FINAL["top_reasons"][:3]
    assert out["decision"]["rationale_for_verdict"] == FINAL["rationale_for_verdict"]
    assert [t["tool"] for t in out["trace"]] == ["get_firm_profile", "get_solicitation", "lookup_naics"]
    assert all(len(t["result_summary"]) <= go_no_go.SUMMARY_CHARS for t in out["trace"])
    assert [c.get("previous_response_id") for c in sdk.calls] == [None, "r1", "r2"]
    assert all(c["reasoning"] == {"effort": "high"} and c["max_output_tokens"] == 8000 for c in sdk.calls)
    first = sdk.calls[1]["input"][0]
    assert first["type"] == "function_call_output" and first["call_id"] == "c1" and json.loads(first["output"])["name"]
    assert [i["call_id"] for i in sdk.calls[1]["input"]] == ["c1", "c2"]
    assert "record_decision" not in {t["name"] for t in sdk.calls[0]["tools"]}
    (saved,) = rows("SELECT * FROM decisions")
    assert (saved["id"], saved["firm_id"], saved["solicitation_id"], saved["score"]) == (out["decision_id"], 1, sol_id, 82)


def test_go_no_go_schema_and_prompt_tightened():
    assert "rationale_for_verdict" in go_no_go.GO_NO_GO_SCHEMA["required"]
    assert "FIRST must be the single decisive factor" in go_no_go.SYSTEM_PROMPT
    assert "ONLY hard disqualifiers" in go_no_go.SYSTEM_PROMPT


def test_get_solicitation_and_firm_profile_expose_title_and_place_of_performance():
    seed_firms()
    sol = tools.get_solicitation(_solicitation())
    assert sol["title"] == "Solicitation 36C10X26Q0042" and sol["place_of_performance"] == "Syracuse, NY"
    assert tools.get_firm_profile(1)["place_of_performance"] == "Syracuse, NY"


def test_go_no_go_feeds_tool_errors_back_to_model():
    seed_firms()
    sol_id = _solicitation()
    llm, _ = _llm(_tool_turn("r1", ("c1", "lookup_naics", {"code": "bad"})), _final("r2"))
    out = go_no_go.run_go_no_go(1, sol_id, llm)
    assert "invalid arguments" in out["trace"][0]["result_summary"]


def test_go_no_go_message_preceded_by_text_in_tool_turn_still_calls_tools():
    seed_firms()
    sol_id = _solicitation()
    llm, sdk = _llm(
        _response("r1", _message("let me check"), _fn_call("c1", "lookup_naics", {"code": "541512"})),
        _final("r2"),
    )
    out = go_no_go.run_go_no_go(1, sol_id, llm)
    assert [t["tool"] for t in out["trace"]] == ["lookup_naics"] and len(sdk.calls) == 2


def test_go_no_go_unparseable_answer_raises_with_text_and_status():
    seed_firms()
    sol_id = _solicitation()
    llm, _ = _llm(_response("r1", _message("not json " * 100), status="incomplete"))
    with pytest.raises(RuntimeError) as exc:
        go_no_go.run_go_no_go(1, sol_id, llm)
    msg = str(exc.value)
    assert "status=incomplete" in msg and "not json" in msg and "JSONDecodeError" in msg
    assert msg.count("not json") <= 500 // len("not json ") + 1


def test_go_no_go_invalid_answer_and_turn_limit_raise():
    seed_firms()
    sol_id = _solicitation()
    llm, _ = _llm(_final(payload={**FINAL, "score": 500}))
    with pytest.raises(RuntimeError, match="validation"):
        go_no_go.run_go_no_go(1, sol_id, llm)
    llm, sdk = _llm(*[_tool_turn(f"r{i}", (f"c{i}", "lookup_naics", {"code": "541512"})) for i in range(go_no_go.MAX_TURNS)])
    with pytest.raises(RuntimeError, match="tool calls never stopped"):
        go_no_go.run_go_no_go(1, sol_id, llm)
    assert len(sdk.calls) == go_no_go.MAX_TURNS


def test_go_no_go_endpoint():
    seed_firms()
    sol_id = _solicitation()
    llm, _ = _llm(_final())
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


def test_go_no_go_retries_once_on_max_output_tokens():
    seed_firms()
    sol_id = _solicitation()
    llm, sdk = _llm(_response("r1", status="incomplete", incomplete_reason="max_output_tokens"), _final("r2"))
    out = go_no_go.run_go_no_go(1, sol_id, llm)

    assert out["decision"]["verdict"] == "bid"
    assert [(c["reasoning"]["effort"], c["max_output_tokens"]) for c in sdk.calls] == [("high", 8000), ("medium", 16000)]


def test_retry_doubles_tokens_up_to_cap_and_lowers_effort_to_floor():
    llm, sdk = _llm(
        _response("r1", status="incomplete", incomplete_reason="max_output_tokens"),
        _response("r2", _message("{}")),
    )
    llm.complete("hi", reasoning_effort="minimal", max_tokens=40000)
    assert [(c["reasoning"]["effort"], c["max_output_tokens"]) for c in sdk.calls] == [("minimal", 40000), ("minimal", 48000)]


def test_other_incomplete_reasons_are_not_retried():
    llm, sdk = _llm(_response("r1", status="incomplete", incomplete_reason="content_filter"))
    assert llm.complete("hi").status == "incomplete" and len(sdk.calls) == 1
