"""Streaming agent run: go/no-go, matrix, narrative, capability statement as Server-Sent Events, plus daily run and replay.

The step functions are the same ones the plain HTTP endpoints call; a run only passes them an emit callback.
"""
import hmac
import json
import logging
import queue
import threading
from typing import Iterator

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import claims, db, firms, ingest
from app.agent import capability, go_no_go, matrix, narrative
from app.agent.events import Emitter
from app.config import get_settings
from app.llm.meta_client import MetaClient
from app.routes import get_llm
from app.shortlist import fetch_shortlist

log = logging.getLogger(__name__)
router = APIRouter()

DAILY_TOP_N = 3
REASON_CHARS = 500
TERMINAL_STATUS = {"run_complete": "complete", "step_failed": "failed"}


class _Stop(Exception):
    """A step emitted step_failed; the run ends."""


def run_cost(run_id: str) -> float:
    s = get_settings()
    with db.session() as conn:
        r = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0) AS i, COALESCE(SUM(completion_tokens), 0) AS o FROM llm_calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return round((r["i"] * s.price_in_per_mtok + r["o"] * s.price_out_per_mtok) / 1_000_000, 6)


def _persist(em: Emitter, firm_id: int, solicitation_id: str) -> None:
    last = em.events[-1]
    status = TERMINAL_STATUS.get(last["event"], "running")
    cost = run_cost(em.run_id) if status != "running" else 0.0
    with db.session() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, created_at, firm_id, solicitation_id, status, est_cost_usd, events)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, est_cost_usd = excluded.est_cost_usd,"
            " events = excluded.events",
            (em.run_id, em.events[0]["timestamp"], firm_id, solicitation_id, status, cost, json.dumps(em.events)),
        )
    if status != "running":
        log.info("run %s %s firm=%s solicitation=%s est_cost_usd=%s", em.run_id, status, firm_id, solicitation_id, cost)


def new_emitter(firm_id: int, solicitation_id: str, stream_sink=None) -> Emitter:
    """An Emitter that stores the event log after every event, so replay works even if the client disconnects."""

    def sink(ev: dict) -> None:
        _persist(em, firm_id, solicitation_id)
        if stream_sink:
            stream_sink(ev)

    em = Emitter(sink)
    return em


def _step(em: Emitter, name: str, fn):
    em("step_started", step=name)
    try:
        return fn()
    except Exception as exc:  # stream boundary: any failure becomes step_failed, never a broken stream
        em("step_failed", step=name, reason=f"{type(exc).__name__}: {exc}"[:REASON_CHARS])
        raise _Stop from exc


def _guard_tokens(em: Emitter, llm: MetaClient, firm: dict, sol: dict) -> None:
    """Count the matrix call's first input (the largest of the chain) before any model call is made."""
    limit = get_settings().max_run_input_tokens
    em("tool_call", tool="count_input_tokens", args={"limit": limit})
    n = llm.count_input_tokens(
        [{"role": "user", "content": matrix._user_message(sol)}],
        system=matrix._system_prompt(claims.redact(firm)),
        tools=matrix.MODEL_TOOLS,
    )
    em("tool_result", tool="count_input_tokens", summary=f"{n} input tokens (limit {limit})")
    if n > limit:
        raise RuntimeError(f"input is {n} tokens, above MAX_RUN_INPUT_TOKENS={limit}; no model call was made")


def execute_run(em: Emitter, firm_id: int, solicitation_id: str, override_no_bid: bool, llm: MetaClient) -> dict:
    """Run the chain, emitting events. Returns {run_id, status, verdict, readiness_pct, est_cost_usd, export_url}."""
    llm.run_id = em.run_id
    firm, sol = firms.get_firm(firm_id), ingest.get_solicitation(solicitation_id)
    out = {"run_id": em.run_id, "status": "failed", "verdict": None, "readiness_pct": None, "est_cost_usd": 0.0, "export_url": None}
    em("run_started", run_id=em.run_id, firm_id=firm_id, solicitation_id=solicitation_id, override_no_bid=override_no_bid)
    try:
        _step(em, "token_guard", lambda: _guard_tokens(em, llm, firm, sol))
        decision = _step(em, "go_no_go", lambda: go_no_go.run_go_no_go(firm_id, solicitation_id, llm, em))["decision"]
        out["verdict"] = decision["verdict"]
        if decision["verdict"] == "no_bid" and not override_no_bid:
            out.update(status="complete", est_cost_usd=run_cost(em.run_id))
            em("run_complete", export_url=None, readiness_pct=None, verdict="no_bid", est_cost_usd=out["est_cost_usd"],
               reasons=decision["top_reasons"])
            return out

        def build_matrix():
            if not (sol["section_c"] or sol["section_l"] or sol["section_m"]):
                raise RuntimeError("solicitation has no Section C, L or M text to build a matrix from")
            return matrix.run_matrix(firm_id, solicitation_id, llm, em)

        readiness = _step(em, "matrix", build_matrix)["matrix"]["summary"]["overall_readiness_pct"]
        _step(em, "narrative", lambda: narrative.run_narrative(firm_id, solicitation_id, llm, em))
        _step(em, "capability_statement", lambda: capability.run_capability(firm_id, solicitation_id, llm, em))
        export_url = f"/agent/export/{solicitation_id}?firm_id={firm_id}&kind=bidpacket"
        out.update(status="complete", readiness_pct=readiness, export_url=export_url, est_cost_usd=run_cost(em.run_id))
        em("run_complete", export_url=export_url, readiness_pct=readiness, verdict=decision["verdict"], est_cost_usd=out["est_cost_usd"])
    except _Stop:
        out["est_cost_usd"] = run_cost(em.run_id)
    return out


