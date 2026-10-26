"""Reads GSPro's own Unity log (output_log.txt) for authoritative game state.

GSPconnect's ConnectDebug.txt (our shot source) carries launch-monitor ball data only. GSPro the
simulator writes a separate log with the game truth the screen can only hint at: whose turn it is,
the hole, the strokes taken so far (penalties included), the lie, the distance to the pin, and
whether a ball was holed. We follow it by byte offset (it is append-only for a GSPro session, no
rotation) and keep a running snapshot the engine reads instead of, or to correct, OCR.

Two quirks shape the parser. There are no timestamps, so freshness comes from tailing newly
appended bytes, not from a clock. And players are a 0-based index, never a name, so the index maps
to the setup-screen roster order. Lines are interleaved with Unity engine noise and
"(Filename: ...)" blocks, which are ignored.

The score line `ActivePlayer: 0  currentHole: 3  strokes: 1  Previous Score: 2` is logged as each
shot settles: "strokes" were just added to the "Previous Score", so the player now has 3 (Previous
Score alone is one stroke behind). How a penalty shows up: a score line with no shot fired since the
last one. A conceded gimmie also bumps the score with no shot fired, but is preceded by the "within
Gimmie distance" line, so the two are told apart by what happened since the previous score line.

GSPro also takes strokes back. A mulligan logs a negative `strokes` (the shot, and any penalty it
drew, removed) before the re-hit, or sometimes nothing, so the re-hit's score line simply repeats
the previous one. A pick-up (the hole capped at its maximum, e.g. quad bogey) logs every stroke
removed, then the capped score written in with no shot, then the conceding stroke.

A ball in the water is flagged at the splash, logged the instant the ball goes in - well before
the penalty score bump, which only lands after the drop animation. The splash shows up as a
`Hit_water` line and a `| material water |` bounce-sound line (newer GSPro), or only as
`Using Material TVGwater` (older); all three set the flag. That early signal drives the instant
water reaction (a per-player "wet" flag, timestamped and cleared on the player's next shot, the
next hole, or a same-course restart).
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class LogEvent:
    kind: str
    player: int | None = None
    hole: int | None = None
    score: int | None = None          # "Previous Score": strokes taken on the hole before the next shot
    surface: str | None = None        # lie the ball came to rest on (fairway/green/rough/sand/water/...)
    to_pin: float | None = None       # raw log METRES to the pin (convert via GsproState.to_pin_of)
    gimmie: float | None = None       # raw log METRES inside which a putt is conceded (via gimme_of)
    course: str | None = None         # GSPro internal course id, e.g. "balsams_gsp"
    display: str | None = None        # human course name, e.g. "Wentworth by the Sea"
    pars: list[int] | None = None     # par for each hole in round order
    active: list[bool] | None = None  # which holes are in the round (from the end-of-round layout)
    putting: bool | None = None       # launch_mode: GSPro had the putter selected for the shot just fired
    strokes: int | None = None        # state: strokes just added to the score (negative = taken back)
    altitude_ft: float | None = None  # altitude: the course altitude GSPro's ball physics is using
    pos: tuple | None = None          # position: the ball's world (x, y, z) in metres; aim: the minimap (x, y)
    holes: list | None = None         # layout: each hole's par and world tee/pin/waypoint (see _layout_holes)
    leaves: tuple | None = None       # leaves: (seconds into the flight, hit count, penalty %, accumulated penalty %)
    roll_ft: float | None = None      # roll: how far the ball rolled after landing, logged as it comes to rest
    descent: float | None = None      # bounce: the ball's descent angle at its first landing (0 = not falling yet)


# GSPro's `Using Material TVG<surface>` names mapped to the booth's lie vocabulary (commentator/hud.py
# LIES). Unmapped materials fall back to the OCR lie rather than guessing a word downstream can't use.
MATERIAL_TO_LIE = {
    "green": "green", "fairway": "fairway", "rough": "rough", "deeprough": "deep rough",
    "semirough": "semi rough", "sand": "sand", "bunker": "bunker", "water": "water",
    "tee": "tee", "fringe": "fringe", "pinestraw": "pine straw", "waste": "waste area",
    "fescue": "fescue", "native": "native", "concrete": "cart path", "wood": "structure",
    "timber": "structure",
}

_STATE = re.compile(
    r"ActivePlayer:\s*(\d+)\s+currentHole:\s*(\d+)\s+strokes:\s*(-?\d+)\s+Previous Score:\s*(\d+)")
_SELECT = re.compile(r"Selecting shot for player:\s*(\d+)")
# A scramble's pick: the chosen ball's result is copied onto each teammate, who plays on from there.
_TEAMMATE = re.compile(r"Updating teammate score: player\s*(\d+)")
_MATERIAL = re.compile(r"Using Material TVG(\w+)")
M_TO_YD = 1.0936133       # GSPro's log distances are Unity metres; the booth works in yards
_NUM = r"\d+(?:\.\d+)?"   # a plain number, so a stray "1.2.3" can't reach float()
_TDIST = re.compile(rf"Logging tdist:\s*({_NUM})\s+and ActiveGameGimmieDistance:\s*({_NUM})")
_PAR = re.compile(r'"par":(\d+)')
_ACTIVE = re.compile(r'"active":(true|false)')
# Logged right after each "Shot Fired": whether GSPro played it as a putt (putter selected) or a full
# shot. That, not the lie or the ball speed, is what makes a stroke a putt: a chip off the green is a
# full shot and a putt from the fringe is a putt.
_LAUNCH_MODE = re.compile(r"^LaunchExt - (\w+) Mode")
# Logged right after "Shot Fired" when the ball was struck from a surface that costs it speed or spin
# (rough, deep rough, sand...): GSPro's own word on the lie the shot was actually played from. The
# "Using Material" line, by contrast, is where the ball first landed, not where it came to rest.
_PENALTY_SURFACE = re.compile(r"^Penalty Surface TVG(\w+),")
# The driving range loads as its own "course": these lines mark it (and there's no `Selected Course:`).
_RANGE_MARKERS = ("GSPro Multi Range", "RangeLevelLoaded()")
_VERSION = re.compile(r"^GSPro Version:\s*(\S+)")
# Each bounce logs the altitude GSPro's physics is using, in feet. It can differ from the course
# file's altitude (Cabot Cliffs: 1,700 in the file, 50 here), and some rounds log 0.
_ALTITUDE = re.compile(rf"^magnitude:.*\|\s*elevation\s+(-?{_NUM})\s*$")
_FLOAT = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
# Where a player's ball came to rest, in Unity world metres (Y is height): logged in the block GSPro
# writes for the shooter right after their shot ("ActivePlayer", "Selecting shot for player"), so it is
# where that player's NEXT stroke starts, not the next shot fired (another player's, in a group). Also
# logged for a mulligan's reset ball and a scramble's picked ball. Never logged before a tee shot.
_POSITION = re.compile(rf"^Data for selected player:\s*X\s*({_FLOAT})\s+Y\s*({_FLOAT})\s+Z\s*({_FLOAT})")
# Where a shot's ball crossed into a penalty area, in world metres, logged (repeatedly, the ball's
# position changing) while it rolls or flies in. The first line can carry a (0.0, 0.0, 0.0) placeholder.
# A ball that lands dry and rolls into the water logs only these, never a splash line. Not proof of a
# penalty on its own: one was logged at the launch of a shot that finished on the green.
_HAZARD = re.compile(rf"^Haz entry Point \(({_FLOAT}), ({_FLOAT}), ({_FLOAT})\)")
# A click on the minimap to move the aim: a bare "(x, y, 0.0)" in minimap units, origin at the map's
# centre. GSPro fits the map so the ball sits near (0, -338) and the pin near (0, +339): y runs up the
# ball-to-pin line and x to its right, about 677 units for the whole distance (see aim_offset).
_AIM = re.compile(rf"^\(({_FLOAT}), ({_FLOAT}), 0\.0\)$")
# The ball went into foliage in flight and lost speed for it. GSPro names no object (tree or bush):
# only the time into the flight, the hit's number on the shot and the speed it cost. Logged among the
# launch lines, before the landing ones. A "Leaves pas thru at hit N" line instead means the ball went
# through the foliage at no cost.
_LEAVES = re.compile(rf"^\*\* Leaves hitTime,\s*({_NUM})\s*,\s*HitCount\s*(\d+),\s*Penalty\s*({_NUM})%\s*,"
                     rf"\s*Accumulated penalty\s*({_NUM})%")
_LEAVES_PASS = re.compile(r"^\* Leaves pas thru at hit (\d+)")
# The ball's first bounce, logged just before its "Using Material" line. A descent angle of 0.0 means
# it hadn't started coming down: it struck something standing up (a tree trunk) on the way up, or
# barely left the ground.
_BOUNCE = re.compile(rf"^Force {_NUM}\|\|.*\|\|\s*descent angle\s*({_NUM})\|\|")
# Logged once per shot (putts too, and water) as the ball comes to rest, before the shooter's score
# block: the end of the ball's motion, whatever the screen shows.
_ROLL = re.compile(rf"Roll dist ft\s*(-?{_NUM})")
# Words that start a line the parser depends on. One that carries the word but doesn't parse means
# GSPro changed its wording (an update): it comes out as an "unparsed" event for the health checks.
# A season's log had none, so any is worth a look.
_MARKERS = ("ActivePlayer:", "Logging tdist", "Selecting shot", "Using Material", "LaunchExt -", "Penalty Surface",
            "Leaves", "Roll dist")


def parse_events(text: str) -> list[LogEvent]:
    """Every meaningful game-state line in order; Unity noise is dropped. A malformed line is skipped,
    never fatal: the tailer applies these continuously and one bad line can't be allowed to stop it."""
    out: list[LogEvent] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("(Filename"):
            continue
        try:
            if m := _STATE.search(line):
                strokes = int(m.group(3))
                out.append(LogEvent("state", player=int(m.group(1)), hole=int(m.group(2)),
                                     score=max(0, int(m.group(4)) + strokes), strokes=strokes))
            elif "Shot Fired" in line:
                out.append(LogEvent("shot_fired"))
            elif m := _LAUNCH_MODE.match(line):
                out.append(LogEvent("launch_mode", putting=m.group(1).lower() == "putting"))
            elif m := _PENALTY_SURFACE.match(line):
                out.append(LogEvent("penalty_surface", surface=m.group(1).lower()))
            elif m := _TEAMMATE.search(line):
                out.append(LogEvent("team_copy", player=int(m.group(1))))
            elif line.startswith("Standard Teammate Result Copy"):
                out.append(LogEvent("team_copy"))
            elif "Hole out" in line:
                out.append(LogEvent("hole_out"))
            elif "within Gimmie distance" in line:
                out.append(LogEvent("gimmie"))
            elif "Hit_water" in line or "| material water |" in line:
                # The splash, logged the instant the ball goes in (before the penalty score bump).
                out.append(LogEvent("hit_water"))
            elif m := _SELECT.search(line):
                out.append(LogEvent("select", player=int(m.group(1))))
            elif m := _MATERIAL.search(line):
                surface = m.group(1).lower()
                out.append(LogEvent("material", surface=surface))
                if surface == "water":
                    # Older GSPro flags the splash only as `Using Material TVGwater`, with no
                    # `Hit_water` line; emit hit_water here too so the wet flag still sets.
                    out.append(LogEvent("hit_water"))
            elif m := _TDIST.search(line):
                out.append(LogEvent("tdist", to_pin=float(m.group(1)), gimmie=float(m.group(2))))
            elif line.startswith(_RANGE_MARKERS):
                out.append(LogEvent("range"))
            elif line.startswith("End Round Button Click"):
                # Leaving a round (finished or quit early) or leaving the range, back to the menus.
                out.append(LogEvent("end_round"))
            elif line.startswith("Skipping hole "):
                # Logged at the course load when hole 1 isn't in the round (a back-nine start).
                out.append(LogEvent("skip_hole", hole=int(line.split()[2])))
            elif line.startswith("Round Is Complete"):
                out.append(LogEvent("round_complete"))
            elif line.startswith("Selected Course:"):
                out.append(LogEvent("course", course=line.split(":", 1)[1].strip()))
            elif line.startswith("Loading course ") and "Course file" not in line:
                out.append(LogEvent("course_display", display=line[len("Loading course "):].strip()))
            elif line.startswith("[{") and '"par"' in line:
                if pars := [int(p) for p in _PAR.findall(line)]:
                    active = [a == "true" for a in _ACTIVE.findall(line)]
                    out.append(LogEvent("layout", pars=pars, active=active if len(active) == len(pars) else None,
                                        holes=_layout_holes(line)))
            elif m := _POSITION.match(line):
                out.append(LogEvent("position", pos=(float(m.group(1)), float(m.group(2)), float(m.group(3)))))
            elif m := _HAZARD.match(line):
                pos = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
                if any(pos):
                    out.append(LogEvent("hazard", pos=pos))
            elif m := _AIM.match(line):
                out.append(LogEvent("aim", pos=(float(m.group(1)), float(m.group(2)))))
            elif m := _LEAVES.match(line):
                out.append(LogEvent("leaves", leaves=(float(m.group(1)), int(m.group(2)), float(m.group(3)),
                                                      float(m.group(4)))))
            elif _LEAVES_PASS.match(line):
                out.append(LogEvent("leaves_pass"))
            elif m := _BOUNCE.match(line):
                out.append(LogEvent("bounce", descent=float(m.group(1))))
            elif m := _ROLL.search(line):
                out.append(LogEvent("roll", roll_ft=float(m.group(1))))
            elif m := _VERSION.match(line):
                out.append(LogEvent("version", display=m.group(1)))
            elif m := _ALTITUDE.match(line):
                out.append(LogEvent("altitude", altitude_ft=float(m.group(1))))
            elif any(k in line for k in _MARKERS):
                out.append(LogEvent("unparsed", display=line[:160]))
        except (ValueError, IndexError):
            log.debug("Skipping unparseable GSPro log line: %r", line[:120])
            if any(k in line for k in _MARKERS):
                out.append(LogEvent("unparsed", display=line[:160]))
    return out


