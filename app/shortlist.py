import httpx
from fastapi import APIRouter, HTTPException

from app.config import get_settings

router = APIRouter()


@router.get("/shortlist/today")
def shortlist_today(firm_id: str):
    """Proxy to CivicProcureBot's read-only shortlist endpoint."""
    s = get_settings()
    if not (s.civicprocurebot_api_base and s.civicprocurebot_api_key):
        raise HTTPException(503, "CivicProcureBot is not configured")
    try:
        r = httpx.get(
            f"{s.civicprocurebot_api_base}/shortlist/today",
            params={"firm_id": firm_id},
            headers={"X-API-Key": s.civicprocurebot_api_key},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"CivicProcureBot unreachable: {type(exc).__name__}")
    if r.status_code >= 400:
        # Pass through caller errors; mask upstream auth/server faults as 502.
        code = r.status_code if r.status_code in (400, 404, 422) else 502
        raise HTTPException(code, f"CivicProcureBot returned {r.status_code}")
    return r.json()
