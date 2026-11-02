import json
import time

from fastapi.testclient import TestClient

from app import db, firms
from app.main import app
from app.seed import DEMO_SOLICITATION_ID, seed_firms
from tests.conftest import rows
from tests.test_day5 import bid_script, _llm, _solicitation
from app.routes import get_llm


def _client():
    seed_firms()
    return TestClient(app)


def test_console_page_and_assets_served():
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "RFP Copilot" in r.text and "DJ AI Business Consultant LLC" in r.text
        for path, kind in (("/static/console.js", "javascript"), ("/static/console.css", "css")):
            a = c.get(path)
            assert a.status_code == 200 and kind in a.headers["content-type"]


def test_health_reports_model_and_api_base():
    body = TestClient(app).get("/health").json()
    assert body["ok"] is True and body["model"] == "muse-spark-1.3" and body["api_base"].startswith("http")


def test_solicitations_lists_demo_and_ingested_newest_first_with_goals():
    seed_firms()
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
    conn = db.connect()
    conn.execute(
        "INSERT INTO solicitations (id, created_at, source, raw_text, title, naics, set_aside, participation_goals, due_date, demo)"
        " VALUES ('newest', ?, 'text', 'x', 'Newest RFP', '541512', 'HUBZone', '30% MWBE', '2026-12-19', 0)", (now,))
    conn.commit()
    conn.close()
    got = TestClient(app).get("/solicitations").json()
    assert got[0]["id"] == "newest" and got[0]["participation_goals"] == "30% MWBE" and got[0]["demo"] is False
    demo = next(s for s in got if s["id"] == DEMO_SOLICITATION_ID)
    assert demo["demo"] is True
    assert set(demo) == {"id", "title", "naics", "set_aside", "participation_goals", "due_date", "demo", "created_at"}
    stamps = [s["created_at"] for s in got]
    assert stamps == sorted(stamps, reverse=True)


