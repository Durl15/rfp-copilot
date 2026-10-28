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
MAX_TOKENS = 24000
SECTION_CHARS = 15000
SUMMARY_CHARS = 200
READINESS_WEIGHT = {"none": 1.0, "partial": 0.5, "full": 0.0}
REQUIREMENT_TYPES = ("capability", "eligibility", "submission", "evaluation")
SCORED_TYPES = ("capability", "eligibility")
NO_EVIDENCE = "No matching evidence in profile."
# Past-performance requirements are eligibility rows whose gap is computed in code, never submission rows.
PAST_PERFORMANCE_RE = re.compile(r"past[\s-]+performance|references?|experience|similar\s+(?:in\s+)?(?:size|scope)", re.I)
OFFICE_RE = re.compile(r"\boffices?\b", re.I)
PP_NONE_MIN = 3  # verified matching entries needed for gap none; 1..2 is partial, 0 is full
PP_MIN_TERMS = 2  # distinct scope terms an entry must match to count
PP_MAX_TERMS = 8
PP_SIZE_RATIO = 0.5  # an entry shows comparable size if it has a number at least this fraction of the solicitation's
PP_SCOPE_TOKENS = 1000
PP_MATCH_TOKENS = 600
SCOPE_PROMPT = (
    "You read the scope of one federal solicitation. terms: 4 to 8 specific scope terms naming the work, technology or "
    "deliverables, e.g. \"help desk\", \"ITSM migration\", \"SLA reporting\", \"end-user training\". Never use generic words "
    "such as services, system, provide, contractor, county, support, management. size_indicator: the stated size of the "
    "work, e.g. \"1,200 users\", or an empty string if none is stated. Reply with the JSON only."
)
MATCH_PROMPT = (
    "You compare one past-performance entry of a firm to the scope terms of a solicitation. relevant: true only if the "
    "entry shows work matching at least 2 of the terms. matched_terms: those terms, copied exactly from the list. "
    "quote: one passage copied character for character from the entry that shows the match, or an empty string if "
    "not relevant. Reply with the JSON only."
)
# The firm profile is already in the system prompt; the model only needs the solicitation and search tools.
MODEL_TOOLS = [d for d in tools.TOOL_DEFINITIONS if d["name"] in ("get_solicitation", "search_past_performance", "lookup_naics")]

# Stable prefix part 1: the rubric. Nothing in it may vary per solicitation, or the cache prefix breaks.
RUBRIC = (
    "You build compliance matrices for HUBZone small-business federal proposals. "
    "You get the text of Sections C (scope), L (instructions to offerors) and M (evaluation factors) of one solicitation, "
    "and the firm's profile and past performance below. Produce one row per distinct requirement, in section order. "
    "Rows: requirement (concise, one obligation); section_ref (C, L or M); "
    "requirement_type (capability = work the firm must be able to perform; eligibility = certifications, set-aside, NAICS, "
    "location, registrations, and any request for past performance, references, or experience of similar size or scope; "
    "submission = format, page limits, volumes, deadlines, forms - never references or past performance; "
    "evaluation = Section M evaluation factors, subfactors, weights and their relative importance); firm_capability; "
    "gap (none = fully met by documented facts, partial = partly met or unproven, full = nothing documented); "
    "evidence_needed (what the firm must provide to close the gap; empty string when gap is none, except an eligibility "
    "requirement met by a quoted certification, where it holds only a verification action such as "
    "\"Confirm HUBZone status is active in SAM.gov before submission\"); draft_response.\n"
    "SUBMISSION ROWS: gap is always none, firm_capability is exactly \"" + NO_EVIDENCE + "\", evidence_needed is empty, and "
    "draft_response is a short action item (e.g. Prepare technical volume within 15 pages) with no capability claims.\n"
    "EVALUATION ROWS follow the same rules: gap none, firm_capability exactly \"" + NO_EVIDENCE + "\", evidence_needed empty, "
    "draft_response a short action item such as \"Weighting noted; structure the proposal to address it.\"\n"
    "LOCAL PRESENCE: when a draft_response speaks of the firm's local presence, write \"Based in\" followed by the profile's "
    "place_of_performance. Never use the word \"office\" unless the profile uses it.\n"
    "DRAFT_RESPONSE RULES for capability and eligibility rows:\n"
    "- gap none: may state the capability, using only facts quoted in firm_capability.\n"
    "- gap partial: draft only from quoted evidence and end with one sentence naming what still has to be supplied.\n"
    "- gap full: must NOT claim capability. Begin with \"Gap:\", state what is missing, then give one closing option "
    "from: teaming partner, subcontractor, hire, or acquire certification. Never write \"We will\" deliver, execute or "
    "provide for a capability the profile does not show.\n"
    "GROUNDING RULES - these are checked by code and violations are rejected:\n"
    "1. firm_capability may rely ONLY on facts present in the firm profile or past performance below. "
    "Every fact must be quoted verbatim in double quotes, copied exactly from the profile data (e.g. \"HUBZone\", \"541512\"). "
    "Do not put double quotes around anything else. Every sentence in firm_capability must contain such a quote, "
    "or be exactly \"" + NO_EVIDENCE + "\".\n"
    "2. gap \"none\" requires at least one such verbatim quote. If you cannot quote a documented fact, the gap is "
    "\"partial\" or \"full\" and evidence_needed says what to provide.\n"
    "3. Never invent credentials, clearances, certifications, contract numbers, customers, dollar values or past performance. "
    "Use the tools to search past performance or read the solicitation if needed. "
    "When finished, reply with the final JSON matrix only."
)

