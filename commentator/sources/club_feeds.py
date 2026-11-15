"""Club data for shots GSPro took from a launch monitor the booth doesn't read directly.

Only Uneekor's shots reach the booth with club data (sources/connector_log.py). Every other monitor's
shot comes from GSPro's own log and files: ball data only. But several monitors' own software logs
each shot it sends GSPro, club numbers included. This follows those logs and attaches a reading to
the GSPro shot with the same ball speed (both describe the same ball, in mph).

Where each log is, and what it has:

- FlightScope (Mevo, Mevo+, X3) through GSPconnect: one JSON line per shot from
  `VGPconnect.FlightscopeForm` in GSPconnect's ConnectDebug.txt (the connector_log path), in metres
  per second. Club speed, attack angle, path, face and dynamic loft, each only when its
  `<field>Available` flag says the radar measured it (path and face need FlightScope's Pro Package).
- rela (SkyTrak, SkyTrak+ and others): %LOCALAPPDATA%\\rela\\rela.log, or %TEMP%\\rela.log when that
  folder can't be written. Newer builds log `SEND=> [ShotEnded] OUT type=... club:speed=X(measured)
  aoa=Y(SIE-physics+MLP) ...`; older ones the Open Connect JSON they send. rela models the rest of a
  SkyTrak's club data from the ball, so only readings it tags measured are kept (from the JSON, which
  doesn't say, only club speed).
- OpenSkyPlus (SkyTrak): a BepInEx plugin in the SkyTrak app, so its log is the app's BepInEx log,
  <SkyTrak>\\BepInEx\\LogOutput.log (OpenSkyPlus 2: `Launch: BallSpeed=` then `Club: ClubHeadSpeed=`,
  in m/s), or <SkyTrak>\\BepInEx\\plugins\\OpenSkyPlus\\Log.txt (version 1: the Open Connect JSON it
  sends, pretty-printed). Club speed only: the SkyTrak doesn't measure the rest.

FlightScope and rela also log the whole launch with a carry (`ClubReading.launch`, the same keys as
GSPro 3.1's `LaunchExt Vars` line). GSPro 3.59 doesn't log its ball data until the ball stops, so for
those two the shot goes out from the monitor's line as soon as GSPro fires it (sources/gspro_log_shots.py).
OpenSkyPlus logs no carry, so its shots still wait for GSPro's files.

None of these formats is a published interface; they come from players' logs (2023-2026). A line
that doesn't parse is skipped, and a shot no reading matches airs with ball data only, as before.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Shot

log = logging.getLogger(__name__)

MPS_TO_MPH = 3600 / 1609.344
M_TO_YD = 1 / 0.9144
POLL_SECONDS = 0.5
KEEP_SECONDS = 120.0     # GSPro 3.59's copy of a shot comes once the ball stops, up to ~15 s after
WAIT_SECONDS = 1.5       # a shot with no reading yet waits this long for one, while a monitor is logging
ACTIVE_SECONDS = 1800.0  # "logging": a reading came this recently
COPY_SECONDS = 5.0       # a monitor's second line for one shot comes within this
SAME_BALL_MPH = 0.6      # GSPro logs ball speed to two decimals; rela's text line to one
MAX_READ = 8 << 20       # bytes read from a file at once (a log replaced with its history)
SKYTRAK_DIRS = [r"C:\SkyTrak", r"C:\GolfSim\SkyTrak", r"%ProgramFiles%\SkyTrak", r"%ProgramFiles(x86)%\SkyTrak"]


@dataclass
class ClubReading:
    source: str
    ball_mph: float
    club_mph: float | None = None
    angle_of_attack: float | None = None
    path: float | None = None
    face_to_target: float | None = None
    loft: float | None = None
    at: float = 0.0     # monotonic, when the booth read it
    # The monitor's whole launch, in GSPro's units and signs, when its line has one with a carry:
    # sp ball speed (mph), el launch angle, az start direction (- left), ts total spin, sa spin axis
    # (- draw), cy carry (yards).
    launch: dict[str, float] | None = None

    @property
    def has_club(self) -> bool:
        return any(v is not None for v in (self.club_mph, self.angle_of_attack, self.path,
                                            self.face_to_target, self.loft))


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _launch(ball_mph: float, el: float | None, az: float | None, ts: float | None, sa: float | None,
            cy: float | None) -> dict[str, float] | None:
    """A full shot's launch, or None when a number is missing or impossible (or it has no carry: a
    putt, or a monitor that leaves carry to GSPro)."""
    if None in (el, az, ts, sa, cy):
        return None
    if not (-20 <= el <= 80 and -60 <= az <= 60 and 0 <= ts <= 14000 and -90 <= sa <= 90 and 0 < cy <= 450):
        return None
    return {"sp": ball_mph, "el": el, "az": az, "ts": ts, "sa": sa, "cy": cy}


def _reading(source: str, ball_mph: float | None, club_mph: float | None = None,
             launch: tuple[float | None, ...] | None = None, **club: float | None) -> ClubReading | None:
    """A reading with impossible numbers dropped; None when nothing usable is left. launch is
    (el, az, ts, sa, cy) in GSPro's units and signs."""
    if ball_mph is None or not 1 <= ball_mph <= 250:
        return None
    if club_mph is not None and not (5 <= club_mph <= 160 and (ball_mph < 30 or 0.3 <= ball_mph / club_mph <= 1.7)):
        club_mph = None
    club = {k: v for k, v in club.items() if v is not None and -45 <= v <= 80}
    full = _launch(ball_mph, *launch) if launch else None
    if club_mph is None and not club and full is None:
        return None
    return ClubReading(source, ball_mph, club_mph, launch=full, **club)


