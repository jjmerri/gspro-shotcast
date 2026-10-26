import pytest
import os
import asyncio
import time
import contextlib
import json
import random
import re

from commentator.config import Config
from commentator.engine import Engine
from commentator.hud import Hud
from commentator.interest import score_shot
from commentator.league import League
from commentator.llm.base import LLMClient, LLMResult
from commentator.models import PreShotEvent, Shot
from commentator.prompts import parse_lines
from commentator.sources.gspro_sniffer import is_shot_message
from commentator.sources.replay import ReplaySource
from commentator.sources.stream import JsonStreamDecoder
from commentator.speech import SpeechQueue
from commentator.state import ShotRecord
from commentator.tts.console_tts import ConsoleTTS

SHOT = {
    "DeviceID": "Uneekor", "Units": "Yards", "ShotNumber": 3, "APIversion": "1",
    "BallData": {"Speed": 160.1, "SpinAxis": -3.0, "TotalSpin": 2500, "HLA": -1.0, "VLA": 12.0, "CarryDistance": 270.4},
    "ClubData": {"Speed": 110.0},
    "ShotDataOptions": {"ContainsBallData": True, "ContainsClubData": True, "IsHeartBeat": False},
}
HEARTBEAT = {"ShotDataOptions": {"ContainsBallData": False, "IsHeartBeat": True}}


def test_stream_decoder_handles_split_and_concatenated_messages():
    payload = (json.dumps(HEARTBEAT) + json.dumps(SHOT)).encode()
    d = JsonStreamDecoder()
    out = d.feed(payload[:30]) + d.feed(payload[30:90]) + d.feed(payload[90:])
    assert out == [HEARTBEAT, SHOT]


def test_stream_decoder_resyncs_after_garbage():
    d = JsonStreamDecoder()
    assert d.feed(b'"Speed": 1}, garbage ' + json.dumps(SHOT).encode())[-1] == SHOT


def test_shot_filtering_and_parsing():
    assert is_shot_message(SHOT)
    assert not is_shot_message(HEARTBEAT)
    shot = Shot.from_gspro(SHOT, {"Club": "DR", "Handed": "RH"})
    assert shot.carry == 270.4 and shot.club == "DR" and shot.spin_axis == -3.0


def test_from_gspro_captures_club_data_and_presence():
    # Modeled on a real EYE XO2 message: rich ClubData, ContainsClubData true.
    msg = {
        "Units": "Yards", "ShotNumber": 7,
        "BallData": {"Speed": 106.6, "SpinAxis": 0.0, "TotalSpin": 0.0, "BackSpin": 3396.6,
                     "SideSpin": -414.7, "HLA": -4.0, "VLA": 21.0, "CarryDistance": 158.0},
        "ClubData": {"Speed": 74.9, "AngleOfAttack": 8.4, "FaceToTarget": -0.5, "Path": -0.4,
                     "VerticalFaceImpact": -7.6, "HorizontalFaceImpact": 8.1, "Loft": 25.7},
        "ShotDataOptions": {"ContainsBallData": True, "ContainsClubData": True, "IsHeartBeat": False},
    }
    shot = Shot.from_gspro(msg, {"Handed": "RH"})
    assert shot.club_measured
    assert shot.angle_of_attack == 8.4 and shot.face_to_target == -0.5 and shot.path == -0.4
    assert shot.horizontal_face_impact == 8.1 and shot.vertical_face_impact == -7.6
    # The EYE XO2 zeroes axis/total and sends back/side; from_gspro reconstructs the axis.
    assert shot.spin_axis is not None and shot.spin_axis != 0.0
    # A player without the club markers: no club data reported.
    plain = Shot.from_gspro({**msg, "ClubData": {},
                             "ShotDataOptions": {"ContainsBallData": True, "ContainsClubData": False}})
    assert not plain.club_measured


def test_club_delivery_facts():
    from commentator.facts import club_delivery_facts, club_delivery_notable

    # No club markers -> silent, whatever the fields say.
    assert club_delivery_facts(Shot(club="7I", angle_of_attack=-5, face_to_target=4, path=-3)) == []
    # A fade shape: face open to the path, out-to-in, caught toward the toe and high.
    shot = Shot(club="7I", club_measured=True, ball_speed=110, club_speed=85, angle_of_attack=-3.5,
                face_to_target=3.0, path=-2.0, horizontal_face_impact=10.0, vertical_face_impact=8.0)
    text = " ".join(club_delivery_facts(shot))
    assert "descending" in text and "out-to-in" in text and "open to the path" in text
    assert "toe" in text and "high on the face" in text
    assert club_delivery_notable(shot)  # face-to-path 5 deg
    # A flush, square driver: smash factor surfaces, no shape/strike noise.
    flush = Shot(club="DR", club_measured=True, ball_speed=167, club_speed=113,
                 angle_of_attack=2.0, face_to_target=0.3, path=0.5)
    ftext = " ".join(club_delivery_facts(flush))
    assert "Smash factor 1.4" in ftext and "hitting up" in ftext
    assert "open to the path" not in ftext and "Strike" not in ftext
    # An ordinary, unremarkable delivery isn't forced notable.
    assert not club_delivery_notable(Shot(club="9I", club_measured=True, ball_speed=115, club_speed=90,
                                          angle_of_attack=-2.0, face_to_target=0.5, path=0.0))
    # Garbage readings are bounded out.
    assert club_delivery_facts(Shot(club="DR", club_measured=True, angle_of_attack=99,
                                    face_to_target=99, path=-99)) == []


def test_putt_detection():
    assert Shot(club="PT", ball_speed=8).is_putt
    assert Shot(ball_speed=9, vla=1.0).is_putt
    assert not Shot(club="DR", ball_speed=160, vla=12).is_putt


def test_tts_active_voices_follows_the_provider():
    from commentator.config import TTSConfig

    assert TTSConfig(provider="kokoro").active_voices == {"pbp": "bm_george", "color": "am_michael"}
    assert TTSConfig(provider="openai").active_voices == {"pbp": "fable", "color": "ballad"}
    # Delivery instructions are OpenAI-only; Kokoro gets none (and ignores the field anyway).
    assert TTSConfig(provider="kokoro").active_instructions == {}
    oi = TTSConfig(provider="openai").active_instructions
    assert "British" in oi["pbp"] and "dry" in oi["color"].lower()
    assert TTSConfig(provider="google").active_voices["pbp"] == "en-GB-Chirp3-HD-Iapetus"
    assert TTSConfig(provider="google").active_instructions == {}


async def test_google_stream_rejoins_split_samples_and_cancels_the_call_when_closed(monkeypatch):
    pytest.importorskip("google.cloud.texttospeech")
    from types import SimpleNamespace

    from commentator.config import TTSConfig
    import numpy as np

    from commentator.tts.google_tts import GoogleTTS

    monkeypatch.setenv("GOOGLE_TTS_API_KEY", "k")
    pcm = np.array([1000, -2000, 3000, 4000], dtype="<i2").tobytes()

    class Call:
        cancelled = False

        def __init__(self, chunks):
            self.chunks = chunks

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for c in self.chunks:
                yield SimpleNamespace(audio_content=c)

        def cancel(self):
            Call.cancelled = True

    sent = []
    connected = asyncio.Event()
    connected.set()

    class Client:
        async def streaming_synthesize(self, requests, timeout):
            sent.extend([r async for r in requests])
            await connected.wait()
            return Call([pcm[:3], pcm[3:7], pcm[7:]])   # chunks that split 16-bit samples

    tts = GoogleTTS(TTSConfig(provider="google"))
    tts._client = Client()
    blocks = [b async for b in tts.stream("Hello.", "en-GB-Chirp3-HD-Iapetus")]
    assert np.allclose(np.concatenate(blocks) * 32768, [1000, -2000, 3000, 4000])
    assert sent[0].streaming_config.voice.language_code == "en-GB" and sent[1].input.text == "Hello."
    assert Call.cancelled

    # Abandoned while the connection is still coming up: the call is cancelled once it arrives.
    Call.cancelled = False
    connected.clear()
    stream = tts.stream("Hello.", "en-GB-Chirp3-HD-Iapetus")
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stream.aclose()
    assert not Call.cancelled
    connected.set()
    await asyncio.sleep(0.01)
    assert Call.cancelled


def test_interest_scoring():
    prior = [ShotRecord("Tony", Shot(club="PW", carry=120), None, Hud(), [])]
    big, tags = score_shot(Shot(club="DR", carry=300, ball_speed=175, hla=0.5), [], Hud())
    assert big >= 0.5 and "monster_carry" in tags
    water, tags = score_shot(Shot(club="7I", carry=150, hla=1), prior, Hud(lie="water"))
    assert "in_water" in tags and water >= 0.5
    dull, _ = score_shot(Shot(club="8I", carry=150, ball_speed=115, hla=1, spin_axis=2), prior, Hud(lie="fairway"))
    assert dull < 0.5
    # A missed green into the rough, and a good approach that finds the green, both get a comment.
    rough, tags = score_shot(Shot(club="7I", carry=150, hla=1), prior, Hud(lie="rough"))
    assert "in_rough" in tags and rough >= 0.5
    green, tags = score_shot(Shot(club="9I", carry=140, hla=1), prior, Hud(lie="green"))
    assert "found_green" in tags and green >= 0.5
    # A makeable putt (3-12 ft) is tagged so it always gets a word; a tap-in and a long lag are not.
    _, tags = score_shot(Shot(ball_speed=6), prior, Hud(lie="green", to_pin="5 feet 2 inches"))
    assert "makeable_putt" in tags
    _, tags = score_shot(Shot(ball_speed=3), prior, Hud(lie="green", to_pin="1 feet 6 inches"))
    assert "makeable_putt" not in tags
    _, tags = score_shot(Shot(ball_speed=12), prior, Hud(lie="green", to_pin="30 feet"))
    assert "makeable_putt" not in tags


def test_a_holed_putt_is_never_routine():
    from commentator.interest import holed_putt_score
    # A drained 32-footer scored 0.10 as a routine lag and went uncalled on a failed dice roll.
    long_hud = Hud(lie="green", to_pin="32 feet 9 inches")
    assert holed_putt_score(0.1, long_hud, ["putt", "long_putt"], 0.5, 0.9) == 0.9
    assert holed_putt_score(0.1, Hud(lie="green", to_pin="7 feet 2 inches"), [], 0.5, 0.9) == 0.5
    assert holed_putt_score(0.1, Hud(lie="green", to_pin="15 feet"), [], 0.5, 0.9) == 0.5
    assert holed_putt_score(0.1, Hud(lie="green", to_pin="1 feet 6 inches"), [], 0.5, 0.9) == 0.1  # tap-in
    assert holed_putt_score(0.1, Hud(lie="green"), ["long_putt"], 0.5, 0.9) == 0.9   # distance unread
    assert holed_putt_score(0.1, Hud(lie="green"), [], 0.5, 0.9) == 0.5


def test_holed_putt_cue_scales_with_length():
    from commentator.interest import holed_putt_cue
    assert holed_putt_cue(8) == "" and holed_putt_cue(None) == ""
    assert "6 out of 10" in holed_putt_cue(15)
    assert "8 out of 10" in holed_putt_cue(25)
    assert "10 out of 10" in holed_putt_cue(45)


def test_player_matching_tolerates_ocr_noise():
    league = League.load("data/league.example.json")
    assert league.match_player("MikeD") == "Mike"
    assert league.match_player("Player: SarahK  Hcp 9.4") == "Sarah"
    assert league.match_player("Big T") == "Tony"
    both = League({"teams": [{"name": "A", "players": [{"name": "Dan"}, {"name": "Daniel"}]}]})
    assert both.match_player("Daniel E Shot 1 350 yds") == "Daniel"
    assert both.match_player("Dan E Shot 1 350 yds") == "Dan"
    assert league.match_player("Priyaa") == "Priya"
    assert league.match_player("Nobody") is None
    # GSPro lists the other players under the active one; the first name is the one hitting.
    assert league.match_player("Tony E Shot 1 400.3 YDS Mike 400.3yds E") == "Tony"


def test_parse_lines_clamps_and_validates():
    raw = json.dumps({"lines": [
        {"speaker": "pbp", "text": "  Lovely   strike. "},
        {"speaker": "ghost", "text": "Short one first. " + "word " * 50},
        {"speaker": "color", "text": ""},
        {"speaker": "color", "text": "word " * 50},
    ]})
    lines = parse_lines(raw, ["pbp", "color"], max_words=10)
    assert [line.speaker for line in lines] == ["pbp", "pbp"]
    assert lines[0].text == "Lovely strike."
    assert lines[1].text == "Short one first."
    assert parse_lines("not json", ["pbp"], 10) == []


def test_parse_lines_logs_the_lines_it_cuts_or_drops(caplog):
    raw = json.dumps({"lines": [{"speaker": "pbp", "text": "Short one first. " + "word " * 20},
                                {"speaker": "pbp", "text": "word " * 20}]})
    with caplog.at_level("WARNING", logger="commentator.prompts"):
        parse_lines(raw, ["pbp"], max_words=10)
    assert ["Cut a 23-word" in caplog.messages[0], "Dropped a 20-word" in caplog.messages[1]] == [True, True]


def test_parse_lines_strips_a_leaked_speaker_label():
    # A model sometimes echoes its own role key or display name into the text; it must not be spoken.
    raw = json.dumps({"lines": [
        {"speaker": "pbp", "text": "pbp: A composed start down the middle."},
        {"speaker": "color", "text": "Duke: and he commits to it."},
        {"speaker": "pbp", "text": "color: pbp: double-prefixed somehow."},
        {"speaker": "pbp", "text": "The pbp: label mid-line stays untouched."},
    ]}, )
    lines = parse_lines(raw, ["pbp", "color"], max_words=20, names=["Nigel", "Duke"])
    assert lines[0].text == "A composed start down the middle."
    assert lines[1].text == "and he commits to it."
    assert lines[2].text == "double-prefixed somehow."
    assert lines[3].text == "The pbp: label mid-line stays untouched."  # only a leading label is stripped
    # A "line" that is only the label (a flight line came back as just "pbp") isn't spoken.
    only = json.dumps({"lines": [{"speaker": "pbp", "text": "pbp"}, {"speaker": "pbp", "text": "Nigel."}]})
    assert parse_lines(only, ["pbp", "color"], max_words=20, names=["Nigel", "Duke"]) == []


class FakeLLM(LLMClient):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.prompts: list[str] = []

    async def complete_json(self, system, user, schema, images=None, cache=True):
        self.prompts.append(user)
        if m := re.search(r"Next hole: (.+)", user):
            text = f"Segment for {m.group(1)}"
        elif "THE ROUND IS" in user:
            text = "Round line."
        elif "golf world" in user or "Golf-world headline" in user:
            text = "What a week out on tour."
        elif m := re.search(r"Player: (\w+)", user):
            text = f"Shot by {m.group(1)}"
        else:
            text = "A line."
        return LLMResult(text=json.dumps({"lines": [{"speaker": "pbp", "text": text}]}), input_tokens=100, output_tokens=20)


async def test_engine_replay_end_to_end(tmp_path):
    cfg = Config(log_dir=str(tmp_path))
    cfg.tts.provider = "console"
    cfg.tts.console_simulate_duration = False
    cfg.commentary.min_seconds_between_comments = 0
    cfg.commentary.routine_comment_rate = 1.0
    llm = FakeLLM(cfg.llm.live)
    engine = Engine(
        cfg=cfg,
        league=League.load("data/league.example.json"),
        source=ReplaySource("data/sample_shots.jsonl", speed=1000),
        live_llm=llm,
        segment_llm=llm,
        speech=SpeechQueue(cfg, ConsoleTTS()),
        rng=random.Random(0),
    )
    await engine.run()

    transcript = (tmp_path / next(p.name for p in tmp_path.glob("transcript-*.txt"))).read_text()
    assert "Shot by Tony" in transcript and "Shot by Sarah" in transcript
    assert "Segment for Hole 2" in transcript
    # First hole banner at startup is not a transition, so only one segment.
    assert transcript.count("Segment for") == 1
    water_prompt = next(p for p in llm.prompts if "now in the water" in p)
    assert "started well left and curved hard left" in water_prompt
    assert engine.stats["lines_rejected"] == 0
    assert len((tmp_path / "usage.jsonl").read_text().splitlines()) == len(llm.prompts)


async def test_speech_queue_stays_busy_until_playback_finishes():
    import asyncio

    from commentator.models import Line
    from commentator.tts.base import TTS, Audio

    class SlowTTS(TTS):
        async def synthesize(self, text, voice, instructions=None):
            await asyncio.sleep(0.05)
            return Audio(samples=None, sample_rate=1)

    played: list[str] = []

    async def fake_play(audio):
        await asyncio.sleep(0.05)
        played.append("x")

    cfg = Config()
    speech = SpeechQueue(cfg, SlowTTS(), player=fake_play)
    task = asyncio.create_task(speech.run())
    speech.say([Line("pbp", "one"), Line("color", "two")])
    await asyncio.sleep(0.01)
    assert not speech.is_idle()
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    assert played == ["x", "x"]


async def test_speech_queue_pauses_between_lines_but_not_after_silence():
    import asyncio
    import time

    from commentator.models import Line
    from commentator.tts.base import TTS, Audio

    class QuickTTS(TTS):
        async def synthesize(self, text, voice, instructions=None):
            return Audio(samples=text, sample_rate=1)

    spans: list[tuple[str, float, float]] = []

    async def fake_play(audio):
        start = time.monotonic()
        await asyncio.sleep(0.02)
        spans.append((audio.samples, start, time.monotonic()))

    async def drain():
        while not speech.is_idle():
            await asyncio.sleep(0.005)

    cfg = Config()
    cfg.tts.turn_gap_seconds, cfg.tts.line_gap_seconds = 0.2, 0.1
    speech = SpeechQueue(cfg, QuickTTS(), player=fake_play)
    task = asyncio.create_task(speech.run())
    try:
        speech.say([Line("pbp", "one"), Line("pbp", "two"), Line("color", "three")])
        await drain()
        assert [s[0] for s in spans] == ["one", "two", "three"]
        slack = 0.02   # Windows' clock ticks about every 16 ms
        assert spans[1][1] - spans[0][2] >= 0.1 - slack     # same speaker carries on
        assert spans[2][1] - spans[1][2] >= 0.2 - slack     # the other one takes over
        assert spans[2][1] - spans[1][2] < 0.3

        await asyncio.sleep(0.25)   # longer than either gap: the next line starts at once
        queued = time.monotonic()
        speech.say([Line("pbp", "four")])
        await drain()
        assert spans[3][1] - queued < 0.05

        # An interrupt doesn't wait out a pending pause.
        await asyncio.sleep(0.25)
        speech.say([Line("color", "five")])
        await asyncio.sleep(0.03)   # "five" is playing
        speech.say([Line("pbp", "six")])
        await asyncio.sleep(0.03)   # "five" done; "six" is in its pause
        cut = time.monotonic()
        speech.say([Line("color", "seven")], interrupt=True)
        await drain()
        assert [s[0] for s in spans[4:]] == ["five", "seven"]
        assert spans[5][1] - cut < 0.05
    finally:
        task.cancel()


class _StallingTTS:
    """A remote voice whose listed calls hang (a stalled cloud request); the rest answer at once."""

    remote = True
    concurrent = True

    def __init__(self, stall_calls=(), delays=None):
        self.calls = 0
        self.stall_calls = set(stall_calls)
        self.delays = delays or {}  # call number -> seconds it takes to answer
        self.active = self.max_active = 0

    async def synthesize(self, text, voice, instructions=None):
        import asyncio

        from commentator.tts.base import Audio

        self.calls += 1
        n = self.calls
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if n in self.stall_calls:
                await asyncio.sleep(60)
            await asyncio.sleep(self.delays.get(n, 0))
        finally:
            self.active -= 1
        return Audio(samples=None, sample_rate=n)


def _fast_tts_cfg(**tts):
    cfg = Config()
    cfg.tts.hedge_after_seconds = tts.get("hedge", 0.05)
    cfg.tts.live_deadline_seconds = tts.get("live", 0.2)
    cfg.tts.prepare_deadline_seconds = tts.get("prepare", 1.0)
    return cfg


async def test_prepare_hedges_a_stalled_tts_request():
    from commentator.models import Line

    tts = _StallingTTS(stall_calls={1})
    speech = SpeechQueue(_fast_tts_cfg(), tts)
    audio = await speech.prepare(Line("pbp", "Finds the fairway."))
    assert audio is not None and audio.sample_rate == 2  # the hedge answered for the stalled first


async def test_a_slow_request_isnt_killed_by_its_hedge():
    from commentator.models import Line

    # The first request is slow but working; the hedge sent after 0.1 s stalls. The slow one wins.
    tts = _StallingTTS(stall_calls={2}, delays={1: 0.15})
    speech = SpeechQueue(_fast_tts_cfg(hedge=0.1), tts)
    audio = await speech.prepare(Line("pbp", "Finds the fairway."))
    assert audio is not None and audio.sample_rate == 1 and tts.calls == 2


async def test_hedging_gives_up_at_the_deadline_with_two_requests_in_flight_at_most():
    import asyncio
    import time

    from commentator.models import Line

    tts = _StallingTTS(stall_calls=set(range(1, 100)))
    speech = SpeechQueue(_fast_tts_cfg(hedge=0.1, prepare=0.45), tts)
    started = time.monotonic()
    assert await speech.prepare(Line("pbp", "never")) is None
    assert time.monotonic() - started < 0.6
    assert tts.calls >= 4 and tts.max_active == 2  # kept re-hedging, never more than two at once
    await asyncio.sleep(0.01)  # let the cancellations land
    assert tts.active == 0  # every request was cancelled once it gave up


async def test_live_line_that_cant_be_voiced_in_time_is_dropped_not_played_late():
    import asyncio

    from commentator.models import Line

    played: list[str] = []

    async def rec_play(audio):
        played.append("x")

    tts = _StallingTTS(stall_calls={1})
    speech = SpeechQueue(_fast_tts_cfg(hedge=1.0, live=0.1), tts, player=rec_play, stopper=lambda: None)
    task = asyncio.create_task(speech.run())
    speech.say([Line("pbp", "too late")])
    started = asyncio.get_running_loop().time()
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    assert asyncio.get_running_loop().time() - started < 0.5  # gave up at the live deadline, not 60 s
    assert played == []
    speech.say([Line("pbp", "on time")])  # the queue isn't wedged behind the stall
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert played == ["x"]


async def test_interrupt_cancels_a_stalled_synth_so_the_new_line_isnt_held_up():
    import asyncio
    import time

    from commentator.models import Line

    played: list[str] = []

    async def rec_play(audio):
        played.append("x")

    tts = _StallingTTS(stall_calls={1})
    speech = SpeechQueue(_fast_tts_cfg(hedge=30, live=30), tts, player=rec_play, stopper=lambda: None)
    task = asyncio.create_task(speech.run())
    speech.say([Line("pbp", "stalls")])
    await asyncio.sleep(0.02)
    started = time.monotonic()
    speech.say([Line("pbp", "the next shot's call")], interrupt=True)
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 0.5 and played == ["x"]


async def test_a_new_shot_drops_waiting_lines_but_lets_the_one_on_air_finish():
    import asyncio

    from commentator.models import Line

    played: list[str] = []
    gate = asyncio.Event()

    async def rec_play(audio):
        played.append("start")
        await gate.wait()
        played.append("end")

    tts = _StallingTTS(stall_calls=set())
    speech = SpeechQueue(_fast_tts_cfg(hedge=30, live=30), tts, player=rec_play, stopper=lambda: None)
    task = asyncio.create_task(speech.run())
    speech.say([Line("pbp", "on air"), Line("color", "old news"), Line("pbp", "older news")])
    while played != ["start"]:
        await asyncio.sleep(0.01)
    speech.drop_queued()
    gate.set()
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    assert played == ["start", "end"]   # the line on air finished, the two behind it never played
    speech.say([Line("pbp", "the new shot's call")])
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert played == ["start", "end", "start", "end"]


async def test_live_lines_voice_in_parallel_so_a_stall_doesnt_delay_the_next():
    import asyncio
    import time

    from commentator.models import Line

    spoken: list[str] = []
    starts: list[float] = []

    class RecTTS(_StallingTTS):
        async def synthesize(self, text, voice, instructions=None):
            starts.append(time.monotonic())
            audio = await super().synthesize(text, voice, instructions)
            audio.samples = text  # tag the clip with its line so playback is visible
            return audio

    async def rec_play(audio):
        spoken.append(audio.samples)

    speech = SpeechQueue(_fast_tts_cfg(hedge=0.3, live=0.3), RecTTS(stall_calls={1}),
                         player=rec_play, stopper=lambda: None)
    task = asyncio.create_task(speech.run())
    started = time.monotonic()
    speech.say([Line("pbp", "stalls")])
    speech.say([Line("pbp", "next")])
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert spoken == ["next"]
    assert starts[1] - started < 0.1  # "next" was voiced alongside the stall, not after its 0.3 s deadline


async def test_losing_a_line_drops_the_replies_in_its_exchange():
    import asyncio

    from commentator.models import Line

    played: list[str] = []

    async def rec_play(audio):
        played.append("x")

    speech = SpeechQueue(_fast_tts_cfg(hedge=1.0, live=0.1), _StallingTTS(stall_calls={1}),
                         player=rec_play, stopper=lambda: None)
    task = asyncio.create_task(speech.run())
    speech.say([Line("pbp", "the setup line"), Line("color", "a reply to it")])
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    speech.say([Line("pbp", "a new exchange")])
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert played == ["x"]  # the reply went with its lost opener; the next exchange still plays


async def test_prepare_exchange_voices_in_parallel_and_cuts_at_the_first_failure():
    import time

    from commentator.models import Line

    lines = [Line("pbp", "one"), Line("color", "two"), Line("pbp", "three")]
    # Line two's first request stalls: every line is voiced at once, the stalled one is hedged, and
    # the whole exchange still lands.
    speech = SpeechQueue(_fast_tts_cfg(hedge=0.2), _StallingTTS(stall_calls={2}))
    started = time.monotonic()
    kept, audio = await speech.prepare_exchange(lines)
    assert kept == lines and len(audio) == 3
    assert time.monotonic() - started < 0.4  # one hedge, not three serial waits

    # Line two can't be voiced at all: the exchange is cut before it.
    speech = SpeechQueue(_fast_tts_cfg(hedge=1.0, prepare=0.1), _StallingTTS(stall_calls={2}))
    kept, audio = await speech.prepare_exchange(lines)
    assert kept == lines[:1] and len(audio) == 1


class _StreamingTTS:
    """A streaming cloud voice. Per call number: "stall" never sends a byte, "error" fails at once,
    "gap" sends a block then goes silent; anything else streams three 5-sample blocks."""

    remote = True
    concurrent = True
    stream_sample_rate = 10

    def __init__(self, plans=None):
        self.plans = plans or {}
        self.calls = 0
        self.active = 0

    async def synthesize(self, text, voice, instructions=None):
        raise AssertionError("streaming voice: whole-clip synth shouldn't be used")

    async def stream(self, text, voice, instructions=None):
        import asyncio

        import numpy as np

        self.calls += 1
        plan = self.plans.get(self.calls)
        self.active += 1
        try:
            if plan == "stall":
                await asyncio.sleep(60)
            if plan == "error":
                raise RuntimeError("HTTP 500")
            if plan == "endless":   # keeps trickling audio, never finishes
                while True:
                    yield np.full(5, 0.1, dtype="float32")
                    await asyncio.sleep(0.02)
            for i in range(3):
                yield np.full(5, 0.1, dtype="float32")
                if plan == "gap":
                    await asyncio.sleep(60)
                await asyncio.sleep(0.01)
        finally:
            self.active -= 1


