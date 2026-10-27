"""Compliance Matrix agent: maps Section C/L/M requirements to the firm's documented capability, with gaps and draft responses."""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError

from app import db, firms, ingest
from app.agent import tools
from app.llm.meta_client import MetaClient
from app.routes import get_llm

log = logging.getLogger(__name__)
router = APIRouter()

MAX_TURNS = 8
MAX_TOKENS = 8000
SECTION_CHARS = 15000
SUMMARY_CHARS = 200
READINESS_WEIGHT = {"none": 1.0, "partial": 0.5, "full": 0.0}
# The firm profile is already in the system prompt; the model only needs the solicitation and search tools.
MODEL_TOOLS = [d for d in tools.TOOL_DEFINITIONS if d["name"] in ("get_solicitation", "search_past_performance", "lookup_naics")]

# Stable prefix part 1: the rubric. Nothing in it may vary per solicitation, or the cache prefix breaks.
RUBRIC = (
    "You build compliance matrices for HUBZone small-business federal proposals. "
    "You get the text of Sections C (scope), L (instructions to offerors) and M (evaluation factors) of one solicitation, "
    "and the firm's profile and past performance below. Produce one row per distinct requirement, in section order. "
    "Rows: requirement (concise, one obligation); section_ref (C, L or M); firm_capability; "
    "gap (none = fully met by documented facts, partial = partly met or unproven, full = nothing documented); "
    "evidence_needed (what the firm must provide to close the gap; empty string when gap is none); "
    "draft_response (proposal-ready prose using only documented facts; for a gap, a short response that states the plan "
    "without claiming experience or credentials the firm does not have).\n"
    "GROUNDING RULES - these are checked by code and violations are rejected:\n"
    "1. firm_capability may rely ONLY on facts present in the firm profile or past performance below. "
    "Every fact must be quoted verbatim in double quotes, copied exactly from the profile data (e.g. \"HUBZone\", \"541512\"). "
    "Do not put double quotes around anything else.\n"
    "2. gap \"none\" requires at least one such verbatim quote. If you cannot quote a documented fact, the gap is "
    "\"partial\" or \"full\" and evidence_needed says what to provide.\n"
    "3. Never invent credentials, clearances, certifications, contract numbers, customers, dollar values or past performance. "
    "Use the tools to search past performance or read the solicitation if needed. "
    "When finished, reply with the final JSON matrix only."
)

_QUOTE_RE = re.compile(r'"([^"]+)"')


class MatrixRow(BaseModel):
    requirement: str
    section_ref: Literal["C", "L", "M"]
    firm_capability: str
    gap: Literal["none", "partial", "full"]
    evidence_needed: str
    draft_response: str


class ModelMatrix(BaseModel):
    rows: list[MatrixRow]


class Summary(BaseModel):
    counts: dict[str, int]
    overall_readiness_pct: int


class ComplianceMatrix(BaseModel):
    rows: list[MatrixRow]
    summary: Summary


# Hand-written for strict structured output. The summary is computed from the rows, so the model is not asked for it.
MATRIX_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string"},
                    "section_ref": {"type": "string", "enum": ["C", "L", "M"]},
                    "firm_capability": {"type": "string", "description": "Documented facts, each quoted verbatim"},
                    "gap": {"type": "string", "enum": ["none", "partial", "full"]},
                    "evidence_needed": {"type": "string"},
                    "draft_response": {"type": "string"},
                },
                "required": list(MatrixRow.model_fields),
                "additionalProperties": False,
            },
        }
    },
    "required": ["rows"],
    "additionalProperties": False,
}


def summarize(rows: list[MatrixRow]) -> Summary:
    counts = {g: sum(r.gap == g for r in rows) for g in READINESS_WEIGHT}
    pct = round(100 * sum(READINESS_WEIGHT[r.gap] for r in rows) / len(rows)) if rows else 0
    return Summary(counts=counts, overall_readiness_pct=pct)


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _leaves(value) -> list[str]:
    if isinstance(value, dict):
        return [x for v in value.values() for x in _leaves(v)]
    if isinstance(value, list):
        return [x for v in value for x in _leaves(v)]
    return [] if value is None else [_norm(str(value))]


def check_grounding(rows: list[MatrixRow], firm: dict) -> list[str]:
    """Problems that make the model retry: quoted capability facts absent from the profile, or gap 'none' with no quote."""
    facts = _leaves(firm)
    problems = []
    for i, row in enumerate(rows, 1):
        quotes = _QUOTE_RE.findall(row.firm_capability)
        bad = [q for q in quotes if not any(_norm(q) in f for f in facts)]
        if bad:
            problems.append(f"row {i} ({row.requirement[:60]!r}) cites facts not in the firm profile: {bad}")
        elif row.gap == "none" and not quotes:
            problems.append(f"row {i} ({row.requirement[:60]!r}) has gap 'none' but quotes no documented fact")
        if row.gap != "none" and not row.evidence_needed.strip():
            problems.append(f"row {i} ({row.requirement[:60]!r}) has a gap but empty evidence_needed")
    return problems


def _system_prompt(firm: dict) -> str:
    # sort_keys keeps the serialized profile byte-identical across runs so the prefix caches.
    return f"{RUBRIC}\n\n<firm_profile>\n{json.dumps(firm, sort_keys=True, default=str)}\n</firm_profile>"


