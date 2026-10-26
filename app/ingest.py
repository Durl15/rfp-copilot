import io
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
from pypdf import PdfReader

from app import db
from app.config import Settings, get_settings
from app.llm.meta_client import MetaClient

log = logging.getLogger(__name__)

SAM_SEARCH_URL = "https://api.sam.gov/prod/opportunities/v2/search"
FIELDS = ("section_c", "section_l", "section_m", "due_date", "naics", "set_aside", "place_of_performance")
LLM_TEXT_LIMIT = 60_000

# ---------------------------------------------------------------- extraction


def extract_pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


def upload_pdf(data: bytes, filename: str, settings: Settings) -> str | None:
    """Upload to the Meta Files API. Returns file_id, or None if the upload fails (text extraction still proceeds)."""
    try:
        r = httpx.post(
            f"{settings.meta_api_base}/files",
            headers={"Authorization": f"Bearer {settings.model_api_key}"},
            files={"file": (filename, data, "application/pdf")},
            data={"purpose": "user_data"},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["id"]
    except Exception:
        log.exception("Meta Files API upload failed for %s", filename)
        return None


def fetch_sam_notice(notice_id: str, settings: Settings) -> dict:
    """Returns {'title', 'text', 'hints': {...}} for a SAM.gov notice id."""
    if not settings.sam_api_key:
        raise ValueError("SAM_API_KEY is not configured")
    today = date.today()
    params = {
        "api_key": settings.sam_api_key,
        "noticeid": notice_id,
        "postedFrom": (today - timedelta(days=364)).strftime("%m/%d/%Y"),
        "postedTo": today.strftime("%m/%d/%Y"),
        "limit": 1,
    }
    r = httpx.get(SAM_SEARCH_URL, params=params, timeout=30)
    r.raise_for_status()
    items = r.json().get("opportunitiesData") or []
    if not items:
        raise LookupError(f"SAM notice {notice_id} not found")
    opp = items[0]
    if str(opp.get("description", "")).startswith("http"):
        d = httpx.get(opp["description"], params={"api_key": settings.sam_api_key}, timeout=30)
        d.raise_for_status()
        desc = re.sub(r"<[^>]+>", " ", d.text)
    else:
        desc = str(opp.get("description") or "")
    pop = opp.get("placeOfPerformance") or {}
    pop_text = ", ".join(
        str(v)
        for v in (
            (pop.get("city") or {}).get("name"),
            (pop.get("state") or {}).get("code"),
            (pop.get("country") or {}).get("code"),
        )
        if v
    )
    hints = {
        "due_date": _norm_date(opp.get("responseDeadLine") or ""),
        "naics": opp.get("naicsCode"),
        "set_aside": opp.get("typeOfSetAsideDescription") or opp.get("typeOfSetAside"),
        "place_of_performance": pop_text or None,
    }
    title = opp.get("title") or ""
    return {"title": title, "text": f"{title}\n\n{desc}".strip(), "hints": hints}


# ------------------------------------------------------------------- tagger

_SECTION_RE = re.compile(r"^[ \t]*SECTION[ \t]+([A-M])\b[^\n]*$", re.I | re.M)
_DATE_RE = (
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
)
_DUE_RE = re.compile(
    r"(?:due|closing|closes?|closed|deadline|response\s+date|receipt\s+of\s+(?:offers|proposals))"
    rf"[^\n]{{0,80}}?({_DATE_RE})",
    re.I,
)
_NAICS_RE = re.compile(r"NAICS(?:\s+code)?[^\d\n]{0,20}(\d{6})", re.I)
_POP_RE = re.compile(r"place\s+of\s+performance\s*[:\-]\s*([^\n]{3,200})", re.I)
_SET_ASIDES = (
    (r"HUBZone", "HUBZone"),
    (r"8\(a\)", "8(a)"),
    (r"SDVOSB|service[- ]disabled\s+veteran", "SDVOSB"),
    (r"EDWOSB", "EDWOSB"),
    (r"WOSB|women[- ]owned", "WOSB"),
    (r"total\s+small\s+business|small\s+business\s+set[- ]aside", "Small Business"),
    (r"full\s+and\s+open", "Full and Open"),
)


def _norm_date(s: str) -> str | None:
    s = s.strip().replace("Sept", "Sep")
    if m := re.match(r"\d{4}-\d{2}-\d{2}", s):  # ISO date or timestamp
        return m.group(0)
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b. %d, %Y", "%b %d %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _sections(text: str) -> dict[str, str]:
    marks = list(_SECTION_RE.finditer(text))
    best: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.start():end].strip()
        letter = m.group(1).upper()
        if letter in "CLM" and len(body) > len(best.get(letter, "")):  # longest wins: skips table-of-contents hits
            best[letter] = body
    return best