def _vec(v) -> tuple | None:
    """A layout vector as (x, y, z), or None for GSPro's all-zero placeholder (a hole not played)."""
    if not isinstance(v, dict):
        return None
    xyz = tuple(float(v.get(k, 0.0)) for k in "xyz")
    return xyz if any(xyz) else None


def _layout_holes(line: str) -> list[dict] | None:
    """The hole list GSPro logs when a round is complete: per hole, its par, whether it was played, and
    the world positions of the tee, the pin (as set for this round), and the designer's aim waypoint
    (ShotWPPos0, a fixed point, not the player's aim). The pin is all zeros on a hole not played."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [{"par": h.get("par"), "played": bool(h.get("played")), "tee": _vec(h.get("teePos")),
             "pin": _vec(h.get("pinPos")), "waypoint": _vec(h.get("ShotWPPos0"))}
            for h in data if isinstance(h, dict)]


AIM_MAP_SPAN = 677.0    # minimap units from the ball (y ~ -338) to the pin (y ~ +339)
AIM_MAP_BALL_Y = -338.0


def aim_offset(map_x: float, map_y: float, to_pin_m: float) -> tuple[float, float]:
    """A minimap aim click as metres (along the ball-to-pin line, right of it), given the distance to
    the pin then. GSPro scales the map to fit ball and pin whatever the distance, so one map unit is
    to_pin / 677 metres. Measured on full shots; a long hole whose map stops short of the green, and the
    putting view, are unverified."""
    scale = to_pin_m / AIM_MAP_SPAN
    return (map_y - AIM_MAP_BALL_Y) * scale, map_x * scale


@dataclass
class GsproState:
    """The live game truth, updated by applying events in order.

    `_score`/`_penalties` are keyed by (player index, hole index) so a reset on the next hole, or a
    partner's separate count, never bleeds across. The engine reads `shot_number`, `penalties`,
    `lie`, `to_pin`, and the hole-out counter for the player currently on the clock.
    """

    course: str | None = None
    course_display: str | None = None
    pars: list[int] = field(default_factory=list)
    # Where GSPro is: "range" (the driving range), "course" (a round selected or being played), "menu"
    # (a round or the range was just left), or None (not seen yet).
    mode: str | None = None
    round_ends: int = 0               # "End Round Button Click"s seen (a finished or abandoned round)
    round_completes: int = 0          # "Round Is Complete"s seen (every hole in the round holed out)
    active_holes: int | None = None   # holes in the round, from the end-of-round layout's active flags
    # Since the current course was selected: shots fired, and the hole (0-based, as logged) of the first
    # score line. No shots means the group is still on its opening tee; some means a round under way.
    shots_since_course: int = 0
    first_hole: int | None = None
    starts_late: bool = False         # "Skipping hole 1" at the load: the round doesn't start on hole 1
    active_player: int | None = None
    current_hole: int | None = None
    lie: str | None = None
    to_pin: float | None = None
    gimmie: float | None = None
    hole_outs: int = 0                # round totals, any player; the per-index ones below are what's read
    gimmies: int = 0
    players_seen: set = field(default_factory=set)   # distinct ActivePlayer indices this round
    # Per-player-index state, so a lagging global `active_player` can't cross-attribute in a 2-ball
    # round. Each setup block in the log (material -> ActivePlayer/select -> tdist) names its player,
    # so these are keyed by that index; the engine maps a name to its index via roster slot order.
    lie_by: dict = field(default_factory=dict)       # index -> raw material of the ball about to be played
    to_pin_by: dict = field(default_factory=dict)    # index -> raw METRES to the pin (to_pin_of converts)
    gimme_by: dict = field(default_factory=dict)     # index -> raw METRES gimme radius (gimme_of converts)
    hole_by: dict = field(default_factory=dict)      # index -> the hole that player is on (the partner's
    #                                                  state line reverts the global current_hole)
    hole_outs_by: dict = field(default_factory=dict)  # index -> holed shots, attributed to the active player
    gimmies_by: dict = field(default_factory=dict)    # index -> conceded tap-ins
    _score: dict = field(default_factory=dict)
    _penalties: dict = field(default_factory=dict)
    _pending_lie: str | None = None   # a material seen, awaiting the player index the block names
    # Whether a shot was fired or a gimmie conceded since that player's previous score line, keyed by
    # index: in a 2-ball the partner's interleaved state lines must not clear this player's flag (or a
    # normal shot reads as a penalty). Classifies each score increment as a stroke, a gimmie, or a penalty.
    _shot_since_state: dict = field(default_factory=dict)
    _gimmie_since_state: dict = field(default_factory=dict)
    # Whether the player's most recently fired shot went in the water, keyed by index. Set on
    # Hit_water, cleared when that player fires their next shot (the drop), moves to a new hole, or
    # the hole is replayed, so it reflects only the shot the booth is currently reacting to. Read
    # via wet(). `_wet_at` stamps when the splash was ingested (monotonic), so the in-flight tease
    # can require the splash to post-date this shot's impact and not fire on a stale earlier one.
    _wet_since_shot: dict = field(default_factory=dict)
    _wet_at: dict = field(default_factory=dict)
    # The latest "Shot Fired" until the shooter's score line names who hit it, and when (monotonic) a
    # splash was seen for it in the meantime.
    _unowned_fire: int | None = None
    _unowned_wet: float | None = None
    _unowned_outs: int = 0
    # Every "Shot Fired" (a running count that never resets), when it was ingested (monotonic), and
    # what GSPro logged for it (putting/regular mode, the penalty surface it was struck from, who hit
    # it), so the engine can ask how GSPro played a given shot.
    fires: int = 0
    _fire_at: dict = field(default_factory=dict)
    _fire_info: dict = field(default_factory=dict)
    _fires_by: dict = field(default_factory=dict)     # index -> that player's recent fire numbers
    # Setups ("Logging tdist" lines) per player index, a running count that never resets. GSPro sets the
    # shooter up again the moment the ball stops, and a gimmie is logged right after that setup, so the
    # first setup after a shot is where that shot's result (conceded or not) is final.
    setups_by: dict = field(default_factory=dict)
    # Strokes GSPro took back. A mulligan's fire number lands in `mulligan_fires`, mapped to how it
    # was seen: "undo" (a negative strokes line, explicit) or "repeat" (the re-hit's score line repeated
    # the last one). In GSPro's team modes it must repeat the player's last shot's own line (a penalty
    # on a partner's picked-over ball logs a line a stroke ahead, and the next stroke repeats that),
    # and the engine, knowing the format, decides whether it was a do-over. A pick-up adds (index,
    # hole) to `pickups`.
    mulligan_fires: dict = field(default_factory=dict)
    pickups: list = field(default_factory=list)
    # A scramble's pick that landed inside the gimme circle: (index, hole) of the picked ball, whose
    # stroke is conceded. Its player putted before a teammate, so that putt's own watch had already
    # settled as a miss by the time GSPro picked it. Append-only, like `pickups`.
    pick_gimmes: list = field(default_factory=list)
    _since_select: bool = False
    _shot_score: dict = field(default_factory=dict)   # index -> the score line of the player's last shot
    _undo: dict = field(default_factory=dict)         # index -> (hole, penalties then, fire taken back)
    _picked: set = field(default_factory=set)          # (index, hole) capped at the maximum
    team_scores: bool = False   # GSPro copied a score between teammates this round (a shared-ball format)
    # Finished holes, (index, hole) -> GSPro's final score, the same number its scorecard shows. A hole
    # ends on a ball holed or conceded: the score line that follows ("Hole out" names no player, so the
    # next line, the shooter's; a gimmie is the active player's) carries the final count. A picked-up
    # hole is final once the player has moved on to another, or the round is over. A stroke taken back
    # reopens the hole. Only the finisher's own index is recorded: in a scramble the round takes the
    # side's score from whichever partner has one (a shamble's partners finish on their own balls).
    hole_scores: dict = field(default_factory=dict)
    last_hole_scores: dict = field(default_factory=dict)   # the last ended round's, kept past its reset
    _end_next: bool = False
    _end_for: set = field(default_factory=set)
    # A scramble's picks. GSPro logs "Selecting shot for player: N" once everyone has hit (and on other
    # rounds too, so alone it proves nothing), then copies that ball onto each teammate. `pick` is the
    # chosen ball's (index, hole), `teammates` the pairs a copy linked, and `dropped_by` counts, per
    # index, the balls a pick threw away: a shot whose ball was dropped never finished where the
    # player's next shot was played from (the team ball), so its finish and lie can't be read off it.
    selected: int | None = None
    pick: tuple | None = None
    _pick_stroke: int | None = None
    teammates: set = field(default_factory=set)
    dropped_by: dict = field(default_factory=dict)
    # This GSPro logs "Penalty Surface" lines, so a shot with none was played from a surface that
    # costs nothing (fairway, fringe, green, tee). Never reset: it's a property of the GSPro build.
    logs_penalty_surface: bool = False
    # For the health checks: the GSPro build that wrote the log, every event kind seen (a running count
    # that never resets, so a round's share is the difference), and the last few lines that carried a
    # marker word but didn't parse.
    version: str | None = None
    altitude_ft: float | None = None   # the altitude GSPro is playing the current course at, once a ball bounces
    kinds: Counter = field(default_factory=Counter)
    unparsed: list = field(default_factory=list)
    # Where a shot is struck from and where it was aimed, attached to that shot's fire info ("start",
    # "aim", and "to_pin", the logged distance in metres; "hazard", where it went into a penalty area). `_rest_by` is each player's ball since their
    # last stroke: {"hole", "start", "to_pin"}, from the block GSPro logs for them after it stops. It is
    # attached when the score line credits that player's next shot, on the same hole, and used once.
    # `_aim` is the last minimap click, made by whoever is up before they swing.
    _rest_by: dict = field(default_factory=dict)
    _aim: tuple | None = None
    # The hole list of the latest completed round (see _layout_holes), and how many have been seen.
    layout_holes: list = field(default_factory=list)
    layouts: int = 0

    def apply(self, ev: LogEvent) -> None:
        self.kinds[ev.kind] += 1
        if ev.kind == "version":
            self.version = ev.display
        elif ev.kind == "unparsed":
            self.unparsed = (self.unparsed + [ev.display])[-5:]
        elif ev.kind == "altitude":
            self.altitude_ft = ev.altitude_ft
        elif ev.kind == "course":
            if ev.course != self.course:
                self._reset_round()         # a real course change is a new round; forget the old one
            self.course = ev.course
            self.course_display = None      # until this course's load names it
            self.altitude_ft = None
            self.mode, self.active_holes = "course", None
            self.shots_since_course, self.first_hole, self.starts_late = 0, None, False
        elif ev.kind == "course_display":
            self.course_display = ev.display
            if ev.display == "Practice":    # the range's load line
                self.mode = "range"
        elif ev.kind == "range":
            self.mode = "range"
        elif ev.kind == "end_round":
            self.mode = "menu"
            self.round_ends += 1
            self._finish_picked()
            self.last_hole_scores = dict(self.hole_scores)   # for the booth to settle the round with
            self._reset_round()             # the next round, even on this same course, starts clean
            self.shots_since_course, self.first_hole, self.starts_late = 0, None, False
        elif ev.kind == "skip_hole":
            if ev.hole == 1:
                self.starts_late = True
        elif ev.kind == "round_complete":
            self.round_completes += 1
            self._finish_picked()
        elif ev.kind == "layout":
            self.pars = ev.pars or []
            if ev.active:
                self.active_holes = sum(ev.active)
            if ev.holes:
                self.layout_holes = ev.holes
                self.layouts += 1
        elif ev.kind == "position":
            if (p := self.active_player) is not None:   # the block's player, set by the preceding state/select
                self._rest_by[p] = {"hole": self.hole_by.get(p, self.current_hole), "start": ev.pos}
        elif ev.kind == "aim":
            self._aim = ev.pos
        elif ev.kind == "bounce":
            if self._unowned_fire is not None and self._unowned_fire in self._fire_info:
                self._fire_info[self._unowned_fire]["descent"] = ev.descent
        elif ev.kind == "material":
            self.lie = self._pending_lie = ev.surface
            if self._unowned_fire is not None and self._unowned_fire in self._fire_info:
                self._fire_info[self._unowned_fire]["landed"] = ev.surface   # where that shot first came down
        elif ev.kind == "tdist":
            self.to_pin, self.gimmie = ev.to_pin, ev.gimmie
            if self.active_player is not None:   # the block's player, set by the preceding state/select
                self.to_pin_by[self.active_player] = ev.to_pin
                self.gimme_by[self.active_player] = ev.gimmie
                self.setups_by[self.active_player] = self.setups_by.get(self.active_player, 0) + 1
                p = self.active_player
                rest = self._rest_by.get(p)
                if rest is None or "to_pin" in rest:   # a block with no position line of its own
                    rest = self._rest_by[p] = {"hole": self.hole_by.get(p, self.current_hole)}
                rest["to_pin"] = ev.to_pin
        elif ev.kind == "select":
            self.active_player = self.selected = ev.player
            self._since_select = True
            if ev.player is not None:
                self.players_seen.add(ev.player)
                self._attach_lie(ev.player)
        elif ev.kind == "shot_fired":
            self.shots_since_course += 1
            self.fires += 1
            self._fire_at[self.fires] = time.monotonic()
            self._fire_info[self.fires] = info = {"player": None}
            if self._aim is not None:
                info["aim"] = self._aim
            self._aim = None
            for old in [n for n in self._fire_at if n <= self.fires - 40]:
                self._fire_at.pop(old, None)
                self._fire_info.pop(old, None)
            # Who hit it isn't known yet: the player last selected may not be the one swinging ("Next
            # Player" hands the clock over without a new select block, and the round's first shot has
            # none at all). The shooter's own score line comes next, so the shot is credited there.
            self._unowned_fire, self._unowned_wet, self._unowned_outs = self.fires, None, 0
        elif ev.kind == "hit_water":
            if self._unowned_fire is not None:
                self._unowned_wet = time.monotonic()
            elif self.active_player is not None:
                self._wet_since_shot[self.active_player] = True
                self._wet_at[self.active_player] = time.monotonic()
        elif ev.kind == "launch_mode":
            if self.fires in self._fire_info:
                self._fire_info[self.fires]["putting"] = ev.putting
        elif ev.kind == "hazard":
            if self.fires in self._fire_info:
                self._fire_info[self.fires].setdefault("hazard", ev.pos)
        elif ev.kind == "leaves":
            if self.fires in self._fire_info:
                info = self._fire_info[self.fires]
                info.setdefault("leaves_at", time.monotonic())
                info.setdefault("leaves", []).append(ev.leaves)
        elif ev.kind == "leaves_pass":
            if self.fires in self._fire_info:
                info = self._fire_info[self.fires]
                info["leaves_pass"] = info.get("leaves_pass", 0) + 1
        elif ev.kind == "roll":
            if self.fires in self._fire_info:
                self._fire_info[self.fires].setdefault("rest_at", time.monotonic())
        elif ev.kind == "penalty_surface":
            self.logs_penalty_surface = True
            if self.fires in self._fire_info:
                self._fire_info[self.fires]["penalty"] = ev.surface
        elif ev.kind == "gimmie":
            self.gimmies += 1
            if self._since_select and self.active_player is not None and self.active_player == self.selected:
                self.pick_gimmes.append((self.active_player, self.hole_by.get(self.active_player, self.current_hole)))
            if self.active_player is not None:
                self._end_for.add(self.active_player)
                self._gimmie_since_state[self.active_player] = True
                self.gimmies_by[self.active_player] = self.gimmies_by.get(self.active_player, 0) + 1
        elif ev.kind == "hole_out":
            self.hole_outs += 1
            self._end_next = True
            if self._unowned_fire is not None:
                self._unowned_outs += 1    # the shooter's score line, next, says whose ball dropped
            elif self.active_player is not None:
                self.hole_outs_by[self.active_player] = self.hole_outs_by.get(self.active_player, 0) + 1
                self._mark_pick(self.active_player)
        elif ev.kind == "team_copy":
            self.team_scores = True
            s, mate = self.selected, ev.player
            if s is not None and mate is not None and mate != s:
                self.pick = (s, self.hole_by.get(s, self.current_hole))
                self._pick_stroke = self._score.get(self.pick)
                self.teammates.add(frozenset((s, mate)))
                self.players_seen.add(mate)
                self.dropped_by[mate] = self.dropped_by.get(mate, 0) + 1
                self._mark_pick(s, [mate])
                # The teammate now plays the picked ball: its lie, distance and spot, not their own.
                for d in (self.lie_by, self.to_pin_by, self.gimme_by):
                    if s in d:
                        d[mate] = d[s]
                if s in self._rest_by:
                    self._rest_by[mate] = dict(self._rest_by[s])
        elif ev.kind == "state":
            self._apply_state(ev)

    def _reset_round(self) -> None:
        for d in (self.players_seen, self.lie_by, self.to_pin_by, self.gimme_by, self.hole_by,
                  self.hole_outs_by, self.gimmies_by, self._score, self._penalties,
                  self._shot_since_state, self._gimmie_since_state, self._wet_since_shot, self._wet_at,
                  self._undo, self._picked, self.dropped_by, self.hole_scores, self._end_for):
            d.clear()
        self._end_next = False
        self.teammates.clear()
        self.selected = self.pick = None
        self._pending_lie = None
        self._aim = None
        self._rest_by.clear()
        self._unowned_fire = self._unowned_wet = None
        self._unowned_outs = 0
        self.active_player = self.current_hole = self.lie = self.to_pin = self.gimmie = None
        self.hole_outs = self.gimmies = 0
        self.team_scores = False

    def _mark_pick(self, player: int, mates: list[int] | None = None) -> None:
        """In a shared-ball round, the player's last shot is the one the team played on (GSPro picked
        it, or it holed): fire info "picked" True, and the teammates' last shots on the same hole, not
        yet settled, False. `mates` defaults to every partner a copy has linked to the player."""
        if not self.team_scores:
            return
        info = self._fire_info.get((self._fires_by.get(player) or [None])[-1])
        if info is None:
            return
        info.setdefault("picked", True)
        if mates is None:
            mates = [m for pair in self.teammates if player in pair for m in pair if m != player]
        for m in mates:
            other = self._fire_info.get((self._fires_by.get(m) or [None])[-1])
            if other is not None and "picked" not in other and other.get("hole") == info.get("hole"):
                other["picked"] = False

    def _attach_lie(self, idx: int) -> None:
        """Commit the material seen just before this player's setup block as that player's lie."""
        if self._pending_lie is not None:
            self.lie_by[idx] = self._pending_lie
            self._pending_lie = None

    def lie_of(self, idx: int | None) -> str | None:
        """This player's current lie in the booth's vocabulary (None if unmapped or unknown)."""
        mat = self.lie_by.get(idx) if idx is not None else None
        return MATERIAL_TO_LIE.get(mat) if mat else None

    def to_pin_of(self, idx: int | None) -> float | None:
        """Distance to the pin in YARDS. GSPro's `Logging tdist` is Unity metres (the gimme reads 1.22
        = 4.00 ft, GSPro's default), so convert; everything downstream works in yards."""
        m = self.to_pin_by.get(idx) if idx is not None else None
        return m * M_TO_YD if m is not None else None

    def gimme_of(self, idx: int | None) -> float | None:
        """Gimme radius in YARDS (the log value is metres, same as tdist)."""
        m = self.gimme_by.get(idx) if idx is not None else None
        return m * M_TO_YD if m is not None else None

    def hole_outs_of(self, idx: int | None) -> int:
        return self.hole_outs_by.get(idx, 0) if idx is not None else self.hole_outs

    def fired_since(self, idx: int | None, fire: int) -> bool:
        """Whether the player has hit (and owned) a stroke after fire number `fire`. False if `fire`
        is another player's, so a mismatched fire can't cut a stroke's watch short."""
        if idx is None or self.fire_info(fire).get("player") not in (None, idx):
            return False
        return any(n > fire for n in self._fires_by.get(idx, []))

    def gimmies_of(self, idx: int | None) -> int:
        return self.gimmies_by.get(idx, 0) if idx is not None else self.gimmies

    @property
    def multiplayer(self) -> bool:
        """More than one player has taken the clock this round."""
        return len(self.players_seen) > 1

    def _apply_state(self, ev: LogEvent) -> None:
        self.active_player, self.current_hole = ev.player, ev.hole
        self._since_select = False
        if self.first_hole is None:
            self.first_hole = ev.hole
        if ev.player is None:
            return
        self.players_seen.add(ev.player)
        # Moved to a new hole: last hole's splash is done. Guard on a KNOWN prior hole so the first
        # state line for a player (default None) isn't read as a hole change that wipes a just-set
        # wet flag (a splash can precede that first state line, since the follower starts at EOF).
        if ev.player in self.hole_by and self.hole_by[ev.player] != ev.hole:
            self._wet_since_shot.pop(ev.player, None)
            self._wet_at.pop(ev.player, None)
            left = (ev.player, self.hole_by[ev.player])
            if left in self._picked and left in self._score:
                self._finish(left, self._score[left])
        self.hole_by[ev.player] = ev.hole
        self._attach_lie(ev.player)
        if self._unowned_fire is not None:
            n, self._unowned_fire = self._unowned_fire, None
            rest = self._rest_by.pop(ev.player, None)
            if n in self._fire_info:
                self._fire_info[n]["player"] = ev.player
                self._fire_info[n]["hole"] = ev.hole
                if rest is not None and rest["hole"] == ev.hole:   # a hole's tee shot has none
                    self._fire_info[n].update({k: rest[k] for k in ("start", "to_pin") if k in rest})
            self._fires_by[ev.player] = (self._fires_by.get(ev.player, []) + [n])[-3:]
            self._shot_since_state[ev.player] = True
            if self._unowned_outs:
                self.hole_outs_by[ev.player] = self.hole_outs_by.get(ev.player, 0) + self._unowned_outs
                self._mark_pick(ev.player)
            if self._unowned_wet is not None:
                self._wet_since_shot[ev.player] = True
                self._wet_at[ev.player] = self._unowned_wet
            else:   # a fresh shot (incl. the drop) is dry
                self._wet_since_shot[ev.player] = False
                self._wet_at.pop(ev.player, None)
        key = (ev.player, ev.hole)
        prev = self._score.get(key)
        shot = self._shot_since_state.get(ev.player)
        undo = self._undo.pop(ev.player, None)
        undo = undo if undo and undo[0] == ev.hole else None
        if prev is not None and ev.strokes is not None and ev.strokes < 0:
            # Strokes taken back: a mulligan (the re-hit follows) or the first half of a pick-up (the
            # capped score follows with no shot). Which one is settled by the next score line.
            fires = self._fires_by.get(ev.player) or [None]
            self._undo[ev.player] = (ev.hole, self._penalties.get(key, 0), prev - ev.score, fires[-1])
            self._score[key] = ev.score
            self.hole_scores.pop(key, None)
        elif undo and prev is not None and ev.score > prev and not shot:
            # The capped score written straight in after every stroke was taken back: a pick-up. Not a
            # penalty, and neither is the conceding stroke GSPro adds next.
            self._penalties[key] = undo[1]
            self._picked.add(key)
            self.pickups.append(key)
            self._score[key] = ev.score
        elif key in self._picked and prev is not None and ev.score > prev and not shot:
            self._score[key] = ev.score
        elif undo and shot:
            # Strokes taken back, then a shot: a mulligan. Any strokes beyond the shot itself that were
            # taken back were the penalty it drew (a mulligan off a ball in the water or out of bounds).
            if undo[3] is not None:
                self.mulligan_fires[undo[3]] = "undo"
            self._penalties[key] = max(0, undo[1] - (undo[2] - 1))
            if not self._penalties[key]:
                self._penalties.pop(key, None)
            self._score[key] = max(prev or 0, ev.score)
        elif prev is not None and ev.score == prev and shot:
            # A shot was fired but the score didn't move: GSPro took the previous one back (a mulligan
            # logged with no negative line), and this shot replaced it.
            fires = self._fires_by.get(ev.player) or []
            if len(fires) >= 2 and (not self.team_scores or self._shot_score.get(ev.player) == (ev.hole, ev.score)):
                self.mulligan_fires.setdefault(fires[-2], "repeat")
            if not self.team_scores:
                self.hole_scores.pop(key, None)   # a holed shot replayed: settled again if this one ends it
        elif prev is not None and ev.score > prev:
            # A score increase with no shot fired and no gimmie since THIS player's last score line is a
            # penalty stroke (a ball in the water or out of bounds, scored before the drop is played).
            # Not when a shared-ball hole has already ended: GSPro then copies the team's final score
            # onto each partner, a bump with no shot that read as a penalty on every scramble hole.
            team = {ev.player} | {m for pair in self.teammates if ev.player in pair for m in pair}
            closed = self.team_scores and any(i in team and h == ev.hole for i, h in self.hole_scores)
            if not self._shot_since_state.get(ev.player) and not self._gimmie_since_state.get(ev.player) \
                    and not closed:
                self._penalties[key] = self._penalties.get(key, 0) + (ev.score - prev)
                # The next shot is from the drop, not where the ball ended (the water), and no fresh
                # material is logged for the drop, so forget the stale lie and let OCR fill it in.
                self.lie_by.pop(ev.player, None)
            self._score[key] = ev.score
        elif prev is not None and ev.score < prev:
            self._penalties.pop(key, None)   # the hole is being replayed (a round restart on this course)
            self._score[key] = ev.score
            self.hole_scores.pop(key, None)
            self._wet_since_shot.pop(ev.player, None)   # the replayed hole starts dry
            self._wet_at.pop(ev.player, None)
        else:
            self._score[key] = max(prev or 0, ev.score)
        if shot:
            self._shot_score[ev.player] = (ev.hole, ev.score)
        self._shot_since_state[ev.player] = False
        self._gimmie_since_state[ev.player] = False
        if self._end_next or ev.player in self._end_for:
            self._end_next = False
            self._end_for.discard(ev.player)
            if key not in self._picked:
                self._finish(key, self._score[key])

    def _finish(self, key: tuple, score: int) -> None:
        self.hole_scores[key] = score

    def _finish_picked(self) -> None:
        """Settle any picked-up hole still open: the round is over, so no move to another hole will."""
        for key in self._picked:
            if key not in self.hole_scores and key in self._score:
                self._finish(key, self._score[key])

    def strokes_before(self, player: int | None = None) -> int:
        """Strokes already taken on the hole by this player (penalties included). Uses the player's own
        hole, not the global one, which the partner's state line can revert at a transition."""
        p = self.active_player if player is None else player
        return self._score.get((p, self.hole_by.get(p, self.current_hole)), 0)

    @property
    def logs_launch_mode(self) -> bool:
        """Whether this GSPro logs the putting/regular mode at all (worth waiting a moment for)."""
        return any("putting" in i for i in self._fire_info.values())

    def fire_of(self, base_fires: int, impact: float, slack: float = 3.0) -> int | None:
        """The "Shot Fired" number of the shot that hit at `impact`, or None if not logged yet.
        `base_fires` is `fires` at impact: the log may already hold this shot's "Shot Fired" (it fired
        within `slack` seconds before impact) or not yet (it's the next one)."""
        n = base_fires if self._fire_at.get(base_fires, float("-inf")) >= impact - slack else base_fires + 1
        return n if n in self._fire_info else None

    def fire_info(self, n: int | None) -> dict:
        return self._fire_info.get(n, {}) if n is not None else {}

    def tree_wood(self, n: int | None) -> bool:
        """Whether the shot's first landing on wood was a tree, not a bridge or wall. GSPro names only
        the material: wood struck before the ball started coming down (a trunk on the way up), or on a
        shot that also went through leaves, was a tree; other wood is taken as a structure."""
        info = self.fire_info(n)
        if info.get("landed") not in ("wood", "timber"):
            return False
        return info.get("descent", 1.0) < 0.5 or bool(info.get("leaves"))

    def putting_mode(self, base_fires: int, impact: float, slack: float = 3.0) -> bool | None:
        """Whether GSPro played the shot that hit at `impact` as a putt, or None if not logged (yet)."""
        return self.fire_info(self.fire_of(base_fires, impact, slack)).get("putting")

    def picked_up(self, idx: int | None, hole: int | None) -> bool:
        return (idx, hole) in self._picked

    def pick_stroke(self) -> int | None:
        """Which stroke the scramble's latest picked ball was, as counted when GSPro picked it."""
        return self._pick_stroke if self.pick else None

    def shot_number(self, player: int | None = None) -> int:
        """The stroke the player is about to play: strokes already taken plus one."""
        return self.strokes_before(player) + 1

    def penalties(self, player: int | None = None) -> int:
        """Penalty strokes counted on this player's current hole."""
        p = self.active_player if player is None else player
        return self._penalties.get((p, self.hole_by.get(p, self.current_hole)), 0)

    def wet(self, player: int | None = None, since: float | None = None) -> bool:
        """True when this player's most recent fired shot went in the water (from Hit_water), before
        they fire the next (drop) shot. Drives the instant splash reaction and the in-flight tease.
        `since` (a monotonic time) requires the splash to have been seen at or after it, so a stale
        splash from an earlier shot can't tease the current shot while it's still in the air."""
        p = self.active_player if player is None else player
        if self._unowned_fire is not None:
            # The shot in play hasn't been credited yet; the asker is the one who just swung.
            return self._unowned_wet is not None and (since is None or self._unowned_wet >= since)
        if not self._wet_since_shot.get(p, False):
            return False
        if since is not None and self._wet_at.get(p, 0.0) < since:
            return False
        return True