def _user_message(sol: dict) -> str:
    parts = [f"solicitation_id={sol['id']}", f"title: {sol['title']}"]
    for letter in "CLM":
        parts.append(f"<section_{letter.lower()}>\n{(sol[f'section_{letter.lower()}'] or '(not found)')[:SECTION_CHARS]}\n</section_{letter.lower()}>")
    return "\n\n".join(parts) + "\n\nReturn the compliance matrix."


def _summary(result) -> str:
    return json.dumps(result, default=str)[:SUMMARY_CHARS]


def run_matrix(firm_id: int, solicitation_id: str, llm: MetaClient) -> dict:
    """Run the tool loop (one retry on schema/grounding failure), persist rows, return {matrix, trace}."""
    firm = firms.get_firm(firm_id)
    sol = ingest.get_solicitation(solicitation_id)
    if not firm or not sol:
        raise LookupError("firm or solicitation not found")
    system = _system_prompt(firm)
    messages = [{"role": "user", "content": _user_message(sol)}]
    trace: list[dict] = []
    previous_id = None
    retried = False
    res = None
    problem = ""
    for turn in range(MAX_TURNS):
        res = llm.complete(
            messages,
            system=system,
            tools=MODEL_TOOLS,
            json_schema=MATRIX_SCHEMA,
            schema_name="compliance_matrix",
            reasoning_effort="high",
            feature="matrix",
            max_tokens=MAX_TOKENS,
            previous_response_id=previous_id,
        )
        log.debug("turn=%d tools=%s usage=%s", turn, [c["name"] for c in res.tool_calls], res.usage)
        if res.tool_calls:
            outputs = []
            for call in res.tool_calls:
                try:
                    result = tools.execute_tool(call["name"], call["arguments"])
                except tools.ToolError as exc:
                    result = {"error": str(exc)}
                trace.append({"tool": call["name"], "args": call["arguments"], "result_summary": _summary(result)})
                outputs.append({"type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(result, default=str)})
            messages, previous_id = outputs, res.response_id
            continue
        problems: list[str] = []
        if res.parsed is None:
            problems = [res.parse_error or "no JSON answer returned"]
        else:
            try:
                rows = ModelMatrix.model_validate(res.parsed).rows
                problems = check_grounding(rows, firm) if rows else ["rows is empty"]
            except ValidationError as exc:
                problems = [f"answer failed validation: {exc.error_count()} error(s)"]
        if not problems:
            matrix = ComplianceMatrix(rows=rows, summary=summarize(rows))
            _save_rows(solicitation_id, firm_id, rows)
            return {"matrix": matrix.model_dump(), "trace": trace}
        problem = "; ".join(problems)
        if retried:
            break
        retried = True
        messages = [{
            "role": "user",
            "content": "Your matrix was rejected: " + problem + ". Fix these rows, quoting only facts present in the firm "
            "profile (otherwise use gap partial or full and say what evidence is needed), and return the full matrix JSON.",
        }]
        previous_id = res.response_id
    last = res.text[:500] if res else ""
    raise RuntimeError(
        f"matrix did not produce a valid answer ({problem or 'tool calls never stopped'});"
        f" status={res.status if res else None}; last text: {last!r}"
    )


def _save_rows(solicitation_id: str, firm_id: int, rows: list[MatrixRow]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db.session() as conn:
        conn.execute("DELETE FROM matrix_rows WHERE solicitation_id = ? AND firm_id = ?", (solicitation_id, firm_id))
        conn.executemany(
            "INSERT INTO matrix_rows (created_at, solicitation_id, firm_id, position, requirement, section_ref,"
            " firm_capability, gap, evidence_needed, draft_response) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (now, solicitation_id, firm_id, i, r.requirement, r.section_ref, r.firm_capability, r.gap,
                 r.evidence_needed, r.draft_response)
                for i, r in enumerate(rows)
            ],
        )


def load_matrix(solicitation_id: str, firm_id: int) -> dict | None:
    with db.session() as conn:
        found = conn.execute(
            "SELECT * FROM matrix_rows WHERE solicitation_id = ? AND firm_id = ? ORDER BY position",
            (solicitation_id, firm_id),
        ).fetchall()
    if not found:
        return None
    rows = [MatrixRow.model_validate({k: r[k] for k in MatrixRow.model_fields}) for r in found]
    return ComplianceMatrix(rows=rows, summary=summarize(rows)).model_dump()


class MatrixIn(BaseModel):
    firm_id: int
    solicitation_id: str


@router.post("/agent/matrix")
def create_matrix(body: MatrixIn, llm: MetaClient = Depends(get_llm)):
    if not firms.get_firm(body.firm_id):
        raise HTTPException(404, f"firm {body.firm_id} not found")
    sol = ingest.get_solicitation(body.solicitation_id)
    if not sol:
        raise HTTPException(404, f"solicitation {body.solicitation_id} not found")
    if not (sol["section_c"] or sol["section_l"] or sol["section_m"]):
        raise HTTPException(422, "solicitation has no Section C, L or M text to build a matrix from")
    try:
        return run_matrix(body.firm_id, body.solicitation_id, llm)
    except (RuntimeError, ValidationError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"agent failed: {type(exc).__name__}: {exc}")


@router.get("/agent/matrix/{solicitation_id}")
def read_matrix(solicitation_id: str, firm_id: int = Query(...)):
    matrix = load_matrix(solicitation_id, firm_id)
    if not matrix:
        raise HTTPException(404, "no matrix for this firm and solicitation")
    return matrix
