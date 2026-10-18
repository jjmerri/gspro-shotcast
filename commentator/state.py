import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .models import Line, Shot

if TYPE_CHECKING:
    from .hud import Hud


@dataclass
class ShotRecord:
    player: str
    shot: Shot
    hole: str | None
    hud: "Hud"
    tags: list[str]
    # Where this shot sits in a tracked round: which hole of it (1 = the round's first hole),
    # that hole's par, the lie it was hit from and GSPro's shot number (penalties included).
    round_id: int | None = None
    hole_seq: int | None = None
    par: int | None = None
    lie_before: str | None = None
    shot_number: int | None = None
    mulligan: bool = False   # GSPro took it back: kept for the record, left out of the round's stats
    to_pin_before: str | None = None   # the HUD's distance to the pin when the swing was set up
    shot_id: int | None = None   # its row in the store
    outcome: str | None = None   # how it ended, from GSPro's log: holed / missed / conceded (inside the gimme)


@dataclass
class SessionState:
    """What the booth remembers during the night. Rebuilt every run; nothing persists."""

    recent_lines_max: int = 30
    current_hole: str | None = None
    last_comment_at: float = 0.0
    shots: list[ShotRecord] = field(default_factory=list)
    recent_lines: deque[Line] = field(default_factory=deque)

    def record_shot(self, rec: ShotRecord) -> None:
        self.shots.append(rec)

    def record_lines(self, lines: list[Line]) -> None:
        for line in lines:
            self.recent_lines.append(line)
        while len(self.recent_lines) > self.recent_lines_max:
            self.recent_lines.popleft()
        if lines:
            self.last_comment_at = time.time()

    def player_shots(self, player: str) -> list[ShotRecord]:
        return [r for r in self.shots if r.player == player]

    def hole_recap(self, hole: str | None, name=lambda player: player) -> list[str]:
        """`name` maps a player to how the booth refers to them (a team, in alternate shot)."""
        from .facts import SCORE_NAMES, club_name

        out = []
        for r in self.shots:
            if r.hole != hole:
                continue
            what = "putt" if r.shot.is_putt else (club_name(r.shot.club) or "shot")
            # A putt for birdie or better is named as one, so the recap's "look at birdie" is grounded.
            if r.shot.is_putt and r.shot_number and r.par and r.shot_number - r.par <= -1 \
                    and (score := SCORE_NAMES.get(r.shot_number - r.par)):
                what = f"putt for {score.split(' ', 1)[1]}"
            desc = f"{name(r.player)}: {what}"
            if r.shot.carry and not r.shot.is_putt:
                desc += f", carried {round(r.shot.carry)}"
            if r.outcome == "conceded":
                desc += ", stopped inside the gimme circle"
            elif r.hud.lie:
                desc += f", finished in the {r.hud.lie}"
            if r.outcome == "holed" or (r.shot.is_putt and r.outcome == "missed"):
                desc += f", {r.outcome}"
            out.append(desc)
            if r.outcome == "conceded":
                # GSPro counts the tap-in nobody hits: a stroke and a putt on the card
                out.append(f"{name(r.player)}: conceded tap-in (counts as a putt)")
        return out
