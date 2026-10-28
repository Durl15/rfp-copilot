import json

from fastapi import APIRouter, HTTPException

from app import db

router = APIRouter()

_JSON_COLS = ("certifications", "naics_codes", "past_performance")


def _decode(row) -> dict:
    out = dict(row)
    out["hubzone"] = bool(out["hubzone"])
    out["demo"] = bool(out["demo"])
    for c in _JSON_COLS:
        out[c] = json.loads(out[c])
    for entry in out["past_performance"]:
        entry["verified"] = entry.get("verified") is True
    return out


def list_firms() -> list[dict]:
    with db.session() as conn:
        return [_decode(r) for r in conn.execute("SELECT * FROM firms ORDER BY id")]


def get_firm(firm_id: int) -> dict | None:
    with db.session() as conn:
        r = conn.execute("SELECT * FROM firms WHERE id = ?", (firm_id,)).fetchone()
    return _decode(r) if r else None


@router.get("/firms")
def read_firms():
    return list_firms()


@router.get("/firms/{firm_id}")
def read_firm(firm_id: int):
    firm = get_firm(firm_id)
    if not firm:
        raise HTTPException(404, "not found")
    return firm
