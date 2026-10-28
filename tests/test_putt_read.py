import asyncio
import json
import time
from types import SimpleNamespace

from PIL import Image, ImageDraw

from commentator.config import Config, PuttReadConfig
from commentator.engine import Engine
from commentator.hud import Hud
from commentator.league import League
from commentator.llm.base import LLMClient, LLMResult
from commentator.putt_read import (REF_HEIGHT, REGION, STRIP_BOTTOM, STRIP_TOP, Drift, PuttRead,
                                   read_break)

# A grab of REGION on a 4K screen.
W, H = round(REGION.width * 3840), round(REGION.height * REF_HEIGHT)


def _frames(drift, n=12):
    """Frames of green with white grid dots; drift(y) gives each dot's (dx, dy) per frame, by its
    height up the screen (0 at the ball, 1 at the top of the strip)."""
    starts = [(x, y) for y in range(60, H - 60, 40) for x in range(W // 2 - 140, W // 2 + 141, 40)]
    out = []
    for f in range(n):
        img = Image.new("RGB", (W, H), (70, 140, 60))
        d = ImageDraw.Draw(img)
        for x, y in starts:
            dx, dy = drift(1 - (REGION.top + y / H * REGION.height - STRIP_TOP) / (STRIP_BOTTOM - STRIP_TOP))
            cx, cy = x + dx * f, y + dy * f
            d.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=(235, 235, 235))
        out.append(img)
    return out


def test_dots_drifting_left_are_a_right_to_left_putt():
    read = read_break(_frames(lambda t: (-5, 0)))
    assert all(d.left > d.right for d in read.sections)
    assert read.facts(PuttReadConfig()) == ["Putt read: it breaks hard right to left all the way."]


def test_a_double_breaker_and_an_early_break():
    double = read_break(_frames(lambda t: (3, 0) if t < 0.3 else (-3, 0)))
    assert double.facts(PuttReadConfig()) == [
        "Putt read: a double breaker, it starts left to right then turns back right to left."]
    early = read_break(_frames(lambda t: (9, 0) if t < 0.6 else (0, 0)))
    assert early.facts(PuttReadConfig()) == ["Putt read: it breaks severely left to right early."]


def test_slope_and_a_flat_putt():
    up = read_break(_frames(lambda t: (0, 1.5)))
    assert up.uphill > up.downhill and up.facts(PuttReadConfig()) == ["Putt read: it's uphill."]
    down = read_break(_frames(lambda t: (0, -1.5)))
    assert down.facts(PuttReadConfig()) == ["Putt read: it's downhill."]
    # Drift too gentle to tell from flat says nothing.
    assert read_break(_frames(lambda t: (0.8, 0.4))).facts(PuttReadConfig()) == []


def test_too_few_frames_or_dots_say_nothing():
    assert read_break(_frames(lambda t: (-5, 0), n=1)).facts(PuttReadConfig()) == []
    blank = [Image.new("RGB", (W, H), (70, 140, 60))] * 5
    assert read_break(blank).facts(PuttReadConfig()) == []
    # A split vote isn't a direction.
    mixed = PuttRead(sections=[Drift(10, 10, 0.1), Drift(), Drift()])
    assert mixed.facts(PuttReadConfig()) == []


class RecordingLLM(LLMClient):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.users: list[str] = []

    async def complete_json(self, system, user, schema, images=None):
        self.users.append(user)
        return LLMResult(text=json.dumps({"lines": []}))


def _engine(tmp_path):
    cfg = Config(log_dir=str(tmp_path))
    cfg.putt_read.timeout_seconds = 0.2
    llm = RecordingLLM(cfg.llm.live)
    return Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                  live_llm=llm, segment_llm=llm, speech=None)


async def test_preshot_adds_the_putt_read_on_a_putt_and_cancels_it_otherwise(tmp_path):
    engine = _engine(tmp_path)

    async def grid():
        return PuttRead(sections=[Drift(0, 20, -5.0)] * 3, uphill=20, dy=1.2)

    hud = Hud(player_text="Tony", shot_number=3, to_pin="12 feet", lie="green")
    _, facts_text = await engine._preshot_model(hud, None, [], None, asyncio.create_task(grid()))
    assert "Putt read: it breaks hard right to left all the way." in facts_text
    assert "Putt read: it's uphill." in facts_text

    # The HUD's measured rise beats the grid's uphill/downhill.
    hud = Hud(player_text="Tony", shot_number=3, to_pin="40 feet", lie="green", elevation_yards=46 / 36)
    _, facts_text = await engine._preshot_model(hud, None, [], None, asyncio.create_task(grid()))
    assert "The putt is 3 feet 10 inches uphill" in facts_text and "it's uphill" not in facts_text
    assert "Putt read: it breaks hard right to left all the way." in facts_text

    task = asyncio.create_task(asyncio.sleep(5))
    hud = Hud(player_text="Tony", shot_number=2, to_pin="160 yards", lie="fairway")
    _, facts_text = await engine._preshot_model(hud, None, [], None, task)
    await asyncio.sleep(0)
    assert task.cancelled() and "Putt read" not in facts_text