def parse_flightscope(line: str) -> ClubReading | None:
    at = line.find("FlightscopeForm")
    start = line.find("{", at) if at >= 0 else -1
    if start < 0:
        return None
    try:
        d = json.loads(line[start:])
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("launchSpeedAvailable") is False:
        return None

    def measured(key: str) -> float | None:
        return _num(d.get(key)) if d.get(key + "Available") is True else None

    ball, club = _num(d.get("launchSpeed")), measured("clubSpeed")
    carry = measured("carryDistance")
    spin = _num(d.get("spinRPM")) if d.get("spinRPMandEstimateAvailable") is True else None
    # Signs as GSPro's: a negative start direction and spin axis are left (its lateral carry agrees).
    launch = (measured("launchElevDeg"), measured("launchAzimDeg"), spin, measured("spinAxisDeg"),
              carry * M_TO_YD if carry is not None else None)
    return _reading("FlightScope", ball * MPS_TO_MPH if ball else None, club * MPS_TO_MPH if club else None,
                    launch=launch, angle_of_attack=measured("attackAngleDeg"), path=measured("clubPathDeg"),
                    face_to_target=measured("clubFaceAngleDeg"), loft=measured("dynamicLoftDeg"))


_RELA_TEXT = re.compile(r"SEND=> \[(?:ShotEnded|BallData)\] OUT type=")
_RELA_JSON = re.compile(r"SEND=> \[(?:ShotEnded|BallData)\](?::Club)? OUT_JSON ")
_RELA_FIELD = re.compile(r"(\S+?)=(-?\d+(?:\.\d+)?)(?:\(([^)\s]*)\))?(?=\s|$)")


def parse_rela(line: str) -> ClubReading | None:
    if _RELA_TEXT.search(line):
        fields = {k: (float(v), tag) for k, v, tag in _RELA_FIELD.findall(line)}

        def take(key: str, tags: tuple[str, ...] = ("measured",)) -> float | None:
            v, tag = fields.get(key, (None, ""))
            return v if tag in tags else None

        # The ball numbers it sends GSPro (mph, yards, GSPro's signs) carry no tag.
        launch = None if "type=PUTT" in line else tuple(take(k, ("",)) for k in ("vla", "hla", "spin", "axis", "carry"))
        return _reading("rela", fields.get("ball:speed", (None,))[0], take("club:speed", ("measured", "raw")),
                        launch=launch, angle_of_attack=take("aoa"), path=take("path"), face_to_target=take("face"))
    if m := _RELA_JSON.search(line):
        return _open_connect("rela", line[m.end():])
    return None


def _open_connect(source: str, text: str) -> ClubReading | None:
    """Club speed from an Open Connect shot message (the one club field every sender measures), and
    its launch when it has a carry."""
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    opts, ball, club = (msg.get(k) if isinstance(msg.get(k), dict) else {}
                        for k in ("ShotDataOptions", "BallData", "ClubData"))
    if opts.get("IsHeartBeat") or msg.get("Units", "Yards") != "Yards":
        return None
    launch = tuple(_num(ball.get(k)) for k in ("VLA", "HLA", "TotalSpin", "SpinAxis", "CarryDistance")) \
        if opts.get("ContainsBallData") else None
    return _reading(source, _num(ball.get("Speed")), _num(club.get("Speed")) if opts.get("ContainsClubData") else None,
                    launch=launch)


_OSP_LAUNCH = re.compile(r"\] Launch: BallSpeed=(-?\d+(?:\.\d+)?)")
_OSP_CLUB = re.compile(r"\] Club: ClubHeadSpeed=(-?\d+(?:\.\d+)?)")