_QUOTE_RE = re.compile(r'"([^"]+)"')
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


class MatrixRow(BaseModel):
    requirement: str
    section_ref: Literal["C", "L", "M"]
    requirement_type: Literal["capability", "eligibility", "submission", "evaluation"]
    firm_capability: str
    gap: Literal["none", "partial", "full"]
    evidence_needed: str
    draft_response: str


class ModelMatrix(BaseModel):
    rows: list[MatrixRow]


class ScopeTerms(BaseModel):
    terms: list[str]
    size_indicator: str


class EntryMatch(BaseModel):
    relevant: bool
    matched_terms: list[str]
    quote: str


SCOPE_SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {"type": "string"}}, "size_indicator": {"type": "string"}},
    "required": list(ScopeTerms.model_fields),
    "additionalProperties": False,
}
MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "matched_terms": {"type": "array", "items": {"type": "string"}},
        "quote": {"type": "string"},
    },
    "required": list(EntryMatch.model_fields),
    "additionalProperties": False,
}


class Summary(BaseModel):
    counts: dict[str, int]
    counts_by_type: dict[str, int]
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
                    "requirement_type": {"type": "string", "enum": list(REQUIREMENT_TYPES)},
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
    scored = [r for r in rows if r.requirement_type in SCORED_TYPES]
    counts = {g: sum(r.gap == g for r in scored) for g in READINESS_WEIGHT}
    pct = round(100 * sum(READINESS_WEIGHT[r.gap] for r in scored) / len(scored)) if scored else 0
    counts_by_type = {t: sum(r.requirement_type == t for r in scored) for t in SCORED_TYPES}
    return Summary(counts=counts, counts_by_type=counts_by_type, overall_readiness_pct=pct)


def _ask(llm: MetaClient, system: str, user: str, schema: dict, model: type[BaseModel], feature: str, max_tokens: int):
    res = llm.complete(
        [{"role": "user", "content": user}], system=system, json_schema=schema, schema_name=feature,
        reasoning_effort="minimal", feature=feature, max_tokens=max_tokens,
    )
    if res.parsed is None:
        raise ValueError(res.parse_error or "no JSON answer returned")
    return model.model_validate(res.parsed)  # ValidationError is a ValueError


