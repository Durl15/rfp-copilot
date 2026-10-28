"""Stateless-safe multi-turn helper: the only place agent loops thread previous_response_id.

Keeps every conversation item locally (inputs, response output items, function_call_outputs) so a missing or
expired previous_response_id can be recovered by resending the full list (handled in MetaClient._create_response).
A response that is not `completed` is never used as previous_response_id.
"""
from typing import Any

from app.llm.meta_client import LLMResult, MetaClient


def _as_input_item(item: Any) -> Any:
    return item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else item


class Conversation:
    def __init__(self, llm: MetaClient, **complete_kwargs):
        self._llm = llm
        self._kwargs = complete_kwargs
        self.items: list = []
        self.previous_id: str | None = None

    def send(self, new_items: list) -> LLMResult:
        self.items.extend(new_items)
        pending = new_items if self.previous_id else list(self.items)
        res = self._llm.complete(
            pending, previous_response_id=self.previous_id, fallback_input=list(self.items), **self._kwargs
        )
        self.items.extend(_as_input_item(i) for i in getattr(res.raw, "output", None) or [])
        self.previous_id = res.response_id if res.status == "completed" else None
        return res
