"""How excited the booth gets about a shot (0 to 10): the pure rules of the app's interest.py that
its highlight reels rank moments by, shared with Shotcast Cloud (shotcast_scoring.highlights). The
app's interest.py keeps its own copy for now; the two are kept the same (its parity test).
"""
from __future__ import annotations

GOOD_HOLED_PUTT_FEET = 12   # holed from beyond this is more than routine
LONG_HOLED_PUTT_FEET = 20   # ...a big moment
MONSTER_HOLED_PUTT_FEET = 40   # ...as big as putting gets

TAP_IN_FEET = 3   # a putt inside this is a tap-in: the shot before it was the moment


def putt_to_par(shot_number: int | None, par: int | None) -> int | None:
    """What holing this putt scores on the hole against par (-1 a birdie, -2 an eagle); None unknown."""
    return shot_number - par if shot_number and par else None


def holed_putt_level(feet: float | None, to_par: int | None = None) -> int | None:
    """How excited (out of 10) the booth should be if a putt from this far drops; None for a routine make.
    `to_par` is what holing it scores on the hole: a putt for eagle or better is always a big call (a
    tap-in one less, the shot before it got the hype), and one for birdie is a notch above the same
    putt for par, but never a tap-in. Unknown distance (0/None) counts as makeable for those."""
    length = (None if not feet or feet <= GOOD_HOLED_PUTT_FEET
              else 10 if feet > MONSTER_HOLED_PUTT_FEET else 8 if feet > LONG_HOLED_PUTT_FEET else 6)
    if to_par is None or to_par > -1:
        return length
    tap_in = bool(feet) and feet < TAP_IN_FEET
    if to_par <= -2:
        return 8 if tap_in else 10 if length else 9
    if tap_in:
        return None
    return min(length + 1, 10) if length else 6


_SHORT_GAME_LIE_BONUS = {"bunker": 2, "sand": 2, "deep rough": 2, "fescue": 2, "native": 2, "trees": 2,
                         "rough": 1, "semi rough": 1, "pine straw": 1, "waste area": 1, "waste": 1, "fringe": -1}


ALMOST_IN_FEET = 1.5   # a ball finishing this close nearly dropped: one more on the scale


def short_game_gimme_level(start_yards: float | None, lie: str | None,
                           finish_feet: float | None = None) -> int | None:
    """How excited (out of 10) the booth should be when a chip or pitch from off the green stops
    inside the gimme circle: harder from further out and from sand or thick rough, and one more if it
    nearly dropped. None for one too simple to hype (a short chip off the fringe or fairway). Tops out
    below a hole-out."""
    lie = (lie or "").lower()
    if not start_yards or lie == "green":
        return None
    level = (5 if start_yards < 10 else 6 if start_yards < 25 else 7) + _SHORT_GAME_LIE_BONUS.get(lie, 0)
    if level < 6:
        return None
    return min(level + (finish_feet is not None and finish_feet <= ALMOST_IN_FEET), 9)


def approach_gimme_level(start_yards: float, finish_feet: float | None = None) -> int:
    """An approach (close_approach_from_yards and up) that stopped inside the gimme circle."""
    return min((9 if start_yards >= 150 else 8) + (finish_feet is not None and finish_feet <= ALMOST_IN_FEET), 9)


def stuffed_level(finish_feet: float) -> int:
    """An approach that finished within close_approach_feet: tap-in range is a bigger call."""
    return 8 if round(finish_feet) <= 5 else 6


def holed_out_level(start_yards: float | None, strokes: int | None, par: int | None,
                    from_yards: float) -> int:
    """A full shot that went in: a hole in one, an eagle or better, or an approach from `from_yards`
    and up is as big as it gets; a chip or pitch in is a 9."""
    if strokes == 1 or (strokes and par and strokes - par <= -2) or (start_yards or 0) >= from_yards:
        return 10
    return 9