class _Rows:
    def __init__(self):
        self.rows = []

    def write(self, row):
        self.rows.append(row)


def _stream_cfg(timeout=0.1, live=1.0, retries=1):
    cfg = Config()
    cfg.tts.first_audio_timeout_seconds = timeout
    cfg.tts.live_deadline_seconds = live
    cfg.tts.retries = retries
    return cfg


async def test_streamed_line_retries_once_when_the_first_request_sends_no_audio():
    import asyncio
    import time

    from commentator.models import Line

    played = []

    async def rec_play(audio):
        played.append((time.monotonic(), audio))
        await audio.finished.wait()

    tts = _StreamingTTS({1: "stall"})
    speech = SpeechQueue(_stream_cfg(), tts, player=rec_play, stopper=lambda: None)
    speech.usage = rows = _Rows()
    task = asyncio.create_task(speech.run())
    started = time.monotonic()
    speech.say([Line("pbp", "Finds the fairway.")])
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert tts.calls == 2 and len(played) == 1
    assert played[0][0] - started < 0.3   # retried at the 0.1 s first-audio timeout, not the 1 s deadline
    assert played[0][1].seconds == 1.5 and played[0][1].ok   # the whole clip, 3 x 5 samples at 10 Hz
    await asyncio.sleep(0.01)
    assert tts.active == 0   # the stalled request was cancelled
    tts_rows = sorted((r for r in rows.rows if r["kind"] == "tts"), key=lambda r: r["attempt"])
    assert [(r["attempt"], r["outcome"]) for r in tts_rows] == [(1, "unanswered"), (2, "ok")]
    assert tts_rows[1]["audio_s"] == 1.5 and tts_rows[1]["first_audio_s"] is not None
    assert tts_rows[0]["est_cost_usd"] == 0
    aired = [r for r in rows.rows if r["kind"] == "line"]
    assert aired[0]["outcome"] == "aired" and aired[0]["streamed"] and aired[0]["wait_s"] < 0.3


async def test_streamed_line_gives_up_after_one_retry_and_retries_an_error_at_once():
    import asyncio
    import time

    from commentator.models import Line

    # Both requests stall: only one retry is sent, and the line is dropped at its deadline.
    tts = _StreamingTTS({1: "stall", 2: "stall", 3: "stall"})
    speech = SpeechQueue(_stream_cfg(timeout=0.05, live=0.3), tts)
    started = time.monotonic()
    ok, audio = await speech._synthesize(Line("pbp", "never"), time.monotonic() + 0.3, live=True)
    assert not ok and audio is None and tts.calls == 2
    assert time.monotonic() - started < 0.5
    await asyncio.sleep(0.01)
    assert tts.active == 0

    # A request that errors is retried straight away, without waiting out the first-audio timeout.
    tts = _StreamingTTS({1: "error"})
    speech = SpeechQueue(_stream_cfg(timeout=5), tts)
    started = time.monotonic()
    ok, audio = await speech._synthesize(Line("pbp", "retry"), time.monotonic() + 5, live=True)
    assert ok and tts.calls == 2 and time.monotonic() - started < 0.2
    await audio.finished.wait()


async def test_streamed_prepare_returns_the_whole_clip_and_a_broken_stream_fails():
    from commentator import speech as speech_mod
    from commentator.models import Line

    speech = SpeechQueue(_stream_cfg(), _StreamingTTS())
    audio = await speech.prepare(Line("pbp", "Ahead of time."))
    assert audio is not None and len(audio.samples) == 15 and audio.sample_rate == 10

    old = speech_mod._STREAM_GAP_SECONDS
    speech_mod._STREAM_GAP_SECONDS = 0.05
    try:
        speech = SpeechQueue(_stream_cfg(), _StreamingTTS({1: "gap"}))
        assert await speech.prepare(Line("pbp", "Cut short.")) is None
    finally:
        speech_mod._STREAM_GAP_SECONDS = old


async def test_interrupt_stops_the_downloads_of_streamed_lines_still_queued():
    import asyncio

    from commentator.models import Line

    async def stuck_play(audio):
        await asyncio.sleep(60)

    tts = _StreamingTTS({n: "endless" for n in range(1, 10)})
    speech = SpeechQueue(_stream_cfg(), tts, player=stuck_play, stopper=lambda: None)
    task = asyncio.create_task(speech.run())
    speech.say([Line("pbp", "one"), Line("color", "two"), Line("pbp", "three")])
    await asyncio.sleep(0.1)
    assert tts.active == 3
    speech.interrupt()
    await asyncio.sleep(0.05)
    assert tts.active == 0 and speech.is_idle()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_streamed_prepare_gives_up_at_its_deadline_even_while_audio_trickles_in():
    import asyncio
    import time

    from commentator.models import Line

    tts = _StreamingTTS({1: "endless"})
    speech = SpeechQueue(_stream_cfg(), tts)
    started = time.monotonic()
    ok, audio = await speech._synthesize(Line("pbp", "slow"), time.monotonic() + 0.2)
    assert not ok and audio is None and time.monotonic() - started < 0.4
    await asyncio.sleep(0.01)
    assert tts.active == 0


async def test_streamed_clip_plays_as_it_arrives_and_an_interrupt_stops_it(monkeypatch, caplog):
    import asyncio
    import sys
    import time
    import types

    import numpy as np

    from commentator import speech as speech_mod
    from commentator.tts.base import StreamedAudio

    written, aborted, dry = [], [], set()

    class FakeStream:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, data):
            written.append(len(data))
            time.sleep(0.005)
            return len(written) in dry   # the card ran dry before this write

        def abort(self):
            aborted.append(True)

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(OutputStream=FakeStream))
    monkeypatch.setattr(speech_mod, "_last_play_end", 0.0)   # long idle: lead silence first

    clip = StreamedAudio(100)
    clip.prebuffer = 0.3
    clip.push(np.zeros(20, dtype="float32"))
    playing = asyncio.create_task(speech_mod.play(clip))
    await asyncio.sleep(0.05)
    assert written == []   # 0.2 s in hand: still waiting for the 0.3 s head start
    clip.push(np.zeros(20, dtype="float32"))
    await asyncio.sleep(0.02)
    clip.push(np.zeros(10, dtype="float32"))   # arrives mid-playback
    clip.finish(True)
    await asyncio.wait_for(playing, 1)
    assert written[:2] == [60, 5] and sum(written) == 110 and not aborted   # 0.6 s lead, then 50 ms blocks
    assert clip.played == 50

    # A clip shorter than the head start plays once it's all in, and a dry card is counted.
    written.clear()
    dry.update({1, 3})   # the first write's flag is the stream starting up, not a gap
    clip = StreamedAudio(100)
    clip.prebuffer = 0.3
    clip.push(np.zeros(15, dtype="float32"))
    clip.finish(True)
    with caplog.at_level("INFO", logger="commentator.speech"):
        await asyncio.wait_for(speech_mod.play(clip), 1)
    assert sum(written) == 15 and "ran dry 1 time(s)" in caplog.text
    dry.clear()

    written.clear()
    clip = StreamedAudio(100)
    clip.push(np.zeros(10_000, dtype="float32"))   # 100 s of audio
    playing = asyncio.create_task(speech_mod.play(clip))
    await asyncio.sleep(0.05)
    playing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await playing
    await asyncio.sleep(0.05)
    assert aborted and len(written) < 50   # stopped within a block or two, not after 100 s
    assert speech_mod._aired_seconds(clip) < 1 < clip.seconds   # aired counts only what played


async def test_cloud_voice_without_prevoice_keeps_branch_text_and_voices_it_as_it_airs(tmp_path):
    from commentator.models import Line

    cfg = Config(log_dir=str(tmp_path))
    speech = SpeechQueue(cfg, _StreamingTTS())
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=speech, screen=None)
    assert speech.usage is engine.usage
    lines = [Line("pbp", "one"), Line("color", "two")]
    said = []
    speech.say = lambda ls, interrupt=False, deadline=None: said.append((ls, interrupt))
    speech.say_prepared_sequence = lambda items: said.append(("prepared", items))

    kept, audio = await engine._voice_ahead(lines)
    assert kept == lines and audio == [None, None] and speech.tts.calls == 0   # nothing voiced yet
    engine._air_exchange(list(zip(kept, audio)))
    assert said == [(lines, True)]

    cfg.tts.prevoice = True   # the old behavior: voiced ahead, played as prepared clips
    kept, audio = await engine._voice_ahead(lines)
    assert len(audio) == 2 and all(a is not None for a in audio)
    engine._air_exchange(list(zip(kept, audio)))
    assert said[-1][0] == "prepared"


def test_usage_prices_openai_cached_tokens_and_summarizes_voice_cost_and_latency(tmp_path):
    from commentator.usage import UsageLog, estimate_cost, summarize, tts_cost

    # OpenAI's prompt_tokens include the cached ones: 1M prompt with 800k cached is 200k full + 800k cached.
    cost = estimate_cost("gpt-6-luna", {"input_tokens": 1_000_000, "cache_read_tokens": 800_000})
    assert cost == pytest.approx(0.2 * 0.10 + 0.8 * 0.01)
    # Cache reads: 0.05x input on Opus/Sonnet 5.5, the standard 0.1x elsewhere.
    assert estimate_cost("claude-opus-5-5", {"cache_read_tokens": 1_000_000}) == pytest.approx(0.20)
    assert estimate_cost("claude-haiku-4-5-20251001", {"cache_read_tokens": 1_000_000}) == pytest.approx(0.10)
    assert tts_cost("gpt-4o-mini-tts", 100, 60) == pytest.approx(0.015)
    assert tts_cost("gpt-4o-mini-tts", 100, 0) == 0 and tts_cost("kokoro", 100, 60) == 0

    log = UsageLog(str(tmp_path))
    log.write({"kind": "tts", "provider": "openai", "model": "gpt-4o-mini-tts", "outcome": "ok",
               "first_audio_s": 0.4, "audio_s": 120, "est_cost_usd": 0.03})
    log.write({"kind": "tts", "provider": "openai", "model": "gpt-4o-mini-tts", "outcome": "unanswered",
               "first_audio_s": None, "audio_s": 0, "est_cost_usd": 0})
    log.write({"kind": "line", "outcome": "aired", "wait_s": 0.6, "audio_s": 60})
    log.write({"kind": "line", "outcome": "dropped", "reason": "no_voice"})
    log.write({"kind": "live", "provider": "openai", "model": "gpt-6-luna", "input_tokens": 1_000_000,
               "cache_read_tokens": 0, "output_tokens": 0, "latency_s": 1.0, "est_cost_usd": 0.0})
    out = summarize(str(tmp_path), weeks=1)
    row = out.splitlines()[1]
    assert "openai $0.10" in row       # recomputed: older OpenAI rows were logged at $0
    assert "$    0.03" in row and "2.0/1.0" in row and "1/2" in row and "0.40/0.40s" in row
    assert row.rstrip().endswith("1")  # one dropped line
    assert "Last night x 1 weeks: $0.13" in out


async def test_a_line_past_its_own_deadline_is_dropped():
    import asyncio
    import time

    from commentator.models import Line

    played: list[str] = []

    async def rec_play(audio):
        played.append("x")

    speech = SpeechQueue(_fast_tts_cfg(), _StallingTTS(), player=rec_play, stopper=lambda: None)
    task = asyncio.create_task(speech.run())
    speech.say([Line("pbp", "the ball already landed")], deadline=time.monotonic() - 0.1)
    speech.say([Line("pbp", "still in time")], deadline=time.monotonic() + 5)
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert played == ["x"]


async def test_say_prepared_sequence_plays_every_line_not_just_the_last():
    # Regression: looping say_prepared interrupts between lines and drops all but the last, which
    # silently truncated the hole wrap and would gut the multi-line closer. The sequence helper
    # interrupts once and feeds the whole exchange.
    import asyncio

    from commentator.models import Line
    from commentator.tts.base import TTS, Audio

    class InstantTTS(TTS):
        async def synthesize(self, text, voice, instructions=None):
            return Audio(samples=None, sample_rate=1)

    spoken: list[str] = []

    async def rec_play(audio):
        spoken.append("x")
        await asyncio.sleep(0.005)

    cfg = Config()
    speech = SpeechQueue(cfg, InstantTTS(), player=rec_play, stopper=lambda: None)
    clips = [(Line("pbp", f"line {i}"), Audio(samples=None, sample_rate=1)) for i in range(5)]
    task = asyncio.create_task(speech.run())
    speech.say_prepared_sequence(clips)
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert len(spoken) == 5  # all five played, not just the last


async def test_interrupt_mid_sequence_releases_the_synth_loop():
    # Regression: a plain interrupt landing mid-sequence must clear the sequencing token, or the synth
    # loop drops every ordinary line forever and the booth goes silent.
    import asyncio

    from commentator.models import Line
    from commentator.tts.base import TTS, Audio

    class InstantTTS(TTS):
        async def synthesize(self, text, voice, instructions=None):
            await asyncio.sleep(0.005)
            return Audio(samples=None, sample_rate=1)

    spoken: list[str] = []

    async def rec_play(audio):
        spoken.append("x")
        await asyncio.sleep(0.005)

    cfg = Config()
    speech = SpeechQueue(cfg, InstantTTS(), player=rec_play, stopper=lambda: None)
    task = asyncio.create_task(speech.run())
    clips = [(Line("pbp", f"seq {i}"), Audio(samples=None, sample_rate=1)) for i in range(5)]
    speech.say_prepared_sequence(clips)
    await asyncio.sleep(0.008)
    speech.say([Line("pbp", "a later reaction")], interrupt=True)  # bumps generation mid-sequence
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    # The token must be released so ordinary lines are heard again.
    assert speech._sequencing is None
    speech.say([Line("pbp", "after")])
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert "x" in spoken  # the post-interrupt line was synthesized and played, not dropped forever


# Exact OCR output captured from GSPro 3.1.x release frames (see data/screens/SOURCES.md).
def test_hud_parsers_on_real_ocr_output():
    from commentator.hud import OcrItem, hole_key, parse_hole_box, parse_lie, parse_player_box, parse_tiles

    p = parse_player_box(["Mixtape_Golf", "4th (-1)", "Shot 4", "19.5 YDS", "▲4FT 6"])
    assert p["shot_number"] == 4 and p["score_to_par"] == "-1" and p["to_pin"] == "19.5 yards"
    assert parse_player_box(["Mixtape_Golf", "E", "Shot 1", "359.1 YDS", "5.0YDS"])["score_to_par"] == "E"
    assert parse_player_box(["Mixtape_Golf", "3rd (E)", "Shot 3", "10 FT 8", "OFT O"])["to_pin"] == "10 feet 8 inches"
    assert parse_player_box(["MyGolfQuest", "2nd (E)", "Shot 2", "98.2 m", "50"])["to_pin"] == "98.2 metres"

    h = parse_hole_box(["10", "DPC Scottsdale", "PAR 4", "338 m"])
    assert h == {"hole_number": 10, "course": "DPC Scottsdale", "par": 4, "hole_length": "338 metres"}
    assert parse_hole_box(["Palmer Hills", "PAR 5", "504 YDS"])["hole_length"] == "504 yards"
    assert hole_key(Hud(par=4, hole_length="338 metres")) == "par 4 338 metres"

    assert parse_lie(["Green", "O0T"]) == "green"
    assert parse_lie(["Rough"]) == "rough"

    # Two-column tiles come out of OCR as value, value, label, label; pairing is by position.
    items = [OcrItem("2.6 1-0", 1725, 115, 30), OcrItem("86.2", 1840, 115, 30),
             OcrItem("CLUB PATH", 1725, 141, 14), OcrItem("CARRY (game)", 1840, 141, 14),
             OcrItem("2.7 0", 1725, 184, 30), OcrItem("94.4", 1840, 184, 30),
             OcrItem("FACE to TARGET", 1725, 210, 14), OcrItem("CARRY (raw)", 1840, 210, 14)]
    assert parse_tiles(items) == {"club path": "2.6 1-0", "carry (game)": "86.2",
                                  "face to target": "2.7 0", "carry (raw)": "94.4"}


def test_read_hud_on_saved_frame():
    import pytest

    pytest.importorskip("rapidocr")
    from pathlib import Path

    frame = Path("data/screens/mix_h2_shot4_hud_t0220.png")
    if not frame.exists():
        pytest.skip("GSPro frames not downloaded")
    from PIL import Image

    from commentator.config import ScreenConfig
    from commentator.screen import Screen

    cfg = ScreenConfig()
    hud = Screen(cfg).read_hud(["player_box", "hole_box", "tiles", "lie"], Image.open(frame).convert("RGB"))
    assert (hud.shot_number, hud.score_to_par, hud.par, hud.lie) == (4, "-1", 5, "rough")
    assert hud.tiles["total length"] == "222.3"


def test_facts_describe_flight_and_count_streaks():
    from commentator.facts import history_facts, shot_facts

    facts = " ".join(shot_facts(Shot(club="7I", hla=-9.5, spin_axis=-26, carry=96.0, ball_speed=82)))
    assert "7-iron" in facts and "started well left" in facts and "curved hard left" in facts and "96 yards" in facts
    assert "mph" not in facts  # slow shots don't get a speed to misquote
    prior = [ShotRecord("Tony", Shot(club="DR", hla=4.1, carry=248), None, Hud(), []),
             ShotRecord("Tony", Shot(club="5I", hla=3.0, carry=180), None, Hud(), [])]
    hist = " ".join(history_facts("Tony", prior))
    assert "Tony's 3rd shot today" in hist and "last 2 full shots before this one all started right" in hist
    assert "180 yards" in hist
    prior[1].carry_game = 191.4   # GSPro's carry for the conditions replaces the launch monitor's
    assert "191 yards" in " ".join(history_facts("Tony", prior)) and "180" not in " ".join(history_facts("Tony", prior))


def test_grounding_rejects_invented_numbers_counts_and_outcomes():
    from commentator.grounding import check_line
    # A live putt reaction with no result from the log can't say it dropped.
    unknown = "Whether the putt went in is not known: don't say it dropped, went in or missed."
    assert not check_line("A gentle touch, and it drops for par.", unknown, "").ok
    assert check_line("A gentle touch, and it drops for par.", "The putt went in the hole: holed.", "").ok
    assert check_line("It drops onto the green.", "The ball is now on the green.", "").ok
    assert check_line("It rolls in the rough.", "The ball is now in the rough.", "").ok

    facts = "Player: Tony\nClub: 7-iron. Carry: 96 yards.\nWhere the ball finished: not known.\nThis is Tony's 2nd shot today."
    league = "points: 31\nhandicap: 12.0"
    assert check_line("Tony carries it 96, and his team sits on 31 points.", facts, league).ok
    assert not check_line("A long putt from 25 feet.", facts, league).ok
    assert not check_line("That's three in a row for Big T.", facts, league).ok
    assert check_line("That's two shots tonight for Tony.", facts, league).ok
    assert not check_line("And it's found the water.", facts, league).ok
    assert not check_line("Tony finds the fairway.", facts, league).ok  # finish unknown
    # Spelled-out numbers get the same rounding tolerance as digits: a yardage may be rounded, but a
    # far-off number is still invented.
    close = "Player: Tony\nThe pin is 18.7 yards away."
    assert check_line("Nineteen yards to the pin.", close, "").ok
    assert not check_line("Thirty yards to the pin.", close, "").ok
    in_rough = facts + "\nThe ball is now in the rough."
    assert check_line("Missed the fairway, Big T just bought the first round.", in_rough, league).ok
    assert check_line("A rough swing thought from Tony.", facts, league).ok
    assert not check_line("Tony finds the middle of the fairway.", in_rough, league).ok
    assert check_line("Tony finds the rough.", in_rough, league).ok
    assert not check_line("Sarah taps in for par.", facts, league).ok
    # A round's results are counted in the plural; "a bogey" is backed by "bogeys", not by nothing.
    card = "Shed Heads: net even. Holes: a birdie (8), bogeys (3, 5, 9)."
    assert check_line("A birdie on eight, though a bogey at the last.", card, "", mode="round").ok
    assert not check_line("An eagle on the fourth.", card, "", mode="round").ok
    # Text-only pre-shot line (seen_image=False): a current-lie mention stands by any synonym (facts say
    # "sand", the line "bunker"), but a hazard it never saw is still a guess.
    from_sand = "Player: Tony\nLie: the ball lies in the sand.\nTo the pin: 25 yards."
    assert check_line("Out of the bunker now, 25 to the flag.", from_sand, "", mode="preshot", seen_image=False).ok
    assert not check_line("Careful of the water long.", from_sand, "", mode="preshot", seen_image=False).ok
    assert check_line("And it's found the water.", facts + '\nOn-screen shot result: "Water Hazard"', league).ok
    assert check_line("Those two have a rivalry brewing.", facts, league).ok
    hook = "Club: 8-iron. The ball started a little left and curved gently left."
    assert not check_line("Lovely soft fade from Mike.", hook, league).ok
    assert check_line("Lovely little draw from Mike.", hook, league).ok
    with_history = hook + "\nMike's previous full shot: driver. The ball started a little right and curved gently right."
    assert check_line("Lovely little draw from Mike.", with_history, league).ok


def test_repeats_flags_copied_phrases_only():
    from commentator.grounding import repeats

    earlier = ["Mike steps up and absolutely leans on that, 296 in the air."]
    assert repeats("Sarah steps up and absolutely leans on that one.", earlier) is not None
    assert repeats("Sarah steps up and makes it look easy.", earlier) is None


def test_flight_and_preshot_modes():
    from commentator.grounding import check_line

    facts = "The ball is in the air right now. It has not landed.\nThe ball started well left and curved hard left."
    assert not check_line("And that's landed on the green.", facts, "", mode="flight").ok
    assert not check_line("Heading for the water left.", facts, "", mode="flight").ok  # never saw the hole
    assert check_line("Heading for the water left.", facts, "", mode="flight", seen_image=True).ok
    assert not check_line("That's in the water.", facts, "", mode="flight", seen_image=True).ok
    assert check_line("Needs to turn back now, this is drifting.", facts, "", mode="flight").ok
    assert not check_line("Fading right into the desert.", facts, "", mode="flight", seen_image=True).ok
    assert check_line("Fading right toward the desert.", facts, "", mode="flight", seen_image=True).ok
    straight = "The ball is in the air right now. It has not landed.\nThe ball started on line and flew fairly straight."
    assert check_line("That's tracking beautifully down the middle of the fairway.", straight, "", mode="flight").ok
    assert not check_line("That's landed in the middle of the fairway.", straight, "", mode="flight").ok
    assert not check_line("It finds the fairway.", straight, "", mode="flight").ok
    assert not check_line("That one's on the green.", straight, "", mode="flight").ok
    assert check_line("Hanging in the air over the fairway, lovely.", straight, "", mode="flight").ok
    pre = "The ball currently lies in the rough.\nThe pin is 84.8 yards away."
    assert not check_line("He has to avoid the bunkers left.", pre, "", mode="preshot").ok
    assert check_line("He has to avoid the bunker short right.", pre, "", mode="preshot", seen_image=True).ok
    assert check_line("Mike's in the rough with 85 to the flag.", pre, "", mode="preshot").ok
    assert check_line("He needs to find the green from here.", pre, "", mode="preshot").ok
    sand = "The ball currently lies in the sand."
    assert check_line("Sarah lies in the sand.", sand, "", mode="preshot", seen_image=True).ok
    assert check_line("Sarah's ball is in the sand.", "The ball is now in the sand.", "").ok


def test_a_general_plural_of_a_hazard_the_facts_name_once_stands():
    from commentator.grounding import check_line

    # "Stay out of the trees" or "the bunkers on this course" is ordinary golf talk, not a count, so
    # one feature in the facts backs the plural on air - and the plural in the facts backs the singular.
    one = ("Lie: fairway. 145 yards to the pin.\n"
           "Hole layout: a bunker right, about 20 to 40 yards out; a tree close to the left.")
    assert check_line("Those bunkers make distance control crucial.", one, "", mode="preshot").ok
    assert check_line("Keep it out of the trees here.", one, "", mode="preshot").ok
    assert check_line("Mind that bunker.", "Hole layout: bunkers short of the green.", "", mode="preshot").ok
    # Still a guess when the facts name no such feature at all.
    assert not check_line("Keep it out of the trees here.", "Lie: fairway. 145 yards to the pin.", "",
                          mode="preshot").ok


def test_flight_projection_facts():
    from commentator.facts import flight_facts, to_yards

    assert round(to_yards("98.2 metres"), 1) == 107.4 and round(to_yards("10 feet 8 inches"), 2) == 3.56
    assert "short of the pin" in " ".join(flight_facts(Shot(club="9I", carry=95, hla=0), "107.4 yards"))
    assert "past the pin" in " ".join(flight_facts(Shot(club="9I", carry=120, hla=0), "107.4 yards"))
    assert "around the pin" in " ".join(flight_facts(Shot(club="GW", carry=86.4, hla=0.8), "84.8 yards"))
    assert "out of reach" in " ".join(flight_facts(Shot(club="DR", carry=240, hla=0), "504.2 yards"))


async def test_flight_line_interrupts_and_beats_reaction(tmp_path):
    cfg = Config(log_dir=str(tmp_path))
    cfg.tts.console_simulate_duration = False
    cfg.commentary.routine_comment_rate = 1.0
    cfg.commentary.min_seconds_between_comments = 0
    cfg.commentary.preshot_rate = 1.0

    class PhaseLLM(LLMClient):
        async def complete_json(self, system, user, schema, images=None):
            kind = "flight" if user.startswith("IN FLIGHT") else "pre" if user.startswith("PRE-SHOT") else "after"
            return LLMResult(text=json.dumps({"lines": [{"speaker": "pbp", "text": f"{kind} call"}]}))

    llm = PhaseLLM(cfg.llm.live)
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"),
                    source=ReplaySource("data/flight_test.jsonl", speed=1e6), live_llm=llm, segment_llm=llm,
                    speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(0), await_preshot=True)
    await engine.run()
    said = [l.split(": ", 1)[1] for l in next(tmp_path.glob("transcript-*")).read_text().splitlines()]
    first_shot = said[: said.index("after call") + 1]
    assert first_shot == ["pre call", "flight call", "after call"]
    assert len(engine.flight_latencies) == 4


async def test_a_reaction_written_after_a_newer_shot_was_hit_is_not_aired(tmp_path):
    cfg = Config(log_dir=str(tmp_path))
    cfg.tts.console_simulate_duration = False
    cfg.commentary.routine_comment_rate = 1.0
    cfg.commentary.min_seconds_between_comments = 0
    cfg.commentary.preshot_rate = 1.0
    holder = {}

    class SupersedingLLM(LLMClient):
        async def complete_json(self, system, user, schema, images=None):
            if any(k in user for k in ("React to this shot", "routine shot", "This is a big moment")):   # the live reaction: the next ball is struck while it's out
                holder["e"]._shot_seq += 1
                holder["reactions"] = holder.get("reactions", 0) + 1
                return LLMResult(text=json.dumps({"lines": [{"speaker": "pbp", "text": "stale reaction"}]}))
            kind = "flight" if user.startswith("IN FLIGHT") else "pre" if user.startswith("PRE-SHOT") else "after"
            return LLMResult(text=json.dumps({"lines": [{"speaker": "pbp", "text": f"{kind} call"}]}))

    llm = SupersedingLLM(cfg.llm.live)
    engine = holder["e"] = Engine(cfg=cfg, league=League.load("data/league.example.json"),
                                  source=ReplaySource("data/flight_test.jsonl", speed=1e6), live_llm=llm,
                                  segment_llm=llm, speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(0),
                                  await_preshot=True)
    await engine.run()
    said = [x.split(": ", 1)[1] for x in next(tmp_path.glob("transcript-*")).read_text().splitlines()]
    assert "flight call" in said and "stale reaction" not in said
    assert holder["reactions"] > 0


