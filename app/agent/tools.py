"""Agent tools: plain functions, pydantic arg models, Responses-API tool definitions, and a dispatcher."""
import json
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import db, firms, ingest

SECTION_CHARS = 3000
EXCERPT_CHARS = 3000

# Approximate SBA size standards (NAICS table, receipts in $M unless noted). Verify against
# sba.gov/document/support-table-size-standards before relying on a threshold in a bid.
NAICS: dict[str, tuple[str, str]] = {
    "236220": ("Commercial and Institutional Building Construction", "$45.0M"),
    "237310": ("Highway, Street, and Bridge Construction", "$45.0M"),
    "238210": ("Electrical Contractors and Other Wiring Installation Contractors", "$19.0M"),
    "238220": ("Plumbing, Heating, and Air-Conditioning Contractors", "$19.0M"),
    "238910": ("Site Preparation Contractors", "$19.0M"),
    "518210": ("Computing Infrastructure Providers, Data Processing, Web Hosting", "$40.0M"),
    "541310": ("Architectural Services", "$12.5M"),
    "541330": ("Engineering Services", "$25.5M"),
    "541340": ("Drafting Services", "$9.5M"),
    "541370": ("Surveying and Mapping (except Geophysical) Services", "$19.0M"),
    "541380": ("Testing Laboratories and Services", "$19.0M"),
    "541511": ("Custom Computer Programming Services", "$34.0M"),
    "541512": ("Computer Systems Design Services", "$34.0M"),
    "541513": ("Computer Facilities Management Services", "$34.0M"),
    "541519": ("Other Computer Related Services", "$34.0M"),
    "541611": ("Administrative Management and General Management Consulting Services", "$24.5M"),
    "541612": ("Human Resources Consulting Services", "$17.0M"),
    "541613": ("Marketing Consulting Services", "$19.5M"),
    "541614": ("Process, Physical Distribution, and Logistics Consulting Services", "$24.5M"),
    "541618": ("Other Management Consulting Services", "$24.5M"),
    "541690": ("Other Scientific and Technical Consulting Services", "$19.5M"),
    "541715": ("R&D in the Physical, Engineering, and Life Sciences", "1,000 employees"),
    "541820": ("Public Relations Agencies", "$19.5M"),
    "541990": ("All Other Professional, Scientific, and Technical Services", "$19.5M"),
    "561110": ("Office Administrative Services", "$12.5M"),
    "561210": ("Facilities Support Services", "$47.0M"),
    "561320": ("Temporary Help Services", "$34.0M"),
    "561720": ("Janitorial Services", "$22.0M"),
    "611430": ("Professional and Management Development Training", "$15.0M"),
    "811210": ("Electronic and Precision Equipment Repair and Maintenance", "$12.5M"),
}

BASE_HOURS = 40
GAP_HOURS = {
    "past_performance": 16,
    "certifications": 8,
    "technical_requirements": 12,
    "staffing": 10,
    "pricing": 6,
}
DEFAULT_GAP_HOURS = 8

# Generic solicitation words that match almost any past-performance entry; never valid search keywords.
STOPWORDS = frozenset(
    "shall will must that this with from have been their they them than then which where when "
    "provide provided providing contractor contract offeror government agency county services service "
    "work include including required requirements requirement under such each other into also "
    "support perform performance using used personnel period section task tasks system systems management".split()
)


class ToolError(ValueError):
    """Unknown tool or invalid arguments. The message is safe to return to the model."""


# ------------------------------------------------------------------ functions


def get_firm_profile(firm_id: int) -> dict:
    return firms.get_firm(firm_id) or {"error": f"firm {firm_id} not found"}


def get_solicitation(solicitation_id: str) -> dict:
    sol = ingest.get_solicitation(solicitation_id)
    if not sol:
        return {"error": f"solicitation {solicitation_id} not found"}
    return {
        "id": sol["id"],
        "title": sol["title"],
        "section_c": (sol["section_c"] or "")[:SECTION_CHARS] or None,
        "section_l": (sol["section_l"] or "")[:SECTION_CHARS] or None,
        "section_m": (sol["section_m"] or "")[:SECTION_CHARS] or None,
        "due_date": sol["due_date"],
        "naics": sol["naics"],
        "set_aside": sol["set_aside"],
        "place_of_performance": sol["place_of_performance"],
        "text_excerpt": sol["raw_text"][:EXCERPT_CHARS],
    }


