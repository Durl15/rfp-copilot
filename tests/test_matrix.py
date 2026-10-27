import pytest
from fastapi.testclient import TestClient

from app.agent import matrix
from app.main import app
from app.routes import get_llm
from app.seed import seed_firms
from tests.conftest import rows
from tests.test_agent import _final, _llm, _solicitation, _tool_turn

GOOD_ROWS = [
    {
        "requirement": "Contractor shall provide software modernization services",
        "section_ref": "C",
        "firm_capability": 'NAICS "541512" is in the firm codes; scope "IT consulting: systems selection, workflow automation, and AI adoption advisory"',
        "gap": "none",
        "evidence_needed": "",
        "draft_response": "DJ AI delivers software modernization under NAICS 541512.",
    },
    {
        "requirement": "Submit a technical volume and a price volume",
        "section_ref": "L",
        "firm_capability": "Firm has not documented a prior two-volume federal submission",
        "gap": "partial",
        "evidence_needed": "A prior federal proposal or template showing technical and price volumes",
        "draft_response": "We will submit separate technical and price volumes.",
    },
    {
        "requirement": "Past performance evaluated on federal DoD contracts",
        "section_ref": "M",
        "firm_capability": "",
        "gap": "full",
        "evidence_needed": "Three DoD contract references with CPARS ratings",
        "draft_response": "We will provide references on request.",
    },
]
BAD_ROW = {
    **GOOD_ROWS[0],
    "firm_capability": 'Holds a "TS/SCI facility clearance" and "ISO 27001" certification',
}


def _matrix_final(rid, matrix_rows):
    return _final(rid, {"rows": matrix_rows})


def test_matrix_parse_gap_counts_and_persistence():
    seed_firms()
    sol_id = _solicitation()
    llm, sdk = _llm(
        _tool_turn("r1", ("c1", "search_past_performance", {"firm_id": 1, "keywords": ["modernization"]})),
        _matrix_final("r2", GOOD_ROWS),
    )
    out = matrix.run_matrix(1, sol_id, llm)

    assert out["matrix"]["summary"] == {"counts": {"none": 1, "partial": 1, "full": 1}, "overall_readiness_pct": 50}
    assert [r["section_ref"] for r in out["matrix"]["rows"]] == ["C", "L", "M"]
    assert [t["tool"] for t in out["trace"]] == ["search_past_performance"]
    assert all(c["reasoning"] == {"effort": "high"} and c["max_output_tokens"] == 8000 for c in sdk.calls)
    assert {t["name"] for t in sdk.calls[0]["tools"]} == {"get_solicitation", "search_past_performance", "lookup_naics"}
    saved = rows("SELECT solicitation_id, firm_id, gap FROM matrix_rows ORDER BY position")
    assert [(r["solicitation_id"], r["firm_id"], r["gap"]) for r in saved] == [
        (sol_id, 1, "none"),
        (sol_id, 1, "partial"),
        (sol_id, 1, "full"),
    ]
    assert matrix.load_matrix(sol_id, 1) == out["matrix"]


def test_firm_profile_is_in_stable_prefix_and_identical_across_solicitations():
    seed_firms()
    a, b = _solicitation(), _solicitation()
    llm, sdk = _llm(_matrix_final("r1", GOOD_ROWS), _matrix_final("r2", GOOD_ROWS))
    matrix.run_matrix(1, a, llm)
    matrix.run_matrix(1, b, llm)
    first, second = (c["instructions"] for c in sdk.calls)
    assert first == second and "DJ AI Business Consultant LLC" in first and "GROUNDING RULES" in first
    assert a in sdk.calls[0]["input"][0]["content"] and a not in first


def test_rerun_replaces_rows():
    seed_firms()
    sol_id = _solicitation()
    llm, _ = _llm(_matrix_final("r1", GOOD_ROWS), _matrix_final("r2", GOOD_ROWS[:2]))
    matrix.run_matrix(1, sol_id, llm)
    matrix.run_matrix(1, sol_id, llm)
    assert len(rows("SELECT id FROM matrix_rows")) == 2


def test_invented_capability_is_rejected_and_retried_once():
    seed_firms()
    sol_id = _solicitation()
    llm, sdk = _llm(_matrix_final("r1", [BAD_ROW, *GOOD_ROWS[1:]]), _matrix_final("r2", GOOD_ROWS))
    out = matrix.run_matrix(1, sol_id, llm)

    assert len(sdk.calls) == 2 and sdk.calls[1]["previous_response_id"] == "r1"
    feedback = sdk.calls[1]["input"][0]["content"]
    assert "rejected" in feedback and "TS/SCI facility clearance" in feedback
    assert out["matrix"]["summary"]["counts"]["none"] == 1


def test_gap_none_without_quoted_fact_is_rejected_and_second_failure_raises():
    seed_firms()
    sol_id = _solicitation()
    unquoted = {**GOOD_ROWS[0], "firm_capability": "Extensive experience"}
    llm, sdk = _llm(_matrix_final("r1", [unquoted]), _matrix_final("r2", [unquoted]))
    with pytest.raises(RuntimeError, match="quotes no documented fact"):
        matrix.run_matrix(1, sol_id, llm)
    assert len(sdk.calls) == 2
    assert rows("SELECT id FROM matrix_rows") == []


def test_matrix_endpoints():
    seed_firms()
    sol_id = _solicitation()
    llm, _ = _llm(_matrix_final("r1", GOOD_ROWS))
    app.dependency_overrides[get_llm] = lambda: llm
    try:
        client = TestClient(app)
        assert client.get(f"/agent/matrix/{sol_id}", params={"firm_id": 1}).status_code == 404
        r = client.post("/agent/matrix", json={"firm_id": 1, "solicitation_id": sol_id})
        assert r.status_code == 200 and r.json()["matrix"]["summary"]["overall_readiness_pct"] == 50
        got = client.get(f"/agent/matrix/{sol_id}", params={"firm_id": 1})
        assert got.status_code == 200 and len(got.json()["rows"]) == 3
        assert client.get(f"/agent/matrix/{sol_id}", params={"firm_id": 2}).status_code == 404
        assert client.post("/agent/matrix", json={"firm_id": 99, "solicitation_id": sol_id}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_cached_tokens_logged():
    seed_firms()
    sol_id = _solicitation()
    resp = _matrix_final("r1", GOOD_ROWS)
    resp.usage.input_tokens_details = type("D", (), {"cached_tokens": 1234})()
    llm, _ = _llm(resp)
    matrix.run_matrix(1, sol_id, llm)
    assert rows("SELECT feature, cached_tokens FROM llm_calls") == [{"feature": "matrix", "cached_tokens": 1234}]
