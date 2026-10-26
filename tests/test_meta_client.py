from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.llm.meta_client import MetaClient
from tests.conftest import rows


def _status_error(cls, code):
    resp = httpx.Response(code, request=httpx.Request("POST", "http://meta.test/v1/messages"))
    return cls("boom", response=resp, body=None)


def _msg(text="ok", inp=11, out=7):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
    )


class FakeSDK:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

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
        [_status_error(anthropic.RateLimitError, 429), _status_error(anthropic.InternalServerError, 503), _msg()]
    )
    res = client.complete([{"role": "user", "content": "hi"}], feature="t")
    assert res.text == "ok" and len(sdk.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_three_attempts():
    client, sdk, _ = make([_status_error(anthropic.InternalServerError, 500)] * 3)
    with pytest.raises(anthropic.InternalServerError):
        client.complete([{"role": "user", "content": "hi"}])
    assert len(sdk.calls) == 3


def test_does_not_retry_400():
    client, sdk, _ = make([_status_error(anthropic.BadRequestError, 400)])
    with pytest.raises(anthropic.BadRequestError):
        client.complete([{"role": "user", "content": "hi"}])
    assert len(sdk.calls) == 1


def test_success_is_logged():
    client, _, _ = make([_msg(inp=123, out=45)])
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
    client, _, _ = make([_status_error(anthropic.RateLimitError, 429)] * 3)
    with pytest.raises(anthropic.RateLimitError):
        client.complete([{"role": "user", "content": "hi"}], feature="x")
    (row,) = rows("SELECT * FROM llm_calls")
    assert row["ok"] == 0 and "RateLimitError" in row["error"] and row["prompt_tokens"] is None


def test_request_params_carry_effort_schema_and_max_tokens():
    client, sdk, _ = make([_msg('{"a": 1}')])
    schema = {"type": "object"}
    res = client.complete([{"role": "user", "content": "hi"}], system="sys", json_schema=schema, reasoning_effort="low")
    kw = sdk.calls[0]
    assert kw["model"] == "muse-spark-1.3" and kw["system"] == "sys" and kw["max_tokens"] > 0
    assert kw["extra_body"]["reasoning_effort"] == "low"
    assert kw["extra_body"]["response_format"]["json_schema"]["schema"] == schema
    assert res.json() == {"a": 1}


def test_stream_yields_deltas_and_logs():
    class Stream:
        text_stream = iter(["he", "llo"])

        def get_final_message(self):
            return _msg(inp=5, out=2)

    class CM:
        def __enter__(self):
            return Stream()

        def __exit__(self, *a):
            return False

    sdk = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: CM()))
    client = MetaClient(client=sdk, sleep=lambda s: None)
    assert "".join(client.stream([{"role": "user", "content": "hi"}], feature="s")) == "hello"
    (row,) = rows("SELECT * FROM llm_calls")
    assert row["feature"] == "s" and row["prompt_tokens"] == 5 and row["ok"] == 1