def test_projection_picks_outcomes_for_flight_test_scenarios():
    from commentator.flight_pick import Hazard, project

    # A: wedge from 84.8 yards, carrying 86.4 on line -> on target on flat ground; from the rough to a
    # pin 4.8 yards uphill it carried short but rolled to 10 feet, so with roll-out it's not "short"
    wedge = Shot(club="GW", carry=86.4, hla=0.8, spin_axis=2)
    assert project(wedge, "84.8 yards", []).outcome == "on_target"
    assert project(wedge, "84.8 yards", [], carry_factor=0.94, elevation_yards=4.8).outcome == "on_target"
    # Elevated tee (pin ~16 yards below): a ball whose flat carry reads a touch shy of the
    # elevation-adjusted pin finishes pin high, not short, because a downhill ball runs past its
    # flat carry. The widened dead-band keeps the booth from confidently calling it short.
    downhill = Shot(club="7I", carry=168, hla=0.0, spin_axis=0)
    assert project(downhill, "196 yards", [], elevation_yards=-16.4).outcome == "on_target"
    # B: par 5 drive starting left and hooking, penalty area down the left -> trouble. A penalty area
    # runs down the side, so it's in play even without a distance; a bunker the model couldn't place
    # at a distance is not (it's as likely beside the green), so that drifts to a plain side call.
    left_pen = [Hazard("penalty area", "left", None, None)]
    assert project(Shot(club="DR", carry=240, hla=-4.5, spin_axis=-18), "504.2 yards", left_pen).outcome == "left_trouble"
    vague_bunker = [Hazard("bunker", "left", None, None)]
    assert project(Shot(club="DR", carry=240, hla=-4.5, spin_axis=-18), "504.2 yards", vague_bunker).outcome == "left"
    # C: drive starting right and slicing, nothing mapped on the right -> plain right
    lake_left = [Hazard("water", "left", 200, 280)]
    assert project(Shot(club="DR", carry=235, hla=3.5, spin_axis=16), "338.4 metres", lake_left).outcome == "right"
    # D: 9-iron, 103 yards raw at a 98.2-metre (107-yard) pin. From a clean lie that's on target,
    # but it was hit from desert sand: GSPro gave it 0.91 of that carry and it finished ~10 m short.
    d = Shot(club="9I", carry=103.2, hla=-0.6, spin_axis=-2)
    assert project(d, "98.2 metres", []).outcome == "on_target"
    assert project(d, "98.2 metres", [], carry_factor=0.9).outcome == "short"
    # Short and long only make sense when the green is in range
    assert project(Shot(club="9I", carry=90, hla=0), "107 yards", []).outcome == "short"
    assert project(Shot(club="9I", carry=125, hla=0), "107 yards", []).outcome == "long"
    # Trouble out of carry range isn't called
    far = [Hazard("water", "left", 280, 320)]
    assert project(Shot(club="DR", carry=200, hla=-4, spin_axis=-15), "400 yards", far).outcome == "left"


async def test_impact_uses_preshot_plan_without_a_model_call(tmp_path):
    if not os.path.isfile("data/screens/mgq_h10_tee_distance_markers_t0272.png"):
        pytest.skip("GSPro frames not downloaded (the hazard read needs the screenshot)")
    cfg = Config(log_dir=str(tmp_path))
    cfg.tts.console_simulate_duration = False
    cfg.commentary.routine_comment_rate = 1.0
    cfg.commentary.min_seconds_between_comments = 0
    cfg.commentary.preshot_rate = 0.0  # plans are still built when no setup line is spoken
    calls: list[str] = []

    class PlanLLM(LLMClient):
        async def complete_json(self, system, user, schema, images=None):
            calls.append(user.split(".", 1)[0])
            # The pre-shot is three calls now: hazards, branches (flight lines), and the spoken line. All
            # start with PRE-SHOT, so check HAZARDS then BRANCHES before the plain line call.
            if "HAZARDS" in user:
                return LLMResult(text=json.dumps(
                    {"hazards": [{"kind": "penalty area", "side": "left", "from_yards": None, "to_yards": None}]}))
            if "BRANCHES" in user:
                n = sum(1 for c in calls if c == "PRE-SHOT BRANCHES")  # fresh per shot; identical lines trip the repeat guard
                lines = {k: None for k in ("on_target", "short", "long", "left", "right", "left_trouble", "right_trouble")}
                lines.update(on_target=f"Right at it {n}, looks good.", left=f"Pulled {n}, drifting left.",
                             left_trouble=f"Bending left {n}, into dangerous country.", right=f"Leaking {n} right now.",
                             short=f"Needs {n} legs.", long=f"Too {n} much club.")
                reactions = {k: f"Settled {n} in the {k}." for k in ("green", "fairway", "rough", "sand")}
                return LLMResult(text=json.dumps({"flight_lines": lines, "reaction_lines": reactions}))
            if user.startswith("PRE-SHOT"):  # the spoken line call
                return LLMResult(text=json.dumps({"lines": []}))
            return LLMResult(text=json.dumps({"lines": [{"speaker": "color", "text": "After it landed."}]}))

    llm = PlanLLM(cfg.llm.live)
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"),
                    source=ReplaySource("data/flight_test.jsonl", speed=1e6), live_llm=llm, segment_llm=llm,
                    speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(0), await_preshot=True)
    await engine.run()
    said = (next(tmp_path.glob("transcript-*")).read_text())
    # Scenario A: rough plus a pin 4.8 yards uphill. This older-GSPro frame kept 0.96 of its carry and
    # rolled to 10 feet, but the league's measured rough default (0.90) projects it short.
    assert "Needs 1 legs." in said
    assert "into dangerous country" in said  # scenario B: the left_trouble branch, generic (text-only)
    assert "right now." in said  # scenario C
    assert "IN FLIGHT" not in calls
    assert engine.stats["flight_planned"] == 4
    assert max(engine.flight_latencies) < 0.5


def test_carry_model_penalties_depend_on_the_shot():
    from commentator.carry_model import CarryModel

    m = CarryModel()
    assert m.factor("fairway", 150) == 1.0 and m.factor(None, 150) == 1.0
    assert m.factor("concrete", 150) == 1.0 and m.factor("pine straw", 150) == 0.97
    for raw in (150, 155, 160):
        m.observe("concrete", raw, raw * 0.8)  # never learned away from no penalty
    assert m.factor("concrete", 155) == 1.0
    # Same Sand surface, very different penalty: a greenside splash vs a full fairway-bunker shot.
    assert m.factor("sand", 13) == 0.74 and m.factor("sand", 160) == 0.90
    assert 0.78 < m.factor("sand", 50) < 0.79  # greenside bunkers play shorter than fairway ones
    assert m.factor("deep rough", 100) == m.factor("deep rough", 170) == 0.72
    assert CarryModel({"rough": 0.9}).factor("rough", 150) == 0.9  # config override


def test_carry_model_learns_course_baseline_and_lie_bands():
    from commentator.carry_model import CarryModel

    m = CarryModel(course_altitude_ft={"Denver CC": 5000})
    assert m.baseline("Denver CC") == 1.1  # rule-of-thumb seed until shots arrive
    for raw, gspro in ((250, 265), (160, 170), (200, 212)):  # clean lies at a course playing ~6% long
        m.observe("fairway", raw, gspro, "Hill Top")
    assert 1.05 < m.baseline("Hill Top") < 1.07
    # Rough on that course: GSPro carries 0.9 x 1.06; the learned penalty is the 0.9 part.
    for raw in (150, 155, 160):
        m.observe("rough", raw, raw * 0.9 * m.baseline("Hill Top"), "Hill Top", vla=18)
    assert 0.89 < m.penalty("rough", 155, 18) < 0.91
    assert m.penalty("rough", 250, 10) == 0.91  # other bands keep the default until they have data
    assert 0.94 < m.factor("rough", 155, "Hill Top", 18) < 0.97
    m.observe("rough", 150, 20, "Hill Top")  # mis-read tile ignored
    assert 0.89 < m.penalty("rough", 155, 18) < 0.91
    rebuilt = CarryModel(history=[("rough", r, r * 0.8, None, 18) for r in (150, 155, 160)])
    assert 0.79 < rebuilt.penalty("rough", 155, 18) < 0.81  # last week's shots carry over


def test_carry_is_leveled_by_where_the_ball_came_to_rest(tmp_path):
    from commentator.store import Store

    st = Store(str(tmp_path / "season.db"))

    def hit(hole, n, carry, game, to_pin, elev, finish=None, fmt=None, shared=False):
        sid = st.record_shot(player="Mike", hole=hole, shot=Shot(club="7I", carry=carry),
                             before=Hud(lie="fairway", shot_number=n, to_pin=to_pin, elevation_yards=elev),
                             after=Hud(), tags=[], carry_game_yards=game, total_yards=None, offline_yards=None,
                             fmt=fmt, shared_ball=shared)
        if finish is not None:
            st.set_shot_result(sid, finish_yards=finish)

    # A drive 300 out, pin 4 yards up: it rested 6 yards below the tee (pin 10 above the next ball),
    # so its 240 game carry played like 234 on level ground.
    hit("Hole 1", 1, 240.0, 240.0, "300 yards", 4.0)
    hit("Hole 1", 2, 60.0, 60.0, "60 yards", 10.0)
    hit("Hole 2", 1, 240.0, 230.0, "400 yards", 2.0)   # no next stroke and far from the pin: unknown
    hit("Hole 3", 1, 150.0, 146.0, "150 yards", 3.0, finish=0.0)   # holed: rested at the pin
    games = [(round(c), round(g)) for _, c, g, *_ in st.carry_pairs()]
    assert games == [(240, 234), (60, 70), (150, 149)]
    # A shot left out of the stats doesn't train the model, but still says where the drive rested.
    st.set_left_out(2, True)
    assert [(round(c), round(g)) for _, c, g, *_ in st.carry_pairs()] == [(240, 234), (150, 149)]
    st.set_left_out(2, False)
    # Nor does a ball knocked down by a tree: its short carry isn't the lie's or the elevation's doing.
    st.db.execute("UPDATE shots SET tree_hits = 1 WHERE id = 4")
    assert [(round(c), round(g)) for _, c, g, *_ in st.carry_pairs()] == [(240, 234), (60, 70)]
    st.db.execute("UPDATE shots SET tree_hits = NULL WHERE id = 4")

    # A later round's second shot on hole 2 says nothing about where this round's tee shot rested.
    hit("Hole 2", 2, 60.0, 60.0, "60 yards", 10.0)
    hit("Hole 4", 1, 240.0, 240.0, "400 yards", 4.0, fmt="scramble")   # the next ball may be a partner's
    hit("Hole 4", 2, 60.0, 60.0, "60 yards", 10.0, fmt="scramble")
    hit("Hole 5", 1, 240.0, 240.0, "400 yards", 4.0)   # a shamble, only known once GSPro copied a ball
    hit("Hole 5", 2, 60.0, 60.0, "60 yards", 10.0, shared=True)
    hit("Hole 6", 1, 240.0, 240.0, "400 yards", 4.0, finish=60.0, fmt="scramble")   # the team's pick:
    hit("Hole 6", 2, 60.0, 60.0, "60 yards", 10.0, fmt="scramble")                   # the next is this ball
    assert [(round(c), round(g)) for _, c, g, *_ in st.carry_pairs()][3:] == [(60, 70)] * 3 + [(240, 234), (60, 70)]

    # Rows are written as shot handlers finish: the approach can be stored before the drive.
    st2 = Store(str(tmp_path / "late.db"))
    for n, ts, game, pin, elev in ((2, 2.0, 80.0, "80 yards", 10.0), (1, 1.0, 240.0, "400 yards", 4.0),
                                   (3, 3.0, None, "5 yards", 2.0)):
        st2.record_shot(player="Mike", hole="Hole 1", shot=Shot(club="7I", carry=game or 1.0, timestamp=ts),
                        before=Hud(lie="fairway", shot_number=n, to_pin=pin, elevation_yards=elev),
                        after=Hud(), tags=[], carry_game_yards=game, total_yards=None, offline_yards=None)
    assert [(round(c), round(g)) for _, c, g, *_ in st2.carry_pairs()] == [(240, 234), (80, 88)]


def test_a_live_shot_learns_its_landing_height_from_the_next_stroke():
    from types import SimpleNamespace

    seen = []
    eng = SimpleNamespace(carry=SimpleNamespace(observe=lambda *a: seen.append(a[2])), _next_stroke=Engine._next_stroke)
    shot = ("fairway", 240.0, 240.0, "Wynn", 12.0, "gspro")
    Engine._learn_carry(eng, (1, "Hole 1", 1), 4.0, 320.0, shot, next_key=(1, "Hole 1", 2), next_elevation=10.0)
    Engine._learn_carry(eng, (1, "Hole 1", 1), 4.0, 320.0, shot, next_key=(1, "Hole 1", 3), next_elevation=10.0)
    Engine._learn_carry(eng, (1, "Hole 1", 1), 4.0, 320.0, shot)
    assert seen == [234.0]   # a penalty or round end in between: the landing is unknown, so nothing learned


def test_carry_waits_for_the_next_stroke_even_when_shot_handlers_finish_out_of_order():
    from types import SimpleNamespace

    OWN = (False, 0)   # not a shared-ball format; no balls dropped
    seen = []
    eng = SimpleNamespace(_carry_trail={}, _learn_carry=lambda *a: seen.append(a), CARRY_TRAIL=Engine.CARRY_TRAIL,
                          _next_stroke=Engine._next_stroke,
                          _own_ball_next=Engine._own_ball_next)
    shot = ("fairway", 240.0, 240.0, "Wynn", 12.0, "gspro")
    Engine._trail_carry(eng, "Mike", (2, (1, "Hole 1", 2), 10.0, 60.0, None, OWN))
    assert not seen
    Engine._trail_carry(eng, "Mike", (1, (1, "Hole 1", 1), 4.0, 300.0, shot, OWN))
    assert seen == [((1, "Hole 1", 1), 4.0, 300.0, shot, (1, "Hole 1", 2), 10.0)]
    seen.clear()
    Engine._trail_carry(eng, "Mike", (5, (1, "Hole 1", 1), 4.0, 300.0, shot, OWN))   # a replay, out of order:
    Engine._trail_carry(eng, "Mike", (7, (1, "Hole 1", 3), 2.0, 20.0, None, OWN))    # 3 before 2
    assert not seen
    Engine._trail_carry(eng, "Mike", (6, (1, "Hole 1", 2), 10.0, 60.0, None, OWN))
    assert seen == [((1, "Hole 1", 1), 4.0, 300.0, shot, (1, "Hole 1", 2), 10.0)]
    seen.clear()
    Engine._trail_carry(eng, None, (3, (1, "Hole 2", 1), 4.0, 300.0, shot, (True, None)))   # unnamed
    Engine._trail_carry(eng, None, (4, (1, "Hole 2", 2), 10.0, 60.0, None, (True, None)))
    assert seen == [((1, "Hole 2", 1), 4.0, 300.0, shot)]   # maybe another ball: near-pin rule only
    seen.clear()
    Engine._trail_carry(eng, "Mike", (8, (1, "Hole 3", 1), 4.0, 300.0, shot, OWN))
    Engine._trail_carry(eng, "Mike", (9, (1, "Hole 3", 2), 10.0, 60.0, None, (True, 1)))   # scramble shows late:
    assert seen == [((1, "Hole 3", 1), 4.0, 300.0, shot)]                                 # a partner's ball
    seen.clear()
    Engine._trail_carry(eng, "Mike", (31, (1, "Hole 6", 1), 4.0, 300.0, shot, (False, None)))   # no GSPro index
    Engine._trail_carry(eng, "Mike", (32, (1, "Hole 6", 2), 10.0, 60.0, None, (False, None)))
    assert seen[-1] == ((1, "Hole 6", 1), 4.0, 300.0, shot, (1, "Hole 6", 2), 10.0)
    seen.clear()
    Engine._trail_carry(eng, "Mike", (21, (1, "Hole 5", 1), 4.0, 300.0, shot, (True, 1)))
    Engine._trail_carry(eng, "Mike", (22, (1, "Hole 5", 2), 10.0, 60.0, None, (True, 1)))   # Mike's was picked
    assert seen == [((1, "Hole 5", 1), 4.0, 300.0, shot, (1, "Hole 5", 2), 10.0)]
    seen.clear()
    eng._carry_trail.clear()
    Engine._trail_carry(eng, "Mike", (12, (2, "Hole 1", 2), 10.0, 60.0, shot, OWN))   # 2, 3, then 1
    Engine._trail_carry(eng, "Mike", (13, (2, "Hole 1", 3), 2.0, 20.0, None, OWN))
    Engine._trail_carry(eng, "Mike", (11, (2, "Hole 1", 1), 4.0, 300.0, shot, OWN))
    assert [a[4:] for a in seen] == [((2, "Hole 1", 3), 2.0), ((2, "Hole 1", 2), 10.0)]


def _backdate(store, date="2000-01-01"):
    store.db.execute("UPDATE sessions SET date = ? WHERE id = ?", (date, store.session_id))
    store.db.commit()


def test_store_keeps_the_season(tmp_path):
    from commentator.models import Line
    from commentator.store import Store

    db = str(tmp_path / "season.db")
    week1 = Store(db)
    drive = Shot(club="DR", carry=250.0, ball_speed=160, hla=0)
    week1.record_shot(player="Mike", hole="Hole 1", shot=drive, before=Hud(lie="tee"), after=Hud(lie="fairway"),
                      tags=[], carry_game_yards=262.0, total_yards=280.0, offline_yards=3.0)
    week1.record_shot(player="Mike", hole="Hole 2", shot=Shot(club="7I", carry=160.0),
                      before=Hud(lie="rough", to_pin="165 yards", elevation_yards=0.0),
                      after=Hud(lie="green"), tags=[], carry_game_yards=152.0, total_yards=158.0, offline_yards=1.0)
    week1.record_lines("shot", [Line("pbp", "Lovely.")], 1)
    _backdate(week1)
    week1.close()

    week2 = Store(db)
    stats = week2.season_stats("Mike")
    assert stats["longest_drive"] == 280.0 and stats["nights"] == 1   # total distance, not carry
    assert week2.season_longest_drive() == ("Mike", 280.0)
    assert ("rough", 160.0, 152.0, None, None, "lm") in week2.carry_pairs()
    board = week2.leaderboard()[0]
    assert board["player"] == "Mike" and board["fairway_pct"] == 100


def test_store_keeps_club_delivery_and_backfills_older_files(tmp_path):
    from commentator.store import CLUB_COLS, Store

    measured = {"BallData": {"Speed": 106.6, "VLA": 21.0, "CarryDistance": 158.0},
                "ClubData": {"Speed": 74.9, "AngleOfAttack": -4.2, "FaceToTarget": -0.5, "Path": 1.3,
                             "VerticalFaceImpact": -7.6, "HorizontalFaceImpact": 8.1, "Loft": 25.7},
                "ShotDataOptions": {"ContainsBallData": True, "ContainsClubData": True}}
    # No club markers: the monitor still sends zeros, which must not read as a square face.
    unmeasured = {**measured, "ClubData": {"Speed": 0.0, "Path": 0.0, "AngleOfAttack": 0.0},
                  "ShotDataOptions": {"ContainsBallData": True, "ContainsClubData": False}}
    db = str(tmp_path / "season.db")
    st = Store(db)
    for msg in (measured, unmeasured):
        st.record_shot(player="Mike", hole="Hole 1", shot=Shot.from_gspro(msg), before=Hud(), after=Hud(),
                       tags=[], carry_game_yards=None, total_yards=None, offline_yards=None)
    cols = ", ".join(CLUB_COLS)
    expected = [(1.3, -4.2, -0.5, 25.7, 8.1, -7.6), (None,) * 6]
    assert [tuple(r) for r in st.db.execute(f"SELECT {cols} FROM shots ORDER BY id")] == expected
    # A season.db from before these columns existed.
    st.db.execute("ALTER TABLE shots RENAME TO shots_new")
    old_cols = [r["name"] for r in st.db.execute("PRAGMA table_info(shots_new)") if r["name"] not in CLUB_COLS]
    st.db.execute(f"CREATE TABLE shots AS SELECT {', '.join(old_cols)} FROM shots_new")
    st.db.execute("DROP TABLE shots_new")
    st.close()
    reopened = Store(db)
    assert [tuple(r) for r in reopened.db.execute(f"SELECT {cols} FROM shots ORDER BY id")] == expected


def test_driving_records_count_only_tee_shots(tmp_path):
    from commentator.store import Store

    db = str(tmp_path / "season.db")
    old = Store(db)
    old.record_shot(player="Mike", hole="Hole 1", shot=Shot(club="DR", carry=240.0), before=Hud(lie="tee", shot_number=1),
                    after=Hud(lie="fairway"), tags=[], carry_game_yards=240.0, total_yards=260.0, offline_yards=1.0)
    old.record_shot(player="Mike", hole="Hole 1", shot=Shot(club="DR", carry=200.0),
                    before=Hud(lie="fairway", shot_number=2), after=Hud(lie="green"), tags=[],
                    carry_game_yards=200.0, total_yards=280.0, offline_yards=1.0)   # driver off the deck
    putt = Shot(club="DR", ball_speed=3.9)
    putt.putter = True                                                              # a putt the HUD read as DR
    old.record_shot(player="Mike", hole="Hole 2", shot=putt, before=Hud(lie="fairway", shot_number=2),
                    after=Hud(lie="green"), tags=[], carry_game_yards=0.0, total_yards=2.2, offline_yards=None)
    _backdate(old)
    old.close()
    stats = Store(db).season_stats("Mike")
    assert stats["avg_drive"] == 260.0 and stats["longest_drive"] == 260.0


async def test_season_best_drive_is_called_out(tmp_path):
    from commentator.store import Store

    db = str(tmp_path / "season.db")
    old = Store(db)
    old.record_shot(player="Mike", hole="Hole 1", shot=Shot(club="DR", carry=280.0), before=Hud(lie="tee"),
                    after=Hud(lie="fairway"), tags=[], carry_game_yards=290.0, total_yards=None, offline_yards=None)
    old.record_shot(player="Tony", hole="Hole 1", shot=Shot(club="DR", carry=240.0), before=Hud(lie="tee"),
                    after=Hud(lie="rough"), tags=[], carry_game_yards=245.0, total_yards=None, offline_yards=None)
    _backdate(old)
    old.close()
    cfg = Config(log_dir=str(tmp_path))
    cfg.tts.console_simulate_duration = False
    cfg.commentary.routine_comment_rate = 1.0
    cfg.commentary.min_seconds_between_comments = 0
    llm = FakeLLM(cfg.llm.live)
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"),
                    source=ReplaySource("data/sample_shots.jsonl", speed=1e6), live_llm=llm, segment_llm=llm,
                    speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(0), store=Store(db))
    await engine.run()
    reactions = [p for p in llm.prompts if p.startswith("FACTS:")]
    tony = next(p for p in reactions if "Player: Tony" in p and "driver" in p)
    assert "new personal best: longer than Tony's longest drive from earlier weeks (245 yards)" in tony
    mike = next(p for p in reactions if "Player: Mike" in p and "driver" in p)
    assert "new personal best: longer than Mike's longest drive from earlier weeks (290 yards)" in mike
def test_slope_and_elevation_parsing():
    from commentator.hud import parse_elevation, parse_slope

    assert parse_slope(["4.7° UP", "3.3º LEFT"]) == {"slope_updown": 4.7, "slope_side": "left", "slope_side_degrees": 3.3}
    assert parse_slope(["0.8°", "DOWN", "0.8° LEFT"])["slope_updown"] == -0.8
    assert parse_elevation("▼11.7YDS", metric=False, arrow=None) == -11.7  # glyph read by OCR
    assert parse_elevation("4.8 YDS", metric=False, arrow="up") == 4.8
    assert parse_elevation("4.8 YDS", metric=False, arrow=None) is None  # direction unknown: don't guess
    assert round(parse_elevation("▼2.6", metric=True, arrow=None), 2) == -2.84  # metres
    assert round(parse_elevation("50", metric=True, arrow="up"), 2) == 0.55  # centimetres
    assert parse_elevation("▲4FT 6", metric=False, arrow="down") == 1.5  # glyph beats pixels
    assert parse_elevation("▲0'8", metric=False, arrow=None) == 8 / 36   # feet'inches
    assert parse_elevation("5IN", metric=False, arrow="down") == -5 / 36


def test_putt_elevation_is_spoken_in_inches_and_feet():
    from commentator.facts import elevation_facts
    from commentator.hud import Hud

    def facts(yards, lie="green"):
        return elevation_facts(Hud(lie=lie, elevation_yards=yards))

    assert facts(2 / 36) == []   # a couple of inches is flat enough
    assert facts(-5 / 36) == ["The putt is 5 inches downhill: the hole sits below the ball."]
    up = facts(46 / 36)
    assert up[0].startswith("The putt is 3 feet 10 inches uphill: the hole sits above the ball. That's a big climb")
    assert facts(1.5)[0].startswith("The putt is 4 feet 6 inches uphill")
    # Off the green it's still yards, from 2 up.
    assert facts(1.5, "fairway") == []
    assert facts(4.8, "fairway") == ["The pin is about 5 yards uphill from the ball."]


def test_elevation_changes_the_projection():
    from commentator.flight_pick import project

    wedge = Shot(club="SW", carry=100, hla=0)
    assert project(wedge, "100 yards", []).outcome == "on_target"
    assert project(wedge, "100 yards", [], elevation_yards=18).outcome == "short"
    assert project(wedge, "100 yards", [], elevation_yards=-12).outcome == "long"


def test_length_call_counts_roll_out_and_a_softer_uphill():
    from commentator.flight_pick import project

    # 8-iron, 131.5 carry at a flat 143.5-yard pin: rolled out to 141 and finished 9 feet away.
    eight = Shot(club="8I", carry=131.5, total_spin=6336, vla=27.8, hla=0)
    assert project(eight, "143.5 yards", [], elevation_yards=-1.4).outcome == "on_target"
    # Lob wedge from the rough to a pin 6.1 yards uphill at 66.1: finished on the green 13 feet away.
    lob = Shot(club="LW", carry=70.8, total_spin=10885, vla=32.9, hla=0)
    assert project(lob, "66.1 yards", [], carry_factor=0.908, elevation_yards=6.1).outcome == "on_target"
    # Still short when it's clearly short, roll and all.
    assert project(Shot(club="7I", carry=130, total_spin=7000, vla=18, hla=0), "160 yards", []).outcome == "short"


def test_slope_facts_follow_gspro_definitions():
    from commentator.facts import slope_facts

    hud = Hud(slope_updown=4.7, slope_side="left", slope_side_degrees=3.3)
    rh = " ".join(slope_facts(hud, "RH"))
    assert "uphill lie" in rh and "above the player's feet" in rh and "start it left" in rh
    assert "below the player's feet" in " ".join(slope_facts(Hud(slope_side="right", slope_side_degrees=4), "RH"))
    lh = " ".join(slope_facts(hud, "LH"))
    assert "tilted 3 degrees sideways" in lh and "feet" not in lh  # undocumented for lefties
    assert slope_facts(Hud(slope_updown=0.8, slope_side="left", slope_side_degrees=1.3), "RH") == []