def extract_scope(sol: dict, llm: MetaClient) -> tuple[list[str], str]:
    """Specific scope terms (generic words dropped in code) and the size indicator, from Section C (title as fallback)."""
    text = (sol["section_c"] or sol["title"] or "")[:SECTION_CHARS]
    try:
        out = _ask(llm, SCOPE_PROMPT, text, SCOPE_SCHEMA, ScopeTerms, "pp_scope", PP_SCOPE_TOKENS)
    except ValueError as exc:
        raise RuntimeError(f"past-performance scope extraction failed: {exc}")
    terms: list[str] = []
    for term in (" ".join(t.split()) for t in out.terms):
        words = re.findall(r"[a-z0-9]+", term.lower())
        if words and not all(w in tools.STOPWORDS for w in words) and term.lower() not in [t.lower() for t in terms]:
            terms.append(term)
    return terms[:PP_MAX_TERMS], out.size_indicator.strip()


def _numbers(text: str) -> list[float]:
    found = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", text)
    return [float(n.replace(",", "")) for n in found if not re.fullmatch(r"(?:19|20)\d\d", n)]  # skip years


def _size_comparable(size: str, verified: list[dict]) -> bool:
    wanted = max(_numbers(size), default=None)
    if wanted is None:
        return False
    return any(n >= PP_SIZE_RATIO * wanted for m in verified for n in _numbers(" ".join(map(str, m["entry"].values()))))


def assess_past_performance(firm: dict, sol: dict, llm: MetaClient) -> dict:
    """Entries that match >= 2 scope terms with a quote verified verbatim in the entry; everything else is no match."""
    terms, size = extract_scope(sol, llm)
    wanted = {t.lower() for t in terms}
    verified = []
    for entry in firm["past_performance"]:
        user = f"scope terms: {json.dumps(terms)}\n\nentry:\n{json.dumps(entry, default=str)}"
        try:
            m = _ask(llm, MATCH_PROMPT, user, MATCH_SCHEMA, EntryMatch, "pp_match", PP_MATCH_TOKENS)
        except ValueError:
            continue
        hits = {t.lower() for t in m.matched_terms} & wanted
        real = m.quote.strip() and '"' not in m.quote and any(m.quote in str(v) for v in entry.values())
        if m.relevant and len(hits) >= PP_MIN_TERMS and real:
            verified.append({"client": entry.get("client"), "quote": m.quote, "entry": entry})
    return {"verified": verified, "size": size}


def _is_pp_row(r: MatrixRow) -> bool:
    """The row asking for references or experience; Section M evaluation rows only weigh it and are never matched."""
    return r.section_ref != "M" and r.requirement_type != "evaluation" and bool(PAST_PERFORMANCE_RE.search(r.requirement))


def apply_past_performance(rows: list[MatrixRow], pp: dict | None) -> list[MatrixRow]:
    """Force past-performance rows to eligibility and compute type, gap, evidence and draft from the assessment."""
    if pp is None:
        return rows
    verified, size = pp["verified"], pp["size"]
    quoted = [f'"{m["quote"]}" for "{m["client"]}".' if m["client"] else f'"{m["quote"]}".' for m in verified]
    n = len(verified)
    gap = "none" if n >= PP_NONE_MIN else "partial" if n else "full"
    undersized = gap == "none" and bool(size) and not _size_comparable(size, verified)
    if undersized:
        gap = "partial"
    capability = " ".join(quoted) or NO_EVIDENCE
    if gap == "none":
        evidence, draft = "", "Past performance of similar scope is documented: " + " ".join(quoted)
    elif undersized:
        evidence = f"Provide a past-performance reference of comparable size ({size})"
        draft = ("Documented similar-scope past performance: " + " ".join(quoted)
                 + f" Still to supply: a reference of comparable size ({size}).")
    elif gap == "partial":
        evidence = f"Provide {PP_NONE_MIN - n} more past-performance reference(s) of similar size and scope"
        draft = ("Documented similar-scope past performance: " + " ".join(quoted)
                 + f" Still to supply: {PP_NONE_MIN - n} more reference(s) of similar size and scope.")
    else:
        evidence = f"Provide {PP_NONE_MIN} past-performance references of similar size and scope"
        draft = "Gap: no past performance of similar scope is documented. Option: teaming partner with relevant contract history."
    return [
        r.model_copy(update={
            "requirement_type": "eligibility", "firm_capability": capability, "gap": gap,
            "evidence_needed": evidence, "draft_response": draft,
        }) if _is_pp_row(r) else r
        for r in rows
    ]


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _leaves(value) -> list[str]:
    if isinstance(value, dict):
        return [x for v in value.values() for x in _leaves(v)]
    if isinstance(value, list):
        return [x for v in value for x in _leaves(v)]
    return [] if value is None else [_norm(str(value))]


