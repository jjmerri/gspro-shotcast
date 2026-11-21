"""Handicap strokes and team nets the way the league's Squabbit computes them.

These rules reproduce every 2025 team result on Squabbit's leaderboards (the app's
`python -m commentator squabbit --validate`):

- Each format's handicap settings say whether to use a USGA course handicap. Aggregate does:
  index x slope/113 + (rating - par), scaled to the holes played (a 9-hole round uses half the
  index and half the rating, against the par of the nine). The others use the bare index scaled to
  the holes played. The index is capped at the format's max handicap (54 for aggregate).
- Then the format's percentage: one per player (best ball 75%, aggregate 100%), or for one-ball
  formats a percentage per teammate, lowest handicap first (25% + 20%), summed before rounding.
- Strokes go to the played holes in stroke-index order (the tee's own index), the hardest first.
  A plus handicap gives strokes back on the easiest holes.

A week card is a plain dict: {"tee": {"rating", "slope"}, "par": {"<hole>": par}, "handicap":
{"use_course_handicap": bool, "allowances": [fractions] | None, "max_index": float | None,
"max_hole_score": {"doublePar", "amountOverPar", "isGross"} | None}}.

Formats: "best_ball", "aggregate", "scramble" and "alternate" (the last two play one team ball).

Callers must pass clean data; the package does not validate. These are the app's rules as they've
always run, taking what the app has always passed (a par of "4" on a card counts as 4, as int() takes
it) and failing as they always have (the Shotcast Cloud server validates its input before calling
them). team_holes, new with the package, has a few rules of its own: see its docstring.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

# One ball per team: the side that owns a score is the team.
TEAM_BALL = ("scramble", "alternate")
FORMATS = ("best_ball", "aggregate", *TEAM_BALL)


def round_half_up(x: float) -> int:
    """Handicaps round .5 away from zero (Python's round() would take 2.5 to 2)."""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def course_handicap(index: float, holes: int, *, use_course: bool = False, rating: float | None = None,
                    slope: float | None = None, par: int | None = None, max_index: float | None = None) -> float:
    """Unrounded handicap for `holes` holes. `par` is the par of just the holes played."""
    if max_index is not None:
        index = min(index, max_index)
    share = holes / 18
    if use_course and rating is not None and slope and par is not None:
        return index * share * slope / 113 + (rating * share - par)
    return index * share


def side_strokes(handicaps: list[float], allowances: list[float], team_ball: bool) -> int:
    """A side's strokes from its players' unrounded handicaps. One ball: lowest handicap takes the
    first allowance, the next the second, summed then rounded. Otherwise the side is one player."""
    if team_ball:
        return round_half_up(sum(h * a for h, a in zip(sorted(handicaps), allowances)))
    return round_half_up(handicaps[0] * allowances[0])


def allocate(strokes: int, order: list[int]) -> dict[int, int]:
    """Strokes per hole, `order` listing the holes hardest first. A plus handicap (negative strokes)
    gives them back from the easiest hole up."""
    out = {h: 0 for h in order}
    if not order:
        return out
    for i in range(abs(strokes)):
        if strokes > 0:
            out[order[i % len(order)]] += 1
        else:
            out[order[-1 - i % len(order)]] -= 1
    return out


def hole_cap(card: dict[str, Any], par: int, strokes: int) -> int | None:
    """The most a hole can count for, gross, under the format's max hole score (e.g. net par + 4)."""
    rule = (card.get("handicap") or {}).get("max_hole_score") or {}
    if rule.get("doublePar"):
        cap = 2 * par
    elif rule.get("amountOverPar") is not None:
        cap = par + int(rule["amountOverPar"])
    else:
        return None
    return cap if rule.get("isGross") else cap + strokes


def strokes_for(card: dict[str, Any], indexes: list[float], holes: list[int], team_ball: bool,
                tee: dict[str, Any] | None = None) -> int:
    """A side's strokes over `holes` from its players' indexes, by the week card's settings."""
    hc = card["handicap"]
    tee = tee or card["tee"]
    par = sum(int(card["par"][str(h)]) for h in holes)
    cap = hc.get("max_index")
    per = [course_handicap(i, len(holes), use_course=hc.get("use_course_handicap", False), rating=tee.get("rating"),
                           slope=tee.get("slope"), par=par, max_index=cap) for i in indexes]
    allow = hc.get("allowances") or [1.0]
    if not team_ball and not hc.get("use_course_handicap"):
        return side_strokes(per, allow, False)
    if not team_ball:   # a USGA course handicap is a whole number before any percentage
        return side_strokes([round_half_up(per[0])], allow, False)
    return side_strokes(per, allow, True)


def capped(card: dict[str, Any] | None, gross: int, par: int, strokes: int) -> int:
    """A hole's gross score as it counts: capped by the card's max hole score (no card: as it is)."""
    cap = hole_cap(card, par, strokes) if card else None
    return gross if cap is None else min(gross, cap)


def team_holes(fmt: str, gross: Sequence[Mapping[int, int | None]], strokes: Sequence[Mapping[int, int]],
               par: Mapping[int, int | None], card: dict[str, Any] | None = None,
               holes: Sequence[int] | None = None, *, complete_sides: bool = True) -> dict[int, int]:
    """A team's net to par hole by hole: {hole: net}. `fmt` one of FORMATS; `gross` and `strokes` per
    side ({hole: gross score}, {hole: handicap strokes}, a hole missing from it 0); `par` per hole;
    `card` the week card whose max hole score caps a blow-up (None: no cap); `holes` the holes to
    score, in order (default: par's, sorted).

    Each side's net to par is its capped score less its strokes less par; best ball counts the best of
    them, any other format their sum. A hole counts once every side has a score on it
    (`complete_sides`, the live round's rule) or, with complete_sides=False, once any side has (the
    others left out of it: Squabbit's leaderboard over its posted rounds). A hole with no score (None
    or absent) or no par (None or absent) is left out of the result.

    ValueError: sides' scores and strokes of different lengths, or a one-ball format (scramble,
    alternate) with more than one side."""
    if len(gross) != len(strokes):
        raise ValueError(f"{len(gross)} sides' scores but {len(strokes)} sides' strokes")
    if fmt in TEAM_BALL and len(gross) > 1:
        raise ValueError(f"a {fmt} team plays one ball: one side, not {len(gross)}")
    out = {}
    for h in sorted(par) if holes is None else holes:
        if par.get(h) is None:
            continue
        scored = [(g[h], s.get(h, 0)) for g, s in zip(gross, strokes) if g.get(h) is not None]
        if not scored or complete_sides and len(scored) < len(gross):
            continue
        nets = [capped(card, g, par[h], given) - given for g, given in scored]
        if fmt in TEAM_BALL:
            out[h] = nets[0] - par[h]
        elif fmt == "best_ball":
            out[h] = min(nets) - par[h]
        else:
            out[h] = sum(n - par[h] for n in nets)
    return out
