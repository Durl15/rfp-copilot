"""One real transcription + extraction call. Run manually (never from tests):
python scripts/voice_smoke.py <audio> [firm_id] [--hints "a, b"]"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, voice  # noqa: E402
from app.main import app  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("firm_id", type=int, nargs="?", default=1)
    ap.add_argument("--hints")
    a = ap.parse_args()
    db.init_db()
    data = a.audio.read_bytes()
    try:
        print(f"converted WAV bytes: {len(voice.to_wav(data, a.audio.name))}")
    except voice.VoiceError as exc:
        sys.exit(f"{exc.status}: {exc.detail}")
    form = {"firm_id": a.firm_id, **({"hints": a.hints} if a.hints else {})}
    r = TestClient(app).post("/voice/past-performance", files={"audio": (a.audio.name, data)}, data=form)
    if r.status_code != 200:
        sys.exit(f"{r.status_code}: {r.text}")
    body = r.json()
    with db.session() as conn:
        raw = json.loads(conn.execute("SELECT raw_response FROM pending_entries WHERE id = ?", (body["pending_id"],)).fetchone()["raw_response"])
    print(f"transcript: {body['transcript']}")
    print(f"audioDurationMs: {raw.get('audioDurationMs')}")
    for f in voice.FIELDS:
        print(f"{f}: {body['entry'][f]!r}  <- {body['evidence'][f]!r}")
    print(f"pending_id: {body['pending_id']}")


if __name__ == "__main__":
    main()
