"""Turns launch data into plain statements so the model never interprets numbers or counts.

Small models are poor at reading spin axes and tallying history, and that's where most
invented claims came from. Everything here is computed; the model only phrases it.
"""

import re
from dataclasses import replace

from .models import Shot
from .state import ShotRecord

CLUB_NAMES = {
    "DR": "driver", "PT": "putter", "LW": "lob wedge", "SW": "sand wedge", "GW": "gap wedge",
    "PW": "pitching wedge", "AW": "approach wedge",
}


def club_name(code: str | None) -> str | None:
    if not code:
        return None
    code = code.upper()
    if code in CLUB_NAMES:
        return CLUB_NAMES[code]
    kinds = {"W": "wood", "H": "hybrid", "I": "iron"}
    if len(code) >= 2 and code[-1] in kinds and code[:-1].isdigit():
        return f"{code[:-1]}-{kinds[code[-1]]}"
    return code


def _direction(hla: float | None) -> str | None:
    if hla is None:
        return None
    side = "right" if hla > 0 else "left"
    if abs(hla) < 2:
        return "started on line"
    if abs(hla) < 6:
        return f"started a little {side}"
    return f"started well {side}"


def _curve(axis: float | None) -> str | None:
    if axis is None:
        return None
    side = "right" if axis > 0 else "left"
    if abs(axis) < 5:
        return "flew fairly straight"
    if abs(axis) < 15:
        return f"curved gently {side}"
    return f"curved hard {side}"


def shot_facts(shot: Shot) -> list[str]:
    club = club_name(shot.club)
    if shot.is_putt:
        pace = "a firm putt" if (shot.ball_speed or 0) > 9 else "a gentle putt"
        # No start line, face or path: a putt is aimed for the break, so starting left or right of the
        # hole is usually the read, not a miss, and the booth was calling a "closed face" on putts.
        return [f"It was {pace}.", "Putts are aimed to allow for break: don't comment on the putter face, "
                "the path or the start line."]
    parts = [f"Club: {club}." if club else "Club: not known."]
    flight = [p for p in (_direction(shot.hla), _curve(shot.spin_axis)) if p]
    if flight:
        parts.append(f"The ball {' and '.join(flight)}.")
    if shot.carry:
        parts.append(f"Carry: {round(shot.carry)} {shot.units.lower()}.")
    if shot.ball_speed and shot.ball_speed >= 100:
        parts.append(f"Ball speed: {round(shot.ball_speed)} mph.")
    return parts


# Launch-monitor club-delivery conventions (GSPro Connect / Uneekor). Documented here because the
# wording below depends on the sign, so a reading that ever reads backwards is a one-line flip:
#   AngleOfAttack  + = hitting up (ascending),   - = hitting down (descending)
#   FaceToTarget   + = face open,                - = face closed   (degrees vs target)
#   Path           + = in-to-out,                - = out-to-in     (degrees)
#   face-to-path (face - path): + = face open to the path (fade), - = closed (draw)
#   HorizontalFaceImpact + = toe,  - = heel   (mm; sign not yet confirmed, revisit if it reads wrong)
#   VerticalFaceImpact   + = high, - = low    (mm; sign not yet confirmed, revisit if it reads wrong)
def club_delivery_facts(shot: Shot) -> list[str]:
    """Club-delivery colour from the launch monitor's own club data: angle of attack, swing path,
    face-to-path shape, smash factor and strike location. Only players with the EYE XO2 club markers
    get this (club_measured); silent for everyone else. Each reading is sanity-bounded so a bad frame
    never airs, and only clear deviations are stated so routine deliveries don't add noise."""
    if shot.is_putt or not shot.club_measured:
        return []
    facts: list[str] = []
    is_driver = (shot.club or "").upper() == "DR"

    aoa = shot.angle_of_attack
    if aoa is not None and -20 <= aoa <= 20 and abs(aoa) >= 1.5:
        if aoa > 0:
            tail = ", adding launch off the tee" if is_driver else ", catching it on the way up"
            facts.append(f"Angle of attack {aoa:+.1f} degrees, hitting up on the ball{tail}.")
        else:
            tail = ", a steep hit for a driver" if is_driver else ", a clean ball-then-turf strike"
            facts.append(f"Angle of attack {aoa:.1f} degrees, a descending blow{tail}.")

    face, path = shot.face_to_target, shot.path
    if path is not None and -20 <= path <= 20 and abs(path) >= 2:
        facts.append(f"Swing path {path:+.1f} degrees, {'in-to-out' if path > 0 else 'out-to-in'}.")
    if face is not None and path is not None and -20 <= face <= 20 and -20 <= path <= 20:
        f2p = face - path
        if f2p >= 1.5:
            facts.append(f"The face was {f2p:.1f} degrees open to the path, the makings of a fade.")
        elif f2p <= -1.5:
            facts.append(f"The face was {abs(f2p):.1f} degrees closed to the path, the makings of a draw.")

    if shot.ball_speed and shot.club_speed and shot.club_speed >= 25:
        smash = shot.ball_speed / shot.club_speed
        if 1.40 <= smash <= 1.6:
            facts.append(f"Smash factor {smash:.2f}: flushed it, {round(shot.ball_speed)} mph ball "
                         f"off {round(shot.club_speed)} mph of club.")

    h, v = shot.horizontal_face_impact, shot.vertical_face_impact
    where = []
    if h is not None and 6 <= abs(h) <= 50:
        where.append("toward the toe" if h > 0 else "toward the heel")
    if v is not None and 6 <= abs(v) <= 50:
        where.append("high on the face" if v > 0 else "low on the face")
    if where:
        facts.append("Strike: caught it " + " and ".join(where) + ".")
    return facts