def _timeline(start: float, end: float) -> list[Hud]:
    """HUD reads OCR'd from a real GSPro 3.1.6 round video at 3 frames a second (Palmer Hills,
    Mixtape Golf, 2026-01-31; see data/screens/SOURCES.md)."""
    import csv

    huds = []
    for r in csv.DictReader(open("tests/data/timeline_palmer_hills.csv")):
        if not start <= float(r["t"]) <= end:
            continue
        tiles = {k: r[c] for k, c in (("total length", "total"), ("carry (raw)", "carry_raw"),
                                      ("carry (game)", "carry_game")) if r[c]}
        shot = int(r["shot"]) if r["shot"] else None
        huds.append(Hud(player_text="Mixtape_Golf" if shot else None, shot_number=shot,
                        to_pin=r["to_pin"] or None, lie=r["lie"] or None, tiles=tiles))
    return huds


async def _follow(frames: list[Hud], before: Hud, tmp_path, max_seconds: float = 25, roll_seconds: float = 8):
    reads = iter(frames)

    class VideoScreen:
        async def read_hud_async(self, parts):
            return next(reads)

    cfg = Config(log_dir=str(tmp_path))
    cfg.screen.follow_poll_seconds = 0.05
    cfg.screen.follow_max_seconds = max_seconds
    cfg.screen.follow_roll_seconds = roll_seconds
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, screen=VideoScreen())
    return await engine._follow_shot(before)


async def test_follow_shot_on_real_gspro_drive(tmp_path):
    # Impact 51.3 s: tiles count up in flight, touchdown in the rough at ~58.7 (carry (game) freezes
    # at 271.0, ball still rolling), ball rolls out to rest by ~60.7. The follow now reads the ball at
    # REST, not at touchdown, so it returns the resting lie (rough), the final carry (271.0, learned)
    # and the settled total length (278.2, up from the 273.3 it was still at mid-roll).
    before = Hud(player_text="Mixtape_Golf", shot_number=1, to_pin="359.1 yards", lie="tee")
    hud, landed = await _follow(_timeline(51.3, 64), before, tmp_path)
    assert landed and hud.shot_number == 1
    assert hud.lie == "rough" and hud.tiles["carry (game)"] == "271.0" and hud.tiles["total length"] == "278.2"


async def test_follow_shot_on_real_gspro_putt(tmp_path):
    # Putt at 101.3 s; within 4 s GSPro has moved to the next hole's tee. That tee lie must not
    # be credited to the putt.
    before = Hud(player_text="Mixtape_Golf", shot_number=3, to_pin="10 feet 8 inches", lie="green")
    hud, landed = await _follow(_timeline(101.3, 107), before, tmp_path)
    assert landed and hud.shot_number == 3 and hud.lie == "green" and hud.tiles["total length"] == "3.5"


async def test_follow_shot_keeps_following_a_long_roll_after_touchdown(tmp_path):
    # A long drive lands late and is still rolling at follow_max_seconds: the follow runs on, up to
    # follow_roll_seconds after touchdown, and reads the lie where it stops.
    def f(lie, carry, total):
        return Hud(player_text="P", shot_number=1, lie=lie,
                   tiles={"carry (game)": str(carry), "total length": str(total)})
    frames = [f("fairway", 200, 200), f("fairway", 224, 224), f("fairway", 224, 226)]
    frames += [f("fairway", 224, 226 + i) for i in range(1, 25)]    # ~1.2 s of roll at 0.05 s polls
    frames += [f("fairway", 224, 250)] * 5
    before = Hud(player_text="P", shot_number=1, lie="tee")
    reads = iter(frames)

    class VideoScreen:
        async def read_hud_async(self, parts):
            return next(reads)

    cfg = Config(log_dir=str(tmp_path))
    cfg.screen.follow_poll_seconds = 0.05
    cfg.screen.follow_max_seconds = 0.3
    cfg.screen.follow_roll_seconds = 5
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, screen=VideoScreen())
    hud, landed = await engine._follow_shot(before)
    assert landed and hud.lie == "fairway" and hud.tiles["total length"] == "250"


async def test_follow_shot_stops_when_the_log_says_the_chip_ended_the_hole(tmp_path):
    # Dan's chip on Kiawah 15: conceded, GSPro moved on with no touchdown or rest ever seen, and the
    # follow sat out its 12 s cap. The log's gimmie ends it at once, naming no mid-roll lie.
    frame = Hud(player_text="P", shot_number=3, lie="deep rough", tiles={"carry (game)": "9.0"})
    reads = 0

    class StuckScreen:
        async def read_hud_async(self, parts):
            nonlocal reads
            reads += 1
            return frame

    cfg = Config(log_dir=str(tmp_path))
    cfg.screen.follow_poll_seconds = 0.05
    cfg.screen.follow_max_seconds = 12
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, screen=StuckScreen())
    started = time.monotonic()
    hud, landed = await engine._follow_shot(Hud(player_text="P", shot_number=3, lie="deep rough"),
                                            ended=lambda: reads >= 3)
    assert time.monotonic() - started < 1 and reads == 3
    assert not landed and hud.lie is None


async def test_follow_shot_stops_when_the_log_has_the_ball_at_rest(tmp_path):
    # Dan's 28-yard mishit: the screen never showed a landing, so the follow sat out its 12 s cap
    # though the ball stopped at once. GSPro logging the roll's end stops it at the next read, which
    # is at rest, so its lie is named and (the tiles having climbed: this shot's) its carry learnable.
    frames = iter([Hud(player_text="P", shot_number=1, lie="rough", tiles={"carry (game)": c, "total length": c})
                   for c in ("15.0", "20.6", "28.1", "28.1", "28.1", "28.1")])
    reads = 0

    class Screen:
        async def read_hud_async(self, parts):
            nonlocal reads
            reads += 1
            return next(frames)

    cfg = Config(log_dir=str(tmp_path))
    cfg.screen.follow_poll_seconds = 0.05
    cfg.screen.follow_max_seconds = 12
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, screen=Screen())
    hud, landed = await engine._follow_shot(Hud(player_text="P", shot_number=1, lie="tee"),
                                            at_rest=lambda: reads >= 3)
    # Logged during the third read: that capture may predate the stop, so the fourth is the one used.
    assert reads == 4 and landed and hud.lie == "rough" and hud.tiles["total length"] == "28.1"


async def test_follow_shot_reads_the_resting_lie_not_the_landing_lie(tmp_path):
    # Lands on the fairway, then releases into the rough and stops there. The follow must report the
    # RESTING lie (rough), not the lie at touchdown (fairway): that is the whole point of following to
    # rest. carry (game) freezes at touchdown and is still the value returned (so learning is correct).
    def f(lie, carry, total):
        return Hud(player_text="P", shot_number=1, lie=lie,
                   tiles={"carry (game)": str(carry), "total length": str(total)})
    frames = [
        f("fairway", 170, 170), f("fairway", 180, 180),  # climbing
        f("fairway", 180, 182),                            # touchdown: carry (game) frozen, landed on fairway
        f("rough", 180, 190), f("rough", 180, 195),        # releasing into the rough
        f("rough", 180, 195), f("rough", 180, 195),        # total steady: at rest in the rough
    ]
    before = Hud(player_text="P", shot_number=1, lie="fairway")
    hud, landed = await _follow(frames, before, tmp_path)
    assert landed and hud.lie == "rough" and hud.tiles["carry (game)"] == "180"


async def test_follow_shot_gives_up_without_a_stop(tmp_path):
    before = Hud(player_text="Mixtape_Golf", shot_number=1, lie="tee")
    frames = [Hud(player_text="Mixtape_Golf", shot_number=1, lie="tee", tiles={"total length": str(10.0 * i)})
              for i in range(1, 200)]
    hud, landed = await _follow(frames, before, tmp_path, max_seconds=5)
    assert not landed


def test_read_region_lines_cached_only_ocrs_changed_regions():
    # The setup-watcher speedup: a slot whose pixels didn't change since last poll is NOT re-OCR'd, so
    # a quick single-slot roster change reads on its own instead of re-reading all eight every poll.
    from PIL import Image
    from commentator.screen import Screen
    from commentator.config import ScreenConfig, Region

    scr = Screen(ScreenConfig(regions={"a": Region(left=0, top=0, width=50, height=50),
                                        "b": Region(left=50, top=0, width=50, height=50)}))
    calls: list[int] = []
    scr.ocr_items = lambda img: (calls.append(1), [])[1]  # count OCR calls, return no text
    img = Image.new("RGB", (100, 50), "black")
    _, cache = scr.read_region_lines_cached(["a", "b"], img, {})
    assert len(calls) == 2            # cold cache: both regions OCR'd
    _, cache = scr.read_region_lines_cached(["a", "b"], img, cache)
    assert len(calls) == 2            # same frame: nothing re-OCR'd
    img2 = img.copy()
    img2.paste((255, 255, 255), (50, 0, 100, 50))  # change region b only
    _, cache = scr.read_region_lines_cached(["a", "b"], img2, cache)
    assert len(calls) == 3            # only region b re-read


def _mini_engine(tmp_path, live_llm=None):
    cfg = Config(log_dir=str(tmp_path))
    return Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                  live_llm=live_llm or FakeLLM(cfg.llm.live), segment_llm=None, speech=None, screen=None)


def test_system_prompt_variant_follows_the_round(tmp_path):
    e = _mini_engine(tmp_path)

    class R:
        def __init__(self, is_league, ended=False):
            self.is_league, self.ended = is_league, ended

    assert e.system is e._system_practice              # no round -> practice (never invents stakes)
    e.round = R(is_league=True)
    assert e.system is e._system_league
    e.round = R(is_league=True, ended=True)
    assert e.system is e._system_practice              # an ENDED league round -> practice, not league
    e.round = R(is_league=False)
    assert e.system is e._system_practice


async def test_compose_threads_the_given_system_prompt(tmp_path):
    # The opener/closer force their round's variant because they compose before (opener) self.round is set.
    captured = []

    class CapturingLLM(FakeLLM):
        async def complete_json(self, system, user, schema, images=None):
            captured.append(system)
            return await super().complete_json(system, user, schema, images)

    cfg = Config(log_dir=str(tmp_path))
    e = _mini_engine(tmp_path, live_llm=CapturingLLM(cfg.llm.live))
    e.round = None  # the front-loaded opener composes before the round exists
    await e._compose("opener", e.live_llm, "Player: Alex", "", [], mode="round", system=e._system_league)
    assert captured and captured[0] is e._system_league   # the explicit variant wins over the property


async def test_wait_putt_result_distinguishes_miss_from_unknown(tmp_path):
    e = _mini_engine(tmp_path)

    class G:
        outs = gim = 0
        shot = 1

        def hole_outs_of(self, idx):
            return self.outs

        def gimmies_of(self, idx):
            return self.gim

        def shot_number(self, idx):
            return self.shot

    e.gspro = G()
    e.gspro.shot = 2                                            # the stroke count advanced: a real miss
    assert await e._wait_putt_result(0, 0, 0, base_shot=1, window=0.3) == "missed"
    e.gspro.shot = 1                                            # nothing advanced in the window
    assert await e._wait_putt_result(0, 0, 0, base_shot=1, window=0.05) == "unknown"
    e.gspro.outs = 1                                            # holed out
    assert await e._wait_putt_result(0, 0, 0, base_shot=1, window=0.3) == "holed"


async def test_finish_distance_comes_from_the_log_and_never_a_stale_one(tmp_path):
    from commentator.sources.gspro_log import GsproState
    e = _mini_engine(tmp_path)
    e.gspro = GsproState()
    e.gspro.to_pin_by[0] = 180.0                                # metres to the pin at impact
    assert await e._wait_finish_yards(0, 180.0, 0.05) is None   # no new tdist yet: not this shot's
    e.gspro.to_pin_by[0] = 1.5                                  # the ball settled ~5 yards away
    assert abs(await e._wait_finish_yards(0, 180.0, 0.05) - 1.5 * 1.0936133) < 0.01


def test_prewritten_putt_reactions_may_quote_the_putt_length_only(tmp_path):
    e = _mini_engine(tmp_path)
    data = {"reaction_lines": {"holed": "Drains it from 28 feet!", "short": "Leaves it 3 feet short.",
                               "past": "Runs it by."}}
    out = e._build_reactions(data, True, "Putt length: 28 feet 4 inches.")
    assert out["holed"] == "Drains it from 28 feet!"
    assert "short" not in out                                   # 3 feet is a guess, not a fact
    assert out["past"] == "Runs it by."


def test_prewritten_putt_reactions_name_the_comeback_for_the_next_score(tmp_path):
    from commentator.facts import preshot_facts
    e = _mini_engine(tmp_path)
    facts = "\n".join(preshot_facts(Hud(shot_number=4, lie="green", to_pin="39 feet 3 inches"), 4))
    assert "the next putt is for bogey" in facts
    data = {"reaction_lines": {"holed": "Josh drains it for par!",
                               "past": "Josh sends it past; the comeback putt for par remains.",
                               "short": "Leaves it short, with a comeback for bogey.",
                               "gimme": "That was for par, and it slides by."}}
    out = e._build_reactions(data, True, facts)
    assert out["holed"] == "Josh drains it for par!"
    assert "past" not in out
    assert out["short"] == "Leaves it short, with a comeback for bogey."
    assert out["gimme"] == "That was for par, and it slides by."   # the putt just missed
    from commentator.facts import wrong_putt_score
    assert wrong_putt_score("The putt for par slides left.", "par", "bogey", holed=False) is None
    assert wrong_putt_score("Misses for par; a tap-in for bogey remains.", "par", "bogey", holed=False) is None
    assert wrong_putt_score("Left with a tester for par.", "par", "bogey", holed=False) == "par"
    assert wrong_putt_score("Leaves the putt for par short.", "par", "bogey", holed=False) is None
    assert wrong_putt_score("His second putt for par slides past.", "par", "bogey", holed=False) is None
    assert wrong_putt_score("Leaves himself a putt for par.", "par", "bogey", holed=False) == "par"


async def test_follow_shot_moves_on_at_the_next_player_placeholder(tmp_path):
    # The next player's address HUD shows carry AND total as "--.--" (non-empty tiles, both parse to
    # None). The follow must read that reset as moved-on and return THIS shot's last read, never theirs.
    def f(lie, carry, total):
        return Hud(player_text="P", shot_number=1, lie=lie,
                   tiles={"carry (game)": str(carry), "total length": str(total)})
    placeholder = {"carry (game)": "--.--", "total length": "--.--"}
    frames = [
        f("fairway", 150, 150), f("fairway", 160, 160), f("fairway", 160, 162),  # climb then touchdown
        f("fairway", 160, 170), f("fairway", 160, 175),                           # rolling out (moving)
        Hud(player_text="Q", shot_number=2, lie="tee", tiles=placeholder),        # next player's address
        Hud(lie="tee", tiles=placeholder),
    ]
    before = Hud(player_text="P", shot_number=1, lie="fairway")
    hud, landed = await _follow(frames, before, tmp_path)
    assert landed and hud.lie == "fairway"   # the last this-shot read, not the next player's "tee"


def _ct(lie, carry, total):
    return Hud(player_text="P", shot_number=1, lie=lie,
               tiles={"carry (game)": str(carry), "total length": str(total)})


async def test_follow_shot_stops_when_the_top_name_switches(tmp_path):
    # Nate's approach (10:34, Riviera 10), from video: the ball stops on the green, ~1.3s later the
    # screen fades, and Leo's address appears with Nate's tiles STILL showing (84.8 / 92.0) under
    # Leo's "rough". Same total, so the tiles can't see the switch; the top name cell can.
    nate, leo = (True, False) * 8, (False, True) * 8
    def f(lie, total, sig=nate, carry=84.8):
        h = _ct(lie, carry, total)
        h.name_sig = sig
        return h
    frames = [f("fairway", 60.0, carry=60.0), f("green", 84.8), f("green", 89.8), f("green", 92.0),
              f("rough", 92.0, sig=leo), f("rough", 92.0, sig=leo)]
    before = Hud(player_text="Nate", shot_number=2, lie="deep rough", name_sig=nate)
    hud, landed = await _follow(frames, before, tmp_path)
    assert landed and hud.lie == "green" and hud.tiles["total length"] == "92.0"
    # The fade between players (a dark name cell) counts as the switch too.
    from commentator.hud import NAME_DARK
    frames = [f("fairway", 60.0, carry=60.0), f("green", 84.8), f("green", 92.0), f(None, 92.0, sig=NAME_DARK),
              f("rough", 92.0, sig=leo), f("rough", 92.0, sig=leo)]
    hud, landed = await _follow(frames, before, tmp_path)
    assert landed and hud.lie == "green"


async def test_a_lie_read_while_the_ball_still_rolls_off_the_green_is_not_named(tmp_path):
    # Premonition 2, 10-21: the box switched 2 s after touchdown with the ball still running on, read
    # "fairway"; it stopped in the rough. Off the green that read is dropped, the tiles kept.
    josh, next_up = (True, False) * 8, (False, True) * 8
    def f(lie, total, sig=josh, carry=250.0):
        h = _ct(lie, carry, total)
        h.name_sig = sig
        return h
    frames = [f("fairway", 240.0, carry=240.0), f("fairway", 255.0), f("fairway", 262.0),
              f("rough", 262.0, sig=next_up)]
    before = Hud(player_text="Josh", shot_number=1, lie="tee", name_sig=josh)
    hud, landed = await _follow(frames, before, tmp_path)
    assert landed and hud.lie is None and hud.tiles["total length"] == "262.0"


def test_name_signature_tells_players_apart_and_ignores_brightness():
    from PIL import Image, ImageDraw, ImageEnhance

    from commentator.hud import NAME_DARK, name_sig_changed
    from commentator.screen import name_signature

    def cell(name):
        im = Image.new("RGB", (450, 56), (5, 22, 24))
        ImageDraw.Draw(im).text((8, 10), name, fill=(250, 250, 250), font_size=36)
        return im
    toby, wes = name_signature(cell("Toby")), name_signature(cell("Wes"))
    dim_josh = name_signature(ImageEnhance.Brightness(cell("Toby")).enhance(0.85))
    assert not name_sig_changed(toby, dim_josh)
    assert name_sig_changed(toby, wes)
    assert name_signature(Image.new("RGB", (450, 56), (8, 8, 8))) == NAME_DARK
    assert name_sig_changed(toby, NAME_DARK) and not name_sig_changed(None, toby)

async def test_follow_shot_rejects_frozen_stale_tiles_as_touchdown(tmp_path):
    # The previous shot's carry (game) / total are already frozen and may still be on screen for the
    # first reads. Without a "seen it climb" guard, that reads as an instant touchdown and hands back
    # the hit-from lie + the last shot's carry. carry (game) never rises here, so it must NOT land.
    frames = [_ct("fairway", 240, 250)] * 40          # stale, frozen from the first read, never climbs
    hud, landed = await _follow(frames, Hud(player_text="P", shot_number=1, lie="tee"), tmp_path, max_seconds=0.5)
    assert not landed


async def test_follow_shot_still_learns_when_touchdown_seen_but_rest_times_out(tmp_path):
    # carry (game) climbs then freezes (touchdown, so the carry is final and learnable), but total
    # length keeps changing to the cap (a ball that never settles). landed must be True off touchdown.
    climbing = [_ct("fairway", 170, 170), _ct("fairway", 185, 180), _ct("rough", 200, 195)]
    rolling = [_ct("rough", 200, 200 + 10 * i) for i in range(1, 14)]   # carry frozen, total never steady
    hud, landed = await _follow(climbing + rolling, Hud(player_text="P", shot_number=1, lie="tee"),
                                tmp_path, max_seconds=0.5, roll_seconds=0.3)
    assert landed and hud.tiles["carry (game)"] == "200"


async def test_follow_shot_lands_a_frozen_chip_that_turned_over_from_the_baseline(tmp_path):
    # A short chip can come to rest before the follow's first read, so carry (game) is already frozen
    # and never climbs. It still lands quickly because the frozen value (18) differs from the impact
    # baseline (the previous shot's 240, still on screen at impact): the tile has turned over to this shot.
    before = _ct("fairway", 240, 250)
    frames = [_ct("rough", 18, 20)] * 10
    hud, landed = await _follow(frames, before, tmp_path, max_seconds=5)
    assert landed and hud.lie == "rough" and hud.tiles["carry (game)"] == "18"


async def test_follow_shot_rejects_stale_tiles_equal_to_the_impact_baseline(tmp_path):
    # The impact baseline already held the previous shot's 240 and the follow keeps reading 240: the
    # tile has NOT turned over to this shot, so it must not count as touchdown (the turnover guard only
    # fires on a value that differs from the baseline).
    before = _ct("fairway", 240, 250)
    frames = [_ct("fairway", 240, 250)] * 40
    hud, landed = await _follow(frames, before, tmp_path, max_seconds=0.5)
    assert not landed


def test_shot_wet_is_false_when_the_log_cant_be_attributed(tmp_path):
    cfg = Config(log_dir=str(tmp_path))
    cfg.source.gspro_log_truth = True
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None)
    # A splash is recorded for the active player in the log, but with no round the name can't be
    # mapped to a GSPro index, so the splash must NOT be borrowed and mis-credited to this player.
    engine.gspro.active_player = 0
    engine.gspro._wet_since_shot[0] = True
    assert engine._gspro_idx("anyone") is None
    assert engine._shot_wet("anyone") is False


async def test_lines_from_earlier_weeks_are_not_repeated(tmp_path):
    from commentator.models import Line
    from commentator.store import Store

    db = str(tmp_path / "season.db")
    week1 = Store(db)
    week1.record_lines("shot", [Line("color", "That is the kind of swing that buys the first round.")])
    week1.close()
    cfg = Config(log_dir=str(tmp_path))
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, store=Store(db))
    good, problems = engine._grounded(
        [Line("color", "Well, that is the kind of swing that buys the first round, Nigel."),
         Line("pbp", "A fresh line about a lovely strike.")], "Player: Tony")
    assert [l.text for l in good] == ["A fresh line about a lovely strike."]
    assert "already said this season" in problems[0]


async def test_a_valid_line_survives_a_siblings_rejection(tmp_path):
    from commentator.models import Line
    from commentator.store import Store

    cfg = Config(log_dir=str(tmp_path))
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, store=Store(str(tmp_path / "s.db")))
    # The first line is rejected (a fabricated number); the second is valid and is kept on its own,
    # even though it addresses Nigel. (The old "drop the reply" rule was eating good standalone lines.)
    good, _ = engine._grounded(
        [Line("pbp", "That carry of 999 yards was special."),
         Line("color", "Nine feet for par, Nigel, and the champions pile up points here.")],
        "Player: Tony\nCarry: 212 yards", mode="result")
    assert [l.text for l in good] == ["Nine feet for par, Nigel, and the champions pile up points here."]


def test_penalty_jump_in_shot_number_is_called_out(tmp_path):
    from commentator.store import Store

    cfg = Config(log_dir=str(tmp_path))
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, store=Store(str(tmp_path / "s.db")))
    # Normal progression says nothing.
    assert engine._penalty_facts("Toby", 1) == []
    assert engine._penalty_facts("Toby", 2) == []
    # Water ball then drop: GSPro jumps 2 -> 4, so stroke 3 is the penalty.
    facts = engine._penalty_facts("Toby", 4)
    assert len(facts) == 1
    assert "penalty" in facts[0].lower()
    assert "stroke 4" in facts[0] and "4th" in facts[0]
    # A later, in-order shot on the same hole is silent again.
    assert engine._penalty_facts("Toby", 5) == []


def test_penalty_tracking_is_per_player_and_resets_each_hole(tmp_path):
    from commentator.store import Store

    cfg = Config(log_dir=str(tmp_path))
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, store=Store(str(tmp_path / "s.db")))
    engine._penalty_facts("Toby", 2)
    # A different player's first shot is not a jump off Toby's count.
    assert engine._penalty_facts("Wes", 1) == []
    # A new hole clears the counts, so shot 1 on the next hole is not read as a drop from Toby's 2.
    engine._last_hud_shot = {}
    assert engine._penalty_facts("Toby", 1) == []


def test_a_tee_shot_never_reads_the_last_holes_penalty_from_the_log(tmp_path):
    from commentator.store import Store

    cfg = Config(log_dir=str(tmp_path))
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, store=Store(str(tmp_path / "s.db")))
    # The log still has the player on the hole before, with a penalty there, until they hit here.
    engine.gspro.current_hole, engine.gspro.hole_by[0] = 13, 13
    engine.gspro._penalties[(0, 13)] = 1
    engine._gspro_idx = lambda player: 0
    assert engine._penalty_facts("Dan", 1) == []
    assert "penalty" in engine._penalty_facts("Dan", 3)[0].lower()   # mid-hole, the log's count stands


def test_a_stale_shot_1_hud_after_a_scramble_pick_takes_the_logs_stroke(tmp_path):
    from commentator.hud import Hud
    e = _mini_engine(tmp_path)
    g = e.gspro
    g.team_scores, g.pick, g.current_hole, g.hole_by[1] = True, (0, 5), 5, 5
    g._score[(1, 5)], g._pick_stroke, g.to_pin_by[1], g.lie_by[1] = 3, 1, 262.1 / 1.0936133, None   # 3: a penalty on his unpicked ball
    hud = Hud(shot_number=1, to_pin="437.8 yards", lie="rough")
    e._fix_stale_pick_hud(hud, 1)
    assert hud.shot_number == 2 and hud.to_pin.startswith("262.") and hud.lie == "rough"
    g.hole_scores[(0, 5)] = 4   # the hole is over: shot 1 is the next tee, left alone
    tee = Hud(shot_number=1, to_pin="437.8 yards", lie="tee")
    e._fix_stale_pick_hud(tee, 1)
    assert tee.shot_number == 1 and tee.to_pin == "437.8 yards"


def test_a_stroke_played_on_is_not_holed(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))

    def rec(n, club="PT", player="Josh", hole="Hole 14, par 3, 158 yards", outcome=None):
        return s.record_shot(player=player, hole=hole, shot=Shot(club=club),
                             before=Hud(lie="green" if club == "PT" else "tee", shot_number=n), after=Hud(),
                             tags=[], carry_game_yards=None, total_yards=None, offline_yards=None, outcome=outcome)

    def outcome(i):
        return s.db.execute("SELECT outcome FROM shots WHERE id = ?", (i,)).fetchone()[0]

    tee = rec(1, "6I", outcome="holed", hole="par 3, 158 yards")   # stale hole-out; number read late
    other = rec(1, "DR", player="Dan")                              # another player's stroke in between
    putt = rec(2)
    assert outcome(tee) is None and outcome(other) is None
    s.set_shot_result(putt, outcome="holed")
    late = rec(1, "7I", hole="Hole 15, par 4, 400 yards")           # next hole: the putt stays holed
    assert outcome(putt) == "holed"
    # A holed result that arrives after the next stroke is already recorded.
    first = rec(2, hole="Hole 15, par 4, 400 yards")
    rec(3, hole="Hole 15, par 4, 400 yards")
    s.set_shot_result(first, outcome="holed")
    assert outcome(first) == "missed"
    # A mulligan's re-hit keeps the stroke number, so it doesn't undo the hole-out.
    s.set_shot_result(late, outcome="holed")
    ace = rec(1, "7I", hole="Hole 16, par 3, 150 yards", outcome="holed")
    rec(1, "7I", hole="Hole 16, par 3, 150 yards")
    assert outcome(ace) == "holed"


