"""Runs made-up rounds through the configured models and prints what the booth says.

Opener, a few shot reactions and the wrap-up, for a best ball round and an alternate shot
round. Uses the real prompts, checks and model from config.yaml, so it costs a few cents.
Nothing is written to the season database.

    python tools/round_demo.py [--config config.yaml]
"""

import argparse
import asyncio
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commentator.config import load_config  # noqa: E402
from commentator.dotenv import load_env  # noqa: E402
from commentator.engine import Engine  # noqa: E402
from commentator.hud import Hud, OtherPlayer, Scorecard  # noqa: E402
from commentator.league import League  # noqa: E402
from commentator.llm import build_llm  # noqa: E402
from commentator.models import HoleChangeEvent, RoundEndEvent, RoundStartEvent, ScorecardEvent, Shot, ShotEvent  # noqa: E402
from commentator.speech import SpeechQueue  # noqa: E402
from commentator.state import ShotRecord  # noqa: E402
from commentator.tts.console_tts import ConsoleTTS  # noqa: E402

LEAGUE = {
    "league_name": "Clubhouse League (demo data)",
    "total_weeks": 12,
    "teams": [
        {"name": "Shed Heads", "rank": 3, "points": 22, "players": [
            {"name": "Toby", "handicap": 14.2, "notes": "Built the AI booth; long off the tee, allergic to fairways"},
            {"name": "Wes", "handicap": 9.8, "notes": "Fairway machine, streaky putter, never lets Toby forget a miss"},
        ]},
        {"name": "Grip It & Rip It", "rank": 1, "points": 27, "players": [
            {"name": "Tony", "handicap": 18.5}, {"name": "Priya", "handicap": 7.9},
        ]},
    ],
    "storylines": ["Shed Heads are five points off the lead with four weeks to go."],
    "schedule": [],
}

PARS = [4, 5, 3, 4, 4, 3, 5, 4, 4]
INDEX = [9, 3, 17, 1, 11, 15, 5, 7, 13]
# (fairway hit, first shot on the green, putts) per hole; strokes follow.
TOBY = [(False, 4, 2), (False, 4, 2), (None, 2, 2), (False, 3, 3), (True, 3, 2), (None, 3, 2), (False, 4, 1),
        (False, 3, 2), (False, 5, 2)]
WES = [(True, 3, 3), (True, 4, 3), (None, 2, 2), (True, 3, 2), (True, 3, 3), (None, 3, 2), (True, 4, 2),
         (False, 4, 2), (True, 3, 2)]


def _records(engine, player, plan, start_seq=1):
    recs = []
    t = 0.0
    for seq, (fairway, on_green, putts) in enumerate(plan, start_seq):
        par = PARS[seq - 1]
        n = 0
        for k in range(1, on_green):
            n += 1
            t += 1
            lie_after = "green" if k == on_green - 1 else ("fairway" if (k == 1 and fairway) or k > 1 else "rough")
            club = "DR" if k == 1 and par >= 4 else "7I"
            shot = Shot(ball_speed=150 if club == "DR" else 110, vla=12, hla=0, total_spin=3000, spin_axis=0,
                        carry=265 if club == "DR" else 160, club=club, timestamp=t)
            recs.append(ShotRecord(player, shot, f"hole {seq}", Hud(lie=lie_after, tiles={}), [],
                                   round_id=engine.round.id, hole_seq=seq, par=par,
                                   lie_before="tee" if k == 1 else "fairway", shot_number=n))
        for _ in range(putts):
            n += 1
            t += 1
            shot = Shot(ball_speed=6, vla=1, hla=0, total_spin=0, spin_axis=0, carry=0, club="PT", timestamp=t)
            recs.append(ShotRecord(player, shot, f"hole {seq}", Hud(lie="green"), [], round_id=engine.round.id,
                                   hole_seq=seq, par=par, lie_before="green", shot_number=n))
    return recs


def _scores(plan):
    return {seq: on_green - 1 + putts for seq, (_, on_green, putts) in enumerate(plan, 1)}


def _engine(cfg, league):
    from commentator.store import Store

    live = build_llm(cfg.llm.live)
    return Engine(cfg=cfg, league=league, source=None, live_llm=live,
                  segment_llm=build_llm(cfg.llm.segment) if cfg.llm.segment else live,
                  flight_llm=build_llm(cfg.llm.flight) if cfg.llm.flight else live,
                  preshot_llm=build_llm(cfg.llm.preshot) if cfg.llm.preshot else live,
                  round_llm=build_llm(cfg.round_llm), opener_llm=build_llm(cfg.opener_llm),
                  closer_llm=build_llm(cfg.closer_llm),
                  news_llm=build_llm(cfg.llm.news) if cfg.llm.news and cfg.asides_on else None,
                  speech=SpeechQueue(cfg, ConsoleTTS()),
                  store=Store(":memory:"), rng=random.Random(4))


