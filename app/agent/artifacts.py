"""Stored model outputs (narrative, capability statement) and the shared number guard."""
import json
import re
from datetime import datetime, timezone

from app import db

_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")


def ungrounded_numbers(text: str, firm: dict) -> list[str]:
    """Digits or dollar amounts in text that do not appear in the firm profile."""
    profile = json.dumps(firm, default=str)
    found = dict.fromkeys(m.rstrip(",.") for m in _NUMBER_RE.findall(text))
    return [n for n in found if n not in profile and n.lstrip("$") not in profile]


def save_artifact(solicitation_id: str, firm_id: int, kind: str, content) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO artifacts (solicitation_id, firm_id, kind, created_at, content) VALUES (?, ?, ?, ?, ?)",
            (solicitation_id, firm_id, kind, datetime.now(timezone.utc).isoformat(), json.dumps(content)),
        )


def load_artifact(solicitation_id: str, firm_id: int, kind: str):
    with db.session() as conn:
        r = conn.execute(
            "SELECT content FROM artifacts WHERE solicitation_id = ? AND firm_id = ? AND kind = ?",
            (solicitation_id, firm_id, kind),
        ).fetchone()
    return json.loads(r["content"]) if r else None