def _unquoted_sentences(text: str) -> list[str]:
    """Sentences with no verbatim quote that are not the exact no-evidence sentence."""
    masked = _QUOTE_RE.sub("QUOTE", text)  # periods inside a quote must not split sentences
    return [
        part for part in _SENTENCE_SPLIT_RE.split(masked.strip())
        if part and "QUOTE" not in part and part != NO_EVIDENCE
    ]


def check_grounding(rows: list[MatrixRow], firm: dict) -> list[str]:
    """Problems that make the model retry: ungrounded or unquoted capability claims, and draft_response rule breaks."""
    facts = _leaves(firm)
    problems = []
    for i, row in enumerate(rows, 1):
        label = f"row {i} ({row.requirement[:60]!r})"
        submission = row.requirement_type == "submission"
        evaluation = row.requirement_type == "evaluation"
        quotes = _QUOTE_RE.findall(row.firm_capability)
        bad = [q for q in quotes if not any(_norm(q) in f for f in facts)]
        loose = _unquoted_sentences(row.firm_capability)
        if bad:
            problems.append(f"{label} cites facts not in the firm profile: {bad}")
        elif loose:
            problems.append(f"{label} has firm_capability sentences with no verbatim quote (use exactly {NO_EVIDENCE!r}): {loose}")
        elif row.gap == "none" and not (submission or evaluation) and not quotes:
            problems.append(f"{label} has gap 'none' but quotes no documented fact")
        if OFFICE_RE.search(row.draft_response) and not any("office" in f for f in facts):
            problems.append(f"{label} uses the word 'office'; write 'Based in' plus the profile's place_of_performance instead")
        if submission:
            if PAST_PERFORMANCE_RE.search(row.requirement):
                problems.append(f"{label} mentions references, past performance or experience, so requirement_type must be 'eligibility', not 'submission'")
            if row.gap != "none":
                problems.append(f"{label} is a submission requirement, so gap must be 'none'")
            continue
        if evaluation:
            if row.gap != "none":
                problems.append(f"{label} is an evaluation factor, so gap must be 'none'")
            if row.firm_capability != NO_EVIDENCE:
                problems.append(f"{label} is an evaluation factor, so firm_capability must be exactly {NO_EVIDENCE!r}")
            continue
        if row.gap != "none" and not row.evidence_needed.strip():
            problems.append(f"{label} has a gap but empty evidence_needed")
        if row.gap == "full":
            draft = row.draft_response.strip()
            if not draft.startswith("Gap:") or draft.lower().startswith("we will"):
                problems.append(f"{label} has gap 'full' so draft_response must begin with 'Gap:' and must not begin with 'We will'")
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
    pp = None  # assessed once, only when a past-performance row appears, and reused across the retry
    for turn in range(MAX_TURNS):
        res = llm.complete(
            messages,
            system=system,
            tools=MODEL_TOOLS,
            json_schema=MATRIX_SCHEMA,
            schema_name="compliance_matrix",
            reasoning_effort="medium",
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
                if pp is None and any(_is_pp_row(r) for r in rows):
                    pp = assess_past_performance(firm, sol, llm)
                rows = apply_past_performance(rows, pp)
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
            "profile (otherwise use gap partial or full and say what evidence is needed), following the SUBMISSION ROWS and "
            "DRAFT_RESPONSE RULES, and return the full matrix JSON.",
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
            " requirement_type, firm_capability, gap, evidence_needed, draft_response)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (now, solicitation_id, firm_id, i, r.requirement, r.section_ref, r.requirement_type,
                 r.firm_capability, r.gap, r.evidence_needed, r.draft_response)
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
