import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import db, limits, storage
from app.agent import run
from app.main import app
from app.routes import get_llm
from app.seed import DEMO_SOLICITATION_ID, seed_firms
from tests.conftest import SAMPLE_TEXT, rows
from tests.test_agent import FINAL, _final, _solicitation
from tests.test_day5 import NO_MATCHES, _daily, _llm, bid_script, names, post_run
from tests.test_matrix import GOOD_ROWS, _matrix_final

NEEDS_REVIEW = {**FINAL, "verdict": "needs_review", "score": 55}


# ---------- 1. needs_review runs the matrix only ----------

def test_needs_review_runs_matrix_only_and_exports_matrix():
    seed_firms()
    llm, sdk = _llm(_final("g", NEEDS_REVIEW), _matrix_final("m", GOOD_ROWS))
    sol_id = _solicitation()
    events = post_run(llm, sol_id=sol_id)
    assert [e["step"] for e in events if e["event"] == "step_started"] == ["token_guard", "go_no_go", "matrix"]
    assert names(events).count("matrix_row") == 3
    assert "narrative" not in names(events) and "capability_statement" not in names(events)
    done = events[-1]
    assert (done["event"], done["verdict"], done["readiness_pct"]) == ("run_complete", "needs_review", 50)
    assert done["export_url"] == f"/agent/export/{sol_id}?firm_id=1&kind=matrix"
    assert len(sdk.calls) == 2
    stored = TestClient(app).get(f"/runs/{events[0]['run_id']}").json()
    assert stored["status"] == "complete" and stored["events"] == events
    r = TestClient(app).get(done["export_url"])
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert TestClient(app).get(done["export_url"].replace("kind=matrix", "kind=bidpacket")).status_code == 409


def test_bid_still_runs_the_full_chain():
    seed_firms()
    llm, _ = _llm(*bid_script())
    events = post_run(llm, sol_id=_solicitation())
    assert {"narrative", "capability_statement"} <= set(names(events)) and events[-1]["export_url"].endswith("kind=bidpacket")


# ---------- 2. run-daily shortlist fallback ----------

def _insert_solicitation(sol_id, created_at, demo=0):
    conn = db.connect()
    conn.execute("INSERT INTO solicitations (id, created_at, source, raw_text, title, demo) VALUES (?, ?, 'text', ?, ?, ?)",
                 (sol_id, created_at, SAMPLE_TEXT, sol_id, demo))
    conn.commit()
    conn.close()


def _fail_shortlist(exc):
    def fetch(firm_id):
        raise exc
    return fetch


@pytest.mark.parametrize("exc,expected", [
    (HTTPException(502, "CivicProcureBot unreachable: ConnectError"), "CivicProcureBot unreachable"),
    (HTTPException(503, "CivicProcureBot is not configured"), "not configured"),
    (ValueError("Expecting value: line 1 column 1"), "ValueError"),
])
def test_run_daily_falls_back_when_shortlist_fails(monkeypatch, exc, expected):
    seed_firms()
    now = datetime.now(timezone.utc)
    _insert_solicitation("recent", (now - timedelta(days=3)).isoformat())
    _insert_solicitation("old", (now - timedelta(days=30)).isoformat())
    monkeypatch.setattr("app.agent.run.fetch_shortlist", _fail_shortlist(exc))
    llm, sdk = _llm(_final("a", NO_MATCHES), _final("b", NO_MATCHES))
    r = _daily(monkeypatch, llm, {"X-Admin-Key": "s3cret"})
    assert r.status_code == 200
    body = r.json()
    assert expected in body["fallback"]
    assert {x["solicitation_id"] for x in body["runs"]} == {DEMO_SOLICITATION_ID, "recent"}
    assert all(x["status"] == "complete" for x in body["runs"]) and len(sdk.calls) == 2
    [fb] = [json.loads(x["events"])[0] for x in rows("SELECT events FROM runs WHERE solicitation_id = 'daily'")]
    assert fb["event"] == "shortlist_fallback" and expected in fb["reason"] and fb["firm_id"] == 1


def test_run_daily_does_not_fall_back_when_shortlist_works(monkeypatch):
    seed_firms()
    monkeypatch.setattr("app.agent.run.fetch_shortlist", lambda firm_id: {"matches": [{"score": 5, "text": SAMPLE_TEXT}]})
    llm, _ = _llm(_final("a", NO_MATCHES))
    body = _daily(monkeypatch, llm, {"X-Admin-Key": "s3cret"}).json()
    assert "fallback" not in body and len(body["runs"]) == 1
    assert rows("SELECT * FROM runs WHERE solicitation_id = 'daily'") == []


# ---------- 4. storage ----------