def test_admin_remove_rejects_missing_or_wrong_key(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = _client()
    before = len(firms.get_firm(2)["past_performance"])
    for headers in ({}, {"X-Admin-Key": "wrong"}, {"X-Admin-Key": ""}):
        assert c.delete("/firms/2/past-performance/0", headers=headers).status_code == 401
    assert len(firms.get_firm(2)["past_performance"]) == before


def test_admin_remove_rejects_everything_when_key_unconfigured(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "")
    assert _client().delete("/firms/2/past-performance/0", headers={"X-Admin-Key": ""}).status_code == 401


def test_admin_remove_deletes_entry_and_emits_event(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = _client()
    before = firms.get_firm(2)["past_performance"]
    r = c.delete("/firms/2/past-performance/1", headers={"X-Admin-Key": "s3cret"})
    assert r.status_code == 200
    after = firms.get_firm(2)["past_performance"]
    assert after == [before[0], before[2]] and r.json()["past_performance"] == after
    stored = rows("SELECT events FROM runs WHERE solicitation_id = 'admin'")
    ev = json.loads(stored[0]["events"])[0]
    assert ev["event"] == "past_performance_removed" and ev["entry_index"] == 1 and ev["entry"]["client"] == before[1]["client"]


def test_admin_remove_unknown_firm_or_index_is_404(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = _client()
    h = {"X-Admin-Key": "s3cret"}
    assert c.delete("/firms/99/past-performance/0", headers=h).status_code == 404
    assert c.delete("/firms/2/past-performance/9", headers=h).status_code == 404
    assert c.delete("/firms/2/past-performance/-1", headers=h).status_code == 404


def test_sse_headers_and_run_usage():
    seed_firms()
    llm, _ = _llm(*bid_script())
    app.dependency_overrides[get_llm] = lambda: llm
    try:
        r = TestClient(app).post("/agent/run", json={"firm_id": 1, "solicitation_id": _solicitation()})
    finally:
        app.dependency_overrides.clear()
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert r.headers["content-type"].startswith("text/event-stream")
    run_id = json.loads(r.text.split("data: ", 1)[1].split("\n", 1)[0])["run_id"]
    usage = TestClient(app).get(f"/runs/{run_id}").json()["usage"]
    assert set(usage) == {"tokens_in", "tokens_out", "cached_tokens", "model_latency_ms"}


# ---------- Day 8: solicitation cleanup ----------

def _sol(sol_id, title, source="text", created="2026-01-01T00:00:00+00:00"):
    conn = db.connect()
    conn.execute("INSERT INTO solicitations (id, created_at, source, raw_text, title, demo) VALUES (?, ?, ?, 'x', ?, 0)",
                 (sol_id, created, source, title))
    conn.execute("INSERT INTO decisions (created_at, solicitation_id, firm_id, verdict, score) VALUES (?, ?, 1, 'no_bid', 10)", (created, sol_id))
    conn.execute("INSERT INTO matrix_rows (created_at, solicitation_id, firm_id, position, requirement, section_ref, firm_capability,"
                 " gap, evidence_needed, draft_response) VALUES (?, ?, 1, 0, 'r', 'C', 'c', 'none', 'e', 'd')", (created, sol_id))
    conn.execute("INSERT INTO artifacts (solicitation_id, firm_id, kind, created_at, content) VALUES (?, 1, 'narrative', ?, 'n')", (sol_id, created))
    conn.execute("INSERT INTO runs (run_id, created_at, firm_id, solicitation_id, status) VALUES (?, ?, 1, ?, 'done')", ("run-" + sol_id, created, sol_id))
    conn.commit()
    conn.close()


def _count(table, sol_id):
    return rows(f"SELECT COUNT(*) AS n FROM {table} WHERE solicitation_id = ?", (sol_id,))[0]["n"]


KEY = {"X-Admin-Key": "s3cret"}


def test_delete_solicitation_requires_admin_key(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = _client()
    _sol("a", "A")
    for headers in ({}, {"X-Admin-Key": "wrong"}):
        assert c.delete("/solicitations/a", headers=headers).status_code == 401
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "")
    assert c.delete("/solicitations/a", headers=KEY).status_code == 401
    assert _count("decisions", "a") == 1


def test_delete_solicitation_cascades_and_returns_counts(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = _client()
    _sol("a", "A")
    _sol("b", "B")
    r = c.delete("/solicitations/a", headers=KEY)
    assert r.status_code == 200
    assert r.json() == {"id": "a", "deleted": {"solicitations": 1, "decisions": 1, "matrix_rows": 1, "artifacts": 1, "runs": 1}}
    assert all(_count(t, "a") == 0 for t in ("decisions", "matrix_rows", "artifacts", "runs"))
    assert rows("SELECT id FROM solicitations WHERE id = 'a'") == []
    assert all(_count(t, "b") == 1 for t in ("decisions", "matrix_rows", "artifacts", "runs"))
    assert c.delete("/solicitations/a", headers=KEY).status_code == 404


def test_dedupe_requires_admin_key(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = _client()
    _sol("a", "")
    for headers in ({}, {"X-Admin-Key": "wrong"}):
        assert c.post("/solicitations/dedupe", headers=headers).status_code == 401
    assert _count("decisions", "a") == 1


def test_dedupe_dry_run_reports_plan_without_deleting(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = _client()
    _sol("old", "Same", created="2026-01-01T00:00:00+00:00")
    _sol("new", "Same", created="2026-02-01T00:00:00+00:00")
    _sol("blank", "  ")
    r = c.post("/solicitations/dedupe?dry_run=true", headers=KEY).json()
    assert r["dry_run"] is True and r["deleted"] is None
    assert {p["id"]: p["reason"] for p in r["removed"]} == {"old": "older duplicate of the same title and source", "blank": "empty title"}
    assert len(rows("SELECT id FROM solicitations WHERE demo = 0")) == 3 and _count("runs", "old") == 1


def test_dedupe_removes_empty_and_older_duplicates_with_cascade(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = _client()
    _sol("old", "Same", created="2026-01-01T00:00:00+00:00")
    _sol("new", "Same", created="2026-02-01T00:00:00+00:00")
    _sol("other-source", "Same", source="pdf", created="2026-01-01T00:00:00+00:00")
    _sol("blank", "")
    conn = db.connect()
    conn.execute("UPDATE solicitations SET title = NULL WHERE id = 'blank'")
    conn.commit()
    conn.close()
    r = c.post("/solicitations/dedupe", headers=KEY).json()
    assert sorted(p["id"] for p in r["removed"]) == ["blank", "old"]
    assert r["deleted"] == {"solicitations": 2, "decisions": 2, "matrix_rows": 2, "artifacts": 2, "runs": 2}
    left = {x["id"] for x in rows("SELECT id FROM solicitations WHERE demo = 0")}
    assert left == {"new", "other-source"}
    assert _count("decisions", "old") == 0 and _count("decisions", "new") == 1
