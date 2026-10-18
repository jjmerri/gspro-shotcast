import asyncio
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("anthropic")
from anthropic import BadRequestError  # noqa: E402

from commentator.config import LLMConfig  # noqa: E402
from commentator.llm.anthropic_client import AnthropicClient, _fit_unions, _union_count  # noqa: E402
from commentator.prompts import preshot_branch_schema  # noqa: E402


def test_branch_schema_fits_claudes_union_limit():
    schema = preshot_branch_schema(["Nigel", "Duke"])
    assert _union_count(schema) > 16
    fitted = _fit_unions(schema)
    assert _union_count(fitted) == 0
    assert fitted["properties"]["flight_lines"]["properties"]["water"]["type"] == "string"


def _reply():
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"lines": []}')],
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def _bad(message):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return BadRequestError(message, response=httpx.Response(400, request=req), body=None)


class _Messages:
    def __init__(self, reject):
        self.reject, self.calls = reject, []

    async def create(self, **params):
        self.calls.append(params)
        if "thinking" in params and params["thinking"]["type"] in self.reject:
            raise _bad(self.reject[params["thinking"]["type"]])
        return _reply()


def _client(monkeypatch, effort, reject):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    c = AnthropicClient(LLMConfig(provider="anthropic", model="claude-haiku-5-5", effort=effort))
    msgs = _Messages(reject)
    c._client = SimpleNamespace(messages=msgs)
    return c, msgs


def _ask(c, schema=None):
    return asyncio.run(c.complete_json("sys", "user", schema or {"type": "object"}))


def test_effort_none_turns_thinking_off(monkeypatch):
    c, msgs = _client(monkeypatch, "none", {})
    _ask(c)
    assert msgs.calls[0]["thinking"] == {"type": "disabled"}
    assert "effort" not in msgs.calls[0]["output_config"]


def test_effort_none_learns_between_tools(monkeypatch):
    c, msgs = _client(monkeypatch, "none", {"disabled": 'send "thinking": {"type": "between_tools"} instead'})
    _ask(c)
    _ask(c)
    assert [m["thinking"]["type"] for m in msgs.calls] == ["disabled", "between_tools", "between_tools"]


def test_effort_none_falls_back_to_low_effort(monkeypatch):
    c, msgs = _client(monkeypatch, "none", {"disabled": '"thinking.type.disabled" is not supported for this model'})
    _ask(c)
    _ask(c)
    assert len(msgs.calls) == 3
    assert all("thinking" not in m and m["output_config"]["effort"] == "low" for m in msgs.calls[1:])


def test_effort_none_falls_through_both_thinking_modes(monkeypatch):
    c, msgs = _client(monkeypatch, "none", {"disabled": 'thinking: use "between_tools"', "between_tools": "thinking not supported"})
    _ask(c)
    assert [m.get("thinking", {}).get("type") for m in msgs.calls] == ["disabled", "between_tools", None]
    assert msgs.calls[-1]["output_config"]["effort"] == "low"


def test_other_bad_requests_still_raise(monkeypatch):
    c, msgs = _client(monkeypatch, "low", {})
    msgs.reject = {}

    async def boom(**params):
        raise _bad("schema too large")
    msgs.create = boom
    with pytest.raises(BadRequestError):
        _ask(c)