class FakeBucket:
    def __init__(self):
        self.data = None
        self.uploads = 0
        self.fail_exists = False

    def blob(self):
        bucket = self

        class Blob:
            def exists(self):
                if bucket.fail_exists:
                    raise RuntimeError("storage unavailable")
                return bucket.data is not None

            def download_to_filename(self, path):
                with open(path, "wb") as f:
                    f.write(bucket.data)

            def upload_from_filename(self, path):
                with open(path, "rb") as f:
                    bucket.data = f.read()
                bucket.uploads += 1

        return Blob()


def _cancel_timer():
    with storage._lock:
        if storage._timer is not None:
            storage._timer.cancel()
            storage._timer = None


@pytest.fixture
def bucket(monkeypatch):
    b = FakeBucket()
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    monkeypatch.setattr(storage, "_blob", b.blob)
    monkeypatch.setattr(storage, "DEBOUNCE_SECONDS", 0.05)
    _cancel_timer()
    yield b
    _cancel_timer()


def _snapshot(data: bytes, tmp_path) -> sqlite3.Connection:
    p = tmp_path / "snap.sqlite3"
    p.write_bytes(data)
    return sqlite3.connect(p)


def test_storage_is_a_noop_without_bucket(monkeypatch):
    monkeypatch.setattr(storage, "_blob", lambda: pytest.fail("no bucket configured"))
    assert storage.download_if_present() is False and storage.upload_now() == 0
    storage.schedule_upload()
    storage.flush()
    seed_firms()
    assert storage._timer is None


def test_write_burst_schedules_one_debounced_upload_and_reads_do_not(bucket, tmp_path, caplog):
    caplog.set_level("INFO", logger="app.storage")
    seed_firms()  # several write transactions in a burst
    seed_firms()
    assert bucket.uploads == 0
    time.sleep(0.4)
    assert bucket.uploads == 1
    assert any("uploaded gs://test-bucket/test.sqlite3" in m and " bytes" in m for m in caplog.messages)
    assert len(TestClient(app).get("/firms").json()) == 3
    time.sleep(0.3)
    assert bucket.uploads == 1


def test_upload_is_a_consistent_snapshot_including_wal_commits(bucket, tmp_path):
    seed_firms()
    live = db.connect()  # keeps WAL content from being checkpointed away
    try:
        assert storage.upload_now() == len(bucket.data) > 0
        snap = _snapshot(bucket.data, tmp_path)
        assert snap.execute("SELECT COUNT(*) FROM firms").fetchone()[0] == 3
        assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        snap.close()
    finally:
        live.close()


def test_startup_downloads_before_migrations_and_shutdown_uploads(bucket, tmp_db):
    seed_firms()
    storage.upload_now()
    tmp_db.unlink()
    for ext in ("-wal", "-shm"):
        tmp_db.with_name(tmp_db.name + ext).unlink(missing_ok=True)
    bucket.uploads = 0
    with TestClient(app) as c:
        assert [f["id"] for f in c.get("/firms").json()] == [1, 2, 3]
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(db.MIGRATIONS)
        conn.close()
    assert bucket.uploads >= 1  # flushed on shutdown


def test_startup_restores_exactly_what_the_bucket_holds(bucket, tmp_db):
    seed_firms()
    conn = db.connect()
    conn.execute("UPDATE firms SET name = 'RESTORED' WHERE id = 3")
    conn.commit()
    conn.close()
    storage.upload_now()
    conn = db.connect()
    conn.execute("UPDATE firms SET name = 'LOCAL ONLY' WHERE id = 3")
    conn.commit()
    conn.close()
    with TestClient(app) as c:
        assert next(f for f in c.get("/firms").json() if f["id"] == 3)["name"] == "RESTORED"


def test_startup_fails_loudly_when_bucket_is_unreachable(bucket):
    bucket.fail_exists = True
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


def test_startup_with_empty_bucket_starts_fresh(bucket):
    with TestClient(app) as c:
        assert len(c.get("/firms").json()) == 3


# ---------- 5. idempotent seeding ----------

def test_restart_seeding_never_duplicates():
    for _ in range(3):
        with TestClient(app):
            pass
    assert [r["id"] for r in rows("SELECT id FROM firms ORDER BY id")] == [1, 2, 3]
    assert [r["id"] for r in rows("SELECT id FROM solicitations")] == [DEMO_SOLICITATION_ID]


def test_restart_keeps_confirmed_entries_on_firm_one():
    seed_firms()
    entry = {"client": "Confirmed client (test)", "scope": "x", "verified": True}
    conn = db.connect()
    conn.execute("UPDATE firms SET past_performance = ? WHERE id = 1", (json.dumps([entry]),))
    conn.commit()
    conn.close()
    seed_firms()
    assert json.loads(rows("SELECT past_performance FROM firms WHERE id = 1")[0]["past_performance"]) == [entry]


# ---------- 6. abuse limits ----------

BODY = {"firm_id": 99, "solicitation_id": "x"}  # unknown firm: the request is counted, then 404s before any model call


def _agent_post(c, path="/agent/go-no-go", ip="1.1.1.1", headers=None):
    return c.post(path, json=BODY, headers={"X-Forwarded-For": ip, **(headers or {})})


