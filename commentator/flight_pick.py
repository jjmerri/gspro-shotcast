"""Picks a pre-written mid-flight line from launch data, with no model call at impact.

The pre-shot call has already seen the hole and written one line per outcome; this only
has to decide which outcome the launch numbers point to. The projection is rough (no
physics, no wind), so it only calls a miss or trouble when the numbers are clear-cut.
"""

import math
from dataclasses import dataclass, field

from .facts import to_yards
from .models import Shot

# Point hazards need a distance to be called "in play" (a bunker beside the green isn't a hook's
# problem); hazards that run down a side (water, penalty area, OB) are in play for any big miss.
POINT_HAZARDS = ("bunker", "sand", "trees")


@dataclass
class Hazard:
    kind: str
    side: str  # left / right / short / long
    from_yards: float | None = None
    to_yards: float | None = None
    # A side hazard measured on the minimap: (yards out along the line, yards off it to the hazard's
    # edge, 0 where the line runs over it), so it's in play only for a miss that reaches it.
    edge: tuple[tuple[float, float], ...] = ()
    # A bunker measured on the minimap: yards off the line to its nearest and farthest edges.
    off_yards: float | None = None
    wide_yards: float | None = None


@dataclass
class FlightPlan:
    """What the pre-shot look produced for one setup."""

    player: str | None
    to_pin: str | None
    # From the setup look, so the flight line can be picked and played at impact with no OCR read.
    elevation_yards: float | None = None
    lie: str | None = None
    hazards: list[Hazard] = field(default_factory=list)
    lines: dict[str, str] = field(default_factory=dict)
    # Voiced while the player sets up, so the call plays the instant the ball is struck.
    audio: dict[str, object] = field(default_factory=dict)
    is_putt: bool = False
    # Post-result lines, one per finishing outcome, written at setup and played the instant the ball
    # stops with no model call. Keys are RESULT_OUTCOMES (full shot) or PUTT_RESULT_OUTCOMES (putt).
    reactions: dict[str, str] = field(default_factory=dict)
    reaction_audio: dict[str, object] = field(default_factory=dict)
    # Side -> the hazard kind its trouble line names ("water", "bunker"), when the minimap read saw
    # it. That line plays only when that hazard is the one in play; otherwise the plain side call.
    named: dict[str, str] = field(default_factory=dict)
    # No short/long call (a tee shot on a par 4 or 5, which isn't aimed at reaching the pin).
    lengthless: bool = False


# The ball's final lie (from the HUD OCR or the GSPro log's material, normalised to lower case) ->
# the reaction branch to play for a full shot. Covers both spellings a lie can arrive in (the OCR
# word and the log's material). The rough tiers are kept distinct (semi rough / rough / deep rough)
# so the booth's reaction matches how good or bad the lie really is; pine straw is the trees, concrete
# is a cart path, wood/timber a man-made structure. A lie not in here returns None from pick_reaction,
# which falls back to a live model call, so an unmapped finish never plays the wrong branch. Out of
# bounds and water have no resting lie (the ball is reloaded / dropped); water is picked from the
# GSPro log's Hit_water in the engine, not from a lie, and OB stays the model's to call.
_LIE_REACTION = {
    "green": "green", "fairway": "fairway",
    "semi rough": "semi rough", "semirough": "semi rough",
    "rough": "rough",
    "deep rough": "deep rough", "deeprough": "deep rough",
    "pine straw": "pine straw", "pinestraw": "pine straw", "trees": "pine straw",
    "sand": "sand", "bunker": "sand",
    "waste": "sand", "waste area": "sand", "desert": "sand",
    "cart path": "cart path", "concrete": "cart path",
    "structure": "structure", "wood": "structure", "timber": "structure",
    # Heavy grass the HUD/log can also name (fescue/native) plays like deep rough.
    "deep fescue": "deep rough", "fescue": "deep rough", "native": "deep rough",
    # An OCR-read penalty area ("hazard") resolves to the water/splash reaction.
    "water": "water", "hazard": "water",
}


def pick_reaction(plan: FlightPlan, after_lie: str | None, start_yards: float | None,
                  roll_yards: float | None, holed: bool | None = None,
                  conceded: bool = False) -> tuple[str, str] | None:
    """The pre-written reaction for how the shot actually finished, or None to fall back to a model
    call. Full shots key off the final lie. A putt is "holed" ONLY when GSPro's log or the hole-result
    banner said so (`holed` True); otherwise it's short or past by the roll, never a guessed make (a
    putt that stops a few feet short rolls nearly the full distance). A `conceded` putt (stopped in the
    gimme circle) takes the "gimme" line when a long putt has one, else short/past like any miss."""
    if plan.is_putt:
        if holed:
            outcome = "holed"
        elif conceded and (gimme := plan.reactions.get("gimme")) and gimme.strip().lower() != "null":
            return "gimme", gimme
        elif start_yards and roll_yards is not None:
            # Compare the roll to the distance to the hole: clearly further than the hole is past,
            # otherwise short.
            if roll_yards > start_yards * 1.05:
                outcome = "past"
            else:
                outcome = "short"
        else:
            return None
    else:
        outcome = _LIE_REACTION.get((after_lie or "").strip().lower())
        if outcome is None:
            return None
    line = plan.reactions.get(outcome)
    # A model that returned null for an outcome must never be spoken as the literal "null".
    return (outcome, line) if line and line.strip().lower() != "null" else None


@dataclass
class Projection:
    carry: float
    lateral: float  # yards, + right / - left
    outcome: str
    in_play: list[Hazard] = field(default_factory=list)   # the hazards behind a *_trouble outcome


