import io
import json
import re
from pathlib import Path

import pytest
from docx import Document
from docx.oxml.ns import qn
from fastapi.testclient import TestClient

from app import claims, export, firms, seed
from app.agent import capability, go_no_go, matrix, narrative, tools
from app.main import app
from app.routes import get_llm
from app.seed import seed_firms
from tests.test_agent import _final, _llm, _message, _response, _solicitation
from tests.test_day4b import HELP_DESK_ENTRIES, STATEMENT, _assess, _match, _scope, _set_pp, _setup_matrix, _text
from tests.test_matrix import GOOD_ROWS

ROOT = Path(__file__).resolve().parent.parent
UNVERIFIED = [{**e, "verified": False} for e in HELP_DESK_ENTRIES]


# 1. verified flag and conservative seed
def test_every_seed_entry_is_unverified_and_free_of_claims():
    for f in seed.FIRMS:
        for e in f["past_performance"]:
            assert e["verified"] is False
            assert set(e) == {"client", "scope", "verified"}
            assert not claims.CLAIM_RE.search(" ".join((e["client"], e["scope"])))
            assert not re.search(r"\d", e["scope"])
    internal = [e["client"] for e in seed.FIRMS[0]["past_performance"] if "internal product" in e["client"]]
    assert len(internal) == 2


def test_missing_verified_flag_defaults_to_false():
    seed_firms()
    _set_pp([{"client": "A", "scope": "B"}, {"client": "C", "scope": "D", "verified": "true"}])
    assert [e["verified"] for e in firms.get_firm(1)["past_performance"]] == [False, False]


# 2. narrative and capability claim guard
@pytest.mark.parametrize("claim", [
    " The team retained clients for follow-on work.",
    " A live demo is available on request.",
    " The platform is in production use.",
    " References are available on request.",
    " Work began in 2023.",
    " Contracts were valued at $50K.",
])
def test_narrative_rejects_claim_words_when_no_entry_is_verified(claim):
    sol_id = _setup_matrix()
    llm, sdk = _llm(_response("n1", _message(_text(100, claim))), _response("n2", _message(_text(100))))
    narrative.run_narrative(1, sol_id, llm)
    assert len(sdk.calls) == 2 and "unverified" in sdk.calls[1]["input"][0]["content"]


def test_narrative_returns_502_after_one_retry():
    sol_id = _setup_matrix()
    bad = _response("n", _message(_text(100, " References are available.")))
    llm, sdk = _llm(bad, bad)
    app.dependency_overrides[get_llm] = lambda: llm
    try:
        r = TestClient(app).post("/agent/narrative", json={"firm_id": 1, "solicitation_id": sol_id})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 502 and len(sdk.calls) == 2


def test_narrative_prompt_hides_unverified_details():
    sol_id = _setup_matrix()
    _set_pp([{"client": "Utica", "scope": "Help desk", "dates": "2019", "reference": "Pat Q", "verified": False}])
    llm, sdk = _llm(_response("n", _message(_text(100))))
    narrative.run_narrative(1, sol_id, llm)
    prompt = sdk.calls[0]["input"][0]["content"]
    assert "Help desk" in prompt and "2019" not in prompt and "Pat Q" not in prompt


def test_verified_entry_may_carry_claims_but_unverified_client_may_not():
    seed_firms()
    _set_pp([
        {"client": "Utica Schools", "scope": "Help desk", "dates": "2021-2023", "verified": True},
        {"client": "Rome Library", "scope": "Web hosting", "dates": "2020", "verified": False},
    ])
    firm = firms.get_firm(1)
    assert claims.check_text("Utica Schools received help desk service from 2021-2023.", firm) == []
    assert claims.check_text("Rome Library received web hosting since 2020.", firm)
    assert claims.check_text("Rome Library receives web hosting.", firm) == []


def test_capability_statement_rejects_claims_then_succeeds():
    seed_firms()
    bad = {**STATEMENT, "past_performance_summaries": ["Commander Briefs is a production system with a live demo."]}
    llm, sdk = _llm(_final("c1", bad), _final("c2", STATEMENT))
    capability.run_capability(1, _solicitation(), llm)
    assert len(sdk.calls) == 2 and "unverified" in sdk.calls[1]["input"][0]["content"]
    llm, _ = _llm(_final("c1", bad), _final("c2", bad))
    with pytest.raises(RuntimeError, match="did not pass validation"):
        capability.run_capability(1, _solicitation(), llm)


# 3. private override
def test_private_profile_replaces_firm_1_and_keeps_verified_flags(tmp_path, monkeypatch):
    path = tmp_path / "private_profile.json"
    entries = [
        {"client": "Real Client", "scope": "Real scope", "dates": "2022", "verified": True},
        {"client": "Other", "scope": "Other scope", "verified": False},
    ]
    path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(seed, "PRIVATE_PROFILE", path)
    seed_firms()
    assert firms.get_firm(1)["past_performance"] == entries
    assert len(firms.get_firm(2)["past_performance"]) == 2


