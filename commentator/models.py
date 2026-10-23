import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hud import Hud, Scorecard

PUTTER_CODES = {"PT", "PUTTER"}

# Dynamic loft at impact -> likely club, for monitors that don't report the club. Dynamic loft
# runs a few degrees above the static loft, and players vary, so this is a best guess.
LOFT_CLUBS = [(14, "DR"), (19, "3W"), (23, "4H"), (25, "5I"), (28, "6I"), (31, "7I"), (35, "8I"),
              (40, "9I"), (46, "PW"), (52, "GW"), (58, "SW")]


def club_from_loft(loft: float | None) -> str | None:
    if not loft:
        return None
    return next((c for limit, c in LOFT_CLUBS if loft < limit), "LW")


@dataclass
class Shot:
    shot_number: int | None = None
    units: str = "Yards"
    ball_speed: float | None = None
    vla: float | None = None
    hla: float | None = None
    total_spin: float | None = None
    spin_axis: float | None = None
    back_spin: float | None = None
    side_spin: float | None = None
    carry: float | None = None
    club_speed: float | None = None
    angle_of_attack: float | None = None
    face_to_target: float | None = None
    path: float | None = None
    loft: float | None = None   # dynamic loft at impact
    # Strike location on the face, in mm from centre. Only measured when the player has the club
    # markers the EYE XO2 needs (club_measured), so these are present for some players and not others.
    vertical_face_impact: float | None = None
    horizontal_face_impact: float | None = None
    # Whether the monitor actually reported club data this shot (ShotDataOptions.ContainsClubData).
    # The EYE XO2 needs club markers for it, so it's false for players without the stickers.
    club_measured: bool = False
    # From GSPro's player-info messages, not the launch monitor.
    club: str | None = None
    handed: str | None = None
    distance_to_target: float | None = None
    # GSPro's own call (its log's putting/regular mode for this shot): the putter was selected. When
    # known it decides is_putt outright, since the club, not the lie or the ball speed, makes a putt.
    putter: bool | None = None
    timestamp: float = field(default_factory=time.time)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_gspro(cls, msg: dict[str, Any], player_info: dict[str, Any] | None = None) -> "Shot":
        ball = dict(msg.get("BallData") or {})
        club = msg.get("ClubData") or {}
        info = player_info or {}
        # Uneekor sends back and side spin with SpinAxis and TotalSpin left at 0.
        back, side = ball.get("BackSpin"), ball.get("SideSpin")
        if back and side is not None and not ball.get("SpinAxis") and not ball.get("TotalSpin"):
            ball["TotalSpin"] = math.hypot(back, side)
            ball["SpinAxis"] = math.degrees(math.atan2(side, back))
        # GSPconnect writes sentinels for a roll it can't measure (a putt): VLA 99999, a huge
        # CarryDistance. Clear them so the shot reads as the putt it is, not an impossible one.
        vla = ball.get("VLA")
        if vla is not None and not -20 <= vla <= 80:
            ball["VLA"] = None
        carry = ball.get("CarryDistance")
        if carry is not None and not 0 <= carry <= 450:
            ball["CarryDistance"] = 0.0
        total = ball.get("TotalSpin")
        if total is not None and total > 14000:  # a putt's unmeasured-spin sentinel (e.g. 141420 @ 45)
            ball["TotalSpin"] = ball["SpinAxis"] = None
        return cls(
            shot_number=msg.get("ShotNumber"),
            units=msg.get("Units") or "Yards",
            ball_speed=ball.get("Speed"),
            vla=ball.get("VLA"),
            hla=ball.get("HLA"),
            total_spin=ball.get("TotalSpin"),
            spin_axis=ball.get("SpinAxis"),
            back_spin=ball.get("BackSpin"),
            side_spin=ball.get("SideSpin"),
            carry=ball.get("CarryDistance"),
            club_speed=club.get("Speed"),
            angle_of_attack=club.get("AngleOfAttack"),
            face_to_target=club.get("FaceToTarget"),
            path=club.get("Path"),
            loft=club.get("Loft"),
            vertical_face_impact=club.get("VerticalFaceImpact"),
            horizontal_face_impact=club.get("HorizontalFaceImpact"),
            club_measured=bool((msg.get("ShotDataOptions") or {}).get("ContainsClubData")),
            # Putters have little loft too, so only guess for full-speed shots.
            club=info.get("Club") or (club_from_loft(club.get("Loft")) if (ball.get("Speed") or 0) >= 40 else None),
            handed=info.get("Handed"),
            distance_to_target=info.get("DistanceToTarget"),
            raw=msg,
        )

    @property
    def is_putt(self) -> bool:
        if self.putter is not None:
            return self.putter
        if self.club and self.club.upper() in PUTTER_CODES:
            return True
        # No club info (the EYE XO2 never reports club): a slow ball with no real launch is a putt.
        # The monitor can't measure launch on a rolling ball, so a putt arrives with a sentinel VLA
        # that from_gspro blanks to None (and occasionally a literal 0.0); a weak chip keeps a real,
        # higher launch. So a missing or near-zero launch is a putt, while a genuine high launch at
        # low speed (a soft lob) stays a full shot. Requiring vla < 5 with None treated as "not a
        # putt" was the bug: it sent every real putt down the full-shot path.
        if self.club is not None:
            return False
        speed = self.ball_speed if self.ball_speed is not None else 99
        return speed < 20 and (self.vla is None or self.vla < 5)


@dataclass
class ShotEvent:
    shot: Shot
    # Replay files can supply these so the pipeline runs without a screen.
    player_hint: str | None = None
    hud_hint: "Hud | None" = None
    # Replay only: the HUD as it read at impact, and the impact screenshot.
    impact_hud_hint: "Hud | None" = None
    impact_image: bytes | None = None
    # Wall-clock time the shot feed wrote the message, when it says (GSPconnect's log does).
    logged_at: float | None = None
    # A feed that delivers a shot late fills these in at impact (monotonic time, the booth's snapshot).
    impact_at: float | None = None
    impact_state: Any = None
    impact_read: Any = None     # the booth's screen read at impact (a task)


@dataclass
class PreShotEvent:
    """A player has set up for a new shot (the player box changed and settled)."""

    hud: "Hud"
    player_hint: str | None = None
    image: bytes | None = None
    # The pre-shot model calls, started at setup so they overlap the rest of the HUD read. Its result
    # is ({call_name: raw_json}, facts_text) over the concurrent line/branch/hazard calls. None when
    # the event isn't driven by the live watcher (e.g. replay).
    model_task: Any = None


@dataclass
class HoleChangeEvent:
    previous: str | None
    current: str
    course: str | None = None
    # Not a new hole but a fix to the hole we're on: "revert" (the last change was a misread banner
    # that flipped back) or "log" (GSPro's log says the group is on another hole than the count).
    correction: str | None = None


@dataclass
class RoundStartEvent:
    """Everyone is on the first tee at even par: the round is about to begin."""

    hud: "Hud"
    # The booth came up mid-round (GSPro's log shows shots already played on this course): track the
    # round from the current hole, with no opener.
    joined: bool = False


@dataclass
class ScorecardEvent:
    card: "Scorecard"


@dataclass
class RoundEndEvent:
    reason: str
    abandoned: bool = False   # quit before the last hole: no closer, no round stats recorded


@dataclass
class Line:
    speaker: str
    text: str
