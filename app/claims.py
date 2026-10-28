"""Verified-claims guard: dates, values, retention, references and production/demo claims need a verified=true entry."""
import re

from app.agent import artifacts

# Fields an unverified past-performance entry may expose: what it does and who it is for.
SAFE_FIELDS = ("client", "scope")
CLAIM_RE = re.compile(
    r"\b(?:19|20)\d\d\b|\bsince\b|\bpresent\b|\byears?\b|\bdecades?\b|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    r"|\bdollars?\b|\bmillion\b|\bvalue[ds]?\b|\bvalued\b|\bworth\b|\bcontract values?\b|\$\s*\d"
    r"|\bretain\w*|\bretention\b|\brepeat\b|\breturning\b|\bfollow-on\b|\brenew\w*"
    r"|\breferences?\b|\btestimonials?\b"
    r"|\bproduction\b|\bin use\b|\bin service\b|\blive\b|\bdemos?\b|\bdemonstrat\w*|\bdeployed\b|\boperational\b",
    re.I,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def is_verified(entry: dict) -> bool:
    return entry.get("verified") is True


def redact(firm: dict) -> dict:
    """Copy of the firm where unverified past-performance entries keep only client, scope and verified=false."""
    entries = [e if is_verified(e) else {**{k: e[k] for k in SAFE_FIELDS if k in e}, "verified": False}
               for e in firm["past_performance"]]
    return {**firm, "past_performance": entries}


def claim_problems(text: str, firm: dict) -> list[str]:
    """Claim words in text whose source is unverified: no verified entry exists, or the sentence names an unverified client."""
    entries = firm["past_performance"]
    any_verified = any(is_verified(e) for e in entries)
    unverified_clients = [str(e["client"]).lower() for e in entries if not is_verified(e) and e.get("client")]
    problems = []
    for sentence in _SENTENCE_SPLIT_RE.split(text.strip()):
        words = [m.group(0) for m in CLAIM_RE.finditer(sentence)]
        if not words:
            continue
        names_unverified = any(c in sentence.lower() for c in unverified_clients)
        if not any_verified or names_unverified:
            problems.append(f"claim words {sorted(set(w.lower() for w in words))} come from an unverified entry; "
                            f"state only function and audience: {sentence[:80]!r}")
    return problems


def check_text(text: str, firm: dict) -> list[str]:
    """Numbers must be grounded in the redacted profile; claim words must come from verified entries."""
    safe = redact(firm)
    problems = []
    if bad := artifacts.ungrounded_numbers(text, safe):
        problems.append(f"numbers not in the firm profile: {bad}")
    return problems + claim_problems(text, firm)