def test_no_private_profile_leaves_the_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "PRIVATE_PROFILE", tmp_path / "missing.json")
    seed_firms()
    assert [e["client"] for e in firms.get_firm(1)["past_performance"]] == [e["client"] for e in seed.FIRMS[0]["past_performance"]]


def test_example_file_has_the_documented_fields_and_data_is_ignored():
    [entry] = json.loads((ROOT / "data" / "private_profile.example.json").read_text(encoding="utf-8"))
    assert set(entry) == {"client", "scope", "value_range", "dates", "role", "outcome", "reference", "verified"}
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8-sig").splitlines()
    assert "data/*" in ignore and "!data/private_profile.example.json" in ignore


# 4. unverified entries are relevance only
def test_only_unverified_matches_give_partial_with_proof_to_supply():
    seed_firms()
    _set_pp(UNVERIFIED)
    out = _assess(_llm(_scope(), *[_match(quote=e["scope"]) for e in UNVERIFIED])[0])
    assert out.gap == "partial"
    assert "proof" in out.evidence_needed and "not yet verified" in out.draft_response


def test_two_verified_plus_one_unverified_is_not_none():
    seed_firms()
    _set_pp([HELP_DESK_ENTRIES[0], HELP_DESK_ENTRIES[1], UNVERIFIED[2]])
    out = _assess(_llm(_scope(), *[_match(quote=e["scope"]) for e in HELP_DESK_ENTRIES])[0])
    assert out.gap == "partial"


def test_unverified_size_does_not_make_a_size_comparable():
    seed_firms()
    big = [{**e, "scope": e["scope"] + " serving 1,500 users"} for e in HELP_DESK_ENTRIES]
    _set_pp([HELP_DESK_ENTRIES[0], HELP_DESK_ENTRIES[1], big[2]])
    _set_pp([{**big[0], "verified": True}, {**big[1], "verified": True}, {**big[2], "verified": True}])
    ms = [_match(quote=e["scope"]) for e in big]
    assert _assess(_llm(_scope("1,200 users"), *ms)[0]).gap == "none"
    _set_pp([{**e, "verified": False} for e in big])
    assert _assess(_llm(_scope("1,200 users"), *ms)[0]).gap == "partial"


def test_capability_row_none_from_unverified_quote_is_rejected():
    seed_firms()
    scope = firms.get_firm(1)["past_performance"][0]["scope"]
    row = matrix.MatrixRow.model_validate({**GOOD_ROWS[0], "firm_capability": f'"{scope}".', "gap": "none", "evidence_needed": ""})
    assert any("unverified past performance" in p for p in matrix.check_grounding([row], firms.get_firm(1)))
    ok = matrix.MatrixRow.model_validate({**GOOD_ROWS[0], "firm_capability": f'"{scope}".', "gap": "partial", "evidence_needed": "x"})
    assert not any("unverified" in p for p in matrix.check_grounding([ok], firms.get_firm(1)))


def test_tools_expose_unverified_entries_without_claim_fields():
    seed_firms()
    _set_pp([{"client": "Utica", "scope": "Help desk", "dates": "2019", "outcome": "retained", "verified": False}])
    [hit] = tools.search_past_performance(1, ["help"])["matches"]
    assert "dates" not in hit and "outcome" not in hit and hit["verified"] is False
    assert "dates" not in tools.get_firm_profile(1)["past_performance"][0]


def test_go_no_go_prompt_treats_unverified_as_relevance_only():
    assert "verified false show relevance only" in go_no_go.SYSTEM_PROMPT


# 5. DOCX widths and size standard
def test_docx_table_is_fixed_layout_with_grid_and_cell_widths():
    sol_id = _setup_matrix()
    doc = Document(io.BytesIO(export.build_docx(sol_id, 1, "matrix")))
    t = doc.tables[0]
    assert t._tbl.tblPr.find(qn("w:tblLayout")).get(qn("w:type")) == "fixed"
    sec = doc.sections[0]
    total = sec.page_width - sec.left_margin - sec.right_margin
    expected = [int(total * w) for w in (0.30, 0.12, 0.08, 0.50)]
    grid = [int(g.get(qn("w:w"))) for g in t._tbl.tblGrid.findall(qn("w:gridCol"))]
    assert grid == [round(w / 635) for w in expected]
    for row in t.rows:
        assert [round(c.width / 635) for c in row.cells] == grid
    assert int(t._tbl.tblPr.find(qn("w:tblW")).get(qn("w:w"))) == pytest.approx(sum(grid), abs=2)


def test_size_standard_sentence():
    assert seed.FIRMS[0]["size_standard"] == "Small business under the $34.0M size standard"


def test_claim_words_do_not_flag_ordinary_words_that_start_like_months():
    assert not claims.CLAIM_RE.search("Structured decision briefs for market and separate novel needs.")
    assert claims.CLAIM_RE.search("Started in March.")
