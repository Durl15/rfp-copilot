"""Agent console: the static page, and the admin route that resets a demo firm's past performance between takes."""
import hmac
import json
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse

from app import db, firms
from app.agent.events import Emitter
from app.agent.run import _persist
from app.config import get_settings

STATIC_DIR = Path(__file__).resolve().parent / "static"
router = APIRouter()


@router.get("/", include_in_schema=False)
def console():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.delete("/firms/{firm_id}/past-performance/{entry_index}")
def remove_past_performance(firm_id: int, entry_index: int, x_admin_key: str | None = Header(None)):
    key = get_settings().copilot_admin_key
    if not key or not x_admin_key or not hmac.compare_digest(x_admin_key.encode(), key.encode()):
        raise HTTPException(401, "invalid or missing X-Admin-Key")
    with db.session() as conn:  # one transaction: read, check, write
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT past_performance FROM firms WHERE id = ?", (firm_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"firm {firm_id} not found")
        entries = json.loads(row["past_performance"])
        if not 0 <= entry_index < len(entries):
            raise HTTPException(404, f"entry {entry_index} not found (firm has {len(entries)} entries)")
        removed = entries.pop(entry_index)
        conn.execute("UPDATE firms SET past_performance = ? WHERE id = ?", (json.dumps(entries), firm_id))
    em = Emitter(lambda ev: _persist(em, firm_id, "admin"))
    em("past_performance_removed", firm_id=firm_id, entry_index=entry_index, entry=removed)
    return firms.get_firm(firm_id)
