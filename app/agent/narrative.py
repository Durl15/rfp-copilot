"""Firm narrative agent: a ~200-word narrative drawn only from the firm profile, never claiming a gap-full capability."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import claims, firms, ingest
from app.agent import artifacts
from app.agent.matrix import load_matrix
from app.llm.conversation import Conversation
from app.llm.meta_client import MetaClient
from app.routes import get_llm

log = logging.getLogger(__name__)
router = APIRouter()

MAX_TOKENS = 3000
MIN_WORDS, MAX_WORDS = 120, 250

SYSTEM_PROMPT = (
    "You write the firm narrative for a federal proposal. Write about 200 words of plain prose in one or two paragraphs. "
    "Use ONLY facts in the firm profile and past performance provided. Never invent numbers, dollar amounts, clients, "
    "certifications or capabilities. Never claim, imply or hint at any capability listed under <gaps>. "
    "Past-performance entries with verified false: state only what the work does and who it is for. Never mention "
    "dates, dollar values, client retention, references, production status or demos for them. "
    "Reply with the narrative text only."
)


def _prompt(firm: dict, sol: dict, gaps: list[str]) -> str:
    return (
        f"<firm_profile>\n{json.dumps(firm, sort_keys=True, default=str)}\n</firm_profile>\n"
        f"<solicitation_title>{sol['title']}</solicitation_title>\n"
        f"<gaps>\n{json.dumps(gaps)}\n</gaps>\nWrite the narrative."
    )


def check_narrative(text: str, firm: dict) -> list[str]:
    problems = []
    problems.extend(claims.check_text(text, firm))
    words = len(text.split())
    if not MIN_WORDS <= words <= MAX_WORDS:
        problems.append(f"narrative is {words} words; write about 200")
    return problems


def run_narrative(firm_id: int, solicitation_id: str, llm: MetaClient) -> dict:
    firm = firms.get_firm(firm_id)
    sol = ingest.get_solicitation(solicitation_id)
    if not firm or not sol:
        raise LookupError("firm or solicitation not found")
    matrix = load_matrix(solicitation_id, firm_id)
    if not matrix:
        raise LookupError("no matrix for this firm and solicitation; run /agent/matrix first")
    gaps = [r["requirement"] for r in matrix["rows"] if r["gap"] == "full" and r["requirement_type"] != "submission"]
    messages = [{"role": "user", "content": _prompt(claims.redact(firm), sol, gaps)}]
    conv = Conversation(llm, system=SYSTEM_PROMPT, reasoning_effort="minimal", feature="narrative", max_tokens=MAX_TOKENS)
    problems = []
    for _ in range(2):
        res = conv.send(messages)
        text = res.text.strip()
        problems = check_narrative(text, firm) if text else ["empty answer"]
        if not problems:
            artifacts.save_artifact(solicitation_id, firm_id, "narrative", text)
            return {"narrative": text}
        messages = [{"role": "user", "content": "Rejected: " + "; ".join(problems) + ". Rewrite using only profile facts."}]
    raise RuntimeError(f"narrative did not pass validation ({'; '.join(problems)})")


class NarrativeIn(BaseModel):
    firm_id: int
    solicitation_id: str


@router.post("/agent/narrative")
def create_narrative(body: NarrativeIn, llm: MetaClient = Depends(get_llm)):
    if not firms.get_firm(body.firm_id):
        raise HTTPException(404, f"firm {body.firm_id} not found")
    if not ingest.get_solicitation(body.solicitation_id):
        raise HTTPException(404, f"solicitation {body.solicitation_id} not found")
    try:
        return run_narrative(body.firm_id, body.solicitation_id, llm)
    except LookupError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, f"agent failed: {exc}")
