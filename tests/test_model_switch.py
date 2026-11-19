"""Tests for the model-switch + expanded-branch work: OpenAI provider wiring, the opener/closer
model split, the richer reaction outcomes, and the closer's final-score override."""
import json

import pytest

from commentator.config import LLMConfig, load_config
from commentator.flight_pick import FlightPlan, pick_reaction
from commentator.prompts import RESULT_OUTCOMES, reaction_outcomes, shot_prompt, segment_prompt


def test_llmconfig_accepts_openai_and_none_effort():
    cfg = LLMConfig(provider="openai", model="gpt-6-luna", effort="none")
    assert cfg.provider == "openai" and cfg.effort == "none"
    # The old anthropic efforts still validate.
    assert LLMConfig(provider="anthropic", model="claude-opus-5-5", effort="medium").effort == "medium"
    with pytest.raises(Exception):
        LLMConfig(provider="openai", effort="bogus")


def test_opener_and_closer_split_from_round(tmp_path):
    yaml = tmp_path / "c.yaml"
    yaml.write_text(
        "llm:\n"
        "  live: {provider: anthropic, model: m-live}\n"
        "  opener: {provider: anthropic, model: m-open}\n"
        "  closer: {provider: anthropic, model: m-close}\n"
    )
    cfg = load_config(yaml)
    assert cfg.opener_llm.model == "m-open"
    assert cfg.closer_llm.model == "m-close"


def test_opener_closer_fall_back_to_round_then_segment(tmp_path):
    yaml = tmp_path / "c.yaml"
    # Neither opener nor closer set: both fall back to round_llm (here, derived from segment/live).
    yaml.write_text("llm:\n  live: {provider: anthropic, model: m-live}\n")
    cfg = load_config(yaml)
    assert cfg.opener_llm.model == "m-live"
    assert cfg.closer_llm.model == "m-live"


async def test_openai_client_builds_request_and_parses(monkeypatch):
    import httpx

    from commentator.llm.openai_client import OpenAIClient

    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"finish_reason": "stop",
                             "message": {"content": json.dumps({"lines": [{"speaker": "pbp", "text": "hi"}]})}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                          "prompt_tokens_details": {"cached_tokens": 80}},
            }

    class FakeHTTP:
        def __init__(self, *a, **k):
            pass

        async def post(self, path, json):
            captured["path"], captured["body"] = path, json
            return FakeResp()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(httpx, "AsyncClient", FakeHTTP)
    client = OpenAIClient(LLMConfig(provider="openai", model="gpt-6-luna", effort="none", max_tokens=400))
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    res = await client.complete_json("sys", "user", schema)

    assert captured["path"] == "/chat/completions"
    body = captured["body"]
    assert body["model"] == "gpt-6-luna"
    assert body["reasoning_effort"] == "none"
    assert body["max_completion_tokens"] == 400
    assert "temperature" not in body  # reasoning models reject it
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "hi" in res.text
    assert res.input_tokens == 100 and res.output_tokens == 20 and res.cache_read_tokens == 80


async def test_openai_client_silences_truncated_and_refused(monkeypatch):
    import httpx

    from commentator.llm.openai_client import OpenAIClient

    class FakeResp:
        status_code = 200

        def __init__(self, finish, refusal=None):
            self._finish, self._refusal = finish, refusal

        def raise_for_status(self):
            pass

        def json(self):
            msg = {"content": '{"lines": ['}  # half-written JSON
            if self._refusal:
                msg["refusal"] = self._refusal
            return {"choices": [{"finish_reason": self._finish, "message": msg}], "usage": {}}

    for finish, refusal in [("length", None), ("content_filter", None), ("stop", "no")]:
        class FakeHTTP:
            def __init__(self, *a, **k):
                pass

            async def post(self, path, json):
                return FakeResp(finish, refusal)

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(httpx, "AsyncClient", FakeHTTP)
        client = OpenAIClient(LLMConfig(provider="openai", model="gpt-6-luna"))
        res = await client.complete_json("s", "u", {"type": "object"})
        assert res.text == ""  # never hand a truncated/refused body to the parser


