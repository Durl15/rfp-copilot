"""Seed firm profiles. Idempotent: existing ids are left untouched.

Past-performance entries describe only what the work does and who it is for, and are verified=false. Real
dates, values, outcomes and references belong in data/private_profile.json (gitignored), which replaces firm 1's
entries at startup. Firms 2 and 3 are fictional.
"""
import json
from pathlib import Path

from app import db

PRIVATE_PROFILE = Path(__file__).resolve().parent.parent / "data" / "private_profile.json"

FIRMS: list[dict] = [
    {
        "id": 1,
        "name": "DJ AI Business Consultant LLC",
        "hubzone": True,
        "certifications": ["HUBZone"],
        "naics_codes": ["541511", "541512", "541611", "518210"],
        "size_standard": "Small business under the $34.0M size standard",
        "place_of_performance": "Syracuse, NY",
        "past_performance": [
            {"client": "Central New York small-business and nonprofit clients", "scope": "IT consulting: systems selection, workflow automation, and AI adoption advisory", "verified": False},
            {"client": "DJ AI internal product: Commander Briefs", "scope": "Civic AI briefing tool for civic and procurement staff: reads agency documents and produces structured decision briefs with an LLM", "verified": False},
            {"client": "DJ AI internal product: grant intelligence", "scope": "Grant intelligence tool for organizations seeking funding: finds, scores, and drafts responses to funding opportunities", "verified": False},
        ],
    },
    {
        "id": 2,
        "name": "Salt City Electric & Controls Inc. (fictional)",
        "hubzone": True,
        "certifications": ["HUBZone", "SDVOSB"],
        "naics_codes": ["238210"],
        "size_standard": "Small business under the $19.0M size standard",
        "place_of_performance": "Syracuse, NY",
        "past_performance": [
            {"client": "Onondaga County Facilities (fictional)", "scope": "Electrical upgrade of county office buildings: panels, lighting, and emergency power", "verified": False},
            {"client": "VA Medical Center Syracuse subcontract (fictional)", "scope": "Electrical and low-voltage installation for clinic renovation", "verified": False},
        ],
    },
    {
        "id": 3,
        "name": "Erie Canal Engineering Associates PLLC (fictional)",
        "hubzone": True,
        "certifications": ["HUBZone", "WOSB"],
        "naics_codes": ["541330"],
        "size_standard": "Small business under the $25.5M size standard",
        "place_of_performance": "Syracuse, NY",
        "past_performance": [
            {"client": "NYS Thruway Authority (fictional)", "scope": "Structural inspection and rehabilitation design for canal-corridor bridges", "verified": False},
            {"client": "City of Oswego (fictional)", "scope": "Stormwater and civil engineering design for waterfront redevelopment", "verified": False},
        ],
    },
]


def load_private_entries() -> list[dict] | None:
    """Past-performance entries from data/private_profile.json, or None when the file is absent."""
    if not PRIVATE_PROFILE.exists():
        return None
    entries = json.loads(PRIVATE_PROFILE.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ValueError(f"{PRIVATE_PROFILE.name} must be a JSON list of past-performance objects")
    return [{**e, "verified": e.get("verified") is True} for e in entries]


def seed_firms() -> int:
    """Insert missing firms, then apply the private override to firm 1. Returns the number of rows added."""
    added = 0
    with db.session() as conn:
        for f in FIRMS:
            cur = conn.execute(
                "INSERT OR IGNORE INTO firms (id, name, hubzone, certifications, naics_codes, size_standard,"
                " place_of_performance, past_performance) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f["id"],
                    f["name"],
                    int(f["hubzone"]),
                    json.dumps(f["certifications"]),
                    json.dumps(f["naics_codes"]),
                    f["size_standard"],
                    f["place_of_performance"],
                    json.dumps(f["past_performance"]),
                ),
            )
            added += cur.rowcount
        if (private := load_private_entries()) is not None:
            conn.execute("UPDATE firms SET past_performance = ? WHERE id = 1", (json.dumps(private),))
    return added