def stream_run(firm_id: int, solicitation_id: str, override_no_bid: bool, llm: MetaClient) -> Iterator[dict]:
    """Run in a worker thread and yield events as they are emitted. The run finishes and is stored even if the client leaves."""
    q: queue.Queue = queue.Queue()
    em = new_emitter(firm_id, solicitation_id, q.put)

    def work() -> None:
        try:
            execute_run(em, firm_id, solicitation_id, override_no_bid, llm)
        except Exception:
            log.exception("run %s crashed", em.run_id)
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()
    while (ev := q.get()) is not None:
        yield ev


def _sse(events: Iterator[dict]) -> Iterator[str]:
    for ev in events:
        yield f"id: {ev['seq']}\nevent: {ev['event']}\ndata: {json.dumps(ev, default=str)}\n\n"


class RunIn(BaseModel):
    firm_id: int
    solicitation_id: str
    override_no_bid: bool = False


@router.post("/agent/run")
def agent_run(body: RunIn, llm: MetaClient = Depends(get_llm)):
    if not firms.get_firm(body.firm_id):
        raise HTTPException(404, f"firm {body.firm_id} not found")
    if not ingest.get_solicitation(body.solicitation_id):
        raise HTTPException(404, f"solicitation {body.solicitation_id} not found")
    return StreamingResponse(
        _sse(stream_run(body.firm_id, body.solicitation_id, body.override_no_bid, llm)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}")
def read_run(run_id: str):
    with db.session() as conn:
        r = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not r:
        raise HTTPException(404, "run not found")
    out = dict(r)
    out["events"] = json.loads(out["events"])
    return out


def _shortlist_items(data) -> list[dict]:
    """The shortlist as a list of items: a bare list, or the first list value of an object (e.g. 'matches')."""
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return [i for i in data if isinstance(i, dict)]


def _ingest_item(item: dict, llm: MetaClient) -> dict:
    notice_id = item.get("sam_notice_id") or item.get("notice_id")
    if notice_id:
        return ingest.ingest_sam(str(notice_id), llm)
    text = item.get("text") or item.get("description")
    if text:
        return ingest.ingest_text(str(text), llm)
    raise ValueError("shortlist item has no sam_notice_id or text to ingest")


class DailyIn(BaseModel):
    firm_id: int


@router.post("/agent/run-daily")
def run_daily(body: DailyIn, x_api_key: str | None = Header(None), llm: MetaClient = Depends(get_llm)):
    """Pull the shortlist, ingest the top 3 by score, run the chain on each, store results. Never sends email."""
    key = get_settings().copilot_admin_key
    if not key or not x_api_key or not hmac.compare_digest(x_api_key.encode(), key.encode()):
        raise HTTPException(401, "invalid or missing X-API-Key")
    if not firms.get_firm(body.firm_id):
        raise HTTPException(404, f"firm {body.firm_id} not found")
    items = _shortlist_items(fetch_shortlist(str(body.firm_id)))
    items.sort(key=lambda i: i.get("score") or 0, reverse=True)
    results = []
    for item in items[:DAILY_TOP_N]:
        try:
            sol = _ingest_item(item, llm)
        except Exception as exc:
            results.append({"score": item.get("score"), "status": "ingest_failed", "reason": f"{type(exc).__name__}: {exc}"[:REASON_CHARS]})
            continue
        em = new_emitter(body.firm_id, sol["id"])
        results.append({"solicitation_id": sol["id"], "title": sol["title"], "score": item.get("score"),
                        **execute_run(em, body.firm_id, sol["id"], False, llm)})
    return {"firm_id": body.firm_id, "runs": results}
