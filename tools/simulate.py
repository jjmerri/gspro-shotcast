"""Drive the whole engine with scripted shots, so the pipeline can be tested without hitting balls.

The launch-monitor read (OCR) is known good; the bugs live in the logic (stale shots across holes,
putts, the follow/touchdown timing, projection direction, hazards). This feeds that logic the exact
HUD frames and shot data a round would produce, through the real run loop, and reports the timeline
plus any rule violations it finds.

    python tools/simulate.py              # fast, deterministic (fake model)
    python tools/simulate.py --real       # real Anthropic model + modelled OCR delay, for timing

A scenario is a list of (delay_seconds, action) steps. Actions: hole(), preshot(), shot(), putt().
Each shot carries the HUD frames its follow should see, so touchdown/stop behave as on the PC.
"""

import argparse
import asyncio
import logging
import random
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commentator.config import Config  # noqa: E402
from commentator.dotenv import load_env  # noqa: E402
from commentator.engine import Engine  # noqa: E402
from commentator.hud import Hud  # noqa: E402
from commentator.league import League  # noqa: E402
from commentator.llm import build_llm  # noqa: E402
from commentator.llm.base import LLMClient, LLMResult  # noqa: E402
from commentator.models import HoleChangeEvent, PreShotEvent, Shot, ShotEvent  # noqa: E402
from commentator.speech import SpeechQueue  # noqa: E402
from commentator.tts.console_tts import ConsoleTTS  # noqa: E402

log = logging.getLogger("sim")


class FakeLLM(LLMClient):
    """Deterministic, instant. Produces valid pre-shot plans (hazards + a flight line per outcome)
    and short reaction/opener lines, so the engine's logic runs end to end without a network call."""

    def __init__(self, cfg):
        super().__init__(cfg)

    async def complete_json(self, system, user, schema, images=None):
        import json

        # The pre-shot is three calls now: a line call (lines), a hazard call (hazards), and a branch
        # call (flight + result). All start with PRE-SHOT/PRE-PUTT, so check HAZARDS/BRANCHES first.
        if "HAZARDS" in user:
            haz = [{"kind": m.group(1), "side": m.group(2)} for m in re.finditer(r"HAZARD (\w+) (\w+)", user)]
            return LLMResult(text=json.dumps({"hazards": haz}), input_tokens=100, output_tokens=20)
        if "BRANCHES" in user:
            if "PRE-PUTT" in user:
                reactions = {o: f"putt {o}" for o in ("holed", "short", "past")}
                return LLMResult(text=json.dumps({"reaction_lines": reactions}), input_tokens=100, output_tokens=40)
            lines = {o: f"flight {o}" for o in
                     ("on_target", "short", "long", "left", "right", "left_trouble", "right_trouble")}
            reactions = {o: f"result {o}" for o in ("green", "fairway", "rough", "sand")}
            return LLMResult(text=json.dumps({"flight_lines": lines, "reaction_lines": reactions}),
                             input_tokens=100, output_tokens=50)
        if "PRE-PUTT" in user:
            return LLMResult(text=json.dumps({"lines": [{"speaker": "pbp", "text": "Read the line."}]}),
                             input_tokens=100, output_tokens=20)
        if "PRE-SHOT" in user:
            return LLMResult(text=json.dumps({"lines": [{"speaker": "pbp", "text": "Setup line."}]}),
                             input_tokens=100, output_tokens=30)
        if "THE ROUND IS" in user:
            text = "Welcome to the booth."
        else:
            text = "Reaction line."
        return LLMResult(text=json.dumps({"lines": [{"speaker": "pbp", "text": text}]}),
                         input_tokens=100, output_tokens=20)


class ScriptedScreen:
    """Serves HUD frames to the engine. _watch_hud's own polls get blanks (the scenario drives the
    events directly); handle_shot's impact read and the follow get the current shot's frames."""

    def __init__(self, read_delay: float = 0.0):
        self.read_delay = read_delay
        self.last_read_seconds = 0.0
        self.last_capture_seconds = 0.0
        self._impact: Hud | None = None
        self._follow: list[Hud] = []
        self._banner: str | None = None

    def arm(self, impact: Hud, follow: list[Hud], banner: str | None = None) -> None:
        self._impact, self._follow, self._banner = impact, list(follow), banner

    async def read_hud_async(self, parts) -> Hud:
        if self.read_delay:
            await asyncio.sleep(self.read_delay)
        self.last_read_seconds = self.read_delay
        if "score_banner" in parts:  # the hole-result read in the reaction path
            return Hud(hole_result=self._banner)
        if "club" in parts and self._impact is not None:  # the impact read in handle_shot
            hud, self._impact = self._impact, None
            return hud
        if "tiles" in parts and "hole_box" not in parts:  # a follow read
            if self._follow:
                return self._follow.pop(0)
            return Hud(player_text="NEXT", shot_number=99)  # looks "moved on" so the follow ends
        return Hud()  # _watch_hud poll: nothing, the scenario posts events itself

    async def snapshot_jpeg(self):
        return None

    def grab_and_read(self, names):  # the pre-round setup watcher polls this; the scenario drives events
        return None, {n: "" for n in names}

    def read_region_lines(self, names, image):
        return {}


