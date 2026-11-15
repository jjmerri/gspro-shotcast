"""GSPro's own record of each stroke of the round in play (currentRound.dat, GSPro 3.59+), as tracks.

GSPro writes the file beside its log once the ball comes to rest (see sources/gspro_files.py for the
entries). Each stroke's entry has, in the course's world metres (as shots.start_*: y is height):

- StartingPOS and EndingPOS: where it was played from and where it stopped (a drop, a sim drop off a
  cart path, is the next stroke's StartingPOS). StartingSurface / EndingSurface: the lie (SURFACES).
- activeShot.sd.pos: the flight, from the strike to the touchdown (sd.tdpoint); empty for most putts.
  activeShot.sd.path: the roll from the touchdown (a putt's whole run), each point with its time.
- DistanceToPin: metres from the hole where it stopped. Hole is 0-based; HoleShot is the stroke number.

It's GSPro's physics, so it's taken over what the booth reads off the screen and the log: the carry
(horizontal touchdown distance, as GSPro's CARRY tile), the total (horizontal start to stop), the lies
and the finish. The file starts over each round, so only the round in play can be read.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .sources.gspro_files import launch_of, swings
from .sources.gspro_log import M_TO_YD

# StartingSurface / EndingSurface codes seen (3.59.11). Others are left alone.
SURFACES = {18: "tee", 1: "rough", 2: "fairway", 3: "deep rough", 4: "cart path", 5: "green", 11: "sand"}
# The booth's lies GSPro's codes can stand for: a finer one read off the HUD (semi rough, fringe, pine
# straw, ...) says more, so it's kept.
PLAIN_LIES = frozenset(SURFACES.values())
PENALTY_RESULTS = (2, 3)   # ShotResult of a penalty's own entry: water, out of bounds
HOLED = 7                  # HoleResult once the ball is in (or conceded)
PATH_STEP_M = 0.5          # points closer than this to the last one kept are dropped


Point = tuple[float, float, float]


@dataclass
class Track:
    shot_id: str                 # GSPro's ShotID
    round_id: int | None
    course_key: str | None       # e.g. "riviera_gsp"
    player: str                  # GSPro's short name ("Josh")
    hole: int                    # 1-based
    shot_number: int
    ball_speed: float
    putt: bool
    start: Point
    end: Point | None            # None when a penalty followed (where it stopped isn't where play went on)
    land: Point | None           # the touchdown, for a shot that flew
    flight: list[Point] = field(default_factory=list)
    flight_s: float | None = None
    roll: list[tuple[float, float, float, float]] = field(default_factory=list)   # (x, y, z, seconds)
    lie_before: str | None = None
    lie_after: str | None = None
    to_pin_m: float | None = None
    holed: bool = False
    penalty: bool = False

    @property
    def carry_yards(self) -> float | None:
        if self.putt or self.land is None or not self.flight:
            return None
        return round(_flat(self.start, self.land) * M_TO_YD, 1)

    @property
    def total_yards(self) -> float | None:
        return None if self.end is None else round(_flat(self.start, self.end) * M_TO_YD, 1)

    @property
    def finish_yards(self) -> float | None:
        if self.end is None:
            return None
        if self.holed:
            return 0.0
        return None if self.to_pin_m is None else round(self.to_pin_m * M_TO_YD, 1)


def _flat(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[2] - a[2])


def _xyz(p) -> Point | None:
    if isinstance(p, dict) and all(isinstance(p.get(k), (int, float)) for k in "xyz"):
        return (float(p["x"]), float(p["y"]), float(p["z"]))
    return None


def _thin(points: list) -> list:
    """Points at least PATH_STEP_M apart (top-down), with the first and last kept."""
    if len(points) <= 2:
        return points
    out = [points[0]]
    for p in points[1:-1]:
        if math.hypot(p[0] - out[-1][0], p[2] - out[-1][2]) >= PATH_STEP_M:
            out.append(p)
    out.append(points[-1])
    return out


def tracks(entries: list[dict]) -> list[Track]:
    """A Track for each stroke the players hit (not a penalty's entry, a mulligan's taken-back stroke or
    the copy GSPro adds when a ball drops), in the file's order."""
    out = []
    for _, e in swings(entries):
        launch = launch_of(e) or {}
        shot = e.get("activeShot") or {}
        sd = shot.get("sd") or {}
        start = _xyz(e.get("StartingPOS"))
        hole, number = e.get("Hole"), e.get("HoleShot")
        if start is None or not isinstance(hole, int) or not isinstance(number, int) or not e.get("PlayerName"):
            continue
        # Its penalty's entry follows it (the same player's, hole's and stroke's number on).
        penalty = any(o.get("ShotResult") in PENALTY_RESULTS and o.get("PlayerName") == e.get("PlayerName")
                      and o.get("Hole") == hole and o.get("HoleShot") == number + 1 for o in entries)
        flight = [p for p in (_xyz(q) for q in sd.get("pos") or []) if p]
        roll = []
        for q in sd.get("path") or []:
            if (p := _xyz((q or {}).get("pos"))) and isinstance(q.get("time"), (int, float)):
                roll.append((*p, float(q["time"])))
        dist = e.get("DistanceToPin")
        t = Track(shot_id=str(e["ShotID"]), round_id=e.get("RoundID"), course_key=e.get("CourseKey"),
                  player=str(e["PlayerName"]), hole=hole + 1, shot_number=number,
                  ball_speed=float(launch.get("sp") or e.get("BallSpeed") or 0.0), putt=bool(sd.get("isPutt")),
                  start=start, end=None if penalty else _xyz(e.get("EndingPOS")),
                  land=_xyz(sd.get("tdpoint")) if flight else None,
                  flight=_thin(flight), flight_s=shot.get("tdTime") if flight else None,
                  roll=_thin(roll), lie_before=SURFACES.get(e.get("StartingSurface")),
                  lie_after=None if penalty else SURFACES.get(e.get("EndingSurface")),
                  to_pin_m=float(dist) if isinstance(dist, (int, float)) else None,
                  holed=bool(sd.get("isHoled")) or e.get("HoleResult") == HOLED, penalty=penalty)
        out.append(t)
    return out


def untracked(entries: list[dict], got: list[Track]) -> set[str]:
    """The ShotIDs of the file's entries that aren't strokes (`got`): a mulligan's taken-back stroke, a
    penalty's or a dropped ball's entry."""
    ids = {str(e["ShotID"]) for e in entries if isinstance(e, dict) and e.get("ShotID") is not None}
    return ids - {t.shot_id for t in got}


def course_fits(course_key: str | None, course: str | None) -> bool:
    """Whether GSPro's course key ("riviera_gsp") names the shots' course ("Riviera")."""
    def norm(s):
        s = (s or "").casefold()
        for tail in ("_gsp", "gsp"):
            s = s.removesuffix(tail)
        return "".join(ch for ch in s if ch.isalnum())
    k, c = norm(course_key), norm(course)
    return bool(k and c) and (k.startswith(c) or c.startswith(k))


def names_fit(gspro_name: str, player: str | None) -> bool:
    """Whether GSPro's short name ("Josh") is the shots' player ("Josh Merritt")."""
    g, p = gspro_name.casefold().strip(), (player or "").casefold().strip()
    return bool(g and p) and (p == g or p.split()[0] == g or g.split()[0] == p.split()[0])