async def _say(engine, title, coro):
    print(f"\n--- {title}")
    await coro
    while not engine.speech.is_idle():
        await asyncio.sleep(0.05)


async def best_ball(cfg, league):
    e = _engine(cfg, league)
    speech = asyncio.create_task(e.speech.run())
    await e.handle_hole_change(HoleChangeEvent(previous=None, current="Hole 10, par 4, 412 yards", course="Wynn Golf Club"))
    cfg.commentary.round_format = "best ball"
    e.hole_number, e.hole_number_read = 10, True
    hud = Hud(player_text="Toby E Shot 1 412.0 YDS", shot_number=1, score_to_par="E", to_pin="412.0 yards",
              others=[OtherPlayer("Wes", "412.0 yards", "E")])
    await _say(e, "Opener: best ball, starting on the back nine", e.handle_round_start(RoundStartEvent(hud=hud)))

    water = Shot(ball_speed=128, vla=14, hla=-9.5, total_spin=4200, spin_axis=-24, carry=171, club="6I")
    hint = Hud(player_text="Toby +1 Shot 2", shot_number=2, to_pin="178.0 yards", score_to_par="+1", lie="water")
    e.cfg.commentary.routine_comment_rate = 1.0
    e.cfg.commentary.min_seconds_between_comments = 0
    await _say(e, "Big moment: approach into the water",
               e.handle_shot(ShotEvent(shot=water, player_hint="Toby", hud_hint=hint)))

    e.state.shots += _records(e, "Toby", TOBY) + _records(e, "Wes", WES)
    e.round.seq = 9
    card = Scorecard(holes=list(range(1, 19)), par={10 + i: p for i, p in enumerate(PARS)},
                     index={10 + i: x for i, x in enumerate(INDEX)},
                     scores={"TOBY": {9 + s: v for s, v in _scores(TOBY).items()},
                             "WES": {9 + s: v for s, v in _scores(WES).items()}})
    await _say(e, "Wrap-up: best ball", e.handle_scorecard(ScorecardEvent(card=card)))
    await _say(e, "Closer: round sign-off", e.handle_round_end(RoundEndEvent(reason="round complete")))
    speech.cancel()


async def alternate(cfg, league):
    e = _engine(cfg, league)
    speech = asyncio.create_task(e.speech.run())
    cfg.commentary.round_format = "alternate shot"
    await e.handle_hole_change(HoleChangeEvent(previous=None, current="Hole 1, par 4, 389 yards", course="Wynn Golf Club"))
    hud = Hud(player_text="Toby E Shot 1 389.0 YDS", shot_number=1, score_to_par="E", to_pin="389.0 yards")
    await _say(e, "Opener: alternate shot (GSPro has only Toby in the round)",
               e.handle_round_start(RoundStartEvent(hud=hud)))
    e.cfg.commentary.routine_comment_rate = 1.0
    e.cfg.commentary.min_seconds_between_comments = 0
    drive = Shot(ball_speed=158, vla=11, hla=0.8, total_spin=2500, spin_axis=2, carry=281, club="DR")
    hint = Hud(player_text="Toby E Shot 1", shot_number=1, to_pin="389.0 yards", score_to_par="E", lie="fairway",
               tiles={"carry (game)": "281.0", "total length": "302.4"})
    await _say(e, "Shot reaction: drive down the middle, one ball for the team",
               e.handle_shot(ShotEvent(shot=drive, player_hint="Toby", hud_hint=hint)))

    team = [(True, 3, 2), (False, 4, 2), (None, 2, 3), (True, 4, 2), (True, 3, 2), (None, 2, 2), (False, 5, 2),
            (True, 3, 1), (True, 4, 2)]
    e.state.shots += _records(e, "Toby", team)
    e.round.seq = 9
    card = Scorecard(holes=list(range(1, 19)), par={1 + i: p for i, p in enumerate(PARS)},
                     index={1 + i: x for i, x in enumerate(INDEX)},
                     scores={"TOBY": {s: v for s, v in _scores(team).items()}})
    await _say(e, "Wrap-up: alternate shot", e.handle_scorecard(ScorecardEvent(card=card)))
    await _say(e, "Closer: round sign-off", e.handle_round_end(RoundEndEvent(reason="round complete")))
    speech.cancel()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    load_env(Path(".env"))
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("commentator.engine").setLevel(logging.INFO)
    cfg = load_config(args.config)
    cfg.tts.console_simulate_duration = False
    cfg.log_dir = str(Path(cfg.log_dir) / "demo")
    cfg.source.gspro_log_truth = False  # made-up rounds: GSPro's real log would contradict them
    league = League(LEAGUE)
    await best_ball(cfg.model_copy(deep=True), league)
    await alternate(cfg.model_copy(deep=True), league)


if __name__ == "__main__":
    asyncio.run(main())