def test_agent_runs_are_limited_to_six_per_ip_per_hour_across_endpoints():
    c = TestClient(app)
    for p in ["/agent/run", "/agent/go-no-go", "/agent/matrix"] * 2:
        assert _agent_post(c, p).status_code == 404
    r = _agent_post(c)
    assert r.status_code == 429 and "6 agent runs per hour" in r.json()["detail"]
    assert _agent_post(c, ip="2.2.2.2").status_code == 404  # another visitor is unaffected


def test_client_ip_is_the_last_forwarded_entry():
    c = TestClient(app)
    for i in range(6):
        assert _agent_post(c, ip=f"10.0.0.{i}, 3.3.3.3").status_code == 404  # spoofed leading entries change nothing
    assert _agent_post(c, ip="10.9.9.9, 3.3.3.3").status_code == 429


def test_admin_key_bypasses_per_ip_limits_and_a_wrong_key_does_not(monkeypatch):
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = TestClient(app)
    for _ in range(8):
        assert _agent_post(c, headers={"X-Admin-Key": "s3cret"}).status_code == 404
    for _ in range(6):
        assert _agent_post(c, headers={"X-Admin-Key": "wrong"}).status_code == 404
    assert _agent_post(c, headers={"X-Admin-Key": "wrong"}).status_code == 429


def test_global_daily_cap_applies_across_ips(monkeypatch):
    monkeypatch.setenv("MAX_DAILY_RUNS", "3")
    c = TestClient(app)
    for i in range(3):
        assert _agent_post(c, ip=f"9.9.9.{i}").status_code == 404
    r = _agent_post(c, ip="9.9.9.200")
    assert r.status_code == 429 and "daily limit" in r.json()["detail"]


def test_global_daily_cap_applies_to_admin_too(monkeypatch):
    monkeypatch.setenv("MAX_DAILY_RUNS", "1")
    monkeypatch.setenv("COPILOT_ADMIN_KEY", "s3cret")
    c = TestClient(app)
    assert _agent_post(c, headers={"X-Admin-Key": "s3cret"}).status_code == 404
    assert _agent_post(c, headers={"X-Admin-Key": "s3cret"}).status_code == 429


def test_max_daily_runs_defaults_to_150():
    from app.config import get_settings
    assert get_settings().max_daily_runs == 150


def _voice_post(c, data=b"x", ip="4.4.4.4", firm_id=99):
    return c.post("/voice/past-performance", files={"audio": ("a.wav", data, "audio/wav")}, data={"firm_id": str(firm_id)},
                  headers={"X-Forwarded-For": ip})


def test_voice_uploads_are_limited_to_ten_per_ip_per_hour():
    c = TestClient(app)
    for _ in range(10):
        assert _voice_post(c).status_code == 404
    r = _voice_post(c)
    assert r.status_code == 429 and "10 voice uploads per hour" in r.json()["detail"]
    assert _voice_post(c, ip="5.5.5.5").status_code == 404


def test_voice_limit_is_separate_from_agent_limit():
    c = TestClient(app)
    for _ in range(6):
        _agent_post(c, ip="4.4.4.4")
    assert _agent_post(c, ip="4.4.4.4").status_code == 429
    assert _voice_post(c).status_code == 404


def test_voice_upload_over_25_mb_is_413():
    seed_firms()
    c = TestClient(app)
    r = _voice_post(c, data=b"\0" * (25 * 1024 * 1024 + 1), firm_id=1)
    assert r.status_code == 413 and "25 MB" in r.json()["detail"]
    declared = c.post("/voice/past-performance", content=b"", headers={"Content-Length": str(limits.MAX_VOICE_BODY_BYTES + 1), "X-Forwarded-For": "6.6.6.6"})
    assert declared.status_code == 413


# ---------- 7. SSE through the proxy ----------

def test_run_stream_sends_proxy_safe_headers_and_keepalive_comments(monkeypatch):
    seed_firms()
    monkeypatch.setattr(run, "KEEPALIVE_SECONDS", 0.05)

    def slow(em, firm_id, sol_id, override, llm):
        time.sleep(0.3)
        em("run_started", run_id=em.run_id, firm_id=firm_id, solicitation_id=sol_id, override_no_bid=override)
        em("run_complete", export_url=None, readiness_pct=None, verdict="no_bid", est_cost_usd=0.0, reasons=[])

    monkeypatch.setattr(run, "execute_run", slow)
    app.dependency_overrides[get_llm] = lambda: _llm()[0]
    try:
        r = TestClient(app).post("/agent/run", json={"firm_id": 1, "solicitation_id": _solicitation()})
    finally:
        app.dependency_overrides.clear()
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert r.text.count(": keepalive\n\n") >= 2
    assert r.text.index(": keepalive") < r.text.index("event: run_started")


def test_keepalive_interval_is_fifteen_seconds():
    assert run.KEEPALIVE_SECONDS == 15
