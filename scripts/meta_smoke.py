"""One real Meta Responses API call. Run manually: python -m scripts.meta_smoke (never from tests)."""
from app.llm.meta_client import MetaClient


def main() -> None:
    res = MetaClient().complete("Reply with the single word OK.", reasoning_effort="low", feature="smoke", max_tokens=256)
    print(f"status: {res.raw.status}")
    print(f"text: {res.text!r}")
    print(f"usage: {res.usage}")


if __name__ == "__main__":
    main()