def _flight(carry: float, final_lie: str, to_pin: str) -> list[Hud]:
    """Follow frames: CARRY (game) counts up in flight then freezes at touchdown (two equal reads),
    the lie flipping to where it landed. Enough frames that touchdown is caught even with slow reads."""
    rising = [carry * f for f in (0.3, 0.55, 0.8, 1.0)]
    frozen = [carry, carry, carry]  # frozen -> touchdown detected on the second equal read
    frames = []
    for i, c in enumerate(rising + frozen):
        land = i >= len(rising) - 1
        frames.append(Hud(player_text="P", shot_number=1, to_pin=to_pin, lie=final_lie if land else "fairway",
                          tiles={"carry (game)": f"{c:.1f}", "total length": f"{c + 5:.1f}"}))
    return frames


class Sim:
    def __init__(self, engine: Engine, screen: ScriptedScreen, image: bytes | None = None):
        self.engine, self.screen, self.image = engine, screen, image
        self.events: list[tuple[float, str]] = []

    def record(self, label: str) -> None:
        self.events.append((time.monotonic(), label))

    async def hole(self, banner: str, course: str = "Wynn Golf Club") -> None:
        self.record(f"HOLE {banner}")
        await self.engine._events.put(HoleChangeEvent(previous=None, current=banner, course=course))

    async def preshot(self, player: str, to_pin: str, lie: str, hazards: str = "") -> None:
        # hazards like "HAZARD water left" land in the facts for the fake model to echo into the plan.
        hud = Hud(player_text=f"{player} E Shot 1 {to_pin}", shot_number=1, score_to_par="E",
                  to_pin=to_pin, lie=lie, elevation_yards=0.0)
        if hazards:
            hud = replace(hud, player_text=hud.player_text + f"  {hazards}")
        self.record(f"SETUP {player} {lie} {to_pin}")
        image = self.image if self.image is not None else (b"x" if hazards else None)
        await self.engine._events.put(PreShotEvent(hud=hud, image=image))

    async def shot(self, label: str, club: str, ball_speed: float, hla: float, spin_axis: float,
                   total_spin: float, carry: float, final_lie: str, to_pin: str, banner: str | None = None) -> None:
        is_putt = club.upper() == "PT"
        if is_putt:  # a putt rolls (total counts up) then steadies; no carry tile
            totals = [0.3, 0.6, 0.9, 1.0, 1.0, 1.0]  # a 3-foot (~1 yard) putt rolling to the hole
            follow = [Hud(player_text="P", shot_number=1, to_pin=to_pin, lie=final_lie,
                          tiles={"total length": f"{t:.1f}"}) for t in totals]
        else:
            follow = _flight(carry, final_lie, to_pin)
        self.screen.arm(
            impact=Hud(player_text="P E Shot 1", shot_number=1, to_pin=to_pin, lie="tee", club=club,
                       tiles={"carry (raw)": f"{carry:.1f}"}),
            follow=follow, banner=banner,  # GSPro's result banner the reaction path will read
        )
        # Build through the real GSPro parser, so sentinel cleaning (a putt's VLA 99999 etc.) is exercised.
        shot = Shot.from_gspro({"ShotNumber": 1, "Units": "Yards", "BallData": {
            "Speed": ball_speed, "VLA": 99999.0 if is_putt else 14, "HLA": hla, "TotalSpin": total_spin,
            "SpinAxis": spin_axis, "CarryDistance": 109360.0 if is_putt else carry}},
            player_info={"Club": club})
        self.record(f"SHOT {label} ({club}, hla {hla}, axis {spin_axis})")
        await self.engine._events.put(ShotEvent(shot=shot, logged_at=time.time()))


