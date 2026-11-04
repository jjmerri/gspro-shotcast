"""A shot feed from GSPro's own log, for any launch monitor.

GSPro logs every shot it plays, whatever sent it, as a "Shot Fired!" block. GSPro 3.1
(output_log.txt) logs the ball data it used there:

    * ------- Shot Fired! ------- *
    LaunchExt Vars: sp 138.37, el 15.21, az -4.52, ts 1588.58, sa -15.74, cy 237.54, id 1467
    LaunchExt - Regular Mode

sp ball speed (mph), el launch angle, az start direction, ts total spin, sa spin axis, cy carry
(yards), id GSPro's shot id. Checked against the Uneekor feed shot for shot (same values).

The 3.59 builds (the 2026 public beta, Player.log) log the fire and the mode line but no
`LaunchExt Vars`. There the same numbers come from the files GSPro saves beside its log once the
ball stops (sources/gspro_files.py), 3-13 s after impact. The booth's impact snapshot is still taken
while the ball is in the air (on_impact, half a second after the fire), so the shot airs late but is
read as it was struck.

There is no club data here (club speed, path, face, attack angle, strike location): that only comes
from a launch monitor feed (connector_log or gspro_sniffer). Everything else the booth records
comes from this log and the screen either way.
"""

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace

from ..models import HoleChangeEvent, Shot, ShotEvent
from .base import ShotSource
from .gspro_files import GsproFiles, LastShot, folder_of, launch_of
from .gspro_log import LOG_RECHECK_SECONDS, live_log

log = logging.getLogger(__name__)

_VARS = re.compile(r"LaunchExt Vars:\s*(.*)")
_FIRED = re.compile(r"\*\s*-+\s*Shot Fired!")
_MODE = re.compile(r"LaunchExt - (Regular|Putting) Mode")

CAPTURE_AFTER = 0.5     # the booth's impact snapshot: the ball is still in the air
FILES_WAIT = 45.0       # a fire with no saved shot by now (the range, a menu) is given up on
LAST_SHOT_GRACE = 1.5   # LastShot changed and currentRound.dat didn't by now: a practice shot
_PAIR = re.compile(r"\b(sp|el|az|ts|sa|cy|id)\s+(-?\d+(?:\.\d+)?)")


def parse_launch(line: str) -> dict[str, float] | None:
    """The numbers on a `LaunchExt Vars:` line, or None for any other line or one missing ball speed."""
    if not (m := _VARS.search(line)):
        return None
    vals = {k: float(v) for k, v in _PAIR.findall(m.group(1))}
    return vals if "sp" in vals else None


def shot_from_launch(vals: dict[str, float]) -> Shot:
    """An Open Connect-shaped message from the log's numbers, through the same cleaning as the
    launch monitor feeds (putt sentinels, impossible carries)."""
    ball = {"Speed": vals.get("sp"), "VLA": vals.get("el"), "HLA": vals.get("az"),
            "TotalSpin": vals.get("ts"), "SpinAxis": vals.get("sa"), "CarryDistance": vals.get("cy")}
    msg = {"Units": "Yards", "BallData": ball, "ShotDataOptions": {"ContainsBallData": True, "ContainsClubData": False},
           "Source": "gspro_log"}
    if "id" in vals:
        msg["ShotNumber"] = int(vals["id"])
    return Shot.from_gspro(msg)


def shot_from_last_shot(row: LastShot) -> Shot | None:
    if (ball := row.launch()) is None:
        return None
    return Shot.from_gspro({"Units": "Yards", "BallData": ball, "Source": "gspro_log",
                            "ShotDataOptions": {"ContainsBallData": True, "ContainsClubData": False}})


@dataclass
class _Fire:
    """A "Shot Fired!" whose numbers aren't in the log (the 3.59 builds)."""
    at: float
    putter: bool
    wall: float = 0.0   # time.time() when it was read, to compare with the round file's write time
    event: ShotEvent = field(default_factory=lambda: ShotEvent(shot=Shot()))   # carries the impact snapshot
    captured: bool = False


