"""One round's league scoring, put together from the rules in handicap.py: a side's strokes from its
players' handicap indexes, the holes those strokes fall on, and a team's net to par hole by hole.

The app's booth scores its live round with these and Shotcast Cloud scores the rounds it has stored,
so a week's result is worked out by one copy of the code.

Holes are whatever the caller keys them by (the app: the hole's place in the round, 1..n; the
server: the course's hole number), as long as `par`, `index`, each side's scores and its strokes
agree and each hole played has a key of its own: a round that plays a hole twice (twice round a
nine) can't be keyed by the course's numbers. Only a week card is always keyed by the course's hole
number (as text), so strokes() takes those numbers.

The app counts a round's holes on from its start hole without wrapping (start_hole + n - 1, so a
round past the 18th has numbers no card has and scores without the card). A caller that wants the
booth's result for such a round passes the same numbers.

Like handicap.py, nothing here validates: callers pass clean data.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import handicap
from .handicap import TEAM_BALL

# Allowances as fractions of each player's handicap, for a league that sets none. One-ball formats
# give the team a share of each player's, lowest handicap first.
DEFAULT_ALLOWANCES: dict[str, list[float]] = {
    "aggregate": [1.0], "best_ball": [0.75], "scramble": [0.25, 0.20], "alternate": [0.25, 0.20],
}


# What a league may call each format, as letters only ("Alt Shot" and "alt-shot" are "altshot").
FORMAT_ALIASES: dict[str, frozenset[str]] = {
    "scramble": frozenset({"scramble", "twopersonscramble", "twomanscramble"}),
    "best_ball": frozenset({"bestball", "fourball", "betterball"}),
    "aggregate": frozenset({"aggregate", "teamsum", "sum", "total", "combined", "teamtotal", "teamaggregate"}),
    "alternate": frozenset({"alternate", "alternateshot", "altshot", "alt", "foursomes"}),
}


def normalize_format(text: object) -> str | None:
    """The format a league's name for one means ("Team Sum" is "aggregate"), or None when it's
    empty or not a name the rules know. Case, spaces, digits and punctuation don't matter.

    Anything that isn't text is read as its text (the app's league file has always been read so), so
    a list holding a name means that name: a caller checking untrusted input makes sure it has a
    string, of a sensible length, first."""
    if not text:
        return None
    key = re.sub(r"[^a-z]", "", str(text).lower())
    return next((fmt for fmt, words in FORMAT_ALIASES.items() if key in words), None)


def allowances(fmt: str | None, custom: Mapping[Any, Any] | None = None) -> list[float]:
    """The format's allowances: the league's own (`custom`: {a format's name: [fractions]}, the
    first entry naming this format whose value is a list, even an empty one), else the default.
    The list's numbers are taken as float() takes them and not checked: a caller with untrusted
    input checks them first."""
    for name, value in (custom or {}).items():
        if normalize_format(name) == fmt and isinstance(value, list):
            return [float(v) for v in value]
    return default_allowances(fmt)


def default_allowances(fmt: str | None) -> list[float]:
    """The format's allowances when the league sets none (an unknown format: the whole handicap)."""
    return list(DEFAULT_ALLOWANCES.get(fmt or "", [1.0]))


def strokes(fmt: str | None, indexes: Sequence[float | None], course_holes: Sequence[int],
            allowances: Sequence[float], card: Mapping[str, Any] | None = None) -> int | None:
    """A side's handicap strokes for the round. `indexes`: its players' handicap indexes, in the
    side's order; `course_holes`: the course's hole numbers played; `allowances`: the format's, used
    as they are without a card and where the card sets none.

    With the week's card (handicap.py describes one): its settings (a USGA course handicap from the
    tee's slope and rating where the format uses one, the max index) and its allowances. A card
    without a par for every hole played is not used. Without one: each index scaled to the holes
    played (an 18-hole index over 9 holes is half), then the allowance.

    None with no players, or with one whose index isn't known."""
    if not indexes or any(i is None for i in indexes):
        return None
    known = [i for i in indexes if i is not None]
    team_ball = fmt in TEAM_BALL
    if card and all(str(h) in card["par"] for h in course_holes):
        used = {**card, "handicap": {**card["handicap"],
                                     "allowances": card["handicap"].get("allowances") or list(allowances)}}
        return handicap.strokes_for(used, known if team_ball else known[:1], list(course_holes), team_ball)
    scaled = [handicap.course_handicap(i, len(course_holes)) for i in known]
    return handicap.side_strokes(scaled, list(allowances), team_ball)


def hole_strokes(given: int, holes: Sequence[int], index: Mapping[int, int]) -> dict[int, int]:
    """The strokes on each of `holes` (in the order played, each once): the hardest first (lowest
    stroke index; a hole absent from `index` counts as 99, after any real one), ties by the order
    played. A plus handicap gives them back from the other end of that order: the easiest first, and
    among ties the one played last. `index` holds real stroke indexes only: a 0 or other stand-in
    for "unknown" would sort as given."""
    place = {h: n for n, h in enumerate(holes)}
    return handicap.allocate(given, sorted(holes, key=lambda h: (index.get(h, 99), place[h])))


@dataclass(frozen=True)
class SideCard:
    """One side of a round as the team's net needs it: who played it, its gross score per hole (None
    or absent: not scored), its strokes for the round (None: a handicap is missing) and the holes
    they fall on. The fields can't be rebound; the mappings are the caller's and are read when scored."""
    players: Sequence[str]
    gross: Mapping[int, int | None]
    strokes: int | None
    hole_strokes: Mapping[int, int] = field(default_factory=dict)

    def scorable(self) -> bool:
        """Whether a net can be worked out: it has strokes, and any it is owed are on their holes."""
        return self.strokes is not None and not (self.strokes and not self.hole_strokes)


def team_holes(fmt: str | None, sides: Sequence[SideCard], par: Mapping[int, int | None],
               holes: Sequence[int], card: dict[str, Any] | None = None,
               members: Collection[str] | None = None) -> dict[int, int] | None:
    """A league team's net to par on each hole it can be counted on, the way Squabbit's leaderboard
    scores it: {hole: net}. `sides`: the team's sides in this round; `holes`: the round's, in order;
    `card`: the week card whose max hole score caps a blow-up; `members`: the team's players in the
    league, when known.

    Scramble and alternate: the team's ball. Best ball: the better net each hole. Aggregate: both
    nets. A one-ball team the round has as several sides (each player's own ball) sums them. A hole
    counts once it has a par and every one of the team's sides has scored it.

    None when there is no format or no side, a side has no strokes (or is owed some that are on no
    hole), no hole counts yet, or (outside the one-ball formats) the sides aren't exactly the team's
    members: the team's score needs every partner."""
    if fmt is None or not sides or not all(s.scorable() for s in sides):
        return None
    if fmt not in TEAM_BALL and members and {p for s in sides for p in s.players} != set(members):
        return None
    counted = [h for h in holes if par.get(h) and all(s.gross.get(h) is not None for s in sides)]
    if not counted:
        return None
    rule = "aggregate" if fmt in TEAM_BALL and len(sides) > 1 else fmt
    return handicap.team_holes(rule, [{h: s.gross.get(h) for h in counted} for s in sides],
                               [s.hole_strokes for s in sides], par, card, counted)


def team_net(fmt: str | None, sides: Sequence[SideCard], par: Mapping[int, int | None],
             holes: Sequence[int], card: dict[str, Any] | None = None,
             members: Collection[str] | None = None) -> tuple[int, int] | None:
    """team_holes summed: (net to par, holes counted), or None when team_holes is."""
    nets = team_holes(fmt, sides, par, holes, card, members)
    return (sum(nets.values()), len(nets)) if nets else None
