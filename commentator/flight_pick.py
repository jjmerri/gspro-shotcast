"""Picks a pre-written mid-flight line from launch data, with no model call at impact.

The pre-shot call has already seen the hole and written one line per outcome; this only
has to decide which outcome the launch numbers point to. The projection is rough (no
physics, no wind), so it only calls a miss or trouble when the numbers are clear-cut.
"""

import math
import re
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
                  conceded: bool = False, cup: str | None = None) -> tuple[str, str] | None:
    """The pre-written reaction for how the shot actually finished, or None to fall back to a model
    call. Full shots key off the final lie. A putt is "holed" ONLY when GSPro's log or the hole-result
    banner said so (`holed` True); otherwise it's short or past by the roll, never a guessed make (a
    putt that stops a few feet short rolls nearly the full distance). A `conceded` putt (stopped in the
    gimme circle) takes the "gimme" line when a long putt has one; with none it falls back too, since a
    short or past line can say another putt is needed when the next one is conceded. `cup` is how the
    ball met the cup (GsproState.cup_of): a putt that lipped out or in takes that line when it has one,
    and a full shot that struck the flagstick or lipped out falls back to the model, which is told."""
    if plan.is_putt:
        special = "lip_in" if holed and cup == "lip_in" else "lip_out" if not holed and cup == "lip_out" else None
        if special and (line := plan.reactions.get(special)) and line.strip().lower() != "null":
            return special, line
        if holed:
            outcome = "holed"
        elif conceded:
            gimme = plan.reactions.get("gimme")
            return ("gimme", gimme) if gimme and gimme.strip().lower() != "null" else None
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
        if cup in ("flagstick", "lip_out"):
            return None
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
    total: float | None = None   # carry plus the expected roll-out, what the length call is judged on
    # Off the line to the target (the pin, with it in reach; else the aim), yards + right: where the start
    # line alone would land it, and how far the curve moves it from there (lateral = start + curve).
    start: float = 0.0
    curve: float = 0.0
    at_pin: bool = False   # the pin is the target: in reach, and not a layup
    length: str | None = None   # "short" / "long" of the (elevation-adjusted) pin, None when pin high or not judged
    gap: float | None = None    # total less the adjusted pin, yards, when length is judged


# Roll-out as a share of carry, from total spin: ~360/spin puts an 8-iron at 6300 rpm near 6% (an
# observed 132 carry rolled 9 yards) and a 10,900 rpm lob wedge near 3%. Clamped so a very low-spin
# strike doesn't project a runaway. Without spin, a typical share for the club.
ROLL_SPIN_FACTOR = 360.0
ROLL_SHARE_MIN, ROLL_SHARE_MAX = 0.02, 0.08
# A yard of rise plays a bit under a yard longer: on the league's approaches GSPro's carry lost a
# median 0.86 yards per yard the pin sat above the ball (36 shots; a 6-yard-uphill lob wedge that
# finished 13 feet away needs no more than this). A drop plays yard for yard.
UPHILL_YARDS_PER_YARD = 0.85


def _roll_share(shot: Shot) -> float:
    if shot.total_spin:
        return min(ROLL_SHARE_MAX, max(ROLL_SHARE_MIN, ROLL_SPIN_FACTOR / shot.total_spin))
    club = (shot.club or "").upper()
    if club in ("PW", "GW", "AW", "SW", "LW"):
        return 0.035
    if club.endswith("I"):
        return 0.06
    if club.startswith("D") or club.endswith(("W", "H")):
        return 0.07
    return 0.05


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

# Where the ball ends up sideways: a share of the start line plus a curve from the sidespin (total spin
# x the sine of the spin axis, so a high-spin wedge bends more than a driver at the same axis). Fitted
# to 953 full shots' finishes in GSPro (the next shot's logged start, off the line to the pin or, from
# a tee, to the hole's aim point): the ball finishes much straighter than its raw numbers say. The old
# 1.0 start line and 1.75e-4 curve missed by a median 9.3 yards on held-out rounds; these by 4.3.
START_LINE_SHARE = 0.58
CURVE_PER_RPM = 5.1e-5
CURVE_RPM_GUESS = 2450   # a shot with no spin reading: a driver's
# GSPro's minimap aim click: the ball sits near (0, AIM_MAP_BALL_Y), with y running up the line to the
# pin and x to its right, AIM_MAP_SPAN units from ball to pin (sources/gspro_log.aim_offset).
AIM_MAP_BALL_Y = -338.0
AIM_MAP_SPAN = 677.0
# A click this far short of the pin is a layup or a way out, not a shot at the flag: in the season's
# aimed shots the ball went about 0.7-0.8 of the way to the click when it was short, 0.85 at the pin.
LAYUP_AIM_SHARE = 0.7
FLAG_WORDS = re.compile(r"\b(flag|flagstick|pin|stick|green|hole|cup)\b", re.I)


