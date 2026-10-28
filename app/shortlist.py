import httpx
from fastapi import APIRouter, HTTPException

from app.config import get_settings

router = APIRouter()


def fetch_shortlist(firm_id: str):
    """CivicProcureBot's read-only shortlist for a firm. Raises HTTPException on config or upstream faults."""
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


@router.get("/shortlist/today")
def shortlist_today(firm_id: str):
    """Proxy to CivicProcureBot's read-only shortlist endpoint."""
    return fetch_shortlist(firm_id)
