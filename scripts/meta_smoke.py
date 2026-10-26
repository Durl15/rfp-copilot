"""One real Meta Responses API call. Run manually: python scripts/meta_smoke.py (never from tests)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm.meta_client import MetaClient  # noqa: E402


def main() -> None:
    res = MetaClient().complete("Reply with the single word OK.", reasoning_effort="minimal", feature="smoke", max_tokens=256)
    print(f"status: {res.raw.status}")
    print(f"text: {res.text!r}")
    print(f"usage: {res.usage}")


if __name__ == "__main__":
    main()