def _curve_yards(carry: float, spin: float, axis: float) -> float:
    return carry * (spin or CURVE_RPM_GUESS) * math.sin(math.radians(axis)) * CURVE_PER_RPM


def aim_turn(aim: tuple[float, float] | None) -> float | None:
    """Radians right of the line to the pin that a minimap aim click points, or None (none, or behind)."""
    if not aim or aim[1] - AIM_MAP_BALL_Y <= 0:
        return None
    return math.atan2(aim[0], aim[1] - AIM_MAP_BALL_Y)


def aim_spot(aim: tuple[float, float] | None, pin_yards: float | None) -> tuple[float, float] | None:
    """Where a minimap aim click points: (yards out along the line to the pin, yards right of it)."""
    if not aim or not pin_yards:
        return None
    scale = pin_yards / AIM_MAP_SPAN
    return (aim[1] - AIM_MAP_BALL_Y) * scale, aim[0] * scale


def spot_words(spot: tuple[float, float]) -> str:
    """'a spot about 60 yards out', or for a click level with or behind the ball, which way it went."""
    along, right = spot
    if along >= 5:
        return f"a spot about {along:.0f} yards out"
    side = "right" if right > 0 else "left"
    return f"a spot {'back and ' if along <= -5 else ''}out to the {side}, about {abs(right):.0f} yards sideways"


def aimed_short(aim: tuple[float, float] | None, pin_yards: float | None) -> bool:
    """The player clicked a spot well short of the pin: laying up or getting out, not going at the flag."""
    spot = aim_spot(aim, pin_yards)
    return bool(spot) and spot[0] < LAYUP_AIM_SHARE * pin_yards


def aim_facts(aim: tuple[float, float] | None, pin_yards: float | None) -> tuple[list[str], bool]:
    """Where the player aimed, as booth facts, and whether it was short of the green (a layup or a way out)."""
    spot = aim_spot(aim, pin_yards)
    if not spot:
        return [], False
    along, right = spot
    way = f"{abs(right):.0f} yards {'right' if right > 0 else 'left'} of the line to the pin"
    if along < LAYUP_AIM_SHARE * pin_yards:
        off = f", {way}" if abs(right) >= 5 and along >= 5 else ""
        return [f"The player aimed at {spot_words(spot)}{off}, short of the green (the pin is "
                f"{pin_yards:.0f} yards): the target is that spot, not the flag."], True
    if abs(right) >= 3:
        return [f"The player aimed about {way}."], False
    return [], False


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
    shot: Shot, to_pin: str | None, hazards: list[Hazard], carry_factor: float = 1.0, elevation_yards: float | None = None,
    aim: tuple[float, float] | None = None,
) -> Projection | None:
    """`carry_factor` scales the launch monitor's carry to what GSPro will allow from this lie;
    `elevation_yards` (pin above the ball, +) makes the pin play longer or shorter; `aim` is the
    player's minimap aim click for this shot (GSPro's log, in minimap units), if they moved it."""
    if not shot.carry or shot.is_putt:
        return None
    carry = shot.carry if (shot.units or "yards").lower().startswith("y") else shot.carry * 1.0936
    carry *= carry_factor
    start = START_LINE_SHARE * carry * math.sin(math.radians(shot.hla or 0))
    curve = _curve_yards(carry, shot.total_spin or 0, shot.spin_axis or 0)
    pin = to_yards(to_pin)
    if aimed_short(aim, pin):
        pin = None   # a layup or a way out: the spot clicked is the target, and short of the pin is the plan
    # The launch monitor measures the start line from where the player aimed. With the green in reach the
    # pin is the target, so a click right of it to play a draw is turned into a miss off the pin line; out
    # of reach (a layup, a drive round a dogleg) the spot they aimed at is the target itself.
    turn = aim_turn(aim)
    at_pin = bool(pin and pin <= carry * 1.25)
    if turn and at_pin:
        start, curve = start * math.cos(turn) + carry * math.sin(turn), curve * math.cos(turn)
    lateral = start + curve
    side_tolerance = max(8.0, carry * 0.06)
    total = carry * (1 + _roll_share(shot))
    side = "right" if lateral > side_tolerance else "left" if lateral < -side_tolerance else None

    elevated = bool(pin and elevation_yards and abs(elevation_yards) >= 2)
    if elevated:
        pin += elevation_yards * (UPHILL_YARDS_PER_YARD if elevation_yards > 0 else 1.0)
    length = gap = None
    if pin and pin <= carry * 1.25:  # green in range; on a tee shot "short of the pin" means nothing
        # The projection has no real physics, and elevation skews the carry in one direction only:
        # downhill a ball runs well past its flat launch-monitor carry (so don't over-call "short"),
        # uphill it falls short of it (so don't over-call "long"). Widen only that side's dead-band
        # by the elevation change, so the booth says "pin high" instead of a confident wrong miss.
        tol = max(5.0, pin * 0.06)
        extra = abs(elevation_yards) * 0.5 if elevated else 0.0
        short_tol = tol + (extra if elevation_yards and elevation_yards < 0 else 0.0)
        long_tol = tol + (extra if elevation_yards and elevation_yards > 0 else 0.0)
        gap = total - pin
        length = "short" if gap < -short_tol else "long" if gap > long_tol else None
    shape = {"start": start, "curve": curve, "at_pin": at_pin, "length": length, "gap": gap}

    if side:
        in_play = [h for h in hazards if h.side == side and _in_play(h, carry, lateral)]
        if in_play:
            return Projection(carry, lateral, f"{side}_trouble", in_play, total, **shape)

    # A big sideways miss matters more than being a few yards long or short.
    if side and (not length or abs(lateral) > side_tolerance * 1.5):
        return Projection(carry, lateral, side, total=total, **shape)
    return Projection(carry, lateral, length or side or "on_target", total=total, **shape)