def club_delivery_notable(shot: Shot) -> bool:
    """Whether the club delivery is interesting enough that the shot deserves a live call even if the
    result was ordinary (a standout strike, shape, or a clear mishit)."""
    if shot.is_putt or not shot.club_measured:
        return False
    aoa, face, path = shot.angle_of_attack, shot.face_to_target, shot.path
    if aoa is not None and abs(aoa) >= 5:
        return True
    if face is not None and path is not None and abs(face - path) >= 4 and abs(face) <= 20 and abs(path) <= 20:
        return True
    if shot.ball_speed and shot.club_speed and shot.club_speed >= 25 and shot.ball_speed / shot.club_speed >= 1.47:
        return True
    for x in (shot.horizontal_face_impact, shot.vertical_face_impact):
        if x is not None and 12 <= abs(x) <= 50:
            return True
    return False


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def history_facts(player: str | None, prior: list[ShotRecord]) -> list[str]:
    who = player or "This player"
    whose = who + ("'" if who.endswith("s") else "'s")
    # The running count is background: aired, it became trivia ("the 23rd shot of the morning").
    facts = [f"This is {whose} {ordinal(len(prior) + 1)} shot today (background: don't quote the running count)."]
    full = [r for r in prior if not r.shot.is_putt]
    if full:
        last = full[-1].shot
        if full[-1].carry_game:   # GSPro's carry, not the launch monitor's flat one ("the 269-yard drive")
            last = replace(last, carry=full[-1].carry_game, units="Yards")
        desc = " ".join(shot_facts(last)).replace("Club: ", "").rstrip(".")
        facts.append(f"{whose} previous full shot: {desc}.")
    # Only state a streak when the code has counted it, so the model can't invent one.
    sides = [("right" if r.shot.hla > 2 else "left" if r.shot.hla < -2 else "straight")
             for r in full if r.shot.hla is not None]
    if sides:
        streak, side = 1, sides[-1]
        for s in reversed(sides[:-1]):
            if s != side:
                break
            streak += 1
        if streak >= 2 and side != "straight":
            facts.append(f"{whose} last {streak} full shots before this one all started {side}.")
    return facts


DIST = re.compile(r"(\d+(?:\.\d+)?)\s*(yards|metres|feet)(?:\s+(\d+)\s+inches)?")
SCORE_NAMES = {-3: "an albatross", -2: "an eagle", -1: "a birdie", 0: "a par", 1: "a bogey", 2: "a double bogey"}
PUTT_FOR = {-3: "albatross", -2: "eagle", -1: "birdie", 0: "par", 1: "bogey", 2: "double bogey", 3: "triple bogey"}
PUTT_FOR_FACT = re.compile(r"This putt is for ([a-z ]+)\. If it misses, the next putt is for ([a-z ]+),")
_PUTT_FOR_SAID = re.compile(r"\bfor (?:an? )?(albatross|eagle|birdie|par|(?:double |triple )?bogey)\b", re.I)


_NEXT_PUTT = re.compile(r"comeback|come-back|\bnext\b|another|return|tap-in|left with|"
                        r"\bleaves (?:himself|herself|themselves|him|her|them|a|an)\b|\bleft\b(?= \w+ putt)", re.I)
