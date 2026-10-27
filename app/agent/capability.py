"""Capability statement agent: structured output from the firm profile; company_data is filled by code."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from app import firms, ingest
from app.agent import artifacts
from app.llm.meta_client import MetaClient
from app.routes import get_llm

log = logging.getLogger(__name__)
router = APIRouter()

MAX_TOKENS = 4000

SYSTEM_PROMPT = (
    "You write a federal capability statement. Use ONLY facts in the firm profile and past performance provided. "
    "Never invent numbers, dollar amounts, clients, certifications or capabilities. "
    "headline: one line. core_competencies: 3-6 short items. differentiators: 2-4 short items. "
    "past_performance_summaries: one sentence per past-performance entry. Reply with the JSON only."
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


def run_capability(firm_id: int, solicitation_id: str, llm: MetaClient) -> dict:
    firm = firms.get_firm(firm_id)
    if not firm or not ingest.get_solicitation(solicitation_id):
        raise LookupError("firm or solicitation not found")
    profile = json.dumps(firm, sort_keys=True, default=str)
    messages = [{"role": "user", "content": f"<firm_profile>\n{profile}\n</firm_profile>\nWrite the capability statement."}]
    previous_id, problem = None, ""
    for _ in range(2):
        res = llm.complete(
            messages, system=SYSTEM_PROMPT, json_schema=STATEMENT_SCHEMA, schema_name="capability_statement",
            reasoning_effort="low", feature="capability", max_tokens=MAX_TOKENS, previous_response_id=previous_id,
        )
        try:
            if res.parsed is None:
                raise ValueError(res.parse_error or "no JSON answer returned")
            part = ModelStatement.model_validate(res.parsed)
            if bad := artifacts.ungrounded_numbers(part.model_dump_json(), firm):
                raise ValueError(f"numbers not in the firm profile: {bad}")
            company = CompanyData(
                name=firm["name"], naics_codes=firm["naics_codes"], certifications=firm["certifications"],
                location=firm["place_of_performance"],
            )
            stmt = CapabilityStatement(**part.model_dump(), company_data=company).model_dump()
            artifacts.save_artifact(solicitation_id, firm_id, "capability", stmt)
            return stmt
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            problem = str(exc)
        messages = [{"role": "user", "content": "Rejected: " + problem + ". Use only profile facts and return the JSON."}]
        previous_id = res.response_id
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
