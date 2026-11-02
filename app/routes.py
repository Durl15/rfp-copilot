import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, model_validator

from app import db, ingest
from app.config import get_settings
from app.llm.meta_client import MetaClient

router = APIRouter()

MAX_PDF_BYTES = 25 * 1024 * 1024


class SolicitationIn(BaseModel):
    text: str | None = None
    sam_notice_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.text and self.text.strip()) == bool(self.sam_notice_id):
            raise ValueError("provide exactly one of: text, sam_notice_id (or upload a PDF as multipart 'file')")
        return self


def get_llm() -> MetaClient:
    return MetaClient()


def _public(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k != "raw_text"}
    out["llm_filled"] = json.loads(out["llm_filled"])
    out["text_chars"] = len(row["raw_text"])
    return out


@router.post("/solicitations", status_code=201)
async def create_solicitation(request: Request):
    llm = get_llm()
    ctype = request.headers.get("content-type", "")
    try:
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(422, "multipart body must include a 'file' field")
            data = await upload.read()
            if not data.startswith(b"%PDF"):
                raise HTTPException(415, "file is not a PDF")
            if len(data) > MAX_PDF_BYTES:
                raise HTTPException(413, "PDF exceeds 25 MB")
            row = await run_in_threadpool(ingest.ingest_pdf, data, upload.filename or "upload.pdf", llm)
        else:
            try:
                body = SolicitationIn.model_validate(await request.json())
            except Exception as exc:
                raise HTTPException(422, str(exc))
            if body.text and body.text.strip():
                row = await run_in_threadpool(ingest.ingest_text, body.text, llm)
            else:
                row = await run_in_threadpool(ingest.ingest_sam, body.sam_notice_id, llm)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _public(row)


@router.get("/solicitations")
def list_solicitations():
    """Every solicitation, demo and ingested, newest first."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, title, naics, set_aside, participation_goals, due_date, demo, created_at"
            " FROM solicitations ORDER BY created_at DESC, id"
        ).fetchall()
    return [{**dict(r), "demo": bool(r["demo"])} for r in rows]


SOL_CHILD_TABLES = ("decisions", "matrix_rows", "artifacts", "runs")


def _require_admin(x_admin_key: str | None) -> None:
    key = get_settings().copilot_admin_key
    if not key or not x_admin_key or not hmac.compare_digest(x_admin_key.encode(), key.encode()):
        raise HTTPException(401, "invalid or missing X-Admin-Key")


def _delete_solicitations(conn, ids: list[str]) -> dict:
    """Delete the solicitations and every dependent row; returns per-table counts."""
    counts = dict.fromkeys(("solicitations", *SOL_CHILD_TABLES), 0)
    for sol_id in ids:
        for table in SOL_CHILD_TABLES:
            counts[table] += conn.execute(f"DELETE FROM {table} WHERE solicitation_id = ?", (sol_id,)).rowcount
        counts["solicitations"] += conn.execute("DELETE FROM solicitations WHERE id = ?", (sol_id,)).rowcount
    return counts


@router.post("/solicitations/dedupe")
def dedupe_solicitations(dry_run: bool = False, x_admin_key: str | None = Header(None)):
    """Remove solicitations with an empty title, and all but the newest of each (title, source) group."""
    _require_admin(x_admin_key)
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        found = conn.execute(
            "SELECT id, title, source, created_at FROM solicitations ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        seen, plan = set(), []
        for r in found:
            title = (r["title"] or "").strip()
            if not title:
                reason = "empty title"
            elif (title, r["source"]) in seen:
                reason = "older duplicate of the same title and source"
            else:
                seen.add((title, r["source"]))
                continue
            plan.append({"id": r["id"], "title": r["title"], "source": r["source"], "created_at": r["created_at"], "reason": reason})
        counts = None if dry_run else _delete_solicitations(conn, [p["id"] for p in plan])
    return {"dry_run": dry_run, "removed": plan, "deleted": counts}


@router.delete("/solicitations/{sol_id}")
def delete_solicitation(sol_id: str, x_admin_key: str | None = Header(None)):
    _require_admin(x_admin_key)
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        counts = _delete_solicitations(conn, [sol_id])
    if not counts["solicitations"]:
        raise HTTPException(404, "not found")
    return {"id": sol_id, "deleted": counts}


@router.get("/solicitations/{sol_id}")
def read_solicitation(sol_id: str):
    row = ingest.get_solicitation(sol_id)
    if not row:
        raise HTTPException(404, "not found")
    return _public(row)


class DemoLabelIn(BaseModel):
    title: str | None = None
    due_date: str | None = None
    set_aside: str | None = None
    participation_goals: str | None = None
    demo: bool


@router.post("/solicitations/{sol_id}/demo-label")
def demo_label(sol_id: str, body: DemoLabelIn, x_admin_key: str | None = Header(None)):
    """Mark a solicitation as a demo and override the given fields. A field sent as null is cleared; an omitted field is untouched."""
    key = get_settings().copilot_admin_key
    if not key or not x_admin_key or not hmac.compare_digest(x_admin_key.encode(), key.encode()):
        raise HTTPException(401, "invalid or missing X-Admin-Key")
    if not ingest.get_solicitation(sol_id):
        raise HTTPException(404, "not found")
    fields = {k: getattr(body, k) for k in body.model_fields_set if k != "demo"}
    if fields.get("title", "") is None:
        raise HTTPException(422, "title cannot be cleared")
    if fields.get("due_date"):
        fields["due_date"] = ingest._norm_date(fields["due_date"])
        if not fields["due_date"]:
            raise HTTPException(422, "due_date must be a date such as 2026-12-19")
    fields["demo"] = int(body.demo)
    with db.session() as conn:
        conn.execute(f"UPDATE solicitations SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), sol_id))
    return _public(ingest.get_solicitation(sol_id))
