"""In-memory abuse limits for the public deployment: per-IP hourly limits and a global daily run cap.

State is per process, which is correct only because the service runs as a single instance.
"""
import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.config import get_settings

AGENT_RUNS_PER_HOUR = 6
VOICE_UPLOADS_PER_HOUR = 10
HOUR = 3600
DAY = 86400
MAX_VOICE_BODY_BYTES = 26 * 1024 * 1024  # 25 MB of audio plus multipart overhead

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_daily: deque = deque()


def reset() -> None:
    with _lock:
        _hits.clear()
        _daily.clear()


def client_ip(request: Request) -> str:
    """Cloud Run appends the real client address as the last X-Forwarded-For entry; earlier entries are client-supplied."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def is_admin(request: Request) -> bool:
    key = get_settings().copilot_admin_key
    sent = request.headers.get("x-admin-key", "")
    return bool(key and sent and hmac.compare_digest(sent.encode(), key.encode()))


def _take(q: deque, limit: int, window: int, now: float) -> bool:
    while q and now - q[0] >= window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def _check_ip(request: Request, bucket: str, limit: int, what: str) -> None:
    if is_admin(request):
        return
    with _lock:
        ok = _take(_hits[(bucket, client_ip(request))], limit, HOUR, time.monotonic())
    if not ok:
        raise HTTPException(429, f"Rate limit reached: {limit} {what} per hour per visitor. Please try again later.")


def agent_run_limit(request: Request) -> None:
    """Dependency for POST /agent/run, /agent/go-no-go, /agent/matrix."""
    _check_ip(request, "agent", AGENT_RUNS_PER_HOUR, "agent runs")
    cap = int(get_settings().max_daily_runs)
    with _lock:
        ok = _take(_daily, cap, DAY, time.monotonic())
    if not ok:
        raise HTTPException(429, "The daily limit for demo agent runs has been reached. Please try again tomorrow.")


def voice_upload_limit(request: Request) -> None:
    """Dependency for POST /voice/past-performance."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_VOICE_BODY_BYTES:
        raise HTTPException(413, "audio exceeds 25 MB")
    _check_ip(request, "voice", VOICE_UPLOADS_PER_HOUR, "voice uploads")