def test_a_scramble_shot_isnt_finished_from_the_next_stroke(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))

    def rec(n, club, yards):
        return s.record_shot(player="Josh", hole="Hole 1, par 4, 400 yards", shot=Shot(club=club),
                             before=Hud(lie="fairway", shot_number=n), after=Hud(), tags=[], carry_game_yards=None,
                             total_yards=None, offline_yards=None, to_pin_yards=yards, fmt="scramble")

    approach = rec(2, "8I", 150)
    rec(3, "PT", 2)   # played from the partner's ball, not where Josh's approach finished
    assert s.db.execute("SELECT finish_yards FROM shots WHERE id = ?", (approach,)).fetchone()[0] is None


def test_a_shot_finishes_where_the_next_stroke_starts(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.store import Store

    path = str(tmp_path / "s.db")
    s = Store(path)

    def rec(n, club, lie, yards, hole="Hole 1, par 4, 400 yards", outcome=None):
        return s.record_shot(player="Josh", hole=hole, shot=Shot(club=club), before=Hud(lie=lie, shot_number=n),
                             after=Hud(), tags=[], carry_game_yards=None, total_yards=None, offline_yards=None,
                             to_pin_yards=yards, gimme_yards=1.5, outcome=outcome)

    def finish(i):
        return s.db.execute("SELECT finish_yards FROM shots WHERE id = ?", (i,)).fetchone()[0]

    drive = rec(1, "DR", "tee", 400)
    approach = rec(2, "8I", "fairway", 150)
    putt = rec(3, "PT", "green", 6)
    assert finish(drive) == 150 and finish(approach) == 6 and finish(putt) is None
    tee = rec(1, "DR", "tee", 380, hole="Hole 2, par 4, 380 yards")
    dropped = rec(3, "7I", "rough", 170, hole="Hole 2, par 4, 380 yards")   # penalty between: from a drop
    assert finish(tee) is None
    s.record_gimme(dropped)
    s.db.execute("UPDATE shots SET finish_yards = NULL WHERE id = ?", (dropped,))
    s.db.commit()
    ace = rec(1, "7I", "tee", 160, hole="Hole 3, par 3, 160 yards", outcome="holed")
    s.db.execute("UPDATE shots SET finish_yards = NULL WHERE id IN (?, ?)", (drive, approach))
    s.db.commit()
    s.close()
    s = Store(path)   # older rows are filled in at startup
    assert (finish(drive), finish(approach), finish(dropped), finish(ace)) == (150, 6, 1.5, 0.0)


def test_prediction_record_tracks_commentator_accuracy(tmp_path):
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    s.record_prediction("color", "putt", "make", "made", True)
    s.record_prediction("color", "putt", "make", "missed", False)
    s.record_prediction("color", "putt", "miss", "missed", True)
    assert s.prediction_record("color") == {"total": 3, "hits": 2}
    assert s.prediction_record("pbp") == {"total": 0, "hits": 0}


def test_a_call_record_is_said_as_a_rate_not_a_count():
    from commentator.engine import _rate_words

    assert _rate_words(33, 50) == "about two times in three"
    assert _rate_words(25, 50) == "half the time"
    assert (_rate_words(47, 50), _rate_words(49, 50)) == ("about nine times in ten", "almost every time")
    assert (_rate_words(0, 5), _rate_words(1, 10), _rate_words(5, 5)) == ("never", "one time in ten", "every time")


def test_player_profile_proximity_and_putting(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    for _ in range(5):   # approaches from ~150, finishing ~8 yards, started 4 deg right
        s.record_shot(player="Toby", hole="hole 1 par 4", shot=Shot(club="7I", carry=150, hla=4.0),
                      before=Hud(lie="fairway", to_pin="150 yards"), after=Hud(lie="green"), tags=[],
                      carry_game_yards=150, total_yards=155, offline_yards=2, course="X",
                      to_pin_yards=150.0, finish_yards=8.0, gimme_yards=1.3, fmt="scramble", attributed=True)
    prof = s.player_profile("Toby")
    assert prof["favorite_club"] == "7I" and prof["avg_start_dir"] == 4.0 and prof["aimed"] == 5
    ap = s.approach_proximity("Toby", 150.0)
    assert ap["n"] == 5 and abs(ap["avg_finish_yards"] - 8.0) < 0.01

    for outcome in ("holed", "holed", "missed"):   # putts from ~10 feet: 2 of 3 made
        s.record_shot(player="Toby", hole="hole 1 par 4", shot=Shot(club="PT"),
                      before=Hud(lie="green", to_pin="10 feet"), after=Hud(lie="green"), tags=[],
                      carry_game_yards=None, total_yards=None, offline_yards=None, course="X",
                      to_pin_yards=10 / 3.0, gimme_yards=1.3, fmt="scramble", attributed=True, outcome=outcome)
    # An unresolved putt (no result signal) is excluded, not counted as a miss.
    s.record_shot(player="Toby", hole="hole 1 par 4", shot=Shot(club="PT"),
                  before=Hud(lie="green", to_pin="10 feet"), after=Hud(lie="green"), tags=[],
                  carry_game_yards=None, total_yards=None, offline_yards=None, course="X",
                  to_pin_yards=10 / 3.0, gimme_yards=1.3, fmt="scramble", attributed=True, outcome=None)
    pm = s.putting_make_rate("Toby", 10.0)
    assert pm["attempts"] == 3 and pm["made"] == 2
    assert prof["favorite_club"] != "PT"   # putter excluded from "favorite club"


def test_three_putt_rate_from_distance(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    clock = [0.0]

    def putt(hole, n, feet):
        clock[0] += 1.0   # distinct, increasing ts so the lag putt sorts first (query orders by ts)
        s.record_shot(player="Toby", hole=hole, shot=Shot(club="PT", timestamp=clock[0]),
                      before=Hud(lie="green", to_pin=f"{feet} feet", shot_number=n), after=Hud(lie="green"),
                      tags=[], carry_game_yards=None, total_yards=None, offline_yards=None, course="X",
                      to_pin_yards=feet / 3.0, gimme_yards=1.3, fmt="scramble", attributed=True, outcome=None)

    # Four holes whose first putt is ~50 ft: one three-putt (50, 6, 1), three two-putts.
    putt("hole 1", 1, 50); putt("hole 1", 2, 6); putt("hole 1", 3, 1)
    for h in ("hole 2", "hole 3", "hole 4"):
        putt(h, 1, 48); putt(h, 2, 3)
    # A short-first-putt hole must not fall in the 50 ft bucket.
    putt("hole 5", 1, 8); putt("hole 5", 2, 1)

    tp = s.three_putt_rate("Toby", 50.0)
    assert tp["holes"] == 4 and tp["three_putts"] == 1
    # Grouping is per hole: the short-putt hole is counted only in its own bucket.
    assert s.three_putt_rate("Toby", 8.0)["three_putts"] == 0


def test_stat_context_split_lifetime_league_practice(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))

    def putt(made, league):
        s.record_shot(player="Toby", hole="hole 1 par 4", shot=Shot(club="PT"),
                      before=Hud(lie="green", to_pin="8 feet", shot_number=2), after=Hud(lie="green"),
                      tags=[], carry_game_yards=None, total_yards=None, offline_yards=None, course="X",
                      to_pin_yards=8 / 3.0, gimme_yards=1.3, fmt="best_ball", attributed=True,
                      outcome="holed" if made else "missed", is_league=league)

    for m in (True, True, True, True, False):      # league: 4 of 5 made
        putt(m, league=True)
    for m in (True, False, False, False, False):   # practice: 1 of 5 made
        putt(m, league=False)

    lg = s.putting_make_rate("Toby", 8.0, context="league")
    pr = s.putting_make_rate("Toby", 8.0, context="practice")
    life = s.putting_make_rate("Toby", 8.0)   # lifetime = both
    assert (lg["attempts"], lg["made"]) == (5, 4)
    assert (pr["attempts"], pr["made"]) == (5, 1)
    assert (life["attempts"], life["made"]) == (10, 5)


def test_team_broadcast_and_hole_stats(tmp_path):
    from commentator.round import SideStats
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    s.session_id = 1   # record two rounds in a PRIOR session for team "Aces"

    def put(scores, pars, gir, scr, sand, net):
        st = SideStats(holes_scored=len(scores), gross=sum(scores.values()), par=sum(pars.values()),
                       scores=scores, gir=gir, scramble=scr, sand=sand)
        s.record_round(course="Pebble", fmt="best_ball", side="Aces", team="Aces", holes=9,
                       stats=st, strokes=0, net_to_par=net, hole_number=lambda k: k, hole_pars=pars)

    put({1: 4, 2: 4, 3: 5, 4: 3, 5: 4}, {1: 4, 2: 5, 3: 4, 4: 4, 5: 4}, (3, 5), (2, 2), (1, 1), -1)
    put({1: 5, 2: 5, 3: 4, 4: 4, 5: 4}, {1: 4, 2: 5, 3: 4, 4: 4, 5: 4}, (2, 5), (1, 2), (0, 1), 2)

    s.session_id = 2   # query as "tonight" so the two rounds count as season history
    bs = s.team_broadcast_stats("Aces")
    assert bs["rounds"] == 2
    assert (bs["scr_hit"], bs["scr_tries"]) == (3, 4)
    assert (bs["sand_hit"], bs["sand_tries"]) == (1, 2)
    assert (bs["gir_hit"], bs["gir_tries"]) == (5, 10)

    hs = s.team_hole_stats("Aces")
    assert hs["holes"] == 10 and hs["birdies"] == 2
    assert hs["par4"] == (1, 8) and hs["par5"] == (-1, 2) and hs["par3"] == (0, 0)
    assert (hs["bounce_hit"], hs["bounce_tries"]) == (1, 2)
    assert (hs["closing_to_par"], hs["closing_holes"]) == (0, 6)

    ch = s.team_course_history("Aces", "Pebble")
    # Net is per nine holes: -1 over these 5-hole rounds is -1.8 per nine.
    assert ch["rounds"] == 2 and ch["best_net_to_par"] == pytest.approx(-1.8)
    assert s.team_course_history("Aces", "Augusta")["rounds"] == 0
    assert s.season_rounds("Aces")["gir_rate"] is not None


def test_player_broadcast_stats_gates_scramble_from_scrambling_and_sand(tmp_path):
    from commentator.round import SideStats
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    s.session_id = 1   # record prior-session player rounds for "Wes"

    def put(fmt, gir, fir, scr, sand):
        st = SideStats(gir=gir, fir=fir, scramble=scr, sand=sand)
        s.record_player_round(player="Wes", course="Pebble", fmt=fmt, stats=st, hole_number=lambda k: k)

    put("best_ball", (4, 9), (3, 4), (2, 3), (1, 2))   # individual ball: everything counts
    put("scramble",  (5, 9), (4, 5), (3, 4), (2, 3))   # shared ball: only GIR/FIR count

    s.session_id = 2   # query as tonight so the two rounds are season history
    bs = s.player_broadcast_stats("Wes")
    assert (bs["fir_hit"], bs["fir_tries"]) == (7, 9)     # FIR counts both formats (own tee shot)
    assert (bs["gir_hit"], bs["gir_tries"]) == (4, 9)     # scramble excluded (shared selected ball)
    assert (bs["scr_hit"], bs["scr_tries"]) == (2, 3)     # scramble excluded from scrambling
    assert (bs["sand_hit"], bs["sand_tries"]) == (1, 2)   # scramble excluded from sand saves


def test_player_hole_stats_and_putts_from_stored_scores(tmp_path):
    from commentator.round import SideStats
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    s.session_id = 1   # record as prior-session history

    def put(fmt, scores, pars, putts=0, putt_holes=0):
        st = SideStats(source="scorecard")
        st.scores = dict(scores)
        st.putts, st.putt_holes = putts, putt_holes
        s.record_player_round(player="Wes", course="X", fmt=fmt, stats=st,
                              hole_number=lambda k: k, hole_pars=pars)

    # A best-ball round: pars 4,4,3,5; scores birdie, bogey, par, birdie.
    put("best_ball", {1: 3, 2: 5, 3: 3, 4: 4}, {1: 4, 2: 4, 3: 3, 4: 5}, putts=7, putt_holes=4)
    # A scramble round must NOT feed the hole-score stats (the score isn't a single player's).
    put("scramble", {1: 2, 2: 2, 3: 2, 4: 2}, {1: 4, 2: 4, 3: 3, 4: 5}, putts=4, putt_holes=4)

    s.session_id = 2   # query as tonight so the above are season history
    hs = s.player_hole_stats("Wes")
    assert hs["holes"] == 4 and hs["birdies"] == 2          # only the best-ball round; holes 1 and 4
    assert hs["bounce_tries"] == 1 and hs["bounce_hit"] == 0  # bogey on 2, not answered by a birdie on 3
    assert hs["par4"] == (0, 2)                             # -1 and +1 over two par 4s
    assert hs["par3"] == (0, 1) and hs["par5"] == (-1, 1)
    assert hs["closing_to_par"] == 0 and hs["closing_holes"] == 3  # holes 2,3,4: +1, 0, -1
    bs = s.player_broadcast_stats("Wes")
    assert bs["putts"] == 7 and bs["putt_holes"] == 4       # scramble putts excluded


def test_shot_level_per_player_stats(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    for after in ("fairway", "fairway", "fairway", "rough", "fairway", "rough"):   # 4 of 6 fairways
        s.record_shot(player="Toby", hole="hole 1", shot=Shot(club="DR", carry=280),
                      before=Hud(lie="tee", shot_number=1), after=Hud(lie=after), tags=[],
                      carry_game_yards=280, total_yards=290, offline_yards=5, course="X",
                      to_pin_yards=400.0, gimme_yards=1.3, fmt="scramble", attributed=True)
    fw = s.fairways_hit("Toby")
    assert fw["tries"] == 6 and fw["hit"] == 4

    for _ in range(4):   # greenside bunker shots finishing ~2 yards (6 ft)
        s.record_shot(player="Toby", hole="hole 1", shot=Shot(club="SW", carry=18),
                      before=Hud(lie="sand", to_pin="18 yards", shot_number=3), after=Hud(lie="green"),
                      tags=[], carry_game_yards=18, total_yards=18, offline_yards=1, course="X",
                      to_pin_yards=18.0, finish_yards=2.0, gimme_yards=1.3, fmt="scramble", attributed=True)
    s.record_shot(player="Toby", hole="hole 2", shot=Shot(club="9I", carry=90),   # fairway bunker, excluded
                  before=Hud(lie="sand", to_pin="90 yards", shot_number=2), after=Hud(lie="green"),
                  tags=[], carry_game_yards=90, total_yards=90, offline_yards=1, course="X",
                  to_pin_yards=90.0, finish_yards=10.0, gimme_yards=1.3, fmt="scramble", attributed=True)
    rp = s.recovery_proximity("Toby", ("sand", "bunker"), max_to_pin=30)
    assert rp["n"] == 4 and abs(rp["avg_finish_yards"] - 2.0) < 0.01


def test_hole_scoring_collects_gross_scores_across_rounds(tmp_path):
    import json

    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    for scores in ({"3": 3, "4": 5}, {"3": 4}, {"3": 2}):
        s.db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, scores) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (s.session_id, 0.0, "2026-10-07", "Pinehurst", "Toby", 9, json.dumps(scores)))
    s.db.commit()
    assert sorted(s.hole_scoring("Pinehurst", 3)) == [2, 3, 4]   # for "most birdied this hole"
    assert s.hole_scoring("Pinehurst", 4) == [5]
    assert s.hole_scoring("Other", 3) == [] and s.hole_scoring("Pinehurst", None) == []


def test_putt_distance_is_always_stated_in_feet():
    from commentator.facts import preshot_facts

    # GSPro shows a long putt in yards; the booth must still speak feet, not "17 yards".
    facts = preshot_facts(Hud(lie="green", to_pin="17 yards", shot_number=2), hole_par=4)
    assert any("feet from the hole" in f for f in facts)
    assert not any("yards" in f for f in facts)
    # A HUD feet reading is kept in feet.
    facts = preshot_facts(Hud(lie="green", to_pin="9 feet", shot_number=2), hole_par=4)
    assert any("9 feet from the hole" in f for f in facts)
    # A full shot keeps the yardage phrasing.
    facts = preshot_facts(Hud(lie="fairway", to_pin="150 yards", shot_number=2), hole_par=4)
    assert any("150 yards" in f for f in facts)


def test_a_chip_is_framed_as_up_and_down_not_a_holed_birdie():
    from commentator.facts import preshot_facts

    # A 23-yard third on a par 4 got "a birdie still available": only a putt gets the holed score.
    chip = " ".join(preshot_facts(Hud(lie="fairway", to_pin="23.4 yards", shot_number=3, score_to_par="+3"), 4))
    assert "birdie" not in chip and "up and down" in chip and "makes a par" in chip
    putt = " ".join(preshot_facts(Hud(lie="green", to_pin="9 feet", shot_number=3, score_to_par="+3"), 4))
    assert "If this putt is holed it's a birdie on the hole, which takes the round to +2" in putt


def test_gimme_circle_fact_flags_a_short_putt_just_outside(tmp_path):
    from commentator.store import Store

    cfg = Config(log_dir=str(tmp_path))
    cfg.source.gspro_log_truth = True
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, store=Store(str(tmp_path / "s.db")))
    g = engine.gspro
    g.gimme_by[0], g.to_pin_by[0] = 1.33, 1.7   # ~4-foot gimme, ~5-foot putt for player index 0
    facts = engine._truth_context_facts(True, 0)
    assert facts and "gimme circle" in facts[0] and "4-foot" in facts[0]
    g.to_pin_by[0] = 10.0                        # ~30 feet: no gimme-circle comment
    assert engine._truth_context_facts(True, 0) == []
    g.to_pin_by[0] = 1.7
    assert engine._truth_context_facts(True, None) == []   # unattributable (e.g. alternate shot)


def test_connector_log_source_on_real_uneekor_messages():
    from commentator.sources.connector_log import ConnectorLogSource
    from commentator.sources.gspro_sniffer import is_shot_message

    real = open("tests/data/uneekor_eyexo2_messages.jsonl").read().splitlines()
    stamp = "2026-10-04 21:21:28,185 [10] INFO  VGPconnect.UneekorConForm [(null)] - "
    heartbeat = stamp + '{"DeviceID":"UNEEKOREYEXO2","ShotNumber":83590,"ShotDataOptions":{"IsHeartBeat":true}}'
    src = ConnectorLogSource("unused")
    assert src.new_messages(heartbeat + "\n\0\0\0") == []  # first read only sets the starting point
    log_text = "\n".join([heartbeat] + [stamp + m for m in real] + ["   ", "\0" * 50, stamp + '{"DeviceID":"broken'])
    fresh = src.new_messages(log_text)
    shots = [m for m in fresh if is_shot_message(m)]
    # new_messages is a parser: it returns every message it can read (the leading heartbeat
    # included), not a filtered-to-new slice. Deduping shots lives in events() so that a shot in a
    # lagging rollover file isn't dropped for carrying a number the heartbeats already ran past.
    assert len(fresh) == len(real) + 1 and len(shots) == 2

    s = Shot.from_gspro(shots[0])
    assert round(s.ball_speed, 1) == 133.5 and round(s.carry, 1) == 217.4 and s.club == "3W"
    # EYE XO2 leaves SpinAxis/TotalSpin at 0 and sends back/side spin instead
    assert round(s.total_spin) == round((1656**2 + 592**2) ** 0.5) and round(s.spin_axis, 1) == -19.7
    assert Shot.from_gspro(shots[1]).club == "4H"  # 20.9 degrees of dynamic loft
    putt = {"BallData": {"Speed": 8.0, "VLA": 2.0, "BackSpin": 100, "SideSpin": 0}, "ClubData": {"Loft": 3.0}}
    assert Shot.from_gspro(putt).club is None and Shot.from_gspro(putt).is_putt
    # A real EYE XO2 putt carries a sentinel VLA (99999) that from_gspro blanks to None, and the
    # monitor can't measure a rolling ball's launch. That must still read as a putt; treating a
    # None launch as "not a putt" sent every real putt down the full-shot path (and onto the next
    # hole). A dead-straight putt that reports a literal VLA 0.0 is a putt too.
    assert Shot.from_gspro({"BallData": {"Speed": 15.1, "VLA": 99999.0, "HLA": -2.6}}).is_putt
    assert Shot.from_gspro({"BallData": {"Speed": 11.4, "VLA": 0.0}}).is_putt
    # A fast strike is not a putt; nor is a slow soft lob with a genuine high launch.
    assert not Shot.from_gspro({"BallData": {"Speed": 120.0, "VLA": 4.0}}).is_putt
    assert not Shot.from_gspro({"BallData": {"Speed": 18.0, "VLA": 55.0}}).is_putt


async def test_prepared_flight_audio_plays_immediately(tmp_path):
    from commentator.models import Line
    from commentator.tts.base import TTS, Audio

    played: list[str] = []

    class SlowTTS(TTS):
        async def synthesize(self, text, voice, instructions=None):
            await asyncio.sleep(0.2)  # a slow CPU
            return Audio(samples=text, sample_rate=1)

    async def play(audio):
        played.append(audio.samples)
        await asyncio.sleep(0.01)

    cfg = Config(log_dir=str(tmp_path))
    speech = SpeechQueue(cfg, SlowTTS(), player=play, stopper=lambda: None)
    task = asyncio.create_task(speech.run())
    audio = await speech.prepare(Line("pbp", "Needs legs."))
    speech.say([Line("color", "A long setup line still going.")])
    started = asyncio.get_running_loop().time()
    speech.say_prepared(Line("pbp", "Needs legs."), audio)
    while "Needs legs." not in played:
        await asyncio.sleep(0.005)
    assert asyncio.get_running_loop().time() - started < 0.1  # no synthesis wait at impact
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    assert "A long setup line still going." not in played  # cut off by the call


class _SetupScreen:
    """Serves the lie/slope/others regions and records which parts were asked for."""

    def __init__(self):
        self.reads: list[list[str]] = []
        self.last_read_seconds = 0.0
        self.last_capture_seconds = 0.0

    async def read_hud_async(self, parts):
        self.reads.append(list(parts))
        hud = Hud()
        if "lie" in parts:
            hud.lie = "rough"
        if "slope" in parts:
            hud.slope_updown, hud.slope_side, hud.slope_side_degrees = 2.0, "left", 1.0
        if "others" in parts:
            from commentator.hud import OtherPlayer
            hud.others = [OtherPlayer(name="Wes", to_pin="150 yards", score_to_par="E")]
        return hud

    async def snapshot_jpeg(self):
        return None


def _setup_engine(tmp_path):
    cfg = Config(log_dir=str(tmp_path))
    screen = _SetupScreen()
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, screen=screen)
    return engine, screen


async def test_a_tee_setup_keeps_the_holes_minimap_even_off_air(tmp_path):
    from PIL import Image

    engine, screen = _setup_engine(tmp_path)
    engine.set_commentary(False)
    screen.detect_mirror = lambda: False
    screen.region = lambda name: None
    screen.grab = lambda region: Image.open("tests/fixtures/minimap_water_long.jpg")
    engine.current_course, engine.hole_number = "Test Links", 7
    await engine._emit_setup(Hud(player_text="Toby 1st (E) Shot 1 400 YDS", shot_number=1, score_to_par="E",
                                 to_pin="400 yards", lie=None))
    for _ in range(100):
        if engine.store.db.execute("SELECT COUNT(*) FROM hole_images").fetchone()[0]:
            break
        await asyncio.sleep(0.02)
    row = engine.store.db.execute("SELECT course, hole, ball_x, pin_x FROM hole_images").fetchone()
    assert (row["course"], row["hole"]) == ("Test Links", 7) and row["ball_x"] and row["pin_x"]


async def test_an_empty_setup_lie_is_read_again_unless_the_player_changes(tmp_path, monkeypatch):
    import commentator.engine as eng

    monkeypatch.setattr(eng, "SETUP_LIE_RETRY_SECONDS", 0)
    engine, screen = _setup_engine(tmp_path)
    lies, sigs = iter([None, None, "fairway"]), iter([(0,) * 100] * 3)
    real = screen.read_hud_async

    async def flaky(parts):
        hud = await real(parts)
        hud.lie, hud.name_sig = next(lies), next(sigs)
        return hud
    screen.read_hud_async = flaky
    hud = Hud(player_text="Josh 2nd (E) Shot 2 126 YDS", shot_number=2, score_to_par="E", to_pin="126 yards")
    await engine._emit_setup(hud)
    assert hud.lie == "fairway" and screen.reads[1:] == [["lie", "player_name"]] * 2
    assert engine._setup_lies == {("Josh", 2, "126 yards"): "fairway"}

    # Another player's name cell on the re-read: their lie isn't this shot's.
    engine, screen = _setup_engine(tmp_path / "b")
    lies, sigs = iter([None, "rough"]), iter([(0,) * 100, (1,) * 100])
    real = screen.read_hud_async
    screen.read_hud_async = flaky
    hud = Hud(player_text="Josh 2nd (E) Shot 2 126 YDS", shot_number=2, score_to_par="E", to_pin="126 yards")
    await engine._emit_setup(hud)
    assert hud.lie is None and engine._setup_lies == {}

    # Struck during the re-reads: the setup is dropped rather than aired after the shot.
    engine, screen = _setup_engine(tmp_path / "c")
    lies, sigs = iter([None, "rough"]), iter([(0,) * 100] * 2)
    real = screen.read_hud_async

    async def swung(parts):
        hud = await flaky(parts)
        if parts == ["lie", "player_name"]:
            engine._shot_seq += 1
        return hud
    screen.read_hud_async = swung
    hud = Hud(player_text="Josh 2nd (E) Shot 2 126 YDS", shot_number=2, score_to_par="E", to_pin="126 yards")
    await engine._emit_setup(hud)
    assert engine._events.empty() and engine._setup_lies == {}


async def test_emit_setup_reads_the_hud_before_the_model_and_skips_others_off_the_tee(tmp_path):
    engine, screen = _setup_engine(tmp_path)
    hud = Hud(player_text="Toby 2nd (+1) Shot 2 150 YDS", shot_number=2, score_to_par="+1",
              to_pin="150 yards", lie=None)
    await engine._emit_setup(hud)

    # Lie and slope are read in one pass BEFORE the model call, so the facts include them; off the tee
    # the heavier "others" region is skipped entirely.
    assert screen.reads == [["lie", "player_name", "slope"]]
    assert hud.lie == "rough" and hud.slope_side == "left"
    assert engine._setup_lies == {("Toby", 2, "150 yards"): "rough"}   # for the impact read to trust

    event = await engine._events.get()
    assert isinstance(event, PreShotEvent) and event.model_task is not None
    raws, facts_text = await event.model_task
    assert "Player:" in facts_text and raws.get("line") and raws.get("branches")  # pre-shot calls ran
    assert engine._events.empty()  # a non-tee setup posts no round-start
    # The slope is read only here, so it's kept for the shot record, even half a reading (up/down missed).
    real = screen.read_hud_async

    async def side_only(parts):
        hud = await real(parts)
        hud.slope_updown = None
        return hud

    screen.read_hud_async = side_only
    await engine._emit_setup(Hud(player_text="Toby 3rd (+1) Shot 3 40 YDS", shot_number=3, score_to_par="+1",
                                 to_pin="40 yards"))
    impact = Hud(player_text="Toby 3rd (+1) Shot 3 40 YDS", shot_number=3, to_pin="40 yards")
    engine._use_setup_lie(impact)
    assert (impact.slope_updown, impact.slope_side, impact.slope_side_degrees) == (None, "left", 1.0)