def search_past_performance(firm_id: int, keywords: list[str]) -> dict:
    firm = firms.get_firm(firm_id)
    if not firm:
        return {"error": f"firm {firm_id} not found"}
    kws = [k for k in (k.lower().strip() for k in keywords) if k and k not in STOPWORDS]
    matches = []
    for entry in firm["past_performance"]:
        haystack = " ".join(str(v) for v in entry.values()).lower()
        hit = [k for k in kws if k in haystack]
        if hit:
            matches.append({**entry, "matched_keywords": hit})
    return {"firm_id": firm_id, "total_entries": len(firm["past_performance"]), "matches": matches}


def lookup_naics(code: str) -> dict:
    hit = NAICS.get(code)
    if not hit:
        return {"error": f"NAICS {code} is not in the bundled table of {len(NAICS)} common codes"}
    return {"code": code, "title": hit[0], "size_standard": hit[1]}


def estimate_effort(gap_counts: dict[str, int]) -> dict:
    hours = BASE_HOURS + sum(GAP_HOURS.get(k, DEFAULT_GAP_HOURS) * n for k, n in gap_counts.items())
    return {"hours": hours, "base_hours": BASE_HOURS}


def record_decision(solicitation_id: str, firm_id: int, verdict: str, score: int, reasons: list[str]) -> dict:
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO decisions (created_at, solicitation_id, firm_id, verdict, score, reasons)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), solicitation_id, firm_id, verdict, score, json.dumps(reasons)),
        )
    return {"decision_id": cur.lastrowid}


# ----------------------------------------------------------------- arg models


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetFirmProfileArgs(_Args):
    firm_id: int = Field(description="Firm id")


class GetSolicitationArgs(_Args):
    solicitation_id: str = Field(description="Solicitation id returned by ingestion")


class SearchPastPerformanceArgs(_Args):
    firm_id: int
    keywords: list[str] = Field(min_length=1, description="Terms to match against past-performance entries")


class LookupNaicsArgs(_Args):
    code: str = Field(pattern=r"^\d{6}$", description="6-digit NAICS code")


class EstimateEffortArgs(_Args):
    gap_counts: dict[str, int] = Field(
        description=(
            "Count of gaps by type, e.g. past_performance, certifications, technical_requirements, staffing, "
            "pricing. Other keys are allowed."
        )
    )


class RecordDecisionArgs(_Args):
    solicitation_id: str
    firm_id: int
    verdict: Literal["bid", "no_bid", "needs_review"]
    score: int = Field(ge=0, le=100)
    reasons: list[str]


TOOLS: dict[str, tuple[type[_Args], Callable[..., Any], str]] = {
    "get_firm_profile": (
        GetFirmProfileArgs,
        get_firm_profile,
        "Get a firm's profile: certifications, HUBZone status, NAICS codes, size standard, location, past performance.",
    ),
    "get_solicitation": (
        GetSolicitationArgs,
        get_solicitation,
        "Get a solicitation's tagged sections (C, L, M), due date, NAICS, set-aside, place of performance, and a text excerpt.",
    ),
    "search_past_performance": (
        SearchPastPerformanceArgs,
        search_past_performance,
        "Find a firm's past-performance entries that match any of the given keywords.",
    ),
    "lookup_naics": (
        LookupNaicsArgs,
        lookup_naics,
        "Look up the title and SBA size standard for a 6-digit NAICS code.",
    ),
    "estimate_effort": (
        EstimateEffortArgs,
        estimate_effort,
        "Estimate proposal effort in hours from counts of gaps between the firm and the requirements.",
    ),
    "record_decision": (
        RecordDecisionArgs,
        record_decision,
        "Persist a Go/No-Go decision for a firm and solicitation.",
    ),
}


def _definition(name: str) -> dict:
    model, _, description = TOOLS[name]
    schema = model.model_json_schema()
    schema.pop("title", None)
    return {"type": "function", "name": name, "description": description, "parameters": schema}


TOOL_DEFINITIONS: list[dict] = [_definition(n) for n in TOOLS]


def execute_tool(name: str, args: dict | str) -> Any:
    if name not in TOOLS:
        raise ToolError(f"unknown tool {name!r}")
    model, fn, _ = TOOLS[name]
    try:
        parsed = model.model_validate_json(args) if isinstance(args, str) else model.model_validate(args)
    except ValidationError as exc:
        raise ToolError(f"invalid arguments for {name}: {exc.errors(include_url=False, include_input=False)}") from exc
    return fn(**parsed.model_dump())