def test_brevity_appended_to_shot_and_segment():
    from commentator.prompts import BREVITY

    shot, _ = shot_prompt(player="Wes", team=None, facts=["Club: 7-iron."], hole="Hole 7",
                          recent_lines=[], routine=False, has_images=False)
    seg, _ = segment_prompt(previous_hole="Hole 6", current_hole="Hole 7", recap=["x"], recent_lines=[])
    assert BREVITY in shot and BREVITY in seg


def test_result_outcomes_cover_the_distinct_lies_and_unmapped_fall_back():
    # Every finish GSPro logs as its own surface gets a distinct pre-written reaction, so the call
    # matches how good or bad the lie really is. The three rough tiers stay apart; cart path, structure,
    # pine straw and water are their own branches. A finish mapping to none falls back to a live call.
    assert set(RESULT_OUTCOMES) == {
        "green", "fairway", "semi rough", "rough", "deep rough", "pine straw",
        "leaves", "sand", "bare ground", "rocks", "cart path", "structure", "water",
    }
    assert set(reaction_outcomes(False)) == set(RESULT_OUTCOMES)

    reactions = {o: f"line for {o}" for o in RESULT_OUTCOMES}
    plan = FlightPlan(player="Wes", to_pin="150 yards", is_putt=False, reactions=reactions)
    # Each lie, in both its OCR word and its GSPro-log material spelling, maps to its own branch.
    for lie, outcome in [
        ("deep rough", "deep rough"), ("deeprough", "deep rough"),
        ("semi rough", "semi rough"), ("semirough", "semi rough"), ("rough", "rough"),
        ("pine straw", "pine straw"), ("pinestraw", "pine straw"),
        ("waste area", "sand"), ("Bunker", "sand"), ("sand", "sand"),
        ("cart path", "cart path"), ("concrete", "cart path"),
        ("structure", "structure"), ("wood", "structure"), ("timber", "structure"),
        ("water", "water"), ("green", "green"), ("fairway", "fairway"),
        # Orphan lies the HUD/log can also emit, folded onto an existing branch.
        ("trees", "pine straw"), ("fescue", "deep rough"), ("native", "deep rough"),
        ("hazard", "water"),   # an OCR-read penalty area resolves to the splash reaction
    ]:
        assert pick_reaction(plan, lie, None, None)[0] == outcome, lie
    # Every lie GSPro's round file can name a finish by gets its own pre-written reaction, except the
    # tee box and the drop zone, which say nothing a canned line could safely claim.
    from commentator.round_file import SURFACES
    for lie in {v for v in SURFACES.values() if v and v not in ("tee", "drop zone")}:
        assert pick_reaction(plan, lie, None, None) is not None, lie
    assert pick_reaction(plan, "drop zone", None, None) is None
    assert pick_reaction(plan, "dirt", None, None)[0] == "bare ground"
    assert pick_reaction(plan, "leaves", None, None)[0] == "leaves"
    assert pick_reaction(plan, "stone", None, None)[0] == "rocks"
    # Unmapped / OB / fringe finishes fall back to a live model call (None).
    for lie in ("out of bounds", "fringe", "lava", ""):
        assert pick_reaction(plan, lie, None, None) is None, lie


async def test_check_tries_every_model_once_and_fails_loudly(monkeypatch, capsys):
    import pytest

    import commentator.llm as llm_mod
    from commentator.__main__ import cmd_check
    from commentator.config import Config, LLMConfig
    from commentator.llm.base import LLMClient, LLMResult

    tried = []

    class Fake(LLMClient):
        async def complete_json(self, system, user, schema, images=None):
            tried.append(self.cfg.model)
            if self.cfg.model == "bad":
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            return LLMResult(text='{"lines": []}')

    monkeypatch.setattr(llm_mod, "build_llm", lambda c: Fake(c))
    cfg = Config()
    cfg.llm.live = LLMConfig(provider="openai", model="good")
    cfg.llm.closer = LLMConfig(provider="anthropic", model="bad")
    with pytest.raises(SystemExit):
        await cmd_check(cfg, None)
    out = capsys.readouterr().out
    assert sorted(tried) == ["bad", "good"]   # six roles, two distinct models
    assert "closer: anthropic:bad FAILED" in out and "League: none set" in out
