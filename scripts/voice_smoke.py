"""One real transcription call. Run manually: python scripts/voice_smoke.py <audio.wav|mp3|m4a> (never from tests)."""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.voice import transcribe  # noqa: E402

TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


def main() -> None:
    if len(sys.argv) != 2 or Path(sys.argv[1]).suffix.lower() not in TYPES:
        sys.exit("usage: python scripts/voice_smoke.py <audio file: .wav, .mp3 or .m4a>")
    path = Path(sys.argv[1])
    db.init_db()
    t = transcribe(path.read_bytes(), path.name, TYPES[path.suffix.lower()], uuid.uuid4().hex)
    print(f"audio_seconds: {t['audio_seconds']}")
    print(t["text"])


if __name__ == "__main__":
    main()