def test_finish_capture_uses_the_next_shots_impact_snapshot(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot

    engine, _ = _setup_engine(tmp_path)
    store = engine.store

    def rec():
        return store.record_shot(player="Toby", hole="hole 1 par 4", shot=Shot(club="DR", carry=250),
                                 before=Hud(lie="tee", to_pin="150 yards"), after=Hud(lie="fairway"),
                                 tags=[], carry_game_yards=250, total_yards=255, offline_yards=1,
                                 course="X", to_pin_yards=150.0, fmt="scramble", attributed=True)

    # Drive stashed with its impact snapshot (ball sat on the tee, 137m to the pin). Snapshot tuple is
    # (to_pin_by, hole_by, hole_outs_by, gimmies_by, _penalties, current_hole, shot_number_by).
    sid = rec()
    engine._pending_finish[0] = (sid, engine._idx_snap(({0: 137.16}, {0: 0}, {}, {}, {}, 0, {}), 0))
    # Next shot's impact snapshot: the drive came to rest 13.716 m (~15 yd) away, same hole.
    engine._resolve_pending_finish(0, ({0: 13.716}, {0: 0}, {}, {}, {}, 0, {}))
    row = store.db.execute("SELECT finish_yards FROM shots WHERE id=?", (sid,)).fetchone()
    assert abs(row["finish_yards"] - 15.0) < 0.1          # 13.716 m converted to yards, not left as 137

    # A shot that holes out (hole advances + hole-out counter up) finishes at 0.
    sid2 = rec()
    engine._pending_finish[0] = (sid2, engine._idx_snap(({0: 30.0}, {0: 0}, {0: 0}, {}, {}, 0, {}), 0))
    engine._resolve_pending_finish(0, ({0: 350.0}, {0: 1}, {0: 1}, {}, {}, 1, {}))   # new hole, +1 hole-out
    assert store.db.execute("SELECT finish_yards FROM shots WHERE id=?", (sid2,)).fetchone()["finish_yards"] == 0.0

    # The log lagging onto a new tee: at the tee shot it still shows the last hole with the ball by the
    # cup (0.6 m, against the screen's 188.5 yards), and that hole's hole-out lands by the next shot.
    # That's not this tee shot holing out; the finish is left for the next stroke's distance instead.
    sid3 = rec()
    engine._pending_finish[0] = (sid3, {**engine._idx_snap(({0: 0.6}, {0: 0}, {0: 0}, {}, {}, 0, {}), 0),
                                        "hud_yards": 188.5})
    engine._resolve_pending_finish(0, ({0: 22.7}, {0: 1}, {0: 1}, {}, {}, 1, {}))
    assert store.db.execute("SELECT finish_yards FROM shots WHERE id=?", (sid3,)).fetchone()["finish_yards"] is None
    # The two agreeing is the normal case, and still resolves.
    sid4 = rec()
    engine._pending_finish[0] = (sid4, {**engine._idx_snap(({0: 137.16}, {0: 0}, {}, {}, {}, 0, {}), 0),
                                        "hud_yards": 150.0})
    engine._resolve_pending_finish(0, ({0: 13.716}, {0: 0}, {}, {}, {}, 0, {}))
    assert abs(store.db.execute("SELECT finish_yards FROM shots WHERE id=?", (sid4,)).fetchone()["finish_yards"] - 15.0) < 0.1


async def test_stakes_facts_fire_late_and_read_the_card(tmp_path):
    from commentator.hud import OtherPlayer
    from commentator.round import Round

    engine, _ = _setup_engine(tmp_path)
    lg = engine.league
    r = Round(league=lg, gspro_players=["MikeD", "SarahK"], course="X", scheduled_format="best_ball",
              is_league=True, holes=9)
    r.seq = 7                      # hole 7 of 9 -> 3 to play (including this one)
    engine.round = r
    hud = Hud(score_to_par="+1", others=[OtherPlayer("SarahK", "10 yards", "E")])
    facts = engine._stakes_facts(hud, "MikeD")
    assert any("3 holes to play" in f for f in facts)              # off-by-one fixed
    assert any("1 back of" in f for f in facts)                    # +1 vs E
    # Outright leader is told he leads, not that he "shares" it.
    lead = engine._stakes_facts(Hud(score_to_par="E", others=[OtherPlayer("SarahK", "10 yards", "+2")]), "MikeD")
    assert any("leads on the card" in f for f in lead) and not any("shares" in f for f in lead)


def test_score_banner_parsing():
    from commentator.hud import parse_score_banner

    assert parse_score_banner(["DBL", "BOGEY"]) == ("Double Bogey", 2)  # matches before plain BOGEY
    assert parse_score_banner(["BIRDIE"]) == ("Birdie", -1)
    assert parse_score_banner(["PAR"]) == ("Par", 0)
    assert parse_score_banner(["EAGLE"]) == ("Eagle", -2)
    assert parse_score_banner(["TRIPLE", "BOGEY"]) == ("Triple Bogey", 3)
    assert parse_score_banner(["BOGEY"]) == ("Bogey", 1)
    assert parse_score_banner(["HOLE IN ONE"]) == ("Ace", None)
    assert parse_score_banner([]) is None and parse_score_banner(["0.0", "9.7"]) is None
    # "FOR BIRDIE"/"FOR BOGEY" is the pre-shot prompt, not a result: the hole isn't over yet.
    assert parse_score_banner(["FOR", "BIRDIE"]) is None
    assert parse_score_banner(["FOR BOGEY"]) is None


def test_pick_reaction_uses_the_banner_for_putts():
    from commentator.flight_pick import FlightPlan, pick_reaction

    putt = FlightPlan(player="Toby", to_pin="10 feet", is_putt=True,
                      reactions={"holed": "drained it", "short": "left short", "past": "ran by"})
    assert pick_reaction(putt, "green", 10.0, 2.0, holed=True)[0] == "holed"   # banner up: made, ignore roll
    assert pick_reaction(putt, "green", 10.0, 9.9, holed=False)[0] == "short"  # no banner: didn't drop
    assert pick_reaction(putt, "green", 10.0, 13.0, holed=False)[0] == "past"  # no banner, ran past
    assert pick_reaction(putt, "green", 10.0, 11.0, holed=False)[0] == "past"  # a putt that just runs by is past, not short
    assert pick_reaction(putt, "green", 10.0, 9.9, holed=None)[0] == "short"   # no log/banner: never a guessed make
    assert pick_reaction(putt, "green", 17.2, 15.7, holed=None)[0] == "short"  # 51ft putt stopping 4ft short


def test_pick_reaction_maps_lies_and_putt_rolls():
    from commentator.flight_pick import FlightPlan, pick_reaction

    full = FlightPlan(player="Toby", to_pin="150 yards",
                      reactions={"green": "on the dance floor", "rough": "missed right", "sand": "in the trap"})
    assert pick_reaction(full, "green", 150, None) == ("green", "on the dance floor")
    assert pick_reaction(full, "sand", 150, None) == ("sand", "in the trap")
    assert pick_reaction(full, "bunker", 150, None) == ("sand", "in the trap")  # GSPro's "bunker" -> sand branch
    assert pick_reaction(full, "tee", 150, None) is None  # no branch for that lie -> model fallback
    assert pick_reaction(full, "fairway", 150, None) is None  # lie maps but no fairway line written

    putt = FlightPlan(player="Toby", to_pin="10 feet", is_putt=True,
                      reactions={"holed": "drained it", "short": "left short", "past": "ran by"})
    assert pick_reaction(putt, "green", 10.0, 9.8)[0] == "short"   # reaching the hole isn't a make
    assert pick_reaction(putt, "green", 10.0, 9.8, holed=True)[0] == "holed"   # log/banner said holed
    assert pick_reaction(putt, "green", 10.0, 6.0)[0] == "short"   # came up well short
    assert pick_reaction(putt, "green", 10.0, 13.0)[0] == "past"   # ran past
    assert pick_reaction(putt, "green", None, 9.8) is None          # no start distance -> fallback


def test_a_long_putt_in_the_gimme_circle_takes_its_gimme_line():
    from commentator.flight_pick import FlightPlan, pick_reaction
    from commentator.prompts import preshot_branch_prompt, preshot_branch_schema

    lag = FlightPlan(player="Dan", to_pin="61 feet", is_putt=True,
                     reactions={"holed": "drained it", "short": "left short", "past": "ran by", "gimme": "lag to tap-in"})
    assert pick_reaction(lag, "green", 20.3, 19.0, holed=False, conceded=True) == ("gimme", "lag to tap-in")
    assert pick_reaction(lag, "green", 20.3, 19.0, holed=False)[0] == "short"   # a plain miss: not a gimme
    short = FlightPlan(player="Dan", to_pin="8 feet", is_putt=True, reactions={"short": "left short"})
    assert pick_reaction(short, "green", 2.7, 2.5, conceded=True) is None   # no gimme line: the live call
    assert pick_reaction(short, "green", 2.7, 3.5, conceded=True) is None   # never "past, another putt"
    reacts = lambda **k: preshot_branch_schema(["pbp"], True, **k)["properties"]["reaction_lines"]["required"]
    assert "gimme" in reacts(lag=True) and "gimme" not in reacts()
    kw = dict(player="Dan", team=None, hole="Hole 1", facts=[], recent_lines=[], speaker="pbp", is_putt=True)
    assert "gimme (" in preshot_branch_prompt(**kw, lag=True)[0] and "gimme" not in preshot_branch_prompt(**kw)[0]


async def test_only_a_putt_past_the_lag_distance_asks_for_a_gimme_line(tmp_path):
    cfg = Config(log_dir=str(tmp_path))
    cfg.tts.console_simulate_duration = False
    llm = FakeLLM(cfg.llm.live)
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=llm, segment_llm=None,
                    speech=SpeechQueue(cfg, ConsoleTTS()), screen=_SetupScreen(), rng=random.Random(0))
    engine.flight_llm = llm
    for to_pin, asked in (("35 feet", True), ("25 feet", False)):
        llm.prompts.clear()
        await engine.handle_preshot(PreShotEvent(hud=Hud(player_text=f"Toby 2nd Shot 2 {to_pin}", shot_number=2,
                                                         lie="green", to_pin=to_pin)))
        branch = next(p for p in llm.prompts if "PRE-PUTT BRANCHES" in p)
        assert ("gimme (" in branch) is asked, to_pin
    assert engine._build_reactions({"reaction_lines": {"gimme": "Great lag, tap-in par."}}, True) == \
        {"gimme": "Great lag, tap-in par."}


async def test_run_loop_cancel_keeps_the_current_setups_plan(tmp_path):
    # The run loop calls _cancel_preshot() right before handle_preshot for a new setup. That must
    # not cancel the model call _emit_setup started for this same setup, or no plan gets built.
    cfg = Config(log_dir=str(tmp_path))
    cfg.tts.console_simulate_duration = False
    cfg.commentary.preshot_rate = 0.0
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None,
                    speech=SpeechQueue(cfg, ConsoleTTS()), screen=_SetupScreen(), rng=random.Random(0))
    hud = Hud(player_text="Toby 2nd (+1) Shot 2 150 YDS", shot_number=2, score_to_par="+1", to_pin="150 yards")
    await engine._emit_setup(hud)
    event = await engine._events.get()
    engine._cancel_preshot()
    await engine.handle_preshot(event)
    assert engine.plan is not None


async def test_emit_setup_ignores_the_finished_rounds_stale_hud(tmp_path):
    # After the sign-off GSPro's HUD still showed the last green, read as a setup, and a pre-shot line
    # ("a tap-in for par on eighteen") aired after the closer. Only a new opening tee counts then.
    import time as _time
    from commentator.round import Round
    engine, screen = _setup_engine(tmp_path)
    engine.round = Round(league=engine.league, gspro_players=["Toby"], course="X", scheduled_format=None, holes=9)
    engine.round.ended = True
    engine._round_ended_at = _time.monotonic()
    await engine._emit_setup(Hud(player_text="Toby Shot 4 0 FT 2", shot_number=4, to_pin="0 feet 2 inches"))
    assert screen.reads == [] and engine._events.empty()
    engine._round_ended_at -= 1000   # long after, but nothing hit since: still the stale HUD
    await engine._emit_setup(Hud(player_text="Toby Shot 4 4349 FT 9", shot_number=4, to_pin="4349 feet 9 inches"))
    assert screen.reads == [] and engine._events.empty()
    engine.gspro.fires += 1          # GSPro logged a new shot: a setup is commentary again
    await engine._emit_setup(Hud(player_text="Toby Shot 2 150 YDS", shot_number=2, to_pin="150 yards"))
    assert isinstance(await engine._events.get(), PreShotEvent)


async def test_emit_setup_reads_others_and_opens_the_round_on_the_tee(tmp_path):
    engine, screen = _setup_engine(tmp_path)
    hud = Hud(player_text="Toby E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards")
    await engine._emit_setup(hud)

    assert screen.reads == [["lie", "player_name", "slope", "others"]]
    assert [o.name for o in hud.others] == ["Wes"]
    kinds = []
    while not engine._events.empty():
        kinds.append(type(await engine._events.get()).__name__)
    assert kinds == ["RoundStartEvent", "PreShotEvent"]


def test_club_box_parsing():
    from commentator.hud import parse_club

    assert parse_club(["Driver", "CLUB", "RH"]) == {"club": "DR", "handed": "RH"}
    assert parse_club(["7 Iron", "CLUB", "LH"]) == {"club": "7I", "handed": "LH"}
    assert parse_club(["3 Wood"])["club"] == "3W" and parse_club(["4 Hybrid"])["club"] == "4H"
    assert parse_club(["Sand Wedge"])["club"] == "SW" and parse_club(["Putter"])["club"] == "PT"
    assert parse_club(["L Wedge", "CLUB", "LH"]) == {"club": "LW", "handed": "LH"}   # GSPro's short names
    assert parse_club(["P Wedge"])["club"] == "PW" and parse_club(["GWedge"])["club"] == "GW"
    assert parse_club(["CLUB"]) == {}


def test_regions_adjust_to_the_screen_shape():
    from commentator.config import DEFAULT_REGIONS, Region

    top = Region(left=0.5, top=0.1, width=0.2, height=0.2, aspect=1.6)
    bottom = top.model_copy(update={"anchor": "bottom", "top": 0.7})
    middle = top.model_copy(update={"anchor": "middle", "top": 0.3})
    # At the calibration shape nothing moves.
    assert top.to_pixels(1600, 1000) == (800, 100, 320, 200)
    assert bottom.to_pixels(1600, 1000) == (800, 700, 320, 200)
    # 16:9: the HUD is drawn to the width, so panels are taller and sit farther from their edge.
    r = (1920 / 1080) / 1.6
    assert top.to_pixels(1920, 1080) == (960, round(0.1 * r * 1080), 384, round(0.2 * r * 1080))
    assert bottom.to_pixels(1920, 1080)[1] == round((1 - 0.1 * r - 0.2 * r) * 1080)
    assert middle.to_pixels(1920, 1080)[1] == round((0.5 - 0.1 * r - 0.1 * r) * 1080)
    # A region without an aspect, or in pixels, is used as written.
    assert Region(left=0.5, top=0.1, width=0.2, height=0.2).to_pixels(1920, 1080) == (960, 108, 384, 216)
    assert Region(left=5, top=10, width=20, height=20, aspect=1.6).to_pixels(1920, 1080) == (5, 10, 20, 20)
    # Every default stays on screen on a tall 4:3 and a 21:9 ultrawide.
    for w, h in ((1600, 1200), (3440, 1440), (3840, 2160)):
        for reg in DEFAULT_REGIONS.values():
            left, t, width, height = reg.to_pixels(w, h)
            assert 0 <= t and t + height <= h and height > 0


def test_mixtape_16x9_frames_read_with_the_16x10_defaults():
    pytest.importorskip("rapidocr")
    frame = "data/screens/mix_h1_second_shot_hud_t0064.png"
    if not os.path.isfile(frame):
        pytest.skip("GSPro frames not downloaded")
    from PIL import Image
    from commentator.config import ScreenConfig
    from commentator.screen import Screen

    cfg = ScreenConfig()
    hud = Screen(cfg).read_hud(["player_box", "hole_box", "tiles", "lie", "slope"], Image.open(frame).convert("RGB"))
    assert (hud.shot_number, hud.par, hud.hole_length, hud.lie) == (2, 4, "359 yards", "rough")
    assert hud.tiles["total length"] == "278.2" and hud.tiles["total spin"] == "1903"
    assert (hud.slope_updown, hud.slope_side) == (4.7, "left")


def test_config_regions_merge_with_defaults():
    from commentator.config import DEFAULT_REGIONS, ScreenConfig

    cfg = ScreenConfig.model_validate({"regions": {"lie": {"left": 0.8, "top": 0.5, "width": 0.2, "height": 0.1}}})
    assert cfg.regions["lie"].left == 0.8
    assert set(cfg.regions) == set(DEFAULT_REGIONS)


def test_files_saved_by_windows_tools_load(tmp_path):
    from commentator.config import load_config

    league = tmp_path / "league.json"
    league.write_bytes(b"\xef\xbb\xbf" + b'{"teams": [{"name": "A", "players": [{"name": "Toby"}]}]}')
    assert League.load(league).players == ["Toby"]
    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(b"\xef\xbb\xbf" + b"league_file: x.json\n")
    assert load_config(cfg).league_file == "x.json"


def test_dotenv_scopes_the_key_to_the_booth(tmp_path, monkeypatch):
    import os

    from commentator.dotenv import load_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "machine-wide-key")
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbf# booth only\nANTHROPIC_API_KEY=\"sk-ant-booth\"\nEMPTY=\n")
    assert load_env(env) == ["ANTHROPIC_API_KEY"]
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-booth"
    assert load_env(tmp_path / "missing") == []


def test_pronunciations_respell_names_for_the_voice_only():
    from commentator.league import League

    lg = League({"pronunciations": {"Szabo": "Sah-bo"}, "teams": []})
    assert lg.spoken("Leo Szabo and the Szabos, Szabo's putt.") == \
        "Leo Sah-bo and the Sah-bos, Sah-bo's putt."
    assert lg.spoken("Szaboy") == "Szaboy"
    assert "pronunciations" not in lg.prompt_text()


async def test_speech_voices_the_respelled_text():
    import asyncio

    from commentator.league import League
    from commentator.models import Line
    from commentator.tts.base import TTS, Audio

    heard: list[str] = []

    class RecTTS(TTS):
        async def synthesize(self, text, voice, instructions=None):
            heard.append(text)
            return Audio(samples=None, sample_rate=1)

    async def fake_play(audio):
        pass

    speech = SpeechQueue(Config(), RecTTS(), player=fake_play)
    speech.spoken = League({"pronunciations": {"Szabo": "Sah-bo"}}).spoken
    line = Line("pbp", "Nate Szabo to putt.")
    await speech.prepare(line)
    task = asyncio.create_task(speech.run())
    speech.say([line])
    while not speech.is_idle():
        await asyncio.sleep(0.01)
    task.cancel()
    assert heard == ["Nate Sah-bo to putt."] * 2 and line.text == "Nate Szabo to putt."


async def test_commentary_off_records_shots_without_calling_a_model_or_speaking(tmp_path):
    from commentator.store import Store

    db = str(tmp_path / "season.db")
    cfg = Config(log_dir=str(tmp_path))
    cfg.commentary.enabled = False
    cfg.tts.console_simulate_duration = False
    llm = FakeLLM(cfg.llm.live)
    spoken: list = []

    async def rec_play(audio):
        spoken.append(audio)

    speech = SpeechQueue(cfg, ConsoleTTS(), player=rec_play)
    said: list = []
    speech.say = lambda lines, **kw: said.extend(lines)  # nothing may even be queued
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"),
                    source=ReplaySource("data/sample_shots.jsonl", speed=1e6), live_llm=llm, segment_llm=llm,
                    speech=speech, rng=random.Random(0), store=Store(db))
    assert speech.muted
    await engine.run()
    assert llm.prompts == [] and said == [] and spoken == []
    import sqlite3

    con = sqlite3.connect(db)
    shots, lines = (con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("shots", "lines"))
    con.close()
    assert shots > 0 and lines == 0


def test_no_commentary_needs_no_api_keys(monkeypatch):
    from commentator.__main__ import _build_engine

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Config()
    cfg.source.type = "replay"
    cfg.source.replay_file = "data/sample_shots.jsonl"
    cfg.llm.live.provider = "openai"
    cfg.tts.provider = "openai"
    cfg.league_file = "data/league.example.json"
    cfg.commentary.enabled = False
    engine = _build_engine(cfg)
    assert not engine.commentary_on and engine.speech.muted


def test_commentary_on_without_api_keys_records_and_warns_on_the_overlay(monkeypatch, tmp_path):
    from commentator.__main__ import _build_engine

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config(log_dir=str(tmp_path), db_path=str(tmp_path / "season.db"))
    cfg.league_file = "data/league.example.json"
    cfg.llm.live.provider = cfg.tts.provider = "openai"
    cfg.screen.ocr = cfg.screen.send_images = False
    cfg.commentary.enabled = True
    engine = _build_engine(cfg)
    assert cfg.overlay.enabled and not engine.commentary_on and engine.speech.muted
    assert "OPENAI_API_KEY" in engine.health.get("commentary.unavailable").message
    engine.set_commentary(True)   # the overlay's click can't put it on air
    assert not engine.commentary_on

    cfg.commentary.enabled = False   # off in config.yaml: no overlay, no warning
    engine = _build_engine(cfg)
    assert not cfg.overlay.enabled and engine.health.get("commentary.unavailable") is None


async def test_paid_and_commentary_only_screen_reads_follow_the_on_air_switch(tmp_path):
    cfg = Config(log_dir=str(tmp_path))
    cfg.commentary.enabled = False
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=None, segment_llm=None, speech=None, screen=object())
    engine._putt_read_ok = True
    made = []
    engine._make_minimap = lambda: made.append(1) or "jev"
    assert engine.minimap is None and not engine.putt_reader
    engine.set_commentary(True)
    assert engine.minimap == "jev" and engine.putt_reader
    engine.set_commentary(False)
    assert not engine.putt_reader
    engine.set_commentary(True)
    assert made == [1]

    started = asyncio.Event()

    async def jev_read():
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(jev_read())
    engine._minimap_tasks.add(task)
    await started.wait()
    engine.set_commentary(False)   # off air stops a Jev read in flight; the setup carries on without it
    assert await engine._await_minimap(task, is_putt=False) is None and task.cancelled()


async def test_late_putt_result_is_recorded_and_a_conceded_tap_in_gets_its_own_row(tmp_path):
    e = _mini_engine(tmp_path)

    class G:
        outs = gim = 0
        shot = 4

        def hole_outs_of(self, idx):
            return self.outs

        def gimmies_of(self, idx):
            return self.gim

        def shot_number(self, idx):
            return self.shot

    e.gspro = g = G()

    def putt(n, yd):
        return e.store.record_shot(player="Paul", hole="Hole 3", shot=Shot(ball_speed=8, club="PT"),
                                   before=Hud(lie="green", shot_number=n), after=Hud(), tags=[],
                                   carry_game_yards=None, total_yards=None, offline_yards=None,
                                   to_pin_yards=yd, gimme_yards=1.3)

    long_putt = putt(4, 12.0)
    task = asyncio.create_task(e._late_result(long_putt, 0, 0, 0, 4, "[t]", True))
    await asyncio.sleep(0.1)
    g.shot = 5                                    # the next setup, long after the reaction window
    await asyncio.wait_for(task, 2)
    second = putt(5, 2.0)
    task = asyncio.create_task(e._late_result(second, 0, 0, 0, 5, "[t]", True))
    g.gim = 1                                     # left inside the gimme: conceded
    await asyncio.wait_for(task, 2)
    rows = e.store.db.execute("SELECT shot_number, is_putt, club, outcome, player, hole FROM shots ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(4, 1, "PT", "missed", "Paul", "Hole 3"), (5, 1, "PT", "conceded", "Paul", "Hole 3"),
                    (6, 1, None, "gimme", "Paul", "Hole 3")]
    assert e.store.putting_make_rate("Paul", 5.0)["attempts"] == 1   # the tap-in isn't an attempt
    assert e.store.three_putt_rate("Paul", 36.0)["three_putts"] == 1


