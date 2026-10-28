import logging
from types import SimpleNamespace

import httpx
import openai

from app.llm.conversation import Conversation
from tests.test_meta_client import _resp, make

USER = [{"role": "user", "content": "hi"}]


def _missing(msg="Referenced response not found or expired"):
    resp = httpx.Response(400, request=httpx.Request("POST", "http://meta.test/v1/responses"))
    return openai.BadRequestError(msg, response=resp, body=None)


def _call_item(call_id="c1"):
    return SimpleNamespace(type="function_call", call_id=call_id, name="lookup", arguments="{}")


def test_store_true_is_sent():
    client, sdk, _ = make([_resp()])
    client.complete(USER)
    assert sdk.calls[0]["store"] is True


def test_fallback_resends_full_item_list_without_previous_id(caplog):
    client, sdk, _ = make([_resp(output=[_call_item()], rid="r1"), _missing(), _resp(rid="r2")])
    conv = Conversation(client, feature="t")
    first = conv.send(USER)
    out = {"type": "function_call_output", "call_id": "c1", "output": "{}"}
    with caplog.at_level(logging.WARNING):
        conv.send([out])
    assert first.response_id == "r1"
    assert sdk.calls[1]["previous_response_id"] == "r1" and sdk.calls[1]["input"] == [out]
    assert "previous_response_id" not in sdk.calls[2] and sdk.calls[2]["store"] is True
    items = sdk.calls[2]["input"]
    assert items[0] == USER[0] and items[-1] == out and any(getattr(i, "type", None) == "function_call" for i in items)
    assert "not found or expired" in caplog.text


def test_incomplete_retry_does_not_reuse_incomplete_id():
    inc = _resp("{", rid="r_inc")
    inc.status, inc.incomplete_details = "incomplete", SimpleNamespace(reason="max_output_tokens")
    client, sdk, _ = make([inc, _resp("done", rid="r_ok"), _resp(rid="r3")])
    conv = Conversation(client, feature="t")
    conv.send(USER)
    assert [c.get("previous_response_id") for c in sdk.calls] == [None, None]
    assert sdk.calls[1]["max_output_tokens"] > sdk.calls[0]["max_output_tokens"]
    conv.send([{"role": "user", "content": "again"}])
    assert sdk.calls[2]["previous_response_id"] == "r_ok"


def test_never_chains_from_incomplete_final_response():
    inc = _resp("{", rid="r_inc")
    inc.status, inc.incomplete_details = "incomplete", SimpleNamespace(reason="max_output_tokens")
    inc2 = _resp("{", rid="r_inc2")
    inc2.status, inc2.incomplete_details = "incomplete", SimpleNamespace(reason="max_output_tokens")
    client, sdk, _ = make([inc, inc2, _resp(rid="r3")])
    conv = Conversation(client, feature="t")
    conv.send(USER)
    conv.send([{"role": "user", "content": "again"}])
    assert "previous_response_id" not in sdk.calls[2]
    assert sdk.calls[2]["input"][0] == USER[0] and sdk.calls[2]["input"][-1]["content"] == "again"


def test_incomplete_retry_missing_id_uses_stateless_fallback():
    inc = _resp("{", rid="r_inc")
    inc.status, inc.incomplete_details = "incomplete", SimpleNamespace(reason="max_output_tokens")
    client, sdk, _ = make([_resp(rid="r1"), inc, _missing(), _resp("done", rid="r3")])
    conv = Conversation(client, feature="t")
    conv.send(USER)
    res = conv.send([{"role": "user", "content": "next"}])
    assert res.text == "done" and res.response_id == "r3"
    assert sdk.calls[1]["previous_response_id"] == "r1" and sdk.calls[2]["previous_response_id"] == "r1"
    assert "previous_response_id" not in sdk.calls[3] and sdk.calls[3]["input"][0] == USER[0]


def test_other_400_is_not_swallowed():
    import pytest

    client, sdk, _ = make([_resp(rid="r1"), _missing("bad schema")])
    conv = Conversation(client, feature="t")
    conv.send(USER)
    with pytest.raises(openai.BadRequestError):
        conv.send([{"role": "user", "content": "x"}])
    assert len(sdk.calls) == 2