_CLAUSE = re.compile(r"[;.!?\u2014\u2013]|,\s|\bbut\b|\band\b")


def wrong_putt_score(text: str, this: str, nxt: str, holed: bool) -> str | None:
    """The score a pre-written putt reaction says a putt is "for" when that's wrong: a holed putt is
    for `this`; after a miss, the putt just missed is still for `this` ("that was for par, and it
    slides by") but the one left over is for `nxt` (not "the comeback putt for par" on a bogey putt)."""
    for m in _PUTT_FOR_SAID.finditer(text):
        said = m.group(1).lower()
        if holed:
            ok = said == this
        else:
            # Only the putt this phrase is about: the words before it in its own clause.
            clause = _CLAUSE.split(text[:m.start()])[-1]
            ok = said == nxt or (said == this and not _NEXT_PUTT.search(clause))
        if not ok:
            return said
    return None


def to_yards(distance: str | None) -> float | None:
    """Parses the HUD's "84.8 yards" / "98.2 metres" / "10 feet 8 inches"."""
    if not distance or not (m := DIST.search(distance)):
        return None
    value, unit, inches = float(m.group(1)), m.group(2), m.group(3)
    if unit == "metres":
        return value * 1.0936
    if unit == "feet":
        return (value + (int(inches) / 12 if inches else 0)) / 3
    return value


def _to_par_value(score_to_par: str | None) -> int | None:
    """GSPro's round score-to-par ("E", "+2", "-1") as an int, or None."""
    if not score_to_par:
        return None
    if score_to_par.strip().upper() == "E":
        return 0
    try:
        return int(score_to_par.strip())
    except ValueError:
        return None


def _fmt_to_par(value: int) -> str:
    return "even par" if value == 0 else (f"+{value}" if value > 0 else str(value))


def slope_facts(hud, handed: str | None = None) -> list[str]:
    """The minimap footer's slope under the ball, when it's enough to matter.

    GSPro documents the sideways reading for right-handers: LEFT means the ball is above the
    feet (tends to start left), RIGHT means below the feet (tends to start right). Left-handed
    behaviour isn't documented, so lefties get the neutral wording.
    """
    facts = []
    if hud.slope_updown is not None and abs(hud.slope_updown) >= 2:
        if hud.slope_updown > 0:
            facts.append(f"It's a {hud.slope_updown:.0f}-degree uphill lie, which adds height and can cost distance.")
        else:
            facts.append(f"It's a {-hud.slope_updown:.0f}-degree downhill lie, which flies it lower with more roll.")
    if hud.slope_side and (hud.slope_side_degrees or 0) >= 2:
        degrees = f"{hud.slope_side_degrees:.0f} degrees"
        if (handed or "").upper() == "RH":
            if hud.slope_side == "left":
                facts.append(f"The ball is above the player's feet ({degrees}), which tends to start it left.")
            else:
                facts.append(f"The ball is below the player's feet ({degrees}), which tends to start it right.")
        else:
            facts.append(f"The lie is tilted {degrees} sideways.")
    return facts


# On a green a few inches of rise or fall changes the putt; under this it's flat enough to skip.
PUTT_ELEVATION_INCHES = 3


def putt_elevation_inches(hud) -> int | None:
    """The putt's rise (+) or fall (-) in whole inches when it's enough to matter, else None."""
    if hud.elevation_yards is None or (hud.lie or "").lower() != "green":
        return None
    inches = round(hud.elevation_yards * 36)
    return inches if abs(inches) >= PUTT_ELEVATION_INCHES else None


def elevation_facts(hud) -> list[str]:
    if (hud.lie or "").lower() == "green":
        if (inches := putt_elevation_inches(hud)) is None:
            return []
        way = "uphill" if inches > 0 else "downhill"
        n = abs(inches)
        size = f"{n} inches" if n < 12 else f"{n // 12} feet" + (f" {n % 12} inches" if n % 12 else "")
        fact = f"The putt is {size} {way}: the hole sits {'above' if inches > 0 else 'below'} the ball."
        if n >= 12:
            fact += (" That's a big climb for a putt; it has to be hit firmly and will play much longer."
                     if inches > 0 else
                     " That's a big drop for a putt; it'll run fast, and just touching it is the challenge.")
        return [fact]
    if hud.elevation_yards is None or abs(hud.elevation_yards) < 2:
        return []
    way = "uphill" if hud.elevation_yards > 0 else "downhill"
    return [f"The pin is about {abs(hud.elevation_yards):.0f} yards {way} from the ball."]