class _Parser:
    """Turns one log's new lines into readings, whatever monitor wrote it. Keeps the state a reading
    spread over several lines needs (OpenSkyPlus's)."""

    def __init__(self) -> None:
        self.osp_ball: float | None = None
        self.block: list[str] | None = None

    def feed(self, line: str) -> ClubReading | None:
        line = line.rstrip("\r")
        if self.block is not None:    # OpenSkyPlus 1's pretty-printed Open Connect message
            self.block.append(line)
            if line == "}":
                text, self.block = "\n".join(self.block), None
                return _open_connect("OpenSkyPlus", text)
            if len(self.block) > 200:
                self.block = None
            return None
        if line == "{":
            self.block = [line]
            return None
        if m := _OSP_LAUNCH.search(line):
            self.osp_ball = float(m.group(1)) * MPS_TO_MPH
            return None
        if m := _OSP_CLUB.search(line):
            ball, self.osp_ball = self.osp_ball, None
            return _reading("OpenSkyPlus", ball, float(m.group(1)) * MPS_TO_MPH)
        if "FlightscopeForm" in line:
            return parse_flightscope(line)
        if "SEND=>" in line:
            return parse_rela(line)
        return None


class _Tail:
    """New complete lines in one file. History there when the booth starts is skipped; a file that
    shrinks or is replaced (BepInEx starts its log over each launch) is read again from the top."""

    def __init__(self, path: Path, history: bool = False) -> None:
        self.path = path
        # None: skip what's there at the first read. A file that turns up later is all new.
        self.offset: int | None = 0 if history else None
        self.ident: tuple | None = None
        self.buf = ""
        self.parser = _Parser()

    def read(self) -> list[str]:
        try:
            st = self.path.stat()
        except OSError:
            self.offset, self.buf, self.parser = 0, "", _Parser()   # gone: when it's back, it's all new
            return []
        ident = (st.st_ino, st.st_dev) if st.st_ino else None
        if self.offset is None:
            self.offset, self.ident = st.st_size, ident
            return []
        if self.ident is None:
            self.ident = ident
        if st.st_size < self.offset or ident != self.ident:
            self.offset, self.ident, self.buf, self.parser = 0, ident, "", _Parser()
        if st.st_size == self.offset:
            return []
        skip = st.st_size - self.offset > MAX_READ
        try:
            with open(self.path, "rb") as fh:
                fh.seek(st.st_size - MAX_READ if skip else self.offset)
                data = fh.read(MAX_READ)
                self.offset = fh.tell()
        except OSError:
            return []
        text = data.decode("utf-8", errors="ignore").replace("\0", "")
        if skip:   # started mid-file: drop the partial first line and anything half-read before it
            text, self.buf, self.parser = text.partition("\n")[2], "", _Parser()
        parts = (self.buf + text).split("\n")
        self.buf = parts[-1][-65536:]
        return parts[:-1]


def skytrak_dirs() -> list[Path]:
    """Where the SkyTrak app is installed: the usual places, and wherever its Unity log says."""
    dirs = [os.path.expandvars(d) for d in SKYTRAK_DIRS]
    if profile := os.environ.get("USERPROFILE"):
        unity = Path(profile, "AppData", "LocalLow", "SkyTrak", "SkyTrak")
        for name in ("Player.log", "Player-prev.log"):
            try:
                with open(unity / name, "rb") as fh:
                    head = fh.read(65536).decode("utf-8", errors="ignore")
            except OSError:
                continue
            if m := re.search(r"([A-Za-z]:[\\/][^\r\n'\"<>|]*?)[\\/]SkyTrak_Data", head):
                dirs.insert(0, m.group(1).replace("/", "\\"))
    return [Path(d) for d in dict.fromkeys(dirs) if "%" not in d]


def log_paths(connector_log: str, extra: Iterable[str] = ()) -> list[tuple[Path, str]]:
    """(folder, glob) for every log a club reading can come from on this PC."""
    out: list[tuple[Path, str]] = []
    connector = Path(connector_log)
    out.append((connector.parent, connector.name + "*"))   # GSPconnect rotates through ConnectDebug.txt.1..N
    for var in ("LOCALAPPDATA", "TEMP"):
        if folder := os.environ.get(var):
            out.append((Path(folder) / "rela" if var == "LOCALAPPDATA" else Path(folder), "rela.log"))
    for d in skytrak_dirs():
        out.append((d / "BepInEx", "LogOutput.log"))
        out.append((d / "BepInEx" / "plugins" / "OpenSkyPlus", "Log.txt"))
    for p in extra:
        p = Path(os.path.expandvars(p))
        out.append((p.parent, p.name))
    return out


_ROLLOVER = re.compile(r".*\.(txt|log)(\.\d+)?$", re.I)


