"""Replay a real round from the season DB through the booth, with the real models, to hear what the
current prompts make of it. Read-only on the DB; nothing is recorded to the season.

    python tools/db_replay.py --db logs/season.db --session 64 --player "Josh Merritt" --out /tmp/run1

Shots are fed in order as pre-shot then shot events, each waiting for the booth to finish talking
before the next, so it's a calm round with no overlap. There are no screenshots, so the HUD is rebuilt
from the stored columns. Putt and hole-out results come from the stored outcomes (GSPro's log on the
night) instead of a live log follower. Model output isn't deterministic: compare several runs.
"""

import argparse
import asyncio
import json
import logging
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commentator.config import load_config  # noqa: E402
from commentator.dotenv import load_env  # noqa: E402
from commentator.engine import Engine  # noqa: E402
from commentator.hud import Hud  # noqa: E402
from commentator.league import League  # noqa: E402
from commentator.llm import build_llm  # noqa: E402
from commentator.models import (HoleChangeEvent, PreShotEvent, RoundEndEvent, RoundStartEvent,  # noqa: E402
                                Shot, ShotEvent)
from commentator.speech import SpeechQueue  # noqa: E402
from commentator.store import Store  # noqa: E402
from commentator.tts.console_tts import ConsoleTTS  # noqa: E402

HOLE = re.compile(r"Hole (\d+), par (\d+), (\d+) yards")


