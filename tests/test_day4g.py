import pytest

from app.agent import go_no_go, narrative
from app.seed import seed_firms
from tests.conftest import fake_pp, rows
from tests.test_agent import FINAL, _final, _llm, _solicitation
from tests.test_day4b import _text


def _run(monkeypatch, verified, payload=None):
    seed_firms()
    monkeypatch.setattr(go_no_go, "assess_past_performance", fake_pp(verified))
    llm, _ = _llm(_final("r1", {**FINAL, **(payload or {})}))
    sol_id = _solicitation()
    return go_no_go.run_go_no_go(1, sol_id, llm)


def test_zero_matches_force_no_bid_and_cap_even_if_model_says_bid(monkeypatch):
    out = _run(monkeypatch, 0)
    d = out["decision"]
    assert (d["verdict"], d["score"], d["verdict_source"]) == ("no_bid", 35, "rule")
    assert "No verified past performance" in d["rule_reason"] and d["top_reasons"][0] == d["rule_reason"]
    (saved,) = rows("SELECT verdict, score, verdict_source, rule_reason FROM decisions")
    assert saved == {"verdict": "no_bid", "score": 35, "verdict_source": "rule", "rule_reason": d["rule_reason"]}


def test_two_matches_cap_at_needs_review(monkeypatch):
    d = _run(monkeypatch, 2)["decision"]
    assert (d["verdict"], d["score"], d["verdict_source"]) == ("needs_review", 65, "rule")
    assert d["rule_reason"]


def test_one_match_does_not_raise_a_no_bid(monkeypatch):
    d = _run(monkeypatch, 1, {"verdict": "no_bid", "score": 20})["decision"]
    assert (d["verdict"], d["score"], d["verdict_source"], d["rule_reason"]) == ("no_bid", 20, "model", "")


def test_three_matches_leave_model_verdict(monkeypatch):
    d = _run(monkeypatch, 3)["decision"]
    assert (d["verdict"], d["score"], d["verdict_source"], d["rule_reason"]) == ("bid", 82, "model", "")
    (saved,) = rows("SELECT verdict_source, rule_reason FROM decisions")
    assert saved == {"verdict_source": "model", "rule_reason": ""}


def test_hard_blocker_forces_no_bid_even_with_three_matches(monkeypatch):
    d = _run(monkeypatch, 3, {"blockers": ["SDVOSB set-aside, firm not certified"]})["decision"]
    assert (d["verdict"], d["verdict_source"]) == ("no_bid", "rule") and "SDVOSB" in d["rule_reason"]


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
