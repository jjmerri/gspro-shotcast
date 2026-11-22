"""Write a made-up round into a season file, through the booth's own writes, in a moment.

For testing what happens after a round (Shotcast Cloud's upload, the stats page) without playing
one: the round gets its record, players' slots, every stroke, a line on each tee shot, each side's
scorecard and each player's round, as the booth writes them at a round's end. The commentary, the
screen reads and GSPro aren't involved (tools/simulate.py drives those).

    python tools/fake_round.py --db logs/season.db [--holes 9] [--players "Ann,Bo"] [--course Pebble]
    python tools/fake_round.py --db logs/season.db --in-progress     a round still being played
    python tools/fake_round.py --db logs/season.db --teams "Ann=Hammers,Bo=Hammers"   their league teams
    python tools/fake_round.py --db logs/season.db --not-league      a practice round

Prints the round's UUID. Each run is a new round; --seed makes its scores repeatable.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commentator.hud import Hud  # noqa: E402
from commentator.models import Line, Shot  # noqa: E402
from commentator.round import SideStats  # noqa: E402
from commentator.store import Store  # noqa: E402

CLUBS = {3: ["7I", "PT"], 4: ["DR", "8I", "PT"], 5: ["DR", "3W", "PW", "PT"]}


def play(store: Store, *, holes: int = 9, players: list[str] | None = None, course: str = "Pebble Beach",
         finish: bool = True, rng: random.Random | None = None, teams: dict[str, str] | None = None,
         league: bool = True) -> str:
    """A round of `holes` holes for `players` (each on their own ball), written into `store`. `teams`:
    a player's league team, on their scorecard; `league` False: a practice round."""
    import uuid

    rng = rng or random.Random()
    players = players or ["Ann", "Bo"]
    pars = {h: rng.choice((3, 4, 4, 5)) for h in range(1, holes + 1)}
    u = store.begin_round(str(uuid.uuid4()), course=course, players={p: None for p in players}, seats=players)
    t = time.time()
    cards: dict[str, dict[int, int]] = {p: {} for p in players}
    putts = dict.fromkeys(players, 0)
    for h, par in pars.items():
        for p in players:
            clubs = list(CLUBS[par])
            extra = rng.choice((-1, 0, 0, 1))
            if extra > 0:
                clubs.insert(-1, "SW")
            elif extra < 0 and len(clubs) > 2:
                clubs.pop(-2)
            if rng.random() < 0.3:
                clubs.append("PT")   # a two-putt
            for n, club in enumerate(clubs, 1):
                t += 0.01
                sid = store.record_shot(
                    player=p, hole=f"Hole {h}", course=course, tags=[],
                    shot=Shot(club=club, timestamp=t, ball_speed=8.0 if club == "PT" else rng.uniform(90, 165),
                              carry=0.0 if club == "PT" else rng.uniform(120, 280)),
                    before=Hud(shot_number=n, lie="tee" if n == 1 else "fairway"),
                    after=Hud(lie="green" if club == "PT" else "fairway"),
                    carry_game_yards=None, total_yards=None, offline_yards=None, is_league=league, record=u)
                if n == 1:
                    store.record_lines("shot", [Line("pbp", f"{p} tees off on {h}.")], shot_id=sid)
            cards[p][h] = len(clubs)
            putts[p] += clubs.count("PT")
    for p in players:
        st = SideStats(holes_scored=holes, gross=sum(cards[p].values()), par=sum(pars.values()),
                       scores=dict(cards[p]), putts=putts[p], putt_holes=holes)
        store.record_round(course=course, fmt="stroke", side=p, team=(teams or {}).get(p), holes=holes, stats=st,
                           strokes=0, net_to_par=st.gross - st.par, hole_number=lambda k: k, hole_pars=pars,
                           players=[p], is_league=league, complete=finish, record=u)
        store.record_player_round(player=p, course=course, fmt="stroke", stats=st, hole_number=lambda k: k,
                                  hole_pars=pars, is_league=league, complete=finish, record=u)
    if finish:
        store.end_round(u, complete=True)
    return u


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="the season file (made if missing)")
    ap.add_argument("--holes", type=int, default=9)
    ap.add_argument("--players", default="Ann,Bo", help="comma separated")
    ap.add_argument("--course", default="Pebble Beach")
    ap.add_argument("--in-progress", action="store_true", help="leave it being played")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--teams", default="", help='each player\'s league team: "Ann=Hammers,Bo=Hammers"')
    ap.add_argument("--not-league", action="store_true", help="a practice round")
    a = ap.parse_args(argv)
    store = Store(a.db)
    try:
        print(play(store, holes=a.holes, players=[p.strip() for p in a.players.split(",") if p.strip()],
                   course=a.course, finish=not a.in_progress, rng=random.Random(a.seed),
                   teams=dict(t.split("=", 1) for t in a.teams.split(",") if "=" in t), league=not a.not_league))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