def load_round(db: str, session: int, player: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM shots WHERE session_id = ? AND player = ? AND mulligan = 0 ORDER BY id", (session, player))]
    con.close()
    if not rows:
        raise SystemExit(f"no shots for {player!r} in session {session}")
    return rows


def _fmt(score: int) -> str:
    return "E" if score == 0 else f"{score:+d}"


def build_events(rows: list[dict], player: str) -> list[tuple[str, object, dict | None]]:
    """(kind, event, log) in play order. `log` is what GSPro's log said of that stroke: its outcome
    and where the ball came to rest."""
    out: list[tuple[str, object, dict | None]] = []
    score, hole, strokes, par = 0, None, 0, 0
    # Conceded tap-ins are stored as unstruck rows; they still count on the card.
    taken = {h: sum(1 for x in rows if x["hole"] == h) for h in {x["hole"] for x in rows}}
    struck = [r for r in rows if r["club"] is not None]
    for i, r in enumerate(struck):
        if r["hole"] != hole:
            if hole is not None:
                score += taken[hole] - par
            hole, strokes = r["hole"], 0
            m = HOLE.match(hole)
            number, par = int(m.group(1)), int(m.group(2))
            out.append(("hole", HoleChangeEvent(previous=None, current=hole, course=r["course"]), None))
            if number == 1 or len(out) == 1:   # a back nine starts its round on 10
                out.append(("start", RoundStartEvent(hud=Hud(
                    player_text=f"{player} E Shot 1", shot_number=1, score_to_par="E", to_pin=r["to_pin"],
                    course=r["course"], par=par, hole_number=number)), None))
        strokes += 1
        m = HOLE.match(hole)
        base = dict(course=r["course"], par=par, hole_number=int(m.group(1)), hole_length=f"{m.group(3)} yards")
        pre = Hud(player_text=player, shot_number=strokes, to_pin=r["to_pin"], score_to_par=_fmt(score),
                  lie=r["lie_before"], elevation_yards=r["elevation_yards"], slope_updown=r["slope_updown"],
                  slope_side=r["slope_side"], slope_side_degrees=r["slope_side_degrees"], club=r["club"], **base)
        out.append(("preshot", PreShotEvent(hud=pre, player_hint=player), None))

        rest = rows[rows.index(r) + 1:]
        conceded_after = bool(rest) and rest[0]["club"] is None and rest[0]["outcome"] == "gimme"
        outcome = r["outcome"] or ("conceded" if conceded_after else ("missed" if r["is_putt"] else None))
        tiles = {}
        if r["carry_game_yards"] is not None:
            tiles["carry (game)"] = f"{r['carry_game_yards']:.1f}"
        if r["total_yards"] is not None:
            tiles["total length"] = f"{r['total_yards']:.1f}"
        if r["offline_yards"] is not None:
            tiles["offline (raw)"] = f"{r['offline_yards']:.1f}"
        # Only what the live follow reads once the ball stops: the lie, the name and the tiles.
        after = Hud(player_text=player, lie=r["lie_after"] if outcome != "holed" else None, tiles=tiles)
        msg = json.loads(r["raw"]) if r["raw"] else None
        if msg is None:
            continue
        shot = Shot.from_gspro(msg, {"Club": r["club"]})
        out.append(("shot", ShotEvent(shot=shot, player_hint=player, hud_hint=after, impact_hud_hint=pre),
                    {"outcome": outcome, "finish_yards": r["finish_yards"]}))
    return out


class ReplayEngine(Engine):
    """The booth with GSPro's log answered from the DB: every stroke resolves to its stored outcome
    and finishing distance. Rounds still end on the hole count, as there's no log to say so."""

    stroke: dict | None = None

    @property
    def _log_truth(self) -> bool:
        return True

    @property
    def _log_rounds(self) -> bool:
        return False

    def _gspro_idx(self, player):
        return 0 if self.round is not None and not self.round.ended else None

    async def _wait_putt_result(self, idx, *args, **kwargs) -> str:
        return (self.stroke or {}).get("outcome") or "unknown"

    async def _wait_finish_yards(self, idx, *args, **kwargs):
        return (self.stroke or {}).get("finish_yards")


async def _idle(e: Engine, settle: float = 1.0, limit: float = 60.0) -> None:
    """Until the booth has been quiet for `settle` seconds: reactions and wraps run in the background."""
    quiet_since, give_up = None, time.monotonic() + limit
    while time.monotonic() < give_up:
        await asyncio.sleep(0.05)
        busy = not e.speech.is_idle() or any(not t.done() for t in (*e._shot_tasks, *e._bg_tasks))
        if busy:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= settle:
            return


async def replay(args) -> Path:
    load_env(Path(args.env))
    cfg = load_config(args.config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg.log_dir = str(out)
    cfg.source.type = "replay"
    cfg.news.enabled = cfg.weather.enabled = False
    cfg.tts.console_simulate_duration = False
    cfg.health.enabled = False
    live = build_llm(cfg.llm.live)
    e = ReplayEngine(
        cfg=cfg, league=League.load(cfg.league_file), source=None, live_llm=live,
        segment_llm=build_llm(cfg.llm.segment) if cfg.llm.segment else live,
        flight_llm=build_llm(cfg.llm.flight) if cfg.llm.flight else live,
        preshot_llm=build_llm(cfg.llm.preshot) if cfg.llm.preshot else live,
        round_llm=build_llm(cfg.round_llm), opener_llm=build_llm(cfg.opener_llm),
        closer_llm=build_llm(cfg.closer_llm), speech=SpeechQueue(cfg, ConsoleTTS()),
        store=Store(":memory:"), rng=random.Random(args.seed), await_preshot=True)
    e.bookings = None
    events = build_events(load_round(args.db, args.session, args.player), args.player)
    speech = asyncio.create_task(e.speech.run())
    t0 = time.time()
    for kind, ev, stroke in events:
        if kind == "hole":
            await e.handle_hole_change(ev)
        elif kind == "start":
            await e.handle_round_start(ev)
        elif kind == "preshot":
            await e.handle_preshot(ev)
        else:
            e.stroke = stroke
            await e.handle_shot(ev)
        await _idle(e)
        # Real play has seconds between strokes; keep the booth's pacing gates from swallowing lines.
        e.state.last_comment_at -= args.gap
    await e.handle_round_end(RoundEndEvent(reason="round complete"))
    await _idle(e)
    speech.cancel()
    logging.getLogger("db_replay").info("Replayed %d events in %.0fs", len(events), time.time() - t0)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", required=True, help="a copy of the season DB (opened read-only)")
    p.add_argument("--session", type=int, required=True)
    p.add_argument("--player", required=True)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("--out", required=True, help="directory for the transcript and booth log")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--gap", type=float, default=30.0, help="seconds of play assumed between strokes")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"Transcript in {asyncio.run(replay(args))}")


if __name__ == "__main__":
    main()
