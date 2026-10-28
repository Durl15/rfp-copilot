"""Demo data endpoints. Demo firms (2, 3) and demo solicitations are fictional."""
from fastapi import APIRouter

from app import db

router = APIRouter()


@router.get("/demo/solicitations")
def demo_solicitations():
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM solicitations WHERE demo = 1 ORDER BY created_at, id").fetchall()
    return [{k: v for k, v in dict(r).items() if k not in ("raw_text", "demo")} | {"demo": True} for r in rows]
