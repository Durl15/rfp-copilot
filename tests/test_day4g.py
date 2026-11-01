import json

import pytest

from app.agent import go_no_go, narrative
from app.seed import seed_firms
from tests.conftest import claimed, rows
from tests.test_agent import FINAL, _final, _llm, _solicitation
from tests.test_day4b import _text

pytestmark = pytest.mark.usefixtures("real_validation")


def _run(matches, payload=None, firm_id=2):
    """Firm 2 has three verified entries (0-2); firm 1 has none."""
    seed_firms()
    llm, _ = _llm(_final("r1", {**FINAL, "verified_matches": matches, **(payload or {})}))
    return go_no_go.run_go_no_go(firm_id, _solicitation(), llm)


def test_zero_matches_force_no_bid_and_cap_even_if_model_says_bid():
    d = _run([])["decision"]
    assert (d["verdict"], d["score"], d["verdict_source"]) == ("no_bid", 35, "rule")
    assert "No verified past performance" in d["rule_reason"] and d["top_reasons"][0] == d["rule_reason"]
    (saved,) = rows("SELECT verdict, score, verdict_source, rule_reason FROM decisions")
    assert saved == {"verdict": "no_bid", "score": 35, "verdict_source": "rule", "rule_reason": d["rule_reason"]}


@pytest.mark.parametrize("n", [1, 2])
def test_one_or_two_validated_matches_cap_at_needs_review(n):
    d = _run(claimed(n))["decision"]
    assert (d["verdict"], d["score"], d["verdict_source"]) == ("needs_review", 65, "rule")
    assert d["rule_reason"] and len(d["verified_matches"]) == n and d["rejected_matches"] == []


def test_one_match_does_not_raise_a_no_bid():
    d = _run(claimed(1), {"verdict": "no_bid", "score": 20})["decision"]
    assert (d["verdict"], d["score"], d["verdict_source"], d["rule_reason"]) == ("no_bid", 20, "model", "")


def test_three_validated_matches_leave_model_verdict_and_are_stored():
    out = _run(claimed(3))
    d = out["decision"]
    assert (d["verdict"], d["score"], d["verdict_source"], d["rule_reason"]) == ("bid", 82, "model", "")
    (saved,) = rows("SELECT verdict_source, rule_reason, verified_matches, rejected_matches FROM decisions")
    assert saved["verdict_source"] == "model" and saved["rule_reason"] == ""
    assert json.loads(saved["verified_matches"]) == claimed(3) and json.loads(saved["rejected_matches"]) == []


def test_claimed_match_on_unverified_entry_is_rejected_and_not_counted():
    d = _run(claimed(3), firm_id=1)["decision"]  # firm 1's seeded entries are all verified=false
    assert d["verified_matches"] == [] and (d["verdict"], d["score"], d["verdict_source"]) == ("no_bid", 35, "rule")
    assert [(m["entry_index"], m["reason"]) for m in d["rejected_matches"]] == [(i, "entry is not verified") for i in range(3)]
    (saved,) = rows("SELECT rejected_matches FROM decisions")
    assert len(json.loads(saved["rejected_matches"])) == 3


def test_out_of_range_and_duplicate_indexes_are_rejected():
    d = _run([*claimed(1), {"entry_index": 7, "why": "x"}, {"entry_index": -1, "why": "x"}, {"entry_index": 0, "why": "again"}])["decision"]
    assert [m["entry_index"] for m in d["verified_matches"]] == [0]
    reasons = {m["entry_index"]: m["reason"] for m in d["rejected_matches"]}
    assert "does not exist" in reasons[7] and "does not exist" in reasons[-1] and reasons[0] == "duplicate entry_index"
    assert (d["verdict"], d["score"]) == ("needs_review", 65)


def test_hard_blocker_forces_no_bid_even_with_three_matches():
    d = _run(claimed(3), {"blockers": ["SDVOSB set-aside, firm not certified"]})["decision"]
    assert (d["verdict"], d["verdict_source"]) == ("no_bid", "rule") and "SDVOSB" in d["rule_reason"]


def test_profile_and_search_expose_entry_index():
    from app.agent import tools
    seed_firms()
    assert [e["entry_index"] for e in tools.get_firm_profile(2)["past_performance"]] == [0, 1, 2]
    hits = tools.search_past_performance(2, ["panelboard"])["matches"]
    assert [h["entry_index"] for h in hits] == [0, 1]


def test_schema_requires_verified_matches():
    assert "verified_matches" in go_no_go.GO_NO_GO_SCHEMA["required"]
    assert "entry_index" in go_no_go.SYSTEM_PROMPT


def test_prompt_states_the_floor_rule():
    assert "zero verified matches forces no_bid" in go_no_go.SYSTEM_PROMPT


@pytest.mark.parametrize("phrase", ["the firm profile", "The Profile", "according to"])
def test_narrative_validator_rejects_banned_phrases(phrase):
    seed_firms()
    from app import firms
    problems = narrative.check_narrative(_text(100, f" Work follows {phrase} the plan."), firms.get_firm(1))
    assert any("never write the phrase" in p for p in problems)


def test_narrative_validator_rejects_repeats_and_word_counts():
    seed_firms()
    from app import firms
    firm = firms.get_firm(1)
    assert any("repeated" in p for p in narrative.check_narrative(_text(100, ". We serve Syracuse. We serve syracuse."), firm))
    assert any("words" in p for p in narrative.check_narrative(_text(60), firm))
    assert any("words" in p for p in narrative.check_narrative(_text(200), firm))
    assert narrative.check_narrative(_text(100), firm) == []