class ClubFeed:
    def __init__(self, paths: list[tuple[Path, str]]) -> None:
        self.paths = paths
        self.tails: dict[Path, _Tail] = {}
        self.readings: list[ClubReading] = []
        self.used: list[ClubReading] = []     # given to a shot: a later copy of the same one isn't another shot
        self.read_at: float | None = None     # monotonic, the newest reading
        self._lock = asyncio.Lock()
        self._said: set[str] = set()
        self._started = False

    @classmethod
    def from_config(cls, source_cfg: Any) -> "ClubFeed":
        return cls(log_paths(source_cfg.connector_log, source_cfg.club_logs))

    def _files(self) -> list[Path]:
        files = []
        for folder, pattern in self.paths:
            try:
                files += [p for p in folder.glob(pattern)
                      if ("*" not in pattern or _ROLLOVER.match(p.name)) and p.is_file()]
            except (OSError, ValueError):
                continue
        return list(dict.fromkeys(files))

    def poll(self) -> list[ClubReading]:
        """Reads what's new in every log; returns the readings found (also kept for matching)."""
        now = time.monotonic()
        found = []
        first, self._started = not self._started, True
        for f in self._files():
            if (tail := self.tails.get(f)) is None:
                tail = self.tails[f] = _Tail(f, history=not first)
            for line in tail.read():
                try:
                    r = tail.parser.feed(line)
                except Exception:
                    log.debug("Club feed: unreadable line in %s", f, exc_info=True)
                    continue
                if r is not None:
                    r.at = now
                    found.append(r)
                    if r.source not in self._said:
                        self._said.add(r.source)
                        log.info("Club data: reading %s shots from %s", r.source, f)
        self.readings = [r for r in self.readings if now - r.at < KEEP_SECONDS]
        self.used = [r for r in self.used if now - r.at < COPY_SECONDS]

        def copy(x: ClubReading, r: ClubReading) -> bool:
            return x.source == r.source and abs(x.ball_mph - r.ball_mph) < 0.05 and r.at - x.at < COPY_SECONDS
        for r in found:
            # The same shot logged twice (rela's ball-data and shot-ended lines): the newer copy wins,
            # unless the first already went to a shot.
            self.read_at = now
            if any(copy(x, r) for x in self.used):
                continue
            self.readings = [x for x in self.readings if not copy(x, r)]
            self.readings.append(r)
        return found

    def take(self, ball_mph: float) -> ClubReading | None:
        """The kept reading with club data for a ball this fast (the closest, then the newest), used up."""
        near = [r for r in self.readings if r.has_club and abs(r.ball_mph - ball_mph) <= SAME_BALL_MPH]
        if not near:
            return None
        best = min(near, key=lambda r: (abs(r.ball_mph - ball_mph), -r.at))
        self.readings.remove(best)
        self.used.append(best)
        return best

    def take_launch(self, fired_at: float, before: float, after: float) -> ClubReading | None:
        """The kept reading with a full launch read from `before` seconds before a fire to `after`
        seconds after it (the closest in time), used up."""
        near = [r for r in self.readings if r.launch and fired_at - before <= r.at <= fired_at + after]
        if not near:
            return None
        best = min(near, key=lambda r: abs(r.at - fired_at))
        self.readings.remove(best)
        self.used.append(best)
        return best

    def active(self) -> bool:
        return self.read_at is not None and time.monotonic() - self.read_at < ACTIVE_SECONDS

    async def refresh(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self.poll)

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("Club feed: reading the launch monitor logs failed")
            await asyncio.sleep(POLL_SECONDS)

    async def fill(self, shot: Shot, before_wait: Callable[[], None] | None = None) -> bool:
        """Gives a shot with no club data the reading for its ball, waiting briefly for one while a
        monitor is logging (its line can land just after GSPro's). True when it got one. before_wait
        is called once, if it's going to wait."""
        if shot.club_measured or not shot.ball_speed or shot.raw.get("MonitorLog"):
            return False   # (a shot from a monitor's own line already had its reading)
        deadline = time.monotonic() + WAIT_SECONDS
        waited = False
        while True:
            await self.refresh()
            if (r := self.take(shot.ball_speed)) is not None:
                apply(shot, r)
                log.info("Club data from %s for the %.1f mph shot: club speed %s", r.source, shot.ball_speed,
                         f"{r.club_mph:.1f} mph" if r.club_mph is not None else "not measured")
                return True
            if not self.active() or time.monotonic() >= deadline:
                return False
            if not waited and before_wait is not None:
                before_wait()
            waited = True
            await asyncio.sleep(0.1)


def apply(shot: Shot, r: ClubReading) -> None:
    if not r.has_club:
        return
    shot.club_speed = r.club_mph
    shot.angle_of_attack = r.angle_of_attack
    shot.path = r.path
    shot.face_to_target = r.face_to_target
    shot.loft = r.loft
    shot.club_measured = True
