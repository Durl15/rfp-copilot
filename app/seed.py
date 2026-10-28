"""Seed firm profiles. Idempotent: existing ids are left untouched.

Past-performance entries describe only what the work does and who it is for, and are verified=false. Real
dates, values, outcomes and references belong in data/private_profile.json (gitignored), which replaces firm 1's
entries at startup. Firms 2 and 3 are fictional demo firms (demo=true); firm 2 carries verified entries and the demo
solicitation below is written to match them. Both are fictional.
"""
import json
import uuid
from datetime import date, timedelta
from pathlib import Path

from app import db, ingest

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
        "demo": True,
        "name": "Salt City Electric & Controls Inc. (fictional)",
        "hubzone": True,
        "certifications": ["HUBZone", "SDVOSB"],
        "naics_codes": ["238210"],
        "size_standard": "Small business under the $19.0M size standard",
        "place_of_performance": "Syracuse, NY",
        "past_performance": [
            {
                "client": "Onondaga County Facilities (fictional)",
                "scope": "County facilities electrical maintenance: preventive maintenance, panelboard repair, and emergency lighting testing across 14 county office buildings",
                "period": "2021-2024", "contract_value": "$2.4M", "reference": "J. Alvarez, Facilities Director (fictional)", "verified": True,
            },
            {
                "client": "Madison County Public Works (fictional)",
                "scope": "Municipal building electrical maintenance: generator and transfer switch servicing, panelboard repair, and 24-hour emergency electrical call-out",
                "period": "2022-2025", "contract_value": "$1.1M", "reference": "R. Chen, Public Works Superintendent (fictional)", "verified": True,
            },
            {
                "client": "Syracuse City School District (fictional)",
                "scope": "School facilities electrical maintenance: preventive maintenance, lighting retrofit, and emergency lighting testing across 22 school buildings",
                "period": "2020-2023", "contract_value": "$1.8M", "reference": "T. Okafor, Facilities Manager (fictional)", "verified": True,
            },
        ],
    },
    {
        "id": 3,
        "demo": True,
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


DEMO_SOLICITATION_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "rfp-copilot/demo/onondaga-electrical-maintenance"))
DEMO_DUE_DAYS = 45
DEMO_TEXT = """Title: County Facilities Electrical Maintenance Services (fictional demo solicitation)
NAICS Code: 238210
This acquisition is a HUBZone set-aside.
Place of Performance: Syracuse, NY
SECTION C - DESCRIPTION
The contractor shall provide county facilities electrical maintenance for county office buildings, including preventive
maintenance of panelboards, emergency lighting testing, generator and transfer switch servicing, and 24-hour emergency
electrical call-out. The county operates 12 buildings.
SECTION L - INSTRUCTIONS TO OFFERORS
Submit a technical volume of no more than 10 pages and a price volume. Provide three past-performance references for
county facilities electrical maintenance of similar size and scope.
SECTION M - EVALUATION FACTORS
Technical approach, past performance, price. Past performance is weighted equally with technical approach.
"""


def seed_demo_solicitation() -> None:
    """Insert the fictional demo solicitation if missing; its due date is refreshed on every startup so it never goes stale."""
    due = (date.today() + timedelta(days=DEMO_DUE_DAYS)).isoformat()
    tags = ingest.regex_tag(DEMO_TEXT)
    tags["due_date"] = due
    row = {
        "id": DEMO_SOLICITATION_ID, "created_at": date.today().isoformat(), "source": "text", "raw_text": DEMO_TEXT,
        "demo": 1, **tags,
    }
    with db.session() as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO solicitations ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        conn.execute("UPDATE solicitations SET due_date = ? WHERE id = ?", (due, DEMO_SOLICITATION_ID))


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
                " place_of_performance, past_performance, demo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f["id"],
                    f["name"],
                    int(f["hubzone"]),
                    json.dumps(f["certifications"]),
                    json.dumps(f["naics_codes"]),
                    f["size_standard"],
                    f["place_of_performance"],
                    json.dumps(f["past_performance"]),
                    int(f.get("demo", False)),
                ),
            )
            added += cur.rowcount
            if f.get("demo"):  # fictional demo firms are owned by this file, so existing rows are brought up to date
                conn.execute(
                    "UPDATE firms SET demo = 1, past_performance = ? WHERE id = ?",
                    (json.dumps(f["past_performance"]), f["id"]),
                )
        if (private := load_private_entries()) is not None:
            conn.execute("UPDATE firms SET past_performance = ? WHERE id = 1", (json.dumps(private),))
    seed_demo_solicitation()
    return added
