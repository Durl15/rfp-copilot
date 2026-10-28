import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import firms, ingest
from app.agent import go_no_go, matrix, narrative
from app.llm.meta_client import MetaClient
from app.main import app
from app.routes import get_llm
from app.seed import DEMO_SOLICITATION_ID, seed_firms
from tests.conftest import SAMPLE_TEXT, fake_pp, rows
from tests.test_agent import FINAL, FakeSDK, _final, _message, _response, _solicitation, _tool_turn
from tests.test_day4b import STATEMENT, _text
from tests.test_matrix import GOOD_ROWS, _matrix_final

BAD = _response("bad", _message("not json"))


def _llm(*responses, tokens=1000):
    """MetaClient over a scripted SDK; responses.input_tokens.count reports `tokens`."""
    sdk = FakeSDK(responses)
    sdk.responses.input_tokens = SimpleNamespace(count=lambda **kw: SimpleNamespace(input_tokens=tokens))
    return MetaClient(client=sdk), sdk


def bid_script():
    return [
        _tool_turn("g1", ("c1", "get_firm_profile", {"firm_id": 1})),
        _final("g2"),
        _matrix_final("m", GOOD_ROWS),
        _response("n", _message(_text(100))),
        _final("c", STATEMENT),
    ]


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in filter(None, text.split("\n\n")):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        ev = json.loads(lines["data"])
        assert lines["event"] == ev["event"] and lines["id"] == str(ev["seq"])
        events.append(ev)
    return events


def post_run(llm, firm_id=1, sol_id=None, **extra):
    app.dependency_overrides[get_llm] = lambda: llm
    try:
        r = TestClient(app).post("/agent/run", json={"firm_id": firm_id, "solicitation_id": sol_id, **extra})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    return parse_sse(r.text)


def names(events):
    return [e["event"] for e in events]


def test_bid_path_event_order():
    seed_firms()
    llm, sdk = _llm(*bid_script())
    events = post_run(llm, sol_id=_solicitation())
    assert names(events) == [
        "run_started",
        "step_started", "tool_call", "tool_result",  # token_guard
        "step_started", "tool_call", "tool_result", "decision",  # go_no_go
        "step_started", "matrix_row", "matrix_row", "matrix_row",
        "step_started", "narrative",
        "step_started", "capability_statement",
        "run_complete",
    ]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["timestamp"] for e in events)
    assert [e["step"] for e in events if e["event"] == "step_started"] == ["token_guard", "go_no_go", "matrix", "narrative", "capability_statement"]
    assert events[5]["tool"] == "get_firm_profile" and events[5]["args"] == {"firm_id": 1}
    assert events[7]["payload"]["verdict"] == "bid" and events[9]["row"]["requirement"] == GOOD_ROWS[0]["requirement"]
    done = events[-1]
    assert done["verdict"] == "bid" and done["readiness_pct"] == 50 and done["est_cost_usd"] > 0
    assert done["export_url"].endswith("kind=bidpacket") and done["export_url"].startswith("/agent/export/")
    run_id = events[0]["run_id"]
    assert {r["run_id"] for r in rows("SELECT run_id FROM llm_calls")} == {run_id}
    assert rows("SELECT status FROM runs WHERE run_id = ?", (run_id,)) == [{"status": "complete"}]


def test_no_bid_stops_after_go_no_go_and_emits_reasons(monkeypatch):
    seed_firms()
    monkeypatch.setattr(go_no_go, "assess_past_performance", fake_pp(0))
    llm, sdk = _llm(_final("g"))
    events = post_run(llm, sol_id=_solicitation())
    assert names(events)[-3:] == ["step_started", "decision", "run_complete"]
    assert "matrix" not in [e.get("step") for e in events]
    done = events[-1]
    assert (done["verdict"], done["export_url"], done["readiness_pct"]) == ("no_bid", None, None)
    assert done["reasons"] == events[-2]["payload"]["top_reasons"] and "No verified past performance" in done["reasons"][0]
    assert len(sdk.calls) == 1


def test_override_no_bid_continues_the_chain(monkeypatch):
    seed_firms()
    monkeypatch.setattr(go_no_go, "assess_past_performance", fake_pp(0))
    llm, _ = _llm(_final("g"), *bid_script()[2:])
    events = post_run(llm, sol_id=_solicitation(), override_no_bid=True)
    assert names(events)[-1] == "run_complete" and "capability_statement" in names(events)
    assert events[-1]["verdict"] == "no_bid" and events[-1]["export_url"]


def test_step_failed_ends_the_run_without_run_complete():
    seed_firms()
    llm, _ = _llm(_final("g"), BAD, BAD)
    events = post_run(llm, sol_id=_solicitation())
    assert names(events)[-2:] == ["step_started", "step_failed"]
    failed = events[-1]
    assert failed["step"] == "matrix" and "matrix did not produce a valid answer" in failed["reason"]
    assert "run_complete" not in names(events) and "narrative" not in names(events)
    assert rows("SELECT status FROM runs") == [{"status": "failed"}]


def test_token_guard_refuses_before_any_model_call(monkeypatch):
    seed_firms()
    monkeypatch.setenv("MAX_RUN_INPUT_TOKENS", "100")
    llm, sdk = _llm(_final("g"), tokens=101)
    events = post_run(llm, sol_id=_solicitation())
    assert names(events) == ["run_started", "step_started", "tool_call", "tool_result", "step_failed"]
    assert events[-1]["step"] == "token_guard" and "101 tokens" in events[-1]["reason"]
    assert sdk.calls == []


def test_token_guard_default_limit_is_60000():
    seed_firms()
    llm, sdk = _llm(_final("g"), tokens=60001)
    events = post_run(llm, sol_id=_solicitation())
    assert events[-1]["event"] == "step_failed" and "60000" in events[-1]["reason"] and sdk.calls == []