async def test_a_late_putt_read_is_dropped(tmp_path):
    engine = _engine(tmp_path)
    hud = Hud(player_text="Tony", shot_number=3, to_pin="12 feet", lie="green")
    _, facts_text = await engine._preshot_model(hud, None, [], None, asyncio.create_task(asyncio.sleep(5)))
    assert "Putt read" not in facts_text


class _Booth:
    """A speech stand-in that stays busy for `busy` seconds and voices a line in `voice` seconds."""

    def __init__(self, busy: float, voice: float = 0.0):
        self.until = time.monotonic() + busy
        self.voice = voice
        self.played: list = []

    def is_idle(self) -> bool:
        return time.monotonic() >= self.until

    async def prepare_exchange(self, lines):
        await asyncio.sleep(self.voice)
        return lines, [b"audio"] * len(lines)

    def say_prepared_sequence(self, items):
        self.played.append((time.monotonic(), items))


def test_a_putt_setup_line_waits_for_the_booth_then_plays_already_voiced():
    from types import SimpleNamespace
    from commentator.engine import Engine

    async def run(booth, limit=1.0, strike_at=None):
        me = SimpleNamespace(speech=booth, _shot_seq=1)
        aired = []
        if strike_at is not None:
            asyncio.get_running_loop().call_later(strike_at, lambda: setattr(me, "_shot_seq", 2))
        await Engine._air_setup_when_idle(me, ["for eagle"], 1, limit, aired.append)
        return aired

    # Voiced while the last reaction plays, so it goes the moment the booth is quiet.
    booth = _Booth(busy=0.3, voice=0.25)
    assert asyncio.run(run(booth)) == [["for eagle"]]
    assert booth.played[0][0] - booth.until < 0.15
    # Never once the putt is struck, or if the booth is still talking when the wait runs out.
    assert asyncio.run(run(_Booth(busy=0.6), strike_at=0.2)) == []
    assert asyncio.run(run(_Booth(busy=5.0), limit=0.3)) == []


async def test_a_long_holed_putt_gets_the_colour_reply_after_the_call(tmp_path):
    from commentator.flight_pick import FlightPlan

    engine = _engine(tmp_path)
    for feet, asked in (("30 feet", True), ("15 feet", False)):
        engine.live_llm.users.clear()
        hud = Hud(player_text="Tony", shot_number=3, to_pin=feet, lie="green")
        await engine._preshot_model(hud, None, [], None, None)
        assert any("holed_reply" in u for u in engine.live_llm.users) == asked

    reactions = engine._build_reactions({"reaction_lines": {"holed": "Tony drains it!",
                                                            "holed_reply": "What a putt, Nigel!"}}, True)
    plan = FlightPlan(player="Tony", to_pin="30 feet", is_putt=True, reactions=reactions,
                      reaction_audio={"holed": b"a", "holed_reply": b"b"})
    said = []
    engine.speech = SimpleNamespace(say_prepared_sequence=lambda items: said.append(("seq", items)),
                                    say=lambda lines, interrupt=False: said.append(("say", lines)))
    engine._play_reaction(plan, "holed", reactions["holed"])
    assert [(line.speaker, line.text, audio) for line, audio in said[0][1]] == \
        [("pbp", "Tony drains it!", b"a"), ("color", "What a putt, Nigel!", b"b")]
    # The reply's clip isn't ready: voice both now rather than air the call alone.
    del plan.reaction_audio["holed_reply"]
    engine._play_reaction(plan, "holed", reactions["holed"])
    assert said[1][0] == "say" and [line.speaker for line in said[1][1]] == ["pbp", "color"]
    # A putt that caught the edge on the way in is still holed: the reply follows its line too.
    engine._play_reaction(plan, "lip_in", "Round the edge and in!")
    assert said[2][0] == "say" and [line.text for line in said[2][1]] == ["Round the edge and in!", "What a putt, Nigel!"]
