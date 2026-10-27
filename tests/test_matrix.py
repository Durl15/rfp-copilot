import pytest
from fastapi.testclient import TestClient

from app import firms
from app.agent import matrix
from app.main import app
from app.routes import get_llm
from app.seed import seed_firms
from tests.conftest import rows
from tests.test_agent import _final, _llm, _response, _solicitation, _tool_turn

GOOD_ROWS = [
    {
        "requirement": "Contractor shall provide software modernization services",
        "section_ref": "C",
        "requirement_type": "capability",
        "firm_capability": 'NAICS "541512" is in the firm codes; scope "IT consulting: systems selection, workflow automation, and AI adoption advisory"',
        "gap": "none",
        "evidence_needed": "",
        "draft_response": "DJ AI delivers software modernization under NAICS 541512.",
    },
    {
        "requirement": "Submit a technical volume and a price volume",
        "section_ref": "L",
        "requirement_type": "submission",
        "firm_capability": matrix.NO_EVIDENCE,
        "gap": "none",
        "evidence_needed": "",
        "draft_response": "Prepare separate technical and price volumes.",
    },
    {
        "requirement": "Past performance evaluated on federal DoD contracts",
        "section_ref": "M",
        "requirement_type": "capability",
        "firm_capability": matrix.NO_EVIDENCE,
        "gap": "full",
        "evidence_needed": "Three DoD contract references with CPARS ratings",
        "draft_response": "Gap: no federal DoD past performance is documented. Option: teaming partner with DoD contract history.",
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

    assert out["matrix"]["summary"] == {
        "counts": {"none": 2, "partial": 0, "full": 1},
        "counts_by_type": {"capability": 2, "eligibility": 0, "submission": 1},
        "overall_readiness_pct": 50,
    }
    assert [r["section_ref"] for r in out["matrix"]["rows"]] == ["C", "L", "M"]
    assert [t["tool"] for t in out["trace"]] == ["search_past_performance"]
    assert all(c["reasoning"] == {"effort": "medium"} and c["max_output_tokens"] == 24000 for c in sdk.calls)
    assert {t["name"] for t in sdk.calls[0]["tools"]} == {"get_solicitation", "search_past_performance", "lookup_naics"}
    saved = rows("SELECT solicitation_id, firm_id, gap FROM matrix_rows ORDER BY position")
    assert [(r["solicitation_id"], r["firm_id"], r["gap"]) for r in saved] == [
        (sol_id, 1, "none"),
        (sol_id, 1, "none"),
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
    assert out["matrix"]["summary"]["counts"]["none"] == 2


def test_gap_none_without_quoted_fact_is_rejected_and_second_failure_raises():
    seed_firms()
    sol_id = _solicitation()
    unquoted = {**GOOD_ROWS[0], "firm_capability": matrix.NO_EVIDENCE}
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


def _row(**over):
    return matrix.MatrixRow.model_validate({**GOOD_ROWS[0], **over})


def _problems(*rows):
    seed_firms()
    return matrix.check_grounding(list(rows), firms.get_firm(1))


def test_submission_rows_must_have_gap_none():
    bad = _row(requirement_type="submission", firm_capability=matrix.NO_EVIDENCE, gap="partial",
               evidence_needed="x", draft_response="Prepare volumes.")
    assert any("submission requirement" in p for p in _problems(bad))
    assert _problems(matrix.MatrixRow.model_validate(GOOD_ROWS[1])) == []


def test_full_gap_draft_starting_we_will_is_rejected_and_retried():
    seed_firms()
    sol_id = _solicitation()
    bad = {**GOOD_ROWS[2], "draft_response": "We will deliver this capability."}
    llm, sdk = _llm(_matrix_final("r1", [bad]), _matrix_final("r2", GOOD_ROWS))
    matrix.run_matrix(1, sol_id, llm)
    assert len(sdk.calls) == 2 and "must begin with 'Gap:'" in sdk.calls[1]["input"][0]["content"]
    assert _problems(_row(**{**GOOD_ROWS[2], "draft_response": "Gap: nothing documented."})) == []
    assert _problems(_row(**{**GOOD_ROWS[2], "draft_response": "Nothing documented."}))


def test_unquoted_claim_in_firm_capability_is_rejected():
    claim = 'Holds "HUBZone" status. Extensive DoD experience.'
    assert any("no verbatim quote" in p and "Extensive DoD experience." in p for p in _problems(_row(firm_capability=claim)))
    assert _problems(_row(firm_capability='Holds "HUBZone" status. Operates from "Syracuse, NY".')) == []


def test_readiness_ignores_submission_rows():
    rows = [matrix.MatrixRow.model_validate(r) for r in GOOD_ROWS]
    only_submission = [rows[1]]
    assert matrix.summarize(only_submission).overall_readiness_pct == 0
    assert matrix.summarize(rows).overall_readiness_pct == 50
    assert matrix.summarize(rows).counts_by_type["submission"] == 1


def test_matrix_retries_once_on_max_output_tokens():
    seed_firms()
    sol_id = _solicitation()
    llm, sdk = _llm(
        _response("r1", status="incomplete", incomplete_reason="max_output_tokens"),
        _matrix_final("r2", GOOD_ROWS),
    )
    out = matrix.run_matrix(1, sol_id, llm)

    assert len(out["matrix"]["rows"]) == 3
    assert [(c["reasoning"]["effort"], c["max_output_tokens"]) for c in sdk.calls] == [("medium", 24000), ("low", 48000)]