def build_state(text: str) -> GsproState:
    state = GsproState()
    for ev in parse_events(text):
        state.apply(ev)
    return state


class GsproLogFollower:
    """Tails output_log.txt by byte offset and keeps a GsproState current.

    Starts at the end of the file so the booth doesn't replay a whole session's history, then reads
    whatever GSPro appends. GSPro truncates (a new session reuses the path) are detected by the file
    shrinking, and the offset resets. A partial trailing line is buffered until its newline arrives.
    """

    def __init__(self, path: str, state: GsproState | None = None, poll: float = 0.3):
        self.path = path
        self.state = state or GsproState()
        self.poll = poll
        self._offset = 0
        self._buf = ""
        self.primed = False              # the backlog has been read (the health checks wait for it)

    def _read_new(self) -> str:
        """New bytes since the last read, decoded. Read in BINARY so the offset is a true byte count:
        text-mode seek/tell returns opaque cookies that can read as going backwards mid-UTF-8 and
        trigger a spurious "file shrank" reset that replays the whole log. GSPro's log is UTF-8."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return ""
        if size < self._offset:          # the file was genuinely truncated/replaced (GSPro restarted)
            self._offset, self._buf = 0, ""
        if size == self._offset:
            return ""
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            raw = f.read()
            self._offset = f.tell()
        return raw.decode("utf-8", "replace")

    def _prime(self) -> None:
        """Skip the historical backlog, but learn from it where GSPro is now: on the range, in the menus,
        or on a course (and whether anyone has hit there yet, and on which holes), so a booth started
        on the range stays quiet and one started mid-round joins it rather than opening a new one."""
        try:
            with open(self.path, "rb") as f:
                text = f.read().decode("utf-8", "replace")
                self._offset = f.tell()
        except OSError:
            self._offset = 0
            self.primed = True
            return
        past = GsproState()
        for ev in parse_events(text):
            past.apply(ev)
        s = self.state
        s.mode, s.shots_since_course, s.first_hole = past.mode, past.shots_since_course, past.first_hole
        s.starts_late = past.starts_late
        s.hole_scores = dict(past.hole_scores)   # holes already finished in a round being joined
        s._score.update({k: past._score[k] for k in past.hole_scores if k in past._score})   # so they can reopen
        s.current_hole = past.current_hole if past.shots_since_course else None
        s.version, s.kinds, s.unparsed = past.version, Counter(past.kinds), list(past.unparsed)
        self.primed = True

    async def run(self) -> None:
        await asyncio.to_thread(self._prime)
        log.info("Following GSPro log %s from offset %d (GSPro is %s)", self.path, self._offset,
                 {"range": "on the range", "course": "on a course", "menu": "in the menus"}.get(self.state.mode, "in an unknown state"))
        while True:
            try:
                chunk = await asyncio.to_thread(self._read_new)
                if chunk:
                    self._buf += chunk
                    self._buf, complete = _split_trailing(self._buf)
                    for ev in parse_events(complete):
                        self.state.apply(ev)
            except Exception:
                # One bad line must not kill the tailer and leave the booth reporting frozen state.
                log.exception("GSPro log follow error; continuing")
            await asyncio.sleep(self.poll)


def _split_trailing(buf: str) -> tuple[str, str]:
    """Split off any partial last line (no trailing newline) to carry into the next read."""
    if buf.endswith("\n"):
        return "", buf
    nl = buf.rfind("\n")
    return (buf[nl + 1:], buf[:nl + 1]) if nl >= 0 else (buf, "")
