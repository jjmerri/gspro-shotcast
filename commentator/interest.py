import re

from .hud import Hud
from .models import Shot
from .state import ShotRecord

TROUBLE_LIES = ("water", "hazard", "out of bounds", "bunker", "sand", "trees", "deep rough", "fescue", "native")


def _to_feet(to_pin: str | None) -> float | None:
    """Parse a 'to pin' readout to feet ('8 feet 6 inches', '21 ft', '12 yards')."""
    if not to_pin:
        return None
    if m := re.search(r"(\d+)\s*(?:feet|ft)\b", to_pin, re.I):
        inch = re.search(r"\b(\d+)\s*(?:inch|inches|in)\b", to_pin, re.I)
        return int(m.group(1)) + (int(inch.group(1)) / 12 if inch else 0)
    if m := re.search(r"([\d.]+)\s*(?:yards?|yds?)\b", to_pin, re.I):
        return float(m.group(1)) * 3
    return None


def score_shot(shot: Shot, prior: list[ShotRecord], hud: Hud) -> tuple[float, list[str]]:
    """Rough 0..1 'is this worth talking about' score, plus tags explaining why.

    Tags go into the prompt too, so the model knows what stood out.
    """
    score = 0.0
    tags: list[str] = []
    if hud.lie in TROUBLE_LIES:
        score += 0.6
        tags.append(f"in_{hud.lie.replace(' ', '_')}")
    elif hud.lie == "rough":
        score += 0.5  # a missed green into the rough is a bad shot worth a word
        tags.append("in_rough")
    elif hud.lie == "green" and not shot.is_putt:
        score += 0.5  # found the green: a good approach (often a close one) is worth noting
        tags.append("found_green")

    if not prior:
        score += 0.4
        tags.append("first_shot_of_night")

    if shot.is_putt:
        tags.append("putt")
        if (shot.ball_speed or 0) > 9:
            tags.append("long_putt")
        # A makeable, missable putt (not a tap-in, not a long lag) is a moment make or miss. Tagged,
        # not scored up: the reaction path needs to stay the pre-written one (which knows holed /
        # short / past); the tag just lets it skip the routine dice so it always gets a word.
        feet = _to_feet(hud.to_pin)
        if feet is not None and 3 <= feet <= 12:
            tags.append("makeable_putt")
        return min(score + 0.1, 1.0), tags

    carry = shot.carry or 0
    if carry >= 290:
        score += 0.7
        tags.append("monster_carry")
    elif carry >= 260:
        score += 0.35
        tags.append("big_carry")

    same_club = [r.shot.carry for r in prior if r.shot.club and r.shot.club == shot.club and r.shot.carry]
    if shot.club and len(same_club) >= 3 and carry > max(same_club):
        score += 0.35
        tags.append(f"longest_{shot.club}_tonight")

    hla = shot.hla or 0
    if abs(hla) >= 8:
        score += 0.5
        tags.append("big_miss_right" if hla > 0 else "big_miss_left")
    axis = shot.spin_axis or 0
    if abs(axis) >= 20:
        score += 0.25
        tags.append("big_slice_or_fade" if axis > 0 else "big_hook_or_draw")

    if (shot.ball_speed or 0) < 70 and carry < 60 and shot.club not in (None, "SW", "LW", "GW", "PW"):
        score += 0.5
        tags.append("mishit")

    if carry and abs(hla) < 2 and abs(axis) < 5 and not tags:
        tags.append("pure_strike")
        score += 0.1

    return min(score, 1.0), tags


GOOD_HOLED_PUTT_FEET = 12   # holed from beyond this is more than routine
LONG_HOLED_PUTT_FEET = 20   # ...a big moment
MONSTER_HOLED_PUTT_FEET = 40   # ...as big as putting gets


def holed_putt_level(feet: float | None) -> int | None:
    """How excited (out of 10) the booth should be if a putt from this far drops; None for a routine make."""
    if not feet or feet <= GOOD_HOLED_PUTT_FEET:
        return None
    return 10 if feet > MONSTER_HOLED_PUTT_FEET else 8 if feet > LONG_HOLED_PUTT_FEET else 6


def holed_putt_cue(feet: float | None) -> str:
    """The pre-written "holed" line's excitement cue. A number, not adjectives, so the model scales
    its call without echoing the cue's wording."""
    level = holed_putt_level(feet)
    return f"; excitement {level} out of 10 if it drops" if level else ""


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


SEASON_BEST_DRIVE_LEVEL = 7
LEAGUE_BEST_DRIVE_LEVEL = 8


def holed_putt_score(score: float, hud: Hud, tags: list[str], notable: float, big: float) -> float:
    """A putt that drops is never routine: a makeable one always gets a word, and one from outside
    20 feet (or a fast, long roll when the distance wasn't read) is a big moment. A tap-in stays as is."""
    feet = _to_feet(hud.to_pin)
    if (feet is not None and feet > LONG_HOLED_PUTT_FEET) or (feet is None and "long_putt" in tags):
        return max(score, big)
    if feet is None or feet >= 3:
        return max(score, notable)
    return score

