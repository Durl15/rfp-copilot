"""Thin wrapper over the OpenAI SDK pointed at Meta's Responses API.

  - reasoning_effort -> reasoning={"effort": ...}
  - json_schema      -> text={"format": {"type": "json_schema", "name": ..., "schema": ..., "strict": True}}
  - system           -> instructions
  - max_tokens       -> max_output_tokens
Pass a prior result's response_id as previous_response_id to preserve reasoning across agent turns.
"""
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import openai

from app import db
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
DEFAULT_MAX_TOKENS = 4096
EFFORTS = ("minimal", "low", "medium", "high")


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    raw: Any = field(repr=False, default=None)
    parsed: Any = None  # decoded JSON when a json_schema was given
    tool_calls: list[dict] = field(default_factory=list)  # {"call_id", "name", "arguments"}; arguments is a JSON string
    usage: Any = field(repr=False, default=None)
    response_id: str | None = None

    def json(self) -> Any:
        return self.parsed if self.parsed is not None else json.loads(self.text)


@dataclass
class ToolCallDelta:
    """Streamed tool-call fragment: `name` is set on the first fragment, `arguments` carries JSON text deltas."""

    call_id: str
    name: str | None = None
    arguments: str = ""


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, openai.APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500)


class MetaClient:
    def __init__(self, settings: Settings | None = None, client: openai.OpenAI | None = None, sleep=time.sleep):
        self.settings = settings or get_settings()
        # SDK retries disabled: retry policy and call logging live here.
        self._client = client or openai.OpenAI(
            base_url=self.settings.meta_api_base,
            api_key=self.settings.model_api_key or "unset",
            max_retries=0,
        )
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self.settings.meta_model

    def _params(
        self, messages, system, tools, json_schema, schema_name, reasoning_effort, max_tokens, previous_response_id
    ) -> dict:
        if reasoning_effort not in EFFORTS:
            raise ValueError(f"reasoning_effort must be one of {EFFORTS}, got {reasoning_effort!r}")
        params: dict[str, Any] = {
            "model": self.model,
            "input": messages,
            "reasoning": {"effort": reasoning_effort},
            "max_output_tokens": max_tokens,
        }
        if system:
            params["instructions"] = system
        if tools:
            params["tools"] = tools
        if previous_response_id:
            params["previous_response_id"] = previous_response_id
        if json_schema:
            params["text"] = {
                "format": {"type": "json_schema", "name": schema_name, "schema": json_schema, "strict": True}
            }
        return params

    def _log(self, feature: str, started: float, usage, error: Exception | None) -> None:
        try:
            db.init_db()  # idempotent; lets scripts log without going through FastAPI startup
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
        messages: list[dict] | str,
        system: str = "",
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        reasoning_effort: str = "medium",
        feature: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        schema_name: str = "output",
        previous_response_id: str | None = None,
    ) -> LLMResult:
        params = self._params(
            messages, system, tools, json_schema, schema_name, reasoning_effort, max_tokens, previous_response_id
        )
        started = time.monotonic()
        try:
            resp = self._with_retry(lambda: self._client.responses.create(**params))
        except Exception as exc:
            self._log(feature, started, None, exc)
            raise
        self._log(feature, started, resp.usage, None)
        text = resp.output_text or ""
        tool_calls = [
            {"call_id": o.call_id, "name": o.name, "arguments": o.arguments}
            for o in resp.output
            if getattr(o, "type", None) == "function_call"
        ]
        parsed = json.loads(text) if json_schema and text.strip() else None
        return LLMResult(
            text, resp.usage.input_tokens, resp.usage.output_tokens, resp, parsed, tool_calls, resp.usage, resp.id
        )

    def stream(
        self,
        messages: list[dict] | str,
        system: str = "",
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        reasoning_effort: str = "medium",
        feature: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        schema_name: str = "output",
        previous_response_id: str | None = None,
    ) -> Iterator[str | ToolCallDelta]:
        """Yield text deltas (str) and ToolCallDelta fragments. Retries only while opening the stream; one llm_calls row per stream."""
        params = self._params(
            messages, system, tools, json_schema, schema_name, reasoning_effort, max_tokens, previous_response_id
        )
        started = time.monotonic()
        usage = None
        call_ids: dict[str, str] = {}  # output item id -> call_id
        try:
            events = self._with_retry(lambda: self._client.responses.create(stream=True, **params))
            for ev in events:
                kind = ev.type
                if kind == "response.output_text.delta":
                    yield ev.delta
                elif kind == "response.output_item.added" and getattr(ev.item, "type", None) == "function_call":
                    call_ids[ev.item.id] = ev.item.call_id
                    yield ToolCallDelta(ev.item.call_id, name=ev.item.name)
                elif kind == "response.function_call_arguments.delta":
                    yield ToolCallDelta(call_ids.get(ev.item_id, ev.item_id), arguments=ev.delta)
                elif kind == "response.completed":
                    usage = ev.response.usage
                elif kind in ("response.failed", "response.incomplete", "error"):
                    raise RuntimeError(f"stream ended with {kind}")
        except Exception as exc:
            self._log(feature, started, None, exc)
            raise
        self._log(feature, started, usage, None)