def regex_tag(text: str) -> dict:
    out: dict = {f: None for f in FIELDS}
    secs = _sections(text)
    out["section_c"], out["section_l"], out["section_m"] = secs.get("C"), secs.get("L"), secs.get("M")
    for m in _DUE_RE.finditer(text):
        if d := _norm_date(m.group(1)):
            out["due_date"] = d
            break
    if m := _NAICS_RE.search(text):
        out["naics"] = m.group(1)
    if m := _POP_RE.search(text):
        out["place_of_performance"] = m.group(1).strip()
    for pat, label in _SET_ASIDES:
        if re.search(pat, text, re.I):
            out["set_aside"] = label
            break
    return out


_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "section_c": {"type": ["string", "null"], "description": "Statement of work / description (Section C), tight excerpt"},
        "section_l": {"type": ["string", "null"], "description": "Instructions to offerors (Section L), tight excerpt"},
        "section_m": {"type": ["string", "null"], "description": "Evaluation factors (Section M), tight excerpt"},
        "due_date": {"type": ["string", "null"], "description": "Date offers are due, YYYY-MM-DD"},
        "naics": {"type": ["string", "null"], "description": "6-digit NAICS code"},
        "set_aside": {"type": ["string", "null"]},
        "place_of_performance": {"type": ["string", "null"]},
    },
    "required": list(FIELDS),
    "additionalProperties": False,
}
_LLM_SYSTEM = (
    "You extract structured fields from US federal and state solicitations. "
    "Fill ONLY the requested fields. Use null when the document does not state the value; never guess. "
    "For due_date, choose the date proposals/offers are due, not issue, Q&A, or period-of-performance dates."
)


def fill_gaps(text: str, tags: dict, llm: MetaClient) -> tuple[dict, list[str]]:
    """One structured-output call for fields the regex pass left empty. Failure leaves tags unchanged."""
    missing = [f for f in FIELDS if not tags.get(f)]
    if not missing:
        return tags, []
    prompt = f"Fields to fill: {', '.join(missing)}\n\n<solicitation>\n{text[:LLM_TEXT_LIMIT]}\n</solicitation>"
    try:
        res = llm.complete(
            [{"role": "user", "content": prompt}],
            system=_LLM_SYSTEM,
            json_schema=_LLM_SCHEMA,
            schema_name="solicitation_tags",
            reasoning_effort="low",
            feature="solicitation_tagger",
        )
        data = res.json()
    except Exception:
        log.exception("LLM gap-fill failed; keeping regex tags only")
        return tags, []
    merged, filled = dict(tags), []
    for f in missing:
        v = data.get(f)
        if f == "due_date" and v:
            v = _norm_date(str(v))
        if v:
            merged[f] = str(v).strip()
            filled.append(f)
    return merged, filled


def tag_solicitation(text: str, llm: MetaClient, hints: dict | None = None) -> tuple[dict, list[str]]:
    tags = regex_tag(text)
    for k, v in (hints or {}).items():
        if v and not tags.get(k):
            tags[k] = v
    return fill_gaps(text, tags, llm)


# ---------------------------------------------------------------- persist


def create_solicitation(
    *,
    source: str,
    text: str,
    llm: MetaClient,
    title: str | None = None,
    sam_notice_id: str | None = None,
    filename: str | None = None,
    file_id: str | None = None,
    hints: dict | None = None,
) -> dict:
    tags, llm_filled = tag_solicitation(text, llm, hints)
    row = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "sam_notice_id": sam_notice_id,
        "filename": filename,
        "file_id": file_id,
        "title": title,
        "raw_text": text,
        **tags,
        "llm_filled": json.dumps(llm_filled),
    }
    with db.session() as conn:
        conn.execute(
            f"INSERT INTO solicitations ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
    return row


def ingest_text(text: str, llm: MetaClient) -> dict:
    return create_solicitation(source="text", text=text, llm=llm)


def ingest_pdf(data: bytes, filename: str, llm: MetaClient, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    text = extract_pdf_text(data)
    if not text:
        raise ValueError("No extractable text in PDF (scanned image PDFs are not supported)")
    file_id = upload_pdf(data, filename, settings)
    return create_solicitation(source="pdf", text=text, llm=llm, filename=filename, file_id=file_id)


def ingest_sam(notice_id: str, llm: MetaClient, settings: Settings | None = None) -> dict:
    notice = fetch_sam_notice(notice_id, settings or get_settings())
    return create_solicitation(
        source="sam",
        text=notice["text"],
        llm=llm,
        title=notice["title"],
        sam_notice_id=notice_id,
        hints=notice["hints"],
    )


def get_solicitation(sol_id: str) -> dict | None:
    with db.session() as conn:
        r = conn.execute("SELECT * FROM solicitations WHERE id = ?", (sol_id,)).fetchone()
    return dict(r) if r else None