def _bend(p: Projection) -> float:
    return max(4.0, p.carry * 0.03)   # less curve than this isn't a shape worth calling


def _near(p: Projection) -> float:
    return max(8.0, p.carry * 0.06)   # project()'s side tolerance: "on the flag's line"


def curving_back(p: Projection | None) -> bool:
    """Started off the flag's line and curving back onto it: the draw or fade working back to the pin."""
    if not p or not p.at_pin or abs(p.curve) < _bend(p) or abs(p.start) < _near(p) * 0.6:
        return False
    return (p.start > 0) != (p.curve > 0) and abs(p.lateral) <= _near(p)


def shape_facts(p: Projection | None, aim: tuple[float, float] | None = None, pin_yards: float | None = None) -> list[str]:
    """The ball's shape against the flag, with the green in reach: where it started from the flag and
    whether the curve brings it back or takes it away, so the booth can call a draw coming back to the
    pin (the launch monitor's start line is from the aim, which may be well off the flag)."""
    if not p or not p.at_pin or abs(p.curve) < _bend(p):
        return []
    cside = "right" if p.curve > 0 else "left"
    near = _near(p)
    facts = []
    if abs(p.start) < near * 0.6:
        facts.append(f"It started at the flag and is curving away to the {cside}.")
    else:
        sside = "right" if p.start > 0 else "left"
        if sside == cside:
            facts.append(f"It started {sside} of the flag and is curving further {cside}, away from it.")
        elif abs(p.lateral) <= near:
            facts.append(f"It started {sside} of the flag and is curving back toward it, on course to come back "
                         "onto the flag's line.")
        elif (p.lateral > 0) == (p.start > 0):
            facts.append(f"It started {sside} of the flag and is curving back toward it, but may not get all the way back.")
        else:
            facts.append(f"It started {sside} of the flag and is curving back across the flag's line to the {cside}.")
    spot = aim_spot(aim, pin_yards)
    if spot and abs(spot[1]) >= 3 and (spot[1] > 0) != (p.curve > 0):
        facts.append(f"That's the shape the player set up for: aimed {'right' if spot[1] > 0 else 'left'} of the flag "
                     f"to bring it back.")
    return facts


def pick_line(
    plan: FlightPlan, shot: Shot, to_pin: str | None, carry_factor: float = 1.0, elevation_yards: float | None = None,
    aim: tuple[float, float] | None = None,
) -> tuple[str, str] | None:
    """Returns (outcome, line), falling back from a trouble call to the plain side call."""
    proj = project(shot, None if plan.lengthless else (to_pin or plan.to_pin), plan.hazards, carry_factor,
                   elevation_yards, aim)
    if not proj:
        return None
    # The lines were written before the player clicked a spot short of the green: one that has the ball
    # going at the flag is wrong for a layup or a way out (the live call, told the aim, says it instead).
    layup = not plan.lengthless and aimed_short(aim, to_yards(to_pin or plan.to_pin))
    special = None
    if layup and proj.outcome == "on_target":
        special = "layup"   # heading at the spot the player picked
    elif curving_back(proj) and (proj.outcome == "on_target" or (
            proj.outcome in ("short", "long") and abs(proj.gap) <= max(10.0, (proj.total - proj.gap) * 0.1))):
        special = "coming_back"   # the shape is the story; a little short or long is no contradiction
    if special and (line := plan.lines.get(special)) and line.strip().lower() != "null":
        return special, line
    for outcome in (proj.outcome, proj.outcome.removesuffix("_trouble")):
        named = plan.named.get(outcome.removesuffix("_trouble")) if outcome.endswith("_trouble") else None
        if named and not any(h.kind == named for h in proj.in_play):
            continue   # the line names a hazard that isn't the one in play: say the side instead
        line = plan.lines.get(outcome)
        if line and line.strip().lower() != "null":  # never speak a model's null outcome as "null"
            return None if layup and FLAG_WORDS.search(line) else (outcome, line)
    return None
