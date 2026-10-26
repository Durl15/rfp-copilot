"""Seed firm profiles. Idempotent: existing ids are left untouched.

Firm 1 is the real DJ AI entry; its past-performance rows are descriptive only and need verified
client names, values and references before use in a live proposal. Firms 2 and 3 are fictional.
"""
import json

from app import db

FIRMS: list[dict] = [
    {
        "id": 1,
        "name": "DJ AI Business Consultant LLC",
        "hubzone": True,
        "certifications": ["HUBZone"],
        "naics_codes": ["541511", "541512", "541611", "518210"],
        "size_standard": "Small (under $34.0M average annual receipts)",
        "place_of_performance": "Syracuse, NY",
        "past_performance": [
            {
                "client": "Central New York small-business and nonprofit clients",
                "scope": "IT consulting: systems selection, workflow automation, and AI adoption advisory",
                "value_range": "$10K-$75K",
                "dates": "2023-present",
                "role": "Prime",
                "outcome": "Delivered on scope; clients retained for follow-on work",
                "reference": "Available on request",
            },
            {
                "client": "DJ AI internal product (Commander Briefs)",
                "scope": "Civic AI briefing system: ingests agency documents and produces structured decision briefs with an LLM",
                "value_range": "Self-funded",
                "dates": "2025-present",
                "role": "Prime developer and operator",
                "outcome": "Production SaaS in use for civic and procurement briefings",
                "reference": "Live system demo available",
            },
            {
                "client": "DJ AI internal product (grant intelligence)",
                "scope": "Grant intelligence system: discovers, scores, and drafts responses to funding opportunities",
                "value_range": "Self-funded",
                "dates": "2025-present",
                "role": "Prime developer and operator",
                "outcome": "Production system ranking opportunities against organization profiles",
                "reference": "Live system demo available",
            },
        ],
    },
    {
        "id": 2,
        "name": "Salt City Electric & Controls Inc. (fictional)",
        "hubzone": True,
        "certifications": ["HUBZone", "SDVOSB"],
        "naics_codes": ["238210"],
        "size_standard": "Small (under $19.0M average annual receipts)",
        "place_of_performance": "Syracuse, NY",
        "past_performance": [
            {
                "client": "Onondaga County Facilities (fictional)",
                "scope": "Electrical upgrade of three county office buildings: panels, lighting, and emergency power",
                "value_range": "$1M-$3M",
                "dates": "2022-2023",
                "role": "Prime",
                "outcome": "Completed on schedule, zero safety incidents",
                "reference": "J. Marrow, Facilities Director (fictional)",
            },
            {
                "client": "VA Medical Center Syracuse subcontract (fictional)",
                "scope": "Electrical and low-voltage installation for clinic renovation",
                "value_range": "$500K-$1M",
                "dates": "2023-2024",
                "role": "Subcontractor",
                "outcome": "Passed all inspections; closed out under budget",
                "reference": "R. Alden, General Contractor PM (fictional)",
            },
        ],
    },
    {
        "id": 3,
        "name": "Erie Canal Engineering Associates PLLC (fictional)",
        "hubzone": True,
        "certifications": ["HUBZone", "WOSB"],
        "naics_codes": ["541330"],
        "size_standard": "Small (under $25.5M average annual receipts)",
        "place_of_performance": "Syracuse, NY",
        "past_performance": [
            {
                "client": "NYS Thruway Authority (fictional)",
                "scope": "Structural inspection and rehabilitation design for four canal-corridor bridges",
                "value_range": "$750K-$1.5M",
                "dates": "2021-2023",
                "role": "Prime",
                "outcome": "Designs accepted without major revision; construction bids came in under estimate",
                "reference": "D. Pruitt, Project Manager (fictional)",
            },
            {
                "client": "City of Oswego (fictional)",
                "scope": "Stormwater and civil engineering design for waterfront redevelopment",
                "value_range": "$250K-$750K",
                "dates": "2023-2024",
                "role": "Prime",
                "outcome": "Permits obtained on first submission",
                "reference": "L. Okafor, City Engineer (fictional)",
            },
        ],
    },
]


def seed_firms() -> int:
    """Insert missing firms. Returns the number of rows added."""
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
    return added
