"""Go/No-Go agent: the model calls read-only tools until it returns a structured GoNoGo decision."""
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator

from app import firms, ingest
from app.agent import tools
from app.llm.meta_client import MetaClient
from app.routes import get_llm

router = APIRouter()

MAX_TURNS = 8
MAX_TOKENS = 4000
SUMMARY_CHARS = 200
# record_decision is persisted by this module after the final answer, so the model never sees it.
MODEL_TOOLS = [d for d in tools.TOOL_DEFINITIONS if d["name"] != "record_decision"]

SYSTEM_PROMPT = (
    "You are a bid-desk analyst for HUBZone small businesses. Decide whether the firm should bid on the solicitation. "
    "Use the tools to read the firm profile and the solicitation, check set-aside and NAICS fit, search past performance "
    "against the scope of work, and estimate effort. Never assume facts the tools did not return. "
    "A set-aside the firm lacks the certification for, or a NAICS code outside the firm's codes, is a blocker. "
    "Score 0-100 for fit (70+ bid, 40-69 needs_review, below 40 no_bid unless a blocker says otherwise). "
    "When you have enough information, reply with the final JSON decision only."
)


class GoNoGo(BaseModel):
    score: int = Field(ge=0, le=100)
    verdict: Literal["bid", "no_bid", "needs_review"]
    top_reasons: list[str]
    blockers: list[str]
    estimated_effort_hours: int = Field(ge=0)
    set_aside_match: bool
    naics_match: bool

    @field_validator("top_reasons")
    @classmethod
    def _max_three(cls, v: list[str]) -> list[str]:
        return v[:3]


# Hand-written: strict structured output is stricter about schema keywords than pydantic's generated schema.
GO_NO_GO_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "Fit score 0-100"},
        "verdict": {"type": "string", "enum": ["bid", "no_bid", "needs_review"]},
        "top_reasons": {"type": "array", "items": {"type": "string"}, "description": "At most 3 reasons"},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "estimated_effort_hours": {"type": "integer"},
        "set_aside_match": {"type": "boolean"},
        "naics_match": {"type": "boolean"},
    },
    "required": list(GoNoGo.model_fields),
    "additionalProperties": False,
}


def _summary(result) -> str:
    return json.dumps(result, default=str)[:SUMMARY_CHARS]


def run_go_no_go(firm_id: int, solicitation_id: str, llm: MetaClient) -> dict:
    """Run the tool loop, persist the decision, and return {decision, decision_id, trace}."""
    messages = [
        {
            "role": "user",
            "content": f"Evaluate firm_id={firm_id} for solicitation_id={solicitation_id}. Return the Go/No-Go decision.",
        }
    ]
    trace: list[dict] = []
    previous_id = None
    for _ in range(MAX_TURNS):
        res = llm.complete(
            messages,
            system=SYSTEM_PROMPT,
            tools=MODEL_TOOLS,
            json_schema=GO_NO_GO_SCHEMA,
            schema_name="go_no_go",
            reasoning_effort="high",
            feature="go_no_go",
            max_tokens=MAX_TOKENS,
            previous_response_id=previous_id,
        )
        if not res.tool_calls:
            decision = GoNoGo.model_validate_json(res.text)
            reasons = decision.top_reasons + [f"BLOCKER: {b}" for b in decision.blockers]
            saved = tools.record_decision(solicitation_id, firm_id, decision.verdict, decision.score, reasons)
            return {"decision": decision.model_dump(), "decision_id": saved["decision_id"], "trace": trace}
        outputs = []
        for call in res.tool_calls:
            try:
                result = tools.execute_tool(call["name"], call["arguments"])
            except tools.ToolError as exc:
                result = {"error": str(exc)}
            trace.append({"tool": call["name"], "args": call["arguments"], "result_summary": _summary(result)})
            outputs.append({"type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(result, default=str)})
        messages, previous_id = outputs, res.response_id
    raise RuntimeError(f"go/no-go did not finish within {MAX_TURNS} model turns")


class GoNoGoIn(BaseModel):
    firm_id: int
    solicitation_id: str


@router.post("/agent/go-no-go")
def go_no_go(body: GoNoGoIn, llm: MetaClient = Depends(get_llm)):
    if not firms.get_firm(body.firm_id):
        raise HTTPException(404, f"firm {body.firm_id} not found")
    if not ingest.get_solicitation(body.solicitation_id):
        raise HTTPException(404, f"solicitation {body.solicitation_id} not found")
    try:
        return run_go_no_go(body.firm_id, body.solicitation_id, llm)
    except (RuntimeError, ValidationError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"agent failed: {type(exc).__name__}: {exc}")
