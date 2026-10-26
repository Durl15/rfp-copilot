from types import SimpleNamespace

import httpx
import openai
import pytest

from app.llm.meta_client import MetaClient, ToolCallDelta
from tests.conftest import rows


def _status_error(cls, code):
    resp = httpx.Response(code, request=httpx.Request("POST", "http://meta.test/v1/responses"))
    return cls("boom", response=resp, body=None)


def _resp(text="ok", inp=11, out=7, rid="resp_1", output=()):
    return SimpleNamespace(
        id=rid,
        status="completed",
        output_text=text,
        output=list(output),
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
    )


class FakeSDK:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def make(outcomes):
    sdk = FakeSDK(outcomes)
    sleeps = []
    return MetaClient(client=sdk, sleep=sleeps.append), sdk, sleeps


def test_retries_429_then_succeeds_with_backoff():
    client, sdk, sleeps = make(
        [_status_error(openai.RateLimitError, 429), _status_error(openai.InternalServerError, 503), _resp()]
    )
    res = client.complete([{"role": "user", "content": "hi"}], feature="t")
    assert res.text == "ok" and len(sdk.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_three_attempts():
    client, sdk, _ = make([_status_error(openai.InternalServerError, 500)] * 3)
    with pytest.raises(openai.InternalServerError):
        client.complete([{"role": "user", "content": "hi"}])
    assert len(sdk.calls) == 3


def test_does_not_retry_400():
    client, sdk, _ = make([_status_error(openai.BadRequestError, 400)])
    with pytest.raises(openai.BadRequestError):
        client.complete([{"role": "user", "content": "hi"}])
    assert len(sdk.calls) == 1


def test_success_is_logged():
    client, _, _ = make([_resp(inp=123, out=45)])
    client.complete([{"role": "user", "content": "hi"}], feature="tagger")
    (row,) = rows("SELECT * FROM llm_calls")
    assert (row["model"], row["feature"], row["prompt_tokens"], row["completion_tokens"], row["ok"], row["error"]) == (
        "muse-spark-1.3",
        "tagger",
        123,
        45,
        1,
        None,
    )
    assert row["latency_ms"] >= 0 and row["ts"]


def test_failure_is_logged_once_after_retries():
    client, _, _ = make([_status_error(openai.RateLimitError, 429)] * 3)
    with pytest.raises(openai.RateLimitError):
        client.complete([{"role": "user", "content": "hi"}], feature="x")
    (row,) = rows("SELECT * FROM llm_calls")
    assert row["ok"] == 0 and "RateLimitError" in row["error"] and row["prompt_tokens"] is None


def test_request_params_carry_effort_schema_and_max_tokens():
    client, sdk, _ = make([_resp('{"a": 1}')])
    schema = {"type": "object"}
    res = client.complete(
        [{"role": "user", "content": "hi"}],
        system="sys",
        json_schema=schema,
        schema_name="things",
        reasoning_effort="low",
        previous_response_id="resp_0",
    )
    kw = sdk.calls[0]
    assert kw["model"] == "muse-spark-1.3" and kw["instructions"] == "sys" and kw["max_output_tokens"] > 0
    assert kw["input"] == [{"role": "user", "content": "hi"}]
    assert kw["reasoning"] == {"effort": "low"}
    assert kw["text"] == {"format": {"type": "json_schema", "name": "things", "schema": schema, "strict": True}}
    assert kw["previous_response_id"] == "resp_0"
    assert res.parsed == {"a": 1} and res.json() == {"a": 1} and res.response_id == "resp_1"


def test_defaults_to_medium_string_input_and_omits_optional_params():
    client, sdk, _ = make([_resp()])
    client.complete("hi")
    kw = sdk.calls[0]
    assert kw["input"] == "hi" and kw["reasoning"] == {"effort": "medium"}
    assert not {"instructions", "tools", "text", "previous_response_id"} & kw.keys()


def test_invalid_effort_rejected_before_calling():
    client, sdk, _ = make([])
    with pytest.raises(ValueError):
        client.complete("hi", reasoning_effort="extreme")
    assert sdk.calls == []


def test_tool_calls_are_returned():
    call = SimpleNamespace(type="function_call", call_id="c1", name="lookup", arguments='{"q": "x"}')
    msg = SimpleNamespace(type="message")
    client, _, _ = make([_resp("", output=[msg, call])])
    res = client.complete("hi", tools=[{"type": "function", "name": "lookup"}])
    assert res.tool_calls == [{"call_id": "c1", "name": "lookup", "arguments": '{"q": "x"}'}]


def test_stream_yields_text_and_tool_deltas_and_logs():
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="he"),
        SimpleNamespace(type="response.output_text.delta", delta="llo"),
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(type="function_call", id="fc_1", call_id="c1", name="lookup"),
        ),
        SimpleNamespace(type="response.function_call_arguments.delta", item_id="fc_1", delta='{"q"'),
        SimpleNamespace(type="response.completed", response=_resp(inp=5, out=2)),
    ]
    client, sdk, _ = make([iter(events)])
    out = list(client.stream([{"role": "user", "content": "hi"}], feature="s"))
    assert out == ["he", "llo", ToolCallDelta("c1", name="lookup"), ToolCallDelta("c1", arguments='{"q"')]
    assert sdk.calls[0]["stream"] is True
    (row,) = rows("SELECT * FROM llm_calls")
    assert row["feature"] == "s" and row["prompt_tokens"] == 5 and row["ok"] == 1


def test_stream_retries_while_opening():
    client, sdk, sleeps = make([_status_error(openai.RateLimitError, 429), iter([])])
    assert list(client.stream("hi")) == []
    assert len(sdk.calls) == 2 and sleeps == [1.0]
