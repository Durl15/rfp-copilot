"""Capability statement agent: structured output from the firm profile; company_data is filled by code."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from app import claims, firms, ingest
from app.agent import artifacts
from app.agent.events import noop
from app.llm.conversation import Conversation
from app.llm.meta_client import MetaClient
from app.routes import get_llm

log = logging.getLogger(__name__)
router = APIRouter()

MAX_TOKENS = 4000

SYSTEM_PROMPT = (
    "You write a federal capability statement. Use ONLY facts in the firm profile and past performance provided. "
    "Never invent numbers, dollar amounts, clients, certifications or capabilities. "
    "headline: one line. core_competencies: 3-6 short items. differentiators: 2-4 short items. "
    "past_performance_summaries: one sentence per past-performance entry; for entries with verified false state only what "
    "the work does and who it is for, with no dates, dollar values, client retention, references, production status or demos. Reply with the JSON only."
)


class ModelStatement(BaseModel):
    headline: str
    core_competencies: list[str]
    differentiators: list[str]
    past_performance_summaries: list[str]


class CompanyData(BaseModel):
    name: str
    naics_codes: list[str]
    certifications: list[str]
    location: str | None


class CapabilityStatement(ModelStatement):
    company_data: CompanyData


_LIST = {"type": "array", "items": {"type": "string"}}
STATEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "core_competencies": _LIST,
        "differentiators": _LIST,
        "past_performance_summaries": _LIST,
    },
    "required": list(ModelStatement.model_fields),
    "additionalProperties": False,
}


def _strings(part: ModelStatement) -> list[str]:
    """Every model-written string, each ended with a period so claims are checked per item."""
    items = [part.headline, *part.core_competencies, *part.differentiators, *part.past_performance_summaries]
    return [i if i.rstrip().endswith((".", "!", "?")) else i + "." for i in items]


def run_capability(firm_id: int, solicitation_id: str, llm: MetaClient, emit=noop) -> dict:
    firm = firms.get_firm(firm_id)
    if not firm or not ingest.get_solicitation(solicitation_id):
        raise LookupError("firm or solicitation not found")
    profile = json.dumps(claims.redact(firm), sort_keys=True, default=str)
    messages = [{"role": "user", "content": f"<firm_profile>\n{profile}\n</firm_profile>\nWrite the capability statement."}]
    conv = Conversation(
        llm, system=SYSTEM_PROMPT, json_schema=STATEMENT_SCHEMA, schema_name="capability_statement",
        reasoning_effort="low", feature="capability", max_tokens=MAX_TOKENS,
    )
    problem = ""
    for _ in range(2):
        res = conv.send(messages)
        try:
            if res.parsed is None:
                raise ValueError(res.parse_error or "no JSON answer returned")
            part = ModelStatement.model_validate(res.parsed)
            if bad := claims.check_text(" ".join(_strings(part)), firm):
                raise ValueError("; ".join(bad))
            company = CompanyData(
                name=firm["name"], naics_codes=firm["naics_codes"], certifications=firm["certifications"],
                location=firm["place_of_performance"],
            )
            stmt = CapabilityStatement(**part.model_dump(), company_data=company).model_dump()
            artifacts.save_artifact(solicitation_id, firm_id, "capability", stmt)
            emit("capability_statement", payload=stmt)
            return stmt
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            problem = str(exc)
        messages = [{"role": "user", "content": "Rejected: " + problem + ". Use only profile facts and return the JSON."}]
    raise RuntimeError(f"capability statement did not pass validation ({problem[:300]})")


class CapabilityIn(BaseModel):
    firm_id: int
    solicitation_id: str


@router.post("/agent/capability-statement")
def create_capability(body: CapabilityIn, llm: MetaClient = Depends(get_llm)):
    if not firms.get_firm(body.firm_id):
        raise HTTPException(404, f"firm {body.firm_id} not found")
    if not ingest.get_solicitation(body.solicitation_id):
        raise HTTPException(404, f"solicitation {body.solicitation_id} not found")
    try:
        return run_capability(body.firm_id, body.solicitation_id, llm)
    except RuntimeError as exc:
        raise HTTPException(502, f"agent failed: {exc}")