async def test_a_full_shot_watch_records_only_a_conceded_tap_in(tmp_path):
    e = _mini_engine(tmp_path)

    class G:
        outs = gim = 0
        shot = 1

        def hole_outs_of(self, idx):
            return self.outs

        def gimmies_of(self, idx):
            return self.gim

        def shot_number(self, idx):
            return self.shot

    e.gspro = g = G()
    sid = e.store.record_shot(player="Paul", hole="Hole 16", shot=Shot(ball_speed=100, vla=20, club="9I"),
                              before=Hud(lie="tee", shot_number=1), after=Hud(), tags=[],
                              carry_game_yards=None, total_yards=None, offline_yards=None)
    g.shot = 2                                    # an ordinary shot: no row added
    await e._late_result(sid, 0, 0, 0, 1, "[t]", False, window=0.5)
    g.shot, g.gim = 1, 1                          # stiffed inside the gimme
    await e._late_result(sid, 0, 0, 0, 1, "[t]", False, window=0.5)
    rows = e.store.db.execute("SELECT shot_number, is_putt, outcome FROM shots ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, 0, None), (2, 1, "gimme")]


async def test_a_tee_shot_watch_ends_when_the_log_moves_to_the_new_hole(tmp_path):
    # At the tee the log can still hold the last hole (stroke 6 there); the new hole's count restarts
    # at 2, never above 6. The watch must end there, not claim the gimme the next stroke earns.
    e = _mini_engine(tmp_path)

    class G:
        outs = gim = 0
        shot = 6
        hole_by = {0: 14}

        def hole_outs_of(self, idx):
            return self.outs

        def gimmies_of(self, idx):
            return self.gim

        def shot_number(self, idx):
            return self.shot

    e.gspro = g = G()
    sid = e.store.record_shot(player="Josh", hole="Hole 16", shot=Shot(ball_speed=100, vla=20, club="9I"),
                              before=Hud(lie="tee", shot_number=1), after=Hud(), tags=[],
                              carry_game_yards=None, total_yards=None, offline_yards=None)
    task = asyncio.create_task(e._late_result(sid, 0, 0, 0, 6, "[t]", False, window=2, base_hole=14))
    await asyncio.sleep(0.6)
    g.hole_by, g.shot = {0: 15}, 2                # the log catches up: hole 16, the chip is up
    await asyncio.sleep(0.6)
    g.gim = 1                                     # the chip finishes inside the gimme
    await task
    rows = e.store.db.execute("SELECT shot_number, is_putt, outcome FROM shots ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, 0, None)]
    # Still on the old hole in the log with nothing new: a tee shot into the gimme is still caught.
    g.hole_by, g.shot, g.gim = {0: 14}, 6, 0
    task = asyncio.create_task(e._late_result(sid, 0, 0, 0, 6, "[t]", False, window=2, base_hole=14))
    await asyncio.sleep(0.6)
    g.gim = 1
    await task
    assert e.store.db.execute("SELECT COUNT(*) FROM shots WHERE outcome = 'gimme'").fetchone()[0] == 1


class _SetupLog:
    outs = gim = 0
    shot = 3

    def __init__(self):
        self.setups_by = {0: 5}
        self.hole_by = {0: 12}

    def hole_outs_of(self, idx):
        return self.outs

    def gimmies_of(self, idx):
        return self.gim

    def fired_since(self, idx, fire):
        return False

    def shot_number(self, idx):
        return self.shot


async def test_a_putts_score_line_before_its_gimmie_is_not_a_miss(tmp_path):
    # GSPro logs the putt's score line, then the setup and the gimmie; the follower can read them a poll
    # apart. The stroke count advancing alone must not settle the putt as missed.
    e = _mini_engine(tmp_path)
    e.gspro = g = _SetupLog()
    task = asyncio.create_task(e._wait_putt_result(0, 0, 0, 3, window=3, poll=0.05, base_setup=5))
    await asyncio.sleep(0.1)
    g.shot = 4                                    # the putt's score line
    await asyncio.sleep(0.5)
    assert not task.done()
    g.setups_by[0], g.gim = 6, 1                  # the setup and its gimmie
    assert await asyncio.wait_for(task, 2) == "conceded"
    # A real miss: the setup comes with no gimmie.
    g.gim, g.shot, g.setups_by[0] = 0, 4, 7
    assert await e._wait_putt_result(0, 0, 0, 3, window=3, poll=0.05, base_setup=6) == "missed"
    # No setup ever follows the advance: the advance still settles it, a moment later.
    assert await e._wait_putt_result(0, 0, 0, 3, window=3, poll=0.05, base_setup=7,
                                     no_setup_after=0.2) == "missed"


async def test_a_scramble_chip_that_was_not_picked_does_not_claim_its_players_putt_gimmie(tmp_path):
    # Dan, Kiawah 12: Derek's chip was picked, so GSPro set Dan's ball up no more; the chip's watch ran
    # on and claimed the gimme Dan's putt was given a minute later (two gimme rows for one hole).
    from commentator.sources.gspro_log import GsproState, parse_events
    e = _mini_engine(tmp_path)
    e.gspro = g = GsproState()

    def feed(text):
        for ev in parse_events(text):
            g.apply(ev)
    fire = "* ------- Shot Fired! ------- *\n"
    feed(fire + "ActivePlayer: 0  currentHole: 11  strokes: 1  Previous Score: 2\n" + fire)
    chip, base = g.fires, (g.hole_outs_of(1), g.gimmies_of(1), g.shot_number(1), dict(g.setups_by).get(1, 0))
    task = asyncio.create_task(e._wait_putt_result(1, *base[:3], window=5, poll=0.05, base_hole=11,
                                                   base_setup=base[3], fire=chip))
    feed("ActivePlayer: 1  currentHole: 11  strokes: 1  Previous Score: 2\n"
         "Selecting shot for player: 0\nData for selected player: X 503.6 Y 24.8 Z 471.8\n"
         "Logging tdist: 2.85 and ActiveGameGimmieDistance: 1.22\n"
         "1503 SelectShot Updating teammate score: player 1\n"
         + fire + "ActivePlayer: 0  currentHole: 11  strokes: 1  Previous Score: 3\n"
         + fire + "ActivePlayer: 1  currentHole: 11  strokes: 1  Previous Score: 3\n"
         "Selecting shot for player: 1\nData for selected player: X 504.3 Y 24.9 Z 474.7\n"
         "Logging tdist: 0.32 and ActiveGameGimmieDistance: 1.22\n"
         "Select Shot - active user ball is within Gimmie distance\n"
         "ActivePlayer: 1  currentHole: 11  strokes: 1  Previous Score: 4\n")
    assert g.gimmies_of(1) > base[1]
    assert await asyncio.wait_for(task, 2) == "missed"
    assert not g.fired_since(1, chip - 1)   # Derek's stroke: not Dan's to settle


async def test_a_chip_watch_ends_at_its_own_setup_not_the_next_putts_gimmie(tmp_path):
    # Dan, Cabot Cliffs 13: GSPro's score didn't move on the chip (it took a stroke back), so the
    # stroke count never advanced and the chip's watch ran on and claimed the next putt's gimmie.
    e = _mini_engine(tmp_path)
    e.gspro = g = _SetupLog()
    sid = e.store.record_shot(player="Dan", hole="Hole 13", shot=Shot(ball_speed=29, vla=20, club="LW"),
                              before=Hud(lie="fairway", shot_number=3), after=Hud(), tags=[],
                              carry_game_yards=None, total_yards=None, offline_yards=None)
    task = asyncio.create_task(e._late_result(sid, 0, 0, 0, 3, "[t]", False, window=3, base_setup=5))
    await asyncio.sleep(0.1)
    g.setups_by[0] = 6                            # set up for the putt, no gimmie: the chip is settled
    await asyncio.sleep(1.5)
    g.shot, g.setups_by[0], g.gim = 4, 7, 1       # the putt finishes inside the gimme
    await asyncio.wait_for(task, 3)
    assert e.store.db.execute("SELECT COUNT(*) FROM shots WHERE outcome = 'gimme'").fetchone()[0] == 0


def test_the_recorded_lie_is_where_gspro_says_the_shot_was_played_from(tmp_path):
    e = _mini_engine(tmp_path)
    e.gspro.logs_penalty_surface = True
    full, putt = Shot(ball_speed=90, vla=25, putter=False), Shot(ball_speed=8, putter=True)
    played = {"putting": False}
    # The first stroke is off the tee, whatever the HUD read.
    assert e._played_from(Hud(lie="fairway", shot_number=1), full, played, None) == "tee"
    # GSPro's penalty surface is the truth (a ball that landed on the green and rolled into the sand).
    assert e._played_from(Hud(lie="green", shot_number=3), full, {**played, "penalty": "sand"}, "green") == "sand"
    # No penalty surface logged: not rough or sand, whatever the landing material or the HUD said.
    assert e._played_from(Hud(lie="rough", shot_number=2), full, played, "rough") == "fairway"
    assert e._played_from(Hud(lie="rough", shot_number=2), full, played, "fringe") == "fringe"
    assert e._played_from(Hud(lie="sand", shot_number=4), putt, {"putting": True}, None) == "green"
    # A ball that landed short and ran onto the green: the landing material isn't where it was played.
    assert e._played_from(Hud(lie="green", shot_number=3), putt, {"putting": True}, "fairway") == "green"
    assert e._played_from(Hud(lie="green", shot_number=3), full, played, "fairway") == "fairway"
    # The reverse: landed on the green and ran off (Dan, Bethpage 4: putted from 11.2 YDS, fairway).
    assert e._played_from(Hud(lie="fairway", shot_number=4), putt, {"putting": True}, "green") == "fairway"
    assert e._played_from(Hud(lie="fringe", shot_number=2), full, played, "green") == "fringe"
    # A GSPro that doesn't log penalty surfaces (or no log for the shot): the HUD read stands.
    assert e._played_from(Hud(lie="rough", shot_number=2), full, {}, "rough") == "rough"
    e.gspro.logs_penalty_surface = False
    assert e._played_from(Hud(lie="rough", shot_number=2), full, played, "fairway") == "rough"


def _log_round(e, fmt=None):
    from commentator.round import Round
    r = Round(league=e.league, gspro_players=["Paul"], course="X", scheduled_format=fmt, holes=18, is_league=False)
    if fmt:
        r.format = fmt
    e.round = r
    return r


def _row(e, r, fire, n, lie, hole=11, seq=12):
    sid = e.store.record_shot(player="Paul", hole=f"Hole {seq}", shot=Shot(ball_speed=150, club="DR"),
                              before=Hud(lie=lie, shot_number=n), after=Hud(lie="fairway"), tags=[],
                              carry_game_yards=None, total_yards=None, offline_yards=None)
    rec = ShotRecord("Paul", Shot(ball_speed=150, club="DR"), f"Hole {seq}", Hud(lie="fairway"), [],
                     round_id=r.id, hole_seq=seq, par=4, lie_before=lie, shot_number=n)
    e.state.record_shot(rec)
    e._fire_rows[fire] = (sid, rec, 0, hole)
    return sid, rec


def test_a_putt_recorded_before_its_score_line_gets_its_start_once_the_line_arrives(tmp_path):
    from commentator.sources.gspro_log import LogEvent
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    g = e.gspro
    for ev in [LogEvent("shot_fired"), LogEvent("state", player=0, hole=11, score=1, strokes=1),
               LogEvent("position", pos=(2.0, 1.0, 295.0)), LogEvent("shot_fired")]:
        g.apply(ev)
    sid, _ = _row(e, r, g.fires, 2, "green")   # the putt, recorded while the ball still rolls
    e._starts_due.add(g.fires)
    e._apply_log_corrections()
    assert e.store.db.execute("SELECT start_x FROM shots WHERE id = ?", (sid,)).fetchone()[0] is None
    g.apply(LogEvent("state", player=0, hole=11, score=2, strokes=1))
    e._apply_log_corrections()
    assert tuple(e.store.db.execute("SELECT start_x, start_y, start_z FROM shots WHERE id = ?", (sid,)).fetchone()) \
        == (2.0, 1.0, 295.0)
    assert not e._starts_due


def test_where_a_recorded_shot_went_into_a_penalty_area_is_stored_once_logged(tmp_path):
    from commentator.sources.gspro_log import LogEvent, parse_events
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    g = e.gspro
    g.apply(LogEvent("shot_fired"))
    sid, _ = _row(e, r, g.fires, 1, "tee")
    for ev in parse_events("Haz entry Point (0.0, 0.0, 0.0), Haz ball pos (5.0, 1.0, 152.0), 0\n"
                           "Haz entry Point (4.0, 1.0, 150.0), Haz ball pos (5.0, 1.0, 152.0), 0\n"
                           "Haz entry Point (4.5, 1.0, 151.0), Haz ball pos (3.0, 9.0, 140.0), 0\n"):
        g.apply(ev)
    e._apply_log_corrections()
    assert tuple(e.store.db.execute("SELECT hazard_x, hazard_y, hazard_z FROM shots WHERE id = ?", (sid,)).fetchone()) \
        == (4.0, 1.0, 150.0)


def test_a_mulligan_is_flagged_and_its_re_hit_takes_its_place(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    sid1, rec1 = _row(e, r, 7, 1, "tee")
    sid2, rec2 = _row(e, r, 8, 2, "fairway")   # the re-hit, numbered before GSPro took the first back
    e.gspro.mulligan_fires[7] = "repeat"
    e._apply_log_corrections()
    assert rec1.mulligan and not rec2.mulligan and (rec2.shot_number, rec2.lie_before) == (1, "tee")
    rows = e.store.db.execute("SELECT id, shot_number, lie_before, mulligan FROM shots ORDER BY id").fetchall()
    assert [tuple(x) for x in rows] == [(sid1, 1, "tee", 1), (sid2, 1, "tee", 0)]
    assert e.store.fairways_hit("Paul")["tries"] == 1


def test_a_repeated_score_in_a_scramble_is_a_mulligan(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e, "scramble")
    _, rec1 = _row(e, r, 7, 1, "tee")
    _, rec2 = _row(e, r, 8, 2, "fairway")
    e.gspro.mulligan_fires[7] = "repeat"
    e._apply_log_corrections()
    assert rec1.mulligan and (rec2.shot_number, rec2.lie_before) == (1, "tee")


def test_a_re_hit_starts_where_the_stroke_it_replaced_did(tmp_path):
    # Bethpage 7 (scramble): GSPro's log put Derek's re-hit where his taken-back ball had stopped.
    e = _mini_engine(tmp_path)
    r = _log_round(e, "scramble")
    sid1, _ = _row(e, r, 7, 2, "rough")
    sid2, _ = _row(e, r, 8, 2, "rough")
    sid3, _ = _row(e, r, 9, 3, "deep rough")
    db = e.store.db
    for sid, x, to_pin in ((sid1, 10.0, 262.1), (sid2, 80.0, 262.1), (sid3, 80.0, 106.3)):
        db.execute("UPDATE shots SET start_x = ?, start_y = 1, start_z = 5, to_pin_yards = ? WHERE id = ?", (x, to_pin, sid))
    e.gspro.mulligan_fires[7] = "repeat"
    e._apply_log_corrections()
    assert db.execute("SELECT start_x FROM shots WHERE id = ?", (sid2,)).fetchone()[0] == 10.0
    # A re-hit set up from elsewhere (a new pick moved the ball) keeps its own spot.
    e.store.set_shot_start(sid3, 3, "deep rough", replaced=sid2)
    assert db.execute("SELECT start_x FROM shots WHERE id = ?", (sid3,)).fetchone()[0] == 80.0


def test_a_repeated_score_in_alternate_shot_flags_nothing(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e, "alternate")
    _, rec1 = _row(e, r, 7, 1, "tee")
    e.gspro.mulligan_fires[7] = "repeat"
    e._apply_log_corrections()
    assert not rec1.mulligan
    e.gspro.mulligan_fires[7] = "undo"      # an explicit take-back counts in any format, but only once
    e._apply_log_corrections()
    assert not rec1.mulligan


def test_a_pick_up_reaches_the_round(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    _row(e, r, 7, 4, "rough", hole=10, seq=11)
    e.gspro.pickups.append((0, 10))
    e._apply_log_corrections()
    assert r.picked_up == {("Paul", 11)}
    side = r.sides[0]
    assert "picked up" in e._score_fact(side, 8, 4, picked=True)


def test_the_next_shot_corrects_where_the_last_one_finished(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    sid, rec = _row(e, r, 7, 1, "tee", hole=13, seq=14)
    e._last_row[0] = (sid, rec, 13, 0, 0)
    snap = ({0: 5.0}, {0: 13}, {}, {}, {}, 13, {0: 2})
    e._correct_last_finish(0, snap, "green")      # a tee shot onto the green the HUD read as fairway
    assert rec.hud.lie == "green"
    assert e.store.db.execute("SELECT lie_after FROM shots WHERE id = ?", (sid,)).fetchone()[0] == "green"
    e._correct_last_finish(0, ({}, {0: 13}, {}, {}, {(0, 13): 1}, 13, {}), "rough")   # after a drop: no
    assert rec.hud.lie == "green"


def test_a_tee_shot_is_corrected_though_the_log_still_named_the_last_hole(tmp_path):
    # GSPro's log moves to a new hole only once its tee shot is logged, so the tee shot's snapshot
    # names the hole before (12) and the second shot's names this one (13).
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    sid, rec = _row(e, r, 7, 1, "tee", hole=13, seq=14)
    rec.hud = Hud(lie=None)                     # the screen never saw it come to rest
    e._last_row[0] = (sid, rec, 12, 0, 0)
    snap = ({0: 94.0}, {0: 13}, {}, {}, {}, 13, {0: 2})
    e._correct_last_finish(0, snap, "rough", "Hole 14", 3)             # stroke 3: a penalty came between
    assert rec.hud.lie is None
    e._correct_last_finish(0, snap, "fairway", "Hole 14", 2, "scramble")   # maybe a partner's ball
    assert rec.hud.lie is None
    picked_mate = (*snap, {0: 1})                # GSPro dropped this player's ball for a partner's
    e._correct_last_finish(0, picked_mate, "fairway", "Hole 14", 2, "scramble")
    assert rec.hud.lie is None
    e._correct_last_finish(0, (*snap, {0: 0}), "deep rough", "Hole 14", 2, "scramble")   # their own was picked
    assert rec.hud.lie == "deep rough"
    e._correct_last_finish(0, snap, "fairway", "Hole 14", 2)
    assert rec.hud.lie == "fairway"
    assert e.store.db.execute("SELECT lie_after FROM shots WHERE id = ?", (sid,)).fetchone()[0] == "fairway"


def test_a_full_shot_inside_the_gimme_finished_on_the_green(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    sid, rec = _row(e, r, 7, 2, "fairway")
    e._record_result(sid, "conceded", "[t]", putt=False, rec=rec)
    assert rec.hud.lie == "green"
    rows = e.store.db.execute("SELECT is_putt, outcome, lie_after FROM shots ORDER BY id").fetchall()
    assert [tuple(x) for x in rows] == [(0, None, "green"), (1, "gimme", None)]
    sid2, _ = _row(e, r, 8, 2, "fairway", hole=12, seq=13)
    e._record_result(sid2, "holed", "[t]", putt=False)
    assert e.store.db.execute("SELECT outcome FROM shots WHERE id = ?", (sid2,)).fetchone()[0] == "holed"


def test_a_setup_replaying_the_last_swing_is_called_a_mulligan(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    r.seq = 12
    e.state.record_shot(ShotRecord("Paul", Shot(ball_speed=120, club="3H"), "Hole 12",
                                   Hud(lie="deep rough", to_pin="191.4 yards"), [], round_id=r.id, hole_seq=12,
                                   par=3, lie_before="tee", shot_number=1))
    nxt = Hud(shot_number=2, to_pin="69.2 yards")
    assert e._mulligan_facts("Paul", nxt) == [] and not getattr(nxt, "_mulligan", False)   # just the next shot
    # Just after a scramble pick the HUD still shows the tee's stroke and distance, but the lie is the
    # picked ball's: not a replay.
    stale = Hud(shot_number=1, to_pin="191.0 yards", lie="fairway")
    assert e._mulligan_facts("Paul", stale) == [] and not getattr(stale, "_mulligan", False)
    again = Hud(shot_number=1, to_pin="191.0 yards", lie="tee")
    facts = e._mulligan_facts("Paul", again)
    assert len(facts) == 1 and "mulligan" in facts[0] and "3-hybrid that finished in the deep rough" in facts[0]
    assert again._mulligan
    assert e._mulligan_facts("Paul", Hud(shot_number=1, to_pin="191.0 yards")) == []   # announced once


def test_a_taken_back_putt_that_barely_moved_is_called_a_misread(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    r.seq = 12

    def putt(speed):
        e.state.record_shot(ShotRecord("Paul", Shot(ball_speed=speed, club="PT"), "Hole 12",
                                       Hud(lie="green", to_pin="15 feet 1 inches"), [], round_id=r.id, hole_seq=12,
                                       par=4, lie_before="green", shot_number=3, to_pin_before="16 feet 0 inches"))
    putt(1.4)   # the launch monitor read Dan setting the ball down: 16 ft putt, went under a foot
    facts = e._mulligan_facts("Paul", Hud(shot_number=3, to_pin="16 feet 0 inches", lie="green"))
    assert len(facts) == 1 and "misread" in facts[0] and "not a mulligan on a bad putt" in facts[0]
    putt(5.0)   # a real (short) putt taken back is still a mulligan
    facts = e._mulligan_facts("Paul", Hud(shot_number=3, to_pin="16 feet 0 inches", lie="green"))
    assert len(facts) == 1 and "misread" not in facts[0] and "mulligan" in facts[0]


def test_a_misread_putt_seen_only_in_the_log_is_called_a_misread(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    _, rec1 = _row(e, r, 7, 3, "green")
    rec1.shot.ball_speed = 1.2
    _, rec2 = _row(e, r, 8, 3, "green")
    e.gspro.mulligan_fires[7] = "repeat"
    e._apply_log_corrections()
    facts = e._replay_facts("Paul", rec2)
    assert len(facts) == 1 and "misread" in facts[0]


def test_best_ball_is_one_ball_each_so_a_repeated_score_is_a_mulligan(tmp_path):
    # Riviera, best ball: Leo replayed his 40-yard pitch on 18 twice; GSPro only repeated the score line.
    e = _mini_engine(tmp_path)
    r = _log_round(e, "best_ball")
    _, rec1 = _row(e, r, 7, 3, "rough")
    _row(e, r, 8, 4, "rough")
    e.gspro.mulligan_fires[7] = "repeat"
    e._apply_log_corrections()
    assert rec1.mulligan


async def test_going_off_air_cuts_speech_and_model_calls_but_back_on_resumes(tmp_path):
    from commentator.speech import SpeechQueue
    from commentator.tts.console_tts import ConsoleTTS

    cfg = Config(log_dir=str(tmp_path))
    speech = SpeechQueue(cfg, ConsoleTTS())
    e = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
               live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=speech, screen=None)
    assert e.commentary_on and not speech.muted
    e.set_commentary(False)
    assert not e.commentary_on and speech.muted
    assert await e._complete("x", e.live_llm, "hi", []) == "{}"     # no model call off air
    e.set_commentary(True)
    assert e.commentary_on and not speech.muted
    await asyncio.sleep(0)


def test_overlay_pulse_cycles():
    from commentator.overlay import PULSE_SECONDS, pulse

    assert pulse(0.0) == 0.0 and abs(pulse(PULSE_SECONDS / 2) - 1.0) < 1e-9


def test_gspro_log_truth_and_ocr_default_on_but_never_for_replays(tmp_path):
    from commentator.sources.replay import ReplaySource

    cfg = Config(log_dir=str(tmp_path))
    assert cfg.source.gspro_log_truth and cfg.screen.ocr
    league = League.load("data/league.example.json")
    live = Engine(cfg=cfg, league=league, source=None, live_llm=FakeLLM(cfg.llm.live), segment_llm=None,
                  speech=None, screen=None)
    rcfg = Config(log_dir=str(tmp_path))
    rcfg.source.type = "replay"
    replay = Engine(cfg=rcfg, league=league, source=ReplaySource("data/sample_shots.jsonl"),
                    live_llm=FakeLLM(rcfg.llm.live), segment_llm=None, speech=None, screen=None)
    assert live._log_truth and live._log_rounds
    assert not replay._log_truth and not replay._log_rounds


def test_overlay_badge_follows_the_hud_on_any_screen_shape():
    from commentator.config import OverlayConfig
    from commentator.overlay import badge_geometry

    cfg = OverlayConfig()
    # The sim PC (16:10, 150% scaling, DPI-aware): unchanged from the hand-placed spot.
    assert badge_geometry(cfg, 3450, 2160) == (2053, 17, 289, 78)
    # 16:9 at any resolution: same fraction of the width, same size relative to the width.
    for w, h in ((1920, 1080), (3840, 2160)):
        x, y, bw, bh = badge_geometry(cfg, w, h)
        assert x == round(0.595 * w) and abs(bh / w - 78 / 3450) < 0.002 and abs(y / w - 17 / 3450) < 0.001
    assert badge_geometry(OverlayConfig(scale=2.0), 3450, 2160)[3] == 155


def test_a_replay_seen_only_in_the_log_is_called_a_mulligan_once(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    _, rec1 = _row(e, r, 7, 3, "fairway")
    _, rec2 = _row(e, r, 8, 3, "fairway")     # the HUD came back unchanged, so no setup was seen
    assert e._replay_facts("Paul", rec2) == []
    e.gspro.mulligan_fires[7] = "repeat"
    e._apply_log_corrections()
    facts = e._replay_facts("Paul", rec2)
    assert len(facts) == 1 and "mulligan" in facts[0] and "driver" in facts[0]
    assert e._replay_facts("Paul", rec2) == []   # announced once


def test_the_mulligan_setup_compares_where_the_swing_was_played_from(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    r.seq = 12
    e.state.record_shot(ShotRecord("Paul", Shot(ball_speed=30, club="LW"), "Hole 12",
                                   Hud(lie="sand", to_pin="11.5 yards"), [], round_id=r.id, hole_seq=12,
                                   par=5, lie_before="sand", shot_number=3, to_pin_before="15.4 yards"))
    assert e._mulligan_facts("Paul", Hud(shot_number=3, to_pin="15.4 yards"))


def test_bunkers_become_a_theme_after_the_second(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    r.start_hole_assumed, r.start_hole = False, 1

    def sand(seq, mull=False):
        e.state.record_shot(ShotRecord("Paul", Shot(ball_speed=100, club="7I"), f"Hole {seq}", Hud(lie="sand"), [],
                                       round_id=r.id, hole_seq=seq, shot_number=2, mulligan=mull))
    sand(2)
    assert e._bunker_facts("Paul") == []
    sand(4, mull=True)                        # a taken-back swing doesn't count
    assert e._bunker_facts("Paul") == []
    sand(5)
    f = e._bunker_facts("Paul")
    assert len(f) == 1 and "bunker 2 times" in f[0] and "holes 2, 5" in f[0]
    sand(6)
    assert "recurring theme" in e._bunker_facts("Paul")[0]


def test_putt_facts_leave_out_face_path_and_start_line():
    from commentator.facts import shot_facts
    f = " ".join(shot_facts(Shot(club="PT", ball_speed=6, hla=2.5)))
    assert "right" not in f and "don't comment on the putter face" in f


def test_grounding_reads_deep_rough_as_rough_and_news_may_mention_scenery():
    from commentator.grounding import check_line

    deep = "Player: Tony\nClub: driver. Carry: 240 yards.\nThe ball is now in the deep rough."
    assert check_line("Tony's in the rough off the tee.", deep, "").ok
    assert not check_line("Tony finds the fairway.", deep, "").ok
    # A news aside talks about the wider golf world: its own water and trees aren't guesses about this hole.
    news = "News: a pro found the water three times on the 17th at Sawgrass."
    assert check_line("Three balls in the water on seventeen, Duke.", news, "", mode="news").ok


def test_hole_watch_needs_a_banner_to_hold_before_it_changes_hole():
    from commentator.engine import HoleWatch

    h = HoleWatch()
    assert h.changed("par 4 340 yards")                 # the first sighting counts at once
    assert not h.changed("par 4 340 yards")
    assert not h.changed("par 3 158 yards")             # one poll isn't enough
    assert not h.changed("par 3 158 yards", busy=True)  # nor while a shot is still being called
    assert h.changed("par 3 158 yards")
    # "158" read as "58" while the camera follows the ball: a lost digit needs to hold far longer.
    assert not any(h.changed("par 3 58 yards") for _ in range(5))
    assert not h.changed("par 3 158 yards")
    assert not h.changed(None)


def test_hole_change_is_checked_against_the_course_card(tmp_path):
    from commentator.course_card import CourseCard
    from commentator.round import Round
    e = _mini_engine(tmp_path)
    r = Round(league=e.league, gspro_players=["Josh"], course="Cabot Cliffs", scheduled_format=None,
              holes=9, start_hole=10, is_league=False)
    r.course_card = CourseCard(name="Cabot Cliffs", index={},
                               par={10: 5, 11: 4, 12: 3, 13: 4, 14: 3, 15: 5, 16: 3, 17: 4, 18: 5})
    e.round, e.hole_number = r, 14
    # Mid-hole-14 the banner misread "par 3, 58 yards": hole 15 is a par 5, so not a new hole.
    assert e._hole_on_card(Hud(par=3, hole_length="58 yards")) == 0
    assert e._hole_on_card(Hud(par=5, hole_length="526 yards")) == 15
    # A skipped hole the banner's number names, if its par agrees.
    assert e._hole_on_card(Hud(par=3, hole_length="122 yards", hole_number=16)) == 16
    assert e._hole_on_card(Hud(par=3, hole_length="122 yards", hole_number=12)) == 0
    e.hole_number = 18
    assert e._hole_on_card(Hud(par=4, hole_length="400 yards")) is None   # past the card
    e.round.course_card = None
    e.hole_number = 14
    assert e._hole_on_card(Hud(par=3, hole_length="58 yards")) is None


async def test_a_banner_that_flips_back_with_no_shot_undoes_the_hole_change(tmp_path):
    from commentator.models import HoleChangeEvent
    e = _mini_engine(tmp_path)
    e.cfg.screen.hud_poll_seconds = 0
    banners = [(4, "340 yards", 13)] * 3 + [(4, "40 yards", None)] * 10 + [(4, "340 yards", 13)] * 3

    class Screen:
        i = 0
        last_read_seconds = last_capture_seconds = 0.0

        async def read_hud_async(self, parts):
            par, length, number = banners[min(self.i, len(banners) - 1)]
            self.i += 1
            return Hud(par=par, hole_length=length, hole_number=number)

    e.screen = Screen()
    task = asyncio.create_task(e._watch_hud())
    events = []
    for _ in range(500):
        while not e._events.empty():
            events.append(e._events.get_nowait())
        if any(getattr(x, "correction", None) for x in events) or e.screen.i > len(banners) + 50:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    changes = [(x.current, x.correction) for x in events if isinstance(x, HoleChangeEvent)]
    assert changes == [("Hole 13, par 4, 340 yards", None), ("Hole 14, par 4, 40 yards", None),
                       ("Hole 13, par 4, 340 yards", "revert")]
    assert e.hole_number == 13


async def test_gsprolog_hole_corrects_the_count_and_moves_the_holes_shots(tmp_path):
    from commentator.course_card import CourseCard
    from commentator.models import HoleChangeEvent
    from commentator.round import Round
    from commentator.state import ShotRecord
    e = _mini_engine(tmp_path)
    e.cfg.source.gspro_log_truth, e.cfg.source.type = True, "gsprolog"
    r = Round(league=e.league, gspro_players=["Josh"], course="Cabot Cliffs", scheduled_format=None,
              holes=9, start_hole=10, is_league=False)
    r.course_card = CourseCard(name="Cabot Cliffs", index={},
                               par={10: 5, 11: 4, 12: 3, 13: 4, 14: 3, 15: 5, 16: 3, 17: 4, 18: 5})
    e.round, e.hole_number, r.seq = r, 16, 7          # counted two holes too many, on hole 14
    r.par.update({5: 3, 6: 5, 7: 3})
    e.state.current_hole = "Hole 16, par 3, 158 yards"
    hud = Hud(par=3, hole_length="158 yards", player_text="Josh E Shot 2", shot_number=2)
    e.gspro.hole_by[0], e.gspro._score[(0, 13)] = 13, 1   # the log: hole 14 (0-based 13), one stroke
    assert e._log_hole_disagrees(hud) == 14
    assert e._log_hole_disagrees(Hud(par=3, hole_length="158 yards", player_text="Josh E Shot 3",
                                     shot_number=3)) is None   # the log's stroke count isn't this one
    assert e._log_hole_disagrees(Hud(par=5, hole_length="158 yards", player_text="Josh E Shot 2",
                                     shot_number=2)) is None   # hole 14 isn't a par 5 on the card
    rec = ShotRecord("Josh", Shot(), e.state.current_hole, Hud(), [], round_id=r.id, hole_seq=7, shot_id=None)
    e.state.record_shot(rec)
    await e.handle_hole_change(HoleChangeEvent(previous=None, current="Hole 14, par 3, 158 yards",
                                               correction="log"))
    assert r.seq == 5 and rec.hole_seq == 5 and rec.hole == "Hole 14, par 3, 158 yards"
    assert r.par == {5: 3} and e.state.current_hole == "Hole 14, par 3, 158 yards"


def test_an_unnumbered_opening_banner_gets_the_start_hole(tmp_path):
    # Dan, Bethpage Black: the first banner read "par 4, 426 yards" with no "1" (and the count was reset
    # at the last round's end), so the opening hole's shots were stored without a hole number.
    from commentator.round import Round
    from commentator.state import ShotRecord
    e = _mini_engine(tmp_path)
    r = Round(league=e.league, gspro_players=["Dan"], course="Bethpage Black", scheduled_format=None,
              holes=9, start_hole=1, is_league=False)
    e.round, e.hole_number = r, None
    e.state.current_hole = "par 4, 426 yards"
    sid = e.store.record_shot(player="Dan", hole="par 4, 426 yards", shot=Shot(ball_speed=150, vla=12, club="DR"),
                              before=Hud(lie="tee", shot_number=1), after=Hud(), tags=[],
                              carry_game_yards=None, total_yards=None, offline_yards=None)
    rec = ShotRecord("Dan", Shot(), "par 4, 426 yards", Hud(), [], round_id=r.id, hole_seq=1, shot_id=sid)
    e.state.record_shot(rec)
    e._number_start_hole(r)
    assert e.hole_number == 1 and e.state.current_hole == "Hole 1, par 4, 426 yards"
    assert rec.hole == "Hole 1, par 4, 426 yards"
    assert e.store.db.execute("SELECT hole FROM shots WHERE id = ?", (sid,)).fetchone()[0] == "Hole 1, par 4, 426 yards"
    # A start hole that's only a guess isn't used; a numbered banner is left alone.
    e.hole_number, e.state.current_hole, r.start_hole_assumed = None, "par 4, 426 yards", True
    e._number_start_hole(r)
    assert e.hole_number is None
    e.state.current_hole, r.start_hole_assumed = "Hole 1, par 4, 426 yards", False
    e._number_start_hole(r)
    assert e.state.current_hole == "Hole 1, par 4, 426 yards"


def test_a_stroke_set_up_in_yards_is_off_the_green(tmp_path):
    # GSPro shows yards off the green and feet on it; switching to putt mode off the green shows feet too.
    e = _mini_engine(tmp_path)
    off = Hud(player_text="Dan 4th (+2) Shot 4 11.2 YDS", shot_number=4, to_pin="11.2 yards", lie="green")
    assert e._set_up_in_yards(off)
    e._setup_yards[("Dan", 5)] = True             # set up in yards, then switched to putt mode: feet now
    assert e._set_up_in_yards(Hud(player_text="Dan 5th Shot 5 33 FT", shot_number=5, to_pin="33 feet", lie="green"))
    e._setup_yards[("Dan", 6)] = False            # set up on the green in feet
    assert not e._set_up_in_yards(Hud(player_text="Dan 6th Shot 6 5 FT", shot_number=6, to_pin="5 feet", lie="green"))
    assert not e._set_up_in_yards(Hud(player_text="Dan 7th Shot 7 9 FT", shot_number=7, to_pin="9 feet", lie="green"))
    assert e._off_green_lie(off) == "fringe"
    assert e._off_green_lie(Hud(lie="fairway")) == "fairway"
    assert e._off_green_lie(Hud(lie="rough")) == "fringe"   # no rough without GSPro's penalty for it


def test_the_impact_lie_is_the_one_read_at_setup(tmp_path):
    e = _mini_engine(tmp_path)
    e._setup_lies[("Josh", 2, "10.8 yards")] = "fairway"
    e._setup_slopes[("Josh", 2, "10.8 yards")] = (-4.2, "right", 3.2)
    # 3 s after the chip the lie box shows the green it landed on; the setup read the fringe-side fairway.
    hud = Hud(player_text="Josh 2nd (-1) Shot 2 10.8 YDS", shot_number=2, to_pin="10.8 yards", lie="green")
    e._use_setup_lie(hud)
    assert hud.lie == "fairway"
    assert (hud.slope_updown, hud.slope_side, hud.slope_side_degrees) == (-4.2, "right", 3.2)   # saved with the shot
    other = Hud(player_text="Sam 2nd Shot 2 8.3 YDS", shot_number=2, to_pin="8.3 yards", lie="green")
    e._use_setup_lie(other)
    assert other.lie == "green"   # no setup read for this stroke: the impact read stands
    assert other.slope_updown is None


def test_strategy_fact_is_offered_at_most_once_a_hole(tmp_path):
    from commentator.minimap import MinimapRead
    engine, _ = _setup_engine(tmp_path)
    engine.rng.random = lambda: 0.0
    read = MinimapRead(threshold=0.7, sides=True, seen={"water_ahead": 0.9})
    hud = Hud(to_pin="140 yards", lie="fairway")
    engine.state.current_hole = "Hole 3"
    assert engine._strategy_facts(read, hud)[0].startswith("Strategy: water short")
    assert engine._strategy_facts(read, hud) == []          # same hole: once is enough
    engine.state.current_hole = "Hole 4"
    engine.rng.random = lambda: 0.99
    assert engine._strategy_facts(read, hud) == []          # and not every hole
    assert engine._strategy_facts(None, hud) == []


def test_a_scramble_ball_that_was_not_picked_keeps_its_own_finish_and_lie(tmp_path):
    e = _mini_engine(tmp_path)
    r = _log_round(e)
    sid, rec = _row(e, r, 7, 1, "tee", hole=0, seq=1)
    e._pending_finish[0] = (sid, e._idx_snap(({0: 300.0}, {0: 0}, {}, {}, {}, 0, {}, {}), 0))
    e._last_row[0] = (sid, rec, 0, 0, 0)
    # The partner's ball was picked: this player's next shot is from the team ball, 125 m out.
    after = ({0: 125.0}, {0: 0}, {}, {}, {}, 0, {}, {0: 1})
    e._resolve_pending_finish(0, after)
    e._correct_last_finish(0, after, "green")
    row = e.store.db.execute("SELECT finish_yards, lie_after FROM shots WHERE id = ?", (sid,)).fetchone()
    assert tuple(row) == (None, "fairway") and rec.hud.lie == "fairway"


def test_gspro_revealing_a_scramble_makes_the_round_one(tmp_path):
    from commentator.round import Round
    e = _mini_engine(tmp_path)
    r = Round(league=e.league, gspro_players=["Pat", "Sam"], course="X", scheduled_format=None, holes=9,
              is_league=False, slot_names=["Pat", "Sam"])
    e.round = r
    sid, rec = _row(e, r, 7, 1, "tee", hole=0, seq=1)
    rec.shot_id = sid
    e.gspro.teammates.add(frozenset((0, 1)))
    e.gspro.pick, e.gspro.current_hole = (1, 0), 0
    e._fire_rows[1] = (sid, rec, 0, 0)
    e.gspro._fire_info[1] = {"player": 0, "picked": False}
    e._apply_log_corrections()
    assert r.format == "scramble" and [s.gspro for s in r.sides] == [["Pat", "Sam"]]
    assert e.store.db.execute("SELECT picked FROM shots WHERE id = ?", (sid,)).fetchone()[0] == 0
    assert not r.anonymous_players()
    assert tuple(e.store.db.execute("SELECT fmt, shared_ball FROM shots WHERE id = ?", (sid,)).fetchone()) == ("scramble", 1)
    assert any("Sam's ball" in f for f in e._format_facts())


def test_the_scramble_pick_fact_names_the_stroke_it_came_from(tmp_path):
    # A reaction to the last hitter's stroke 2 once read "the team picked Sam's ball" (stroke 1's
    # pick) as stroke 2's, and the booth announced a choice GSPro hadn't made.
    from commentator.round import Round
    e = _mini_engine(tmp_path)
    e.round = Round(league=e.league, gspro_players=["Pat", "Sam"], course="X", scheduled_format="scramble",
                    holes=9, is_league=False, slot_names=["Pat", "Sam"])
    g = e.gspro
    g.pick, g._pick_stroke, g.current_hole = (1, 0), 1, 0
    assert e._pick_facts(2, setup=True) == ["Stroke 2 is played from Sam's ball, the team's pick from stroke 1."]
    assert e._pick_facts(3, setup=True) == []   # a stale pick is no setup fact
    assert e._pick_facts(1) == ["The team picked Sam's ball from the stroke-1 shots; stroke 2 is played from there."]
    late = e._pick_facts(2)
    assert "Stroke 2 was played from Sam's ball" in late[0] and "don't say or guess" in late[1]
    g.pick = None
    assert len(e._pick_facts(1)) == 1 and "don't say or guess" in e._pick_facts(1)[0]
    assert e._pick_facts(1, setup=True) == []


def test_the_hole_recap_counts_strokes_not_swings(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.state import SessionState, ShotRecord
    st = SessionState()
    def rec(player, n, carry, game=None, picked=None, mulligan=False):
        r = ShotRecord(player, Shot(club="9I", carry=carry), "8", Hud(lie="green"), [], shot_number=n,
                       carry_game=game, picked=picked)
        r.mulligan = mulligan
        st.record_shot(r)
    rec("Derek", 1, 114.6, 131.9, picked=False, mulligan=True)
    rec("Derek", 1, 114.6, 131.9, picked=True)
    rec("Dan", 1, 114.6, 128.0, picked=False)
    recap = st.hole_recap("8")
    assert len(recap) == 2 and "carried 132" in recap[0] and "stroke 1" in recap[0]
    assert "played on from this ball" in recap[0] and "not used" in recap[1]


def test_a_shamble_stores_no_scramble_picks(tmp_path):
    from commentator.round import Round
    e = _mini_engine(tmp_path)
    r = Round(league=e.league, gspro_players=["Pat", "Sam"], course="X", scheduled_format=None, holes=9,
              is_league=False, slot_names=["Pat", "Sam"])
    r.no_scramble = True   # the setup screen said shamble: partners play their own balls after the drive
    e.round = r
    sid, rec = _row(e, r, 7, 1, "tee", hole=0, seq=1)
    e.gspro.teammates.add(frozenset((0, 1)))
    e._fire_rows[1] = (sid, rec, 0, 0)
    e.gspro._fire_info[1] = {"player": 0, "picked": False}
    e._apply_log_corrections()
    assert e.store.db.execute("SELECT picked FROM shots WHERE id = ?", (sid,)).fetchone()[0] is None
    # Nor once a scramble follows it.
    r2 = Round(league=e.league, gspro_players=["Pat", "Sam"], course="X", scheduled_format=None, holes=9,
               is_league=False, slot_names=["Pat", "Sam"])
    e.round = r2
    r2.play_scramble([["Pat", "Sam"]])
    e._apply_log_corrections()
    assert r2.format == "scramble" and r2.id != r.id
    assert e.store.db.execute("SELECT picked FROM shots WHERE id = ?", (sid,)).fetchone()[0] is None


def test_the_opener_and_closer_greet_for_the_time_of_day():
    from datetime import datetime

    from commentator.prompts import closer_prompt, opener_prompt

    _, morning = opener_prompt(facts=["Playing: Dan"], recent_lines=[], now=datetime(2026, 10, 20, 10, 39))
    assert "Tuesday morning, 10:39" in morning and "good morning" in morning
    _, evening = closer_prompt(facts=[], recent_lines=[], now=datetime(2026, 10, 20, 19, 5))
    assert "good evening" in evening and '"tonight"' in evening


async def test_after_touchdown_the_follow_reads_the_grab_where_the_ball_stopped(tmp_path):
    from PIL import Image, ImageDraw

    # Premonition, 10-21: OCR reads 1.2-1.5 s apart caught the drive mid-roll on the fairway; it ran on
    # into the rough and GSPro moved on ~1 s after it stopped. Pixel grabs see the stop in time. Loudon,
    # 10-21: the live scene beside the tile panel never holds still, so only Total Length's number is
    # compared; and a ball creeping on a slope can hold one poll and roll on, so it must hold for two.
    josh = Image.new("RGB", (40, 10), (200, 200, 200))
    next_up = Image.new("RGB", (40, 10), (10, 10, 10))
    frame = iter(range(1000))

    def tiles(total):
        img = Image.new("RGB", (120, 20))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 59, 19), fill=(next(frame) * 97 % 256,) * 3)   # the moving scene
        draw.text((62, 2), str(total), fill=(255, 255, 255))
        return img

    grabs = [(tiles(t), "fairway") for t in (252.0, 254.5, 256.1, 256.1)] + [(tiles(258.1), "rough") for _ in range(3)]
    grabs += [(tiles(258.1), "next") for _ in range(5)]
    ocr = iter([Hud(player_text="P", shot_number=1, lie="fairway", tile_boxes={"total length": (60, 0, 120, 20)},
                    tile_crop_size=(120, 20),
                    tiles={"carry (game)": str(c), "total length": str(c)}) for c in (200.0, 240.0, 243.2, 243.2)])
    names = {"next": next_up}

    class Screen:
        i = 0

        async def read_hud_async(self, parts):
            return next(ocr)

        def grab_regions(self, parts):
            img, lie = grabs[min(self.i, len(grabs) - 1)]
            self.i += 1
            return {"tiles": img, "player_name": names.get(lie, josh), "lie": lie}

        def read_hud(self, parts, image=None, crops=None):
            return Hud(lie=crops["lie"], tiles={"carry (game)": "243.2", "total length": "258.1"})

    from commentator.screen import name_signature

    cfg = Config(log_dir=str(tmp_path))
    cfg.screen.follow_poll_seconds = 0.05
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, screen=Screen())
    before = Hud(player_text="P", shot_number=1, lie="tee", name_sig=name_signature(josh))
    hud, landed = await engine._follow_shot(before)
    assert landed and hud.lie == "rough" and hud.tiles["total length"] == "258.1"


async def test_following_to_rest_never_takes_the_next_players_blank_tiles(tmp_path):
    from PIL import Image

    # No name signature to see the switch: two identical grabs of the next address (tiles "--.--")
    # mustn't be read as this ball at rest; a failed grab is skipped, not fatal.
    blank = Image.new("RGB", (60, 20))
    ocr = iter([Hud(player_text="P", shot_number=1, lie="fairway",
                    tiles={"carry (game)": str(c), "total length": str(c)}) for c in (150.0, 160.0, 160.0)])

    class Screen:
        n = 0

        async def read_hud_async(self, parts):
            return next(ocr)

        def grab_regions(self, parts):
            self.n += 1
            if self.n == 1:
                raise OSError("capture failed")
            return {"tiles": blank}

        def read_hud(self, parts, image=None, crops=None):
            return Hud(lie="tee", tiles={"carry (game)": "--.--", "total length": "--.--"})

    cfg = Config(log_dir=str(tmp_path))
    cfg.screen.follow_poll_seconds = 0.05
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=None, screen=Screen())
    hud, landed = await engine._follow_shot(Hud(player_text="P", shot_number=1, lie="tee"))
    assert landed and hud.lie is None and hud.tiles["carry (game)"] == "160.0"


def test_tile_boxes_locate_each_value_in_the_unscaled_crop():
    from commentator.hud import OcrItem
    from commentator.screen import _tile_boxes

    # OCR'd at 2x: TOTAL LENGTH's value "168.5" centred at (200, 300), 40 tall, 120 wide.
    items = [OcrItem(text="168.5", x=200, y=300, h=40, left=140),
             OcrItem(text="TOTAL LENGTH", x=200, y=350, h=20, left=120)]
    assert _tile_boxes(items, 2.0, 400, 800) == {"total length": (63, 134, 137, 166)}


def test_putt_calls_are_rationed_and_never_back_to_back(tmp_path):
    import random
    e = _mini_engine(tmp_path)
    e.rng = random.Random(7)
    e.current_par = 4
    birdie = Hud(player_text="Josh", shot_number=2, lie="green", to_pin="10 feet")
    bogey = Hud(player_text="Josh", shot_number=4, lie="green", to_pin="20 feet")
    calls = [bool(e._prediction_facts("Josh", True, birdie)) for _ in range(300)]
    assert not any(a and b for a, b in zip(calls, calls[1:]))       # never two putts in a row
    assert 0.3 < sum(calls) / len(calls) < 0.5                      # a 10-foot birdie look: often
    others = [bool(e._prediction_facts("Josh", True, bogey)) for _ in range(300)]
    assert 0.1 < sum(others) / len(others) < 0.3                    # a 20-foot bogey putt: seldom
    makes = set()
    for _ in range(300):
        if e._prediction_facts("Josh", True, birdie):
            makes.add(birdie._prediction[1])
    assert makes == {True, False}                                    # calls go both ways


async def test_a_model_call_cut_off_by_its_deadline_or_failing_is_still_timed(tmp_path, caplog):
    cfg = Config(log_dir=str(tmp_path))

    class SlowLLM(LLMClient):
        async def complete_json(self, system, user, schema, images=None):
            if "FAIL" in user:
                raise RuntimeError("boom")
            await asyncio.sleep(5)

    llm = SlowLLM(cfg.llm.live)
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"),
                    source=ReplaySource("data/flight_test.jsonl", speed=1e6), live_llm=llm, segment_llm=llm,
                    speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(0))
    with caplog.at_level("INFO", logger="commentator.engine"):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(engine._complete("flight", llm, "IN FLIGHT", []), timeout=0.05)
        with pytest.raises(RuntimeError):
            await engine._complete("shot", llm, "FAIL", [])
    assert re.search(r"flight call \(.+\): cancelled after 0\.\ds", caplog.text)
    assert re.search(r"shot call \(.+\): failed after 0\.\ds: boom", caplog.text)


async def test_the_closer_skips_prompt_caching_and_other_calls_keep_it(tmp_path):
    from types import SimpleNamespace

    from commentator.llm.anthropic_client import AnthropicClient

    cfg = Config(log_dir=str(tmp_path))
    cfg.llm.live.model = "claude-opus-5-5"
    sent = []

    class Recorder(AnthropicClient):
        async def _create(self, params):
            sent.append(params["system"][0])
            return SimpleNamespace(stop_reason="end_turn", content=[], usage=SimpleNamespace(
                input_tokens=1, output_tokens=1))

    llm = Recorder(cfg.llm.live)
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None, live_llm=llm,
                    segment_llm=llm, speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(0))
    await engine._complete("closer", llm, "hi", [])
    await engine._complete("news", llm, "hi", [])
    assert "cache_control" not in sent[0] and "cache_control" in sent[1]


async def test_a_tree_on_the_line_always_gets_its_setup_line(tmp_path, caplog):
    import logging
    from commentator.models import PreShotEvent

    cfg = Config(log_dir=str(tmp_path))
    cfg.tts.console_simulate_duration = False
    cfg.tts.prevoice = False
    cfg.commentary.preshot_rate = 0.0
    e = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
               live_llm=FakeLLM(cfg.llm.live), segment_llm=None, speech=SpeechQueue(cfg, ConsoleTTS()),
               screen=None, rng=random.Random(0))

    async def raws():
        line = {"lines": [{"speaker": "pbp", "text": "Josh has a tree right in his way here."}]}
        return {"line": json.dumps(line)}, "Hole layout: a tree stands right on the line to the pin."

    def event(stymie):
        hud = Hud(player_text="Josh", shot_number=2, lie="rough", to_pin="151.8 yards")
        if stymie:
            hud._stymie = True
        return PreShotEvent(hud=hud, model_task=asyncio.ensure_future(raws()))

    with caplog.at_level(logging.INFO, logger="commentator.engine"):
        await e.handle_preshot(event(stymie=False))
        assert "Setup line skipped" in caplog.text
        await e.handle_preshot(event(stymie=True))
    assert [l.text for l in e.state.recent_lines] == ["Josh has a tree right in his way here."]


async def test_an_opener_without_its_welcome_is_written_again(tmp_path):
    replies = [
        [{"speaker": "color", "text": "Just one name on the sheet, Nigel."},
         {"speaker": "pbp", "text": "Quite the pedigree."}],
        [{"speaker": "pbp", "text": "Good afternoon and welcome to Loudon, everyone."},
         {"speaker": "color", "text": "I'll be watching those irons, Duke style."},
         {"speaker": "color", "text": "One name on the sheet, Nigel."}],
    ]

    class SeqLLM(FakeLLM):
        async def complete_json(self, system, user, schema, images=None):
            self.prompts.append(user)
            return LLMResult(text=json.dumps({"lines": replies[len(self.prompts) - 1]}))

    e = _mini_engine(tmp_path)
    llm = SeqLLM(e.cfg.llm.live)
    lines = await e._compose("opener", llm, "THE ROUND IS ABOUT TO START.", "", [], mode="round")
    assert len(llm.prompts) == 2 and "welcoming everyone" in llm.prompts[1]
    assert [l.text for l in lines] == ["Good afternoon and welcome to Loudon, everyone.",
                                       "One name on the sheet, Nigel."]   # Duke naming himself is dropped


def test_a_name_run_into_what_is_theirs_is_rejected(tmp_path):
    from commentator.models import Line
    from commentator.round import Side
    e = _mini_engine(tmp_path)

    class R:
        sides = [Side(key="Josh Merritt", label="Josh Merritt", players=["Josh Merritt"], gspro=["Josh"])]
        ended = False
        is_league = False

    e.round = R()
    facts = "Josh Merritt shot 42."
    good, problems = e._grounded([Line("pbp", "And that's Josh round in 42 at Loudon.")], facts, mode="round")
    assert not good and any("needs a possessive" in p for p in problems)
    good, _ = e._grounded([Line("pbp", "And that's Josh's round in 42 at Loudon.")], facts, mode="round")
    assert good
    good, _ = e._grounded([Line("pbp", "Watch Josh drive this one, with Josh putting well.")], facts, mode="round")
    assert good


def test_a_great_finish_asks_for_excitement_and_names_the_score_without_a_stock_phrase():
    from commentator.engine import _great_shot_fact

    gimme = _great_shot_fact(200.0, "finished inside the 4-foot gimme circle", strokes=3, par=4)
    assert gimme.startswith("From 200 yards out, the ball finished inside the 4-foot gimme circle, which makes a birdie.")
    assert "stuffed" not in gimme and "excitement" in gimme
    assert "which makes a hole in one" in _great_shot_fact(165.0, "went in the hole", strokes=1, par=3)
    assert "which makes an eagle" in _great_shot_fact(140.0, "went in the hole", strokes=2, par=4)
    # A close approach names no score (the next-putt fact does), and an unknown start drops the distance.
    assert _great_shot_fact(180.0, "finished 6 feet from the hole").startswith("From 180 yards out, the ball finished 6 feet from the hole. ")
    assert _great_shot_fact(None, "went in the hole", strokes=3, par=4).startswith("The ball went in the hole, which makes a birdie.")


def test_a_chip_to_the_gimme_is_scaled_by_its_lie_and_distance():
    from commentator.engine import _great_shot_fact
    from commentator.interest import short_game_gimme_level

    # Dan's lob wedge on Kiawah 15: 16 yards out of the deep rough to a conceded foot.
    assert short_game_gimme_level(16.0, "deep rough") == 8
    assert short_game_gimme_level(8.0, "bunker") == 7
    assert short_game_gimme_level(60.0, "fairway") == 7
    assert short_game_gimme_level(30.0, "bunker") == 9                 # stays below a hole-out
    assert short_game_gimme_level(6.0, "fringe") is None               # a tap-in chip isn't a moment
    assert short_game_gimme_level(6.0, "fairway") is None
    assert short_game_gimme_level(20.0, "green") is None
    fact = _great_shot_fact(16.0, "finished inside the 3-foot gimme circle", 4, 4, lie="deep rough", level=8)
    assert fact.startswith("From 16 yards out of the deep rough, the ball finished inside the 3-foot gimme "
                           "circle, which makes a par.")
    assert "Excitement 8 out of 10" in fact and "exceptional" not in fact
    assert "From 60 yards off the fairway," in _great_shot_fact(60.0, "x", lie="fairway", level=7)
    # Within a foot and a half it nearly dropped: one more, still below a chip-in.
    assert short_game_gimme_level(16.0, "deep rough", finish_feet=1.2) == 9
    assert short_game_gimme_level(16.0, "rough", finish_feet=3.7) == 7


def test_every_great_shot_has_its_own_excitement_level():
    from commentator.interest import approach_gimme_level, holed_out_level, stuffed_level

    assert holed_out_level(165.0, 1, 3, 100) == 10        # an ace
    assert holed_out_level(120.0, 3, 4, 100) == 10        # an approach holed
    assert holed_out_level(30.0, 3, 5, 100) == 10         # a chip in for eagle
    assert holed_out_level(30.0, 3, 4, 100) == 9          # a chip in for birdie
    assert (approach_gimme_level(120.0), approach_gimme_level(160.0)) == (8, 9)
    assert approach_gimme_level(120.0, finish_feet=0.8) == 9
    assert (stuffed_level(12.0), stuffed_level(5.1), stuffed_level(5.6)) == (6, 8, 6)   # as it is said


def test_a_booth_left_running_overnight_starts_the_morning_in_a_new_session(tmp_path):
    import time as _time

    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    night = s.session_id
    s.record_shot(player="Dan", hole="Hole 1", shot=Shot(club="DR"), before=Hud(lie="tee", shot_number=1), after=Hud(),
                  tags=[], carry_game_yards=None, total_yards=None, offline_yards=None)
    last = s.db.execute("SELECT ts FROM shots").fetchone()[0]
    assert not s.roll_session_if_idle(now=last + 3600) and s.session_id == night   # a break, same night
    assert s.roll_session_if_idle(now=last + 10 * 3600) and s.session_id != night
    assert s.db.execute("SELECT date FROM sessions WHERE id = ?", (s.session_id,)).fetchone()[0] == \
        _time.strftime("%Y-%m-%d", _time.localtime(last + 10 * 3600))