class GsproLogShotSource(ShotSource):
    """Tails GSPro's log from its end and yields a shot for each one GSPro fires: from its
    `LaunchExt Vars` line, or from the shot files when the log has none."""

    def __init__(self, path: str, poll_seconds: float = 0.1, backup: bool = False) -> None:
        self.configured = path
        self.path = live_log(path)
        self._checked = time.monotonic()
        self.backup = backup   # standing in for GSPconnect only while it's silent (sources/fallback.py)
        self.poll = poll_seconds
        self._offset: int | None = None
        self._buf = ""
        self.files = GsproFiles(folder_of(self.path))
        self._fired_at: float | None = None   # the last fire, until its Vars or mode line says which kind
        self._waiting: list[_Fire] = []       # fires waiting on the shot files, oldest first
        self._last_shot: tuple[float, LastShot] | None = None
        self._round_sp = 0.0   # ball speed of the last shot from the round file (its LastShot row may come later)
        # Called with a waiting fire's event while its ball is in the air, so the booth can snapshot
        # the impact-time state for when the shot airs (see Engine._capture_impact).
        self.on_impact = None

    def _switch(self) -> None:
        """Follow the other GSPro log (see gspro_log.live_log) from its end once it is the one written."""
        if (now := time.monotonic()) - self._checked < LOG_RECHECK_SECONDS:
            return
        self._checked = now
        if (path := live_log(self.configured)) != self.path:
            log.info("GSPro is writing %s now; reading shots from it instead of %s", path, self.path)
            self.path, self._offset, self._buf = path, None, ""
            self._fired_at, self._waiting, self._last_shot, self._round_sp = None, [], None, 0.0
            self.files.move(folder_of(path))

    def _read_new_lines(self) -> list[str]:
        self._switch()
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if self._offset is None:   # start at the end: history is not new shots
            self._offset = size
            return []
        if size < self._offset:    # GSPro restarted and the log was replaced
            self._offset, self._buf = 0, ""
        if size == self._offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            data = f.read()
            self._offset = f.tell()
        parts = (self._buf + data.decode("utf-8", "replace")).split("\n")
        self._buf = parts[-1]
        return parts[:-1]

    def _files_events(self, now: float) -> list[ShotEvent]:
        """Shots for the fires waiting on GSPro's shot files, as the files get them."""
        out: list[ShotEvent] = []
        if not self.files.primed:
            self.files.prime()
            return out
        new, written = self.files.new_swings()
        if self.files.db_changed(now) and (row := self.files.new_last_shot()) is not None:
            if abs((row.data.get("BallSpeed") or 0) - self._round_sp) < 0.01:
                pass   # a round shot's row, read after its round file entry aired it
            elif self._waiting and self.files.db_written < self._waiting[-1].wall:
                log.debug("LastShot row written before the latest fire: an earlier shot's")
            else:
                self._last_shot = (now, row)
        if new:
            # Nobody swings until the last ball stops, so a file entry is the latest fire's from before
            # the file was written; an older fire got none (a practice shot LastShot missed) and won't
            # now, and a later one is the next shot's. (All of them if the booth stalled and read the
            # fire after the file was written.)
            pool = [f for f in self._waiting if f.wall <= written] or list(self._waiting)
            if len(new) > len(pool):
                log.debug("%d shot file entries with no fire waiting (3.1 logged them, or the booth "
                          "started mid-shot)", len(new) - len(pool))
                new = new[len(new) - len(pool):] if pool else []
            for fire in pool[:len(pool) - len(new)]:
                self._drop_fire(fire)
            for fire, entry in zip(pool[len(pool) - len(new):], new):
                self._waiting.remove(fire)
                vals = launch_of(entry)
                self._round_sp = vals["sp"]
                if self._last_shot and abs((self._last_shot[1].data.get("BallSpeed") or 0) - vals["sp"]) < 0.01:
                    self._last_shot = None   # this shot's own row
                if not 0 < vals["sp"] < 250:
                    log.info("Skipping implausible shot: ball speed %s", vals["sp"])
                    continue
                log.info("GSPro shot file: %s, hole %s shot %s, ball speed %.1f, %.1fs after the fire",
                         entry.get("PlayerName"), (entry.get("Hole") or 0) + 1, entry.get("HoleShot"),
                         vals["sp"], now - fire.at)
                out.append(self._aired(fire, shot_from_launch(vals)))
        if self._last_shot is not None:
            seen_at, row = self._last_shot
            if not self._waiting or seen_at < self._waiting[-1].at:
                self._last_shot = None   # an earlier shot's row (its file entry came first)
            elif now - seen_at >= LAST_SHOT_GRACE:
                # No round file entry came with it: a practice shot.
                self._last_shot = None
                self._drop(len(self._waiting) - 1)
                fire = self._waiting.pop(0)
                if (shot := shot_from_last_shot(row)) is not None and 0 < (shot.ball_speed or 0) < 250:
                    log.info("GSPro LastShot: ball speed %.1f, %.1fs after the fire (practice: no round "
                             "file entry)", shot.ball_speed or 0, now - fire.at)
                    out.append(self._aired(fire, shot))
        while self._waiting and now - self._waiting[0].at > FILES_WAIT:
            self._drop(1)
        return out

    def _drop(self, n: int) -> None:
        for fire in self._waiting[:max(0, n)]:
            self._drop_fire(fire)

    def _drop_fire(self, fire: _Fire) -> None:
        self._waiting.remove(fire)
        log.info("GSPro saved no shot for the fire %.0fs ago (the range, a menu, or a holed practice "
                 "putt): dropped", time.monotonic() - fire.at)

    @staticmethod
    def _aired(fire: _Fire, shot: Shot) -> ShotEvent:
        # Stamped when it was struck, not when GSPro saved it once the ball stopped.
        ev = ShotEvent(shot=replace(shot, putter=fire.putter, timestamp=fire.wall or shot.timestamp), landed=True)
        ev.impact_at, ev.impact_state, ev.impact_read, ev.impact_seq = (
            fire.event.impact_at, fire.event.impact_state, fire.event.impact_read, fire.event.impact_seq)
        ev.setup_hud, ev.buffer_frames = fire.event.setup_hud, fire.event.buffer_frames
        return ev

    def _capture(self, now: float) -> None:
        for fire in self._waiting:
            if not fire.captured and now - fire.at >= CAPTURE_AFTER:
                fire.captured = True
                if self.on_impact is not None:
                    self.on_impact(fire.event)

    async def events(self) -> AsyncIterator[ShotEvent | HoleChangeEvent]:
        log.info("%s: GSPro's log %s (ball data only; no club data)",
                 "Backup shot feed, while GSPconnect is silent" if self.backup else "Shot feed", self.path)
        if not os.path.exists(self.path):
            log.warning("%s doesn't exist yet; waiting for GSPro to write it", self.path)
        while True:
            try:
                lines = await asyncio.to_thread(self._read_new_lines)
            except OSError as e:
                log.warning("Can't read %s: %s", self.path, e)
                lines = []
            now = time.monotonic()
            for line in lines:
                if _FIRED.search(line):
                    self._fired_at = now
                    continue
                if (m := _MODE.search(line)) and self._fired_at is not None:
                    # No Vars line between the fire and its mode line: the numbers are in the files.
                    self._waiting.append(_Fire(at=self._fired_at, putter=m.group(1) == "Putting",
                                               wall=time.time()))
                    self._fired_at = None
                    continue
                if (vals := parse_launch(line)) is None:
                    continue
                self._fired_at = None
                if not 0 < vals["sp"] < 250:
                    log.info("Skipping implausible shot: ball speed %s", vals["sp"])
                    continue
                log.info("GSPro log shot: id %s, ball speed %s", vals.get("id"), vals["sp"])
                yield ShotEvent(shot=shot_from_launch(vals))
            self._capture(now)
            try:
                for ev in await asyncio.to_thread(self._files_events, now):
                    yield ev
            except OSError as e:
                log.warning("Can't read GSPro's shot files in %s: %s", self.files.folder, e)
            await asyncio.sleep(self.poll)