async def run_scenario(steps, real: bool, config_path: str = "config.yaml") -> None:
    # In --real, load the DEPLOYED config so the exact production model mix, word limits and deadlines
    # drive the run (the booth's config.yaml, pulled from the PC). Otherwise the fast FakeLLM path.
    if real:
        from commentator.config import load_config

        cfg = load_config(config_path)
    else:
        cfg = Config()
    cfg.log_dir = "logs/sim"
    cfg.source.gspro_log_truth = False  # scripted shots: GSPro's real log would contradict them
    cfg.tts.provider = "console"  # no real TTS: this measures LLM + logic timing, not speech synthesis
    cfg.tts.console_simulate_duration = False
    cfg.commentary.min_seconds_between_comments = 0
    cfg.commentary.routine_comment_rate = 1.0
    cfg.commentary.preshot_rate = 1.0
    cfg.screen.ocr = True
    cfg.screen.follow_poll_seconds = 0.05
    cfg.screen.follow_max_seconds = 12
    screen = ScriptedScreen(read_delay=2.0 if real else 0.0)
    image = None
    if real:
        cfg.screen.send_images = True
        cfg.news.enabled = True          # exercise golf-world banter on routine shots
        cfg.news.banter_rate = 0.5
        live = build_llm(cfg.llm.live)
        segment = build_llm(cfg.llm.segment) if cfg.llm.segment else live
        flight = build_llm(cfg.llm.flight) if cfg.llm.flight else live
        preshot = build_llm(cfg.llm.preshot) if cfg.llm.preshot else live
        round_llm, opener, closer = build_llm(cfg.round_llm), build_llm(cfg.opener_llm), build_llm(cfg.closer_llm)
        news = build_llm(cfg.news_llm)
        frame = Path(__file__).resolve().parent / "sim_frame.jpg"
        image = frame.read_bytes() if frame.exists() else None
    else:
        live = segment = flight = preshot = FakeLLM(cfg.llm.live)
        round_llm = opener = closer = news = live
    engine = Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                    live_llm=live, segment_llm=segment, round_llm=round_llm, preshot_llm=preshot,
                    flight_llm=flight, opener_llm=opener, closer_llm=closer, news_llm=news,
                    speech=SpeechQueue(cfg, ConsoleTTS()), screen=screen, rng=random.Random(0))
    sim = Sim(engine, screen, image=image)
    if real and engine.news is not None:
        try:
            await engine.news.refresh()  # load live headlines so banter has topics from the first shot
        except Exception:
            log.warning("News prefetch failed; banter will be skipped")
    loop = asyncio.create_task(engine.run())
    speech = asyncio.create_task(engine.speech.run())
    pace = 1.0 if real else 0.12  # scenario gaps are realistic seconds; compress them for fast logic runs
    t0 = time.monotonic()
    for delay, action in steps:
        await asyncio.sleep(delay * pace)
        await action(sim)
    await asyncio.sleep(5 * pace)
    engine._events.put_nowait(None)
    await asyncio.gather(loop, return_exceptions=True)
    speech.cancel()

    print("\n=== timeline ===")
    for t, label in sim.events:
        print(f"  +{t - t0:5.1f}s  {label}")
    transcript = Path(cfg.log_dir) / f"transcript-{time.strftime('%Y-%m-%d')}.txt"
    if transcript.exists():
        print("\n=== spoken ===")
        print(transcript.read_text(encoding="utf-8"))


# A par 4 then a par 3, exercising: tee shot, a high-spin wedge that should read LEFT, a chip, a
# putt (so putts are narrated), and a hole change while the putt is still being handled.
# Realistic seconds between steps (settle, let the plan build, then swing; follow + react before
# the next setup). Compressed in fast mode, real-time in --real.
SCENARIO = [
    (0.0, lambda s: s.hole("Hole 1, par 4, 400 yards")),
    (0.5, lambda s: s.preshot("Toby", "400 yards", "tee", "HAZARD water left")),
    (7.0, lambda s: s.shot("tee drive", "DR", 160, -0.3, -3, 2600, 270, "fairway", "400 yards")),
    (13.0, lambda s: s.preshot("Toby", "150 yards", "fairway", "HAZARD sand right")),
    (7.0, lambda s: s.shot("approach into the bunker", "PW", 108, 4, 12, 6200, 150, "sand", "150 yards")),
    (13.0, lambda s: s.preshot("Toby", "25 yards", "sand")),
    (6.0, lambda s: s.shot("bunker shot out", "SW", 45, 0, 0, 8000, 23, "green", "25 yards")),
    (6.0, lambda s: s.preshot("Toby", "8 feet", "green")),  # addressing the putt -> a PRE-PUTT plan
    (4.0, lambda s: s.shot("par putt", "PT", 7, 0, 10, 141420, 0, "green", "8 feet", banner="Par")),
    (8.0, lambda s: s.hole("Hole 2, par 3, 180 yards")),
    (0.5, lambda s: s.preshot("Toby", "180 yards", "tee", "HAZARD water long")),
    (7.0, lambda s: s.shot("tee shot into the water", "6I", 125, -2.0, -20, 4200, 175, "water", "180 yards")),
    (13.0, lambda s: s.preshot("Toby", "60 yards", "fairway")),  # after the penalty drop
    (6.0, lambda s: s.shot("pitch on", "SW", 62, 0, 0, 7500, 58, "green", "60 yards")),
    (6.0, lambda s: s.preshot("Toby", "15 feet", "green")),
    (4.0, lambda s: s.shot("bogey putt (misses the 15, taps in)", "PT", 8, 0, 10, 141420, 0, "green", "15 feet", banner="Bogey")),
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="Real models (the deployed mix) + modelled OCR delay")
    parser.add_argument("--config", default="config.yaml", help="Config whose model mix --real uses (the PC's config.yaml)")
    args = parser.parse_args()
    load_env(Path(".env"))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("commentator").setLevel(logging.INFO)
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    await run_scenario(SCENARIO, args.real, args.config)


if __name__ == "__main__":
    asyncio.run(main())
