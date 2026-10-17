"""A shot feed from GSPro's own log (output_log.txt), for any launch monitor.

GSPro logs every shot it plays, whatever sent it, as a "Shot Fired!" block with the ball data it
used:

    * ------- Shot Fired! ------- *
    LaunchExt Vars: sp 138.37, el 15.21, az -4.52, ts 1588.58, sa -15.74, cy 237.54, id 1467

sp ball speed (mph), el launch angle, az start direction, ts total spin, sa spin axis, cy carry
(yards), id GSPro's shot id. Checked against the Uneekor feed shot for shot (same values).

There is no club data here (club speed, path, face, attack angle, strike location): that only comes
from a launch monitor feed (connector_log or gspro_sniffer). Everything else the booth records
comes from this log and the screen either way.
"""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator

from ..models import HoleChangeEvent, Shot, ShotEvent
from .base import ShotSource

log = logging.getLogger(__name__)

_VARS = re.compile(r"LaunchExt Vars:\s*(.*)")
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


class GsproLogShotSource(ShotSource):
    """Tails output_log.txt from its end and yields a shot for each `LaunchExt Vars` line."""

    def __init__(self, path: str, poll_seconds: float = 0.1, backup: bool = False) -> None:
        self.path = path
        self.backup = backup   # standing in for GSPconnect only while it's silent (sources/fallback.py)
        self.poll = poll_seconds
        self._offset: int | None = None
        self._buf = ""

    def _read_new_lines(self) -> list[str]:
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
            for line in lines:
                if (vals := parse_launch(line)) is None:
                    continue
                if not 0 < vals["sp"] < 250:
                    log.info("Skipping implausible shot: ball speed %s", vals["sp"])
                    continue
                log.info("GSPro log shot: id %s, ball speed %s", vals.get("id"), vals["sp"])
                yield ShotEvent(shot=shot_from_launch(vals))
            await asyncio.sleep(self.poll)