def test_replay_returns_identical_events_without_model_spend():
    seed_firms()
    llm, sdk = _llm(*bid_script())
    events = post_run(llm, sol_id=_solicitation())
    calls = len(sdk.calls)
    r = TestClient(app).get(f"/runs/{events[0]['run_id']}")
    assert r.status_code == 200 and r.json()["events"] == events
    assert r.json()["status"] == "complete" and r.json()["est_cost_usd"] == events[-1]["est_cost_usd"]
    assert len(sdk.calls) == calls
    assert TestClient(app).get("/runs/nope").status_code == 404


def test_unknown_firm_or_solicitation_is_404_before_streaming():
    seed_firms()
    client = TestClient(app)
    assert client.post("/agent/run", json={"firm_id": 99, "solicitation_id": _solicitation()}).status_code == 404
    assert client.post("/agent/run", json={"firm_id": 1, "solicitation_id": "nope"}).status_code == 404


def _daily(monkeypatch, llm, headers, key="s3cret"):
    if key:
        monkeypatch.setenv("COPILOT_ADMIN_KEY", key)
    app.dependency_overrides[get_llm] = lambda: llm
    try:
        return TestClient(app).post("/agent/run-daily", json={"firm_id": 1}, headers=headers)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("headers,key", [({}, "s3cret"), ({"X-API-Key": "wrong"}, "s3cret"), ({"X-API-Key": ""}, ""), ({"X-API-Key": "x"}, "")])
def test_run_daily_rejects_missing_wrong_or_unconfigured_key(monkeypatch, headers, key):
    seed_firms()
    monkeypatch.setenv("COPILOT_ADMIN_KEY", key)
    monkeypatch.setattr("app.agent.run.fetch_shortlist", lambda firm_id: pytest.fail("shortlist must not be pulled"))
    llm, sdk = _llm()
    assert _daily(monkeypatch, llm, headers, key=None).status_code == 401
    assert sdk.calls == []


def test_run_daily_runs_top_three_by_score(monkeypatch):
    seed_firms()
    monkeypatch.setattr(go_no_go, "assess_past_performance", fake_pp(0))
    items = [{"score": s, "text": SAMPLE_TEXT} for s in (40, 90, 10, 70)]
    seen = {}
    monkeypatch.setattr("app.agent.run.fetch_shortlist", lambda firm_id: seen.update(firm_id=firm_id) or {"matches": items})
    llm, sdk = _llm(_final("a"), _final("b"), _final("c"))
    r = _daily(monkeypatch, llm, {"X-API-Key": "s3cret"})
    assert r.status_code == 200 and seen["firm_id"] == "1"
    runs = r.json()["runs"]
    assert [x["score"] for x in runs] == [90, 70, 40]
    assert all(x["verdict"] == "no_bid" and x["status"] == "complete" for x in runs)
    assert len(rows("SELECT * FROM runs")) == 3 and len(sdk.calls) == 3
    assert len({x["solicitation_id"] for x in runs}) == 3


def test_demo_data_and_endpoint():
    seed_firms()
    seed_firms()  # idempotent
    assert [f["demo"] for f in firms.list_firms()] == [False, True, True]
    assert [e["verified"] for e in firms.get_firm(2)["past_performance"]] == [True, True, True]
    r = TestClient(app).get("/demo/solicitations")
    [sol] = r.json()
    assert sol["id"] == DEMO_SOLICITATION_ID and sol["demo"] is True and "raw_text" not in sol
    assert (sol["naics"], sol["set_aside"], sol["place_of_performance"]) == ("238210", "HUBZone", "Syracuse, NY")
    assert sol["section_c"] and sol["section_l"] and sol["section_m"] and sol["due_date"]
    assert len(rows("SELECT * FROM solicitations WHERE demo = 0")) == 0


def test_demo_firm_2_reaches_bid_or_needs_review(monkeypatch):
    seed_firms()
    monkeypatch.setattr(go_no_go, "assess_past_performance", matrix.assess_past_performance)
    terms = ["electrical maintenance", "preventive maintenance", "panelboard repair", "emergency lighting"]
    llm, _ = _llm(
        _final("g", {**FINAL, "verdict": "bid"}),
        _final("s", {"terms": terms, "size_indicator": ""}),
        _final("m1", {"relevant": True, "matched_terms": terms[1:3], "quote": "preventive maintenance"}),
        _final("m2", {"relevant": True, "matched_terms": [terms[0], terms[2]], "quote": "panelboard repair"}),
        _final("m3", {"relevant": True, "matched_terms": [terms[1], terms[3]], "quote": "emergency lighting"}),
    )
    d = go_no_go.run_go_no_go(2, DEMO_SOLICITATION_ID, llm)["decision"]
    assert d["verdict"] in ("bid", "needs_review") and d["verdict_source"] == "model"


def test_narrative_keeps_the_size_standard_dollar_sign():
    seed_firms()
    firm = firms.get_firm(1)
    good = _text(100, " Small business under the $34.0M size standard.")
    assert narrative.check_narrative(good, firm) == []
    problems = narrative.check_narrative(_text(100, " Small business under the 34.0M size standard."), firm)
    assert any("$34.0M" in p for p in problems)
    assert "Small business under the $34.0M size standard" in narrative.SYSTEM_PROMPT


def test_emit_defaults_leave_plain_functions_silent():
    seed_firms()
    sol_id = _solicitation()
    llm, _ = _llm(_matrix_final("m", GOOD_ROWS))
    got = []
    matrix.run_matrix(1, sol_id, llm, lambda event, **data: got.append(event))
    assert got == ["matrix_row"] * 3
