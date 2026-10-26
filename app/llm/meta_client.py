"""Thin wrapper over the anthropic SDK pointed at the Meta Model API.

Wire-format assumptions (Meta-specific, passed via extra_body so the SDK stays untouched):
  - reasoning_effort -> top-level "reasoning_effort"
  - json_schema      -> top-level "response_format": {"type": "json_schema", ...}
"""
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import anthropic

from app import db
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
DEFAULT_MAX_TOKENS = 4096


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    raw: Any = field(repr=False, default=None)

    def json(self) -> Any:
        return json.loads(self.text)


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, anthropic.APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500)


class MetaClient:
    def __init__(self, settings: Settings | None = None, client: anthropic.Anthropic | None = None, sleep=time.sleep):
        self.settings = settings or get_settings()
        # SDK retries disabled: retry policy and call logging live here.
        self._client = client or anthropic.Anthropic(
            base_url=self.settings.meta_api_base,
            api_key=self.settings.model_api_key or "unset",
            max_retries=0,
        )
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self.settings.meta_model

    def _params(self, messages, system, tools, json_schema, reasoning_effort, max_tokens) -> dict:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools
        extra: dict[str, Any] = {"reasoning_effort": reasoning_effort}
        if json_schema:
            extra["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema, "strict": True},
            }
        params["extra_body"] = extra
        return params

    def _log(self, feature: str, started: float, usage, error: Exception | None) -> None:
        try:
            with db.session() as conn:
                conn.execute(
                    "INSERT INTO llm_calls (ts, model, feature, prompt_tokens, completion_tokens, latency_ms, ok, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        self.model,
                        feature,
                        getattr(usage, "input_tokens", None),
                        getattr(usage, "output_tokens", None),
                        int((time.monotonic() - started) * 1000),
                        0 if error else 1,
                        f"{type(error).__name__}: {error}"[:500] if error else None,
                    ),
                )
        except Exception:
            log.exception("failed to write llm_calls row")

    def _with_retry(self, fn):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return fn()
            except Exception as exc:
                if attempt == MAX_ATTEMPTS or not _retryable(exc):
                    raise
                self._sleep(BACKOFF_BASE_S * 2 ** (attempt - 1))

    def complete(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        reasoning_effort: str = "medium",
        feature: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResult:
        params = self._params(messages, system, tools, json_schema, reasoning_effort, max_tokens)
        started = time.monotonic()
        try:
            msg = self._with_retry(lambda: self._client.messages.create(**params))
        except Exception as exc:
            self._log(feature, started, None, exc)
            raise
        self._log(feature, started, msg.usage, None)
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        return LLMResult(text, msg.usage.input_tokens, msg.usage.output_tokens, msg)

    def stream(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        reasoning_effort: str = "medium",
        feature: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Iterator[str]:
        """Yield text deltas. Retries only while opening the stream; one llm_calls row per stream."""
        params = self._params(messages, system, tools, json_schema, reasoning_effort, max_tokens)
        started = time.monotonic()
        usage = None
        try:
            cm = self._with_retry(lambda: self._open_stream(params))
            with cm as s:
                for delta in s.text_stream:
                    yield delta
                usage = s.get_final_message().usage
        except Exception as exc:
            self._log(feature, started, None, exc)
            raise
        self._log(feature, started, usage, None)

    def _open_stream(self, params):
        cm = self._client.messages.stream(**params)
        # Enter eagerly so 429/5xx on connect surfaces inside the retry loop.
        stream = cm.__enter__()

        class _Opened:
            def __enter__(_self):
                return stream

            def __exit__(_self, *exc):
                return cm.__exit__(*exc)

        return _Opened()