def flight_facts(
    shot: Shot, to_pin: str | None, carry_factor: float = 1.0, lie: str | None = None,
    elevation_yards: float | None = None,
) -> list[str]:
    """What a commentator knows the instant the ball leaves the club."""
    facts = ["The ball is in the air right now. It has not landed."]
    if lie:
        facts.append(f"It was hit from the {lie}.")
    facts += [f for f in shot_facts(shot) if not f.startswith(("Carry:", "Ball speed:"))]
    pin = to_yards(to_pin)
    if pin and elevation_yards and abs(elevation_yards) >= 2:
        pin += elevation_yards  # plays longer uphill, shorter downhill
    carry = shot.carry if (shot.units or "yards").lower().startswith("y") else (shot.carry or 0) * 1.0936
    carry = (carry or 0) * carry_factor  # GSPro takes distance off for rough, sand and so on
    if not (pin and carry):
        return facts
    gap = carry - pin
    if pin > carry * 1.25:
        facts.append("The green is out of reach with this shot; it's about position.")
    elif abs(gap) <= max(3, pin * 0.05):
        facts.append("The launch monitor projects it to carry right around the pin distance.")
    elif gap < 0:
        facts.append(f"The launch monitor projects it to carry about {round(-gap)} yards short of the pin.")
    else:
        facts.append(f"The launch monitor projects it to carry about {round(gap)} yards past the pin.")
    return facts


def preshot_facts(hud, hole_par: int | None, handed: str | None = None) -> list[str]:
    facts = []
    if hud.shot_number:
        facts.append(f"This is shot {hud.shot_number} on this hole.")
    putting = (hud.lie or "").lower() == "green"
    if hud.to_pin:
        # Putts are always spoken in feet: GSPro's HUD switches a long putt to yards, which the booth
        # then read out as "17 yards" for a putt. Convert any on-the-green distance to feet.
        pin_yards = to_yards(hud.to_pin)
        if putting and pin_yards is not None:
            feet = pin_yards * 3
            facts.append("The ball is a foot or less from the hole, a tap-in." if feet < 1.5
                         else f"The ball is about {feet:.0f} feet from the hole.")
        else:
            facts.append(f"The pin is {hud.to_pin} away.")
    if hud.lie:
        prep = "on" if hud.lie in ("green", "tee", "fringe") else "in"
        facts.append(f"The ball currently lies {prep} the {hud.lie}.")
    round_par = _to_par_value(hud.score_to_par)
    if hud.score_to_par:
        score = "even par" if round_par == 0 else f"{_fmt_to_par(round_par)}"
        # This is the total through COMPLETED holes; the hole in play isn't counted until it's holed.
        facts.append(f"Player's round score through the holes already completed: {score}. This hole is "
                     "still in progress, so its strokes are not in that number yet.")
    facts += elevation_facts(hud) + slope_facts(hud, handed)
    # On a putt, state what holing it scores on the hole and where that leaves the round, so the booth
    # never calls a score "intact" when the hole in play is about to change it. Off the green, holing out
    # is a long shot ("a birdie still available" before a 23-yard chip on a par 4), so the facts give
    # what getting up and down scores instead.
    diff = (hud.shot_number - hole_par) if (hud.shot_number and hole_par) else None
    if putting and diff is not None and (name := SCORE_NAMES.get(diff)):
        line = f"If this putt is holed it's {name} on the hole"
        if round_par is not None:
            line += f", which takes the round to {_fmt_to_par(round_par + diff)}"
        facts.append(line + ".")
    elif (pin := to_yards(hud.to_pin)) is not None and pin < 60 and diff is not None \
            and (name := SCORE_NAMES.get(diff + 1)):
        facts.append(f"Getting up and down from here (this shot close, then one putt) makes {name} on the "
                     "hole. Holing this shot is a long shot: don't talk as if it's likely or expected.")
    # A missed putt's comeback is for the next score, not this one: a pre-written "past" reaction
    # carried "for par" over to a comeback that was for bogey.
    if putting and diff is not None and (this := PUTT_FOR.get(diff)) and (nxt := PUTT_FOR.get(diff + 1)):
        facts.append(f"This putt is for {this}. If it misses, the next putt is for {nxt}, not {this}.")
    return facts