# A hazard that runs down a side has no measured distance off the line, so only a miss well past the
# fairway and its rough brings it in: a projected 12-30 yard drift called "the water comes into play"
# on four drives and wedges in a row that all finished in the fairway.
SIDE_HAZARD_MIN_YARDS = 25
SIDE_HAZARD_SHARE = 0.15
# Where a measured side hazard counts: from a little short of the carry to where the roll ends, and
# a miss this close to its edge is close enough.
EDGE_SHORT_YARDS = 20
EDGE_LONG_YARDS = 30
EDGE_SLACK_YARDS = 5


def _in_play(h: Hazard, carry: float, lateral: float = float("inf")) -> bool:
    # A point hazard (a bunker) is only in play when it was placed at a distance the carry reaches: a
    # distanceless bunker was firing "trouble" for any shot drifting that way, including one beside or
    # behind the green. A hazard that runs down the side (water, a penalty area, out of bounds) is in
    # play only for a big miss (SIDE_HAZARD_*). When we can't claim the hazard, the plain side call
    # ("drifting left") is still accurate.
    if any(k in h.kind.lower() for k in POINT_HAZARDS) and h.from_yards is None:
        return False
    if h.off_yards is not None and abs(lateral) < h.off_yards - EDGE_SLACK_YARDS:
        return False   # the miss doesn't get out to it
    if h.wide_yards is not None and abs(lateral) > h.wide_yards + EDGE_SLACK_YARDS:
        return False   # nor one flying well past it
    if h.edge:
        # Measured: in play when the miss gets out to the hazard's edge where the ball comes down.
        near = [off for along, off in h.edge if carry - EDGE_SHORT_YARDS <= along <= carry + EDGE_LONG_YARDS]
        return bool(near) and abs(lateral) >= min(near) - EDGE_SLACK_YARDS
    if h.from_yards is None:
        return abs(lateral) > max(SIDE_HAZARD_MIN_YARDS, carry * SIDE_HAZARD_SHARE)
    return h.from_yards - 10 <= carry <= (h.to_yards or h.from_yards) + 15


def project(
    shot: Shot, to_pin: str | None, hazards: list[Hazard], carry_factor: float = 1.0, elevation_yards: float | None = None
) -> Projection | None:
    """`carry_factor` scales the launch monitor's carry to what GSPro will allow from this lie;
    `elevation_yards` (pin above the ball, +) makes the pin play longer or shorter."""
    if not shot.carry or shot.is_putt:
        return None
    carry = shot.carry if (shot.units or "yards").lower().startswith("y") else shot.carry * 1.0936
    carry *= carry_factor
    # Start line plus curve. The curve is driven by sidespin (total spin x the sine of the spin
    # axis), not the axis angle alone, so a high-spin wedge bends far more than a driver at the same
    # axis. Calibrated to a driver: 250 yd, 2500 rpm, 10 deg axis -> ~19 yd (matching the old fit).
    spin, axis = shot.total_spin or 0, shot.spin_axis or 0
    curve = carry * spin * math.sin(math.radians(axis)) * 1.75e-4 if spin else carry * axis * 0.0075
    lateral = carry * math.sin(math.radians(shot.hla or 0)) + curve
    side_tolerance = max(8.0, carry * 0.06)
    side = "right" if lateral > side_tolerance else "left" if lateral < -side_tolerance else None

    if side:
        in_play = [h for h in hazards if h.side == side and _in_play(h, carry, lateral)]
        if in_play:
            return Projection(carry, lateral, f"{side}_trouble", in_play)

    pin = to_yards(to_pin)
    elevated = bool(pin and elevation_yards and abs(elevation_yards) >= 2)
    if elevated:
        pin += elevation_yards  # rule of thumb: a yard of rise plays about a yard longer
    length = None
    if pin and pin <= carry * 1.25:  # green in range; on a tee shot "short of the pin" means nothing
        # The projection has no real physics, and elevation skews the carry in one direction only:
        # downhill a ball runs well past its flat launch-monitor carry (so don't over-call "short"),
        # uphill it falls short of it (so don't over-call "long"). Widen only that side's dead-band
        # by the elevation change, so the booth says "pin high" instead of a confident wrong miss.
        tol = max(5.0, pin * 0.06)
        extra = abs(elevation_yards) * 0.5 if elevated else 0.0
        short_tol = tol + (extra if elevation_yards and elevation_yards < 0 else 0.0)
        long_tol = tol + (extra if elevation_yards and elevation_yards > 0 else 0.0)
        length = "short" if carry < pin - short_tol else "long" if carry > pin + long_tol else None

    # A big sideways miss matters more than being a few yards long or short.
    if side and (not length or abs(lateral) > side_tolerance * 1.5):
        return Projection(carry, lateral, side)
    return Projection(carry, lateral, length or side or "on_target")


def pick_line(
    plan: FlightPlan, shot: Shot, to_pin: str | None, carry_factor: float = 1.0, elevation_yards: float | None = None
) -> tuple[str, str] | None:
    """Returns (outcome, line), falling back from a trouble call to the plain side call."""
    proj = project(shot, None if plan.lengthless else (to_pin or plan.to_pin), plan.hazards, carry_factor,
                   elevation_yards)
    if not proj:
        return None
    for outcome in (proj.outcome, proj.outcome.removesuffix("_trouble")):
        named = plan.named.get(outcome.removesuffix("_trouble")) if outcome.endswith("_trouble") else None
        if named and not any(h.kind == named for h in proj.in_play):
            continue   # the line names a hazard that isn't the one in play: say the side instead
        line = plan.lines.get(outcome)
        if line and line.strip().lower() != "null":  # never speak a model's null outcome as "null"
            return outcome, line
    return None
