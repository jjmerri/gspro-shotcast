import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone

import numpy as np
import pytest

from commentator.config import Config, TTSConfig
from commentator.models import Line
from commentator.speech import SpeechQueue
from commentator.tts import build_tts
from commentator.tts.azure_tts import QuotaExhausted, _check, ssml
from commentator.tts.console_tts import ConsoleTTS
from commentator.tts.tiered import TieredTTS, VoiceBudget, billing_month


def _ts(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def test_google_months_turn_over_on_pacific_time_and_azure_on_utc():
    # 05:00 UTC on Nov 1 is still Oct 31 in California (PDT); 07:30 UTC on Dec 1 is Nov 30 (PST).
    assert billing_month("google", _ts(2026, 11, 1, 5)) == "2026-10"
    assert billing_month("google", _ts(2026, 11, 1, 8)) == "2026-11"
    assert billing_month("google", _ts(2026, 12, 1, 7, 30)) == "2026-11"
    assert billing_month("google", _ts(2026, 12, 1, 8, 30)) == "2026-12"
    assert billing_month("azure", _ts(2026, 11, 1, 5)) == "2026-11"


def _tiered_cfg(**tts) -> TTSConfig:
    return TTSConfig(provider="google", fallback=["azure"], **tts)


def test_a_round_goes_to_the_first_voice_whose_free_allowance_covers_it():
    budget = VoiceBudget(_tiered_cfg())
    needed = 18 * 2 * 1500   # 54k: an 18-hole twosome at the default per player-hole
    assert budget.choose(needed) == "google"
    budget.add("google", 850_000)   # 900k is the cap with the 10% margin: 54k no longer fits
    assert budget.choose(needed) == "azure"
    assert budget.choose(9 * 1 * 1500) == "google"   # a short round still fits
    budget.add("azure", 420_000)    # azure's cap is 450k
    assert budget.choose(needed) == "google"   # paid: unset means tts.provider
    assert VoiceBudget(_tiered_cfg(paid="azure")).choose(10**9) == "azure"
    # A provider with no listed allowance has none to run out of.
    assert VoiceBudget(TTSConfig(provider="openai", fallback=["google"])).choose(10**9) == "openai"


def test_the_budget_counts_this_months_requests_and_learns_the_per_hole_rate(tmp_path):
    now = time.time()
    rows = [{"kind": "tts", "provider": "google", "chars": 100, "ts": now},
            {"kind": "tts", "provider": "google", "chars": 999, "ts": now - 40 * 86400},   # an older month
            {"kind": "tts", "provider": "azure", "chars": 50, "ts": now},
            {"kind": "line", "provider": "google", "chars": 7, "ts": now}]
    rows += [{"kind": "tts_round", "holes": 9, "players": 2, "chars": c * 18, "ts": now}
             for c in (1000, 1100, 1200, 1300, 2000)]
    path = tmp_path / "usage.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    budget = VoiceBudget(_tiered_cfg())
    budget.load(path)
    assert budget.remaining("google") == 900_000 - 100
    assert budget.remaining("azure") == 450_000 - 50
    assert budget.chars_per_player_hole() == 2000   # p90 of the five rounds
    assert VoiceBudget(_tiered_cfg(chars_per_player_hole=1234)).chars_per_player_hole() == 1234


class _Voice:
    """A streaming cloud voice: records (voice, excited) per request; `quota` raises QuotaExhausted."""

    remote = concurrent = True
    stream_sample_rate = 10

    def __init__(self, expressive=False, quota=False):
        self.expressive, self.quota, self.calls = expressive, quota, []

    async def synthesize(self, text, voice, instructions=None, **kw):
        raise AssertionError("streaming only")

    async def stream(self, text, voice, instructions=None, **kw):
        self.calls.append((voice, kw.get("excited", False)))
        if self.quota:
            raise QuotaExhausted("403")
        for _ in range(3):
            yield np.full(5, 0.1, dtype="float32")
            await asyncio.sleep(0)


async def _say(speech, lines):
    async def play(audio):
        await audio.finished.wait()

    speech._play, speech._stop = play, lambda: None
    task = asyncio.create_task(speech.run())
    speech.say(lines)
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_the_round_keeps_its_voice_and_a_spent_allowance_hands_over_to_the_paid_one(tmp_path):
    from commentator.usage import UsageLog

    cfg = Config()
    cfg.tts = _tiered_cfg()
    google, azure = _Voice(), _Voice(expressive=True)
    tts = TieredTTS(cfg.tts, {"google": google, "azure": azure})
    speech = SpeechQueue(cfg, tts)
    speech.usage = usage = UsageLog(str(tmp_path))
    tts.load_usage(usage)
    tts.budget.add("google", 890_000)

    def logged():
        return [json.loads(l) for l in usage.path.read_text().splitlines()] if usage.path.exists() else []

    speech.start_round(18, 2)
    assert tts.provider == "azure"
    stale = await speech.prepare(Line("color", "Voiced ahead."))   # before Azure runs out
    assert stale.provider == "azure"
    azure.calls.clear()
    big = Line("pbp", "In the hole!")
    big._excited = True
    await _say(speech, [big, Line("color", "Lucky.")])
    assert azure.calls == [("en-GB-RyanNeural", True), ("en-US-AndrewNeural", False)] and not google.calls
    tts_rows = [r for r in logged() if r["kind"] == "tts"]
    assert {r["provider"] for r in tts_rows} == {"azure"} and tts_rows[0]["model"] == "azure"
    assert tts.budget.remaining("azure") == 450_000 - len("Voiced ahead.In the hole!Lucky.")

    # Azure says the month's allowance is gone mid-round: the line goes out on the paid voice, which
    # keeps the rest of the round, and Azure isn't picked again this month.
    azure.quota = True
    await _say(speech, [Line("pbp", "Next up.")])
    assert google.calls == [("en-GB-Chirp3-HD-Iapetus", False)] and tts.provider == "google"
    assert tts.budget.remaining("azure") == 0
    assert [r["provider"] for r in logged() if r["kind"] == "tts"][-1] == "google"   # who voiced it
    assert tts.budget.remaining("google") == 900_000 - 890_000 - len("Next up.")

    # The clip Azure voiced ahead isn't played once the paid voice has taken over.
    speech.say_prepared(Line("color", "Voiced ahead."), stale)
    await _say(speech, [])
    assert [r["reason"] for r in logged() if r["kind"] == "line" and r["outcome"] == "dropped"] == ["voice"]

    speech.end_round(18, 2)
    (rnd,) = [r for r in logged() if r["kind"] == "tts_round"]
    assert rnd["holes"] == 18 and rnd["players"] == 2 and rnd["chars"] == sum(r["chars"] for r in tts_rows) + 8
    speech.start_round(9, 1)
    assert tts.provider == "google"
    # A restart this month still knows Azure's allowance is spent.
    again = VoiceBudget(cfg.tts)
    again.load(usage.path)
    assert again.remaining("azure") == 0 and again.per_hole == [rnd["chars"] / 36]



async def test_a_spent_allowance_on_the_paid_voice_itself_fails_the_line():
    tts = TieredTTS(TTSConfig(provider="google", fallback=["azure"], paid="azure"),
                    {"google": _Voice(), "azure": _Voice(quota=True)})
    tts.provider = "azure"
    with pytest.raises(QuotaExhausted):
        async for _ in tts.stream("Hi.", lambda p: ("v", None)):
            pass


def test_one_voice_provider_is_used_on_its_own():
    assert isinstance(build_tts(TTSConfig(provider="console")), ConsoleTTS)
    # No fallbacks: tts.paid is ignored, not a second voice.
    assert isinstance(build_tts(TTSConfig(provider="console", fallback=[], paid="openai")), ConsoleTTS)
    # `--console` keeps config.yaml's fallbacks: a print-only run still starts.
    assert isinstance(build_tts(TTSConfig(provider="console", fallback=["google"])), ConsoleTTS)


def test_azure_requests_escape_the_line_and_tell_a_spent_allowance_from_the_rate_limit():
    body = ssml("Tom & Jerry <3", "en-GB-RyanNeural", "cheerful", 2)
    assert "Tom &amp; Jerry &lt;3" in body and 'xml:lang="en-GB"' in body
    assert '<mstts:express-as style="cheerful" styledegree="2">' in body
    assert "express-as" not in ssml("Hi", "en-US-AndrewNeural")
    with pytest.raises(QuotaExhausted):
        _check(403, "Out of call volume quota for TextToSpeech F0 pricing tier")
    with pytest.raises(RuntimeError) as e:
        _check(429, "Too many requests")
    assert not isinstance(e.value, QuotaExhausted)
