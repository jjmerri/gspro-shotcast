import asyncio
import json
import logging
import re
import time
from datetime import datetime
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..models import HoleChangeEvent, Shot, ShotEvent
from .base import ShotSource
from .gspro_sniffer import is_shot_message

log = logging.getLogger(__name__)

DEFAULT_LOG = r"C:\GSProV1\Core\GSPC\ConnectDebug.txt"

# A shot read more than this long after GSPconnect logged it is stale (the booth fell behind); it
# belongs to a hole or a player the broadcast has already moved past, so it's dropped.
STALE_SHOT_SECONDS = 10.0


STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})")


def parse_log(text: str) -> list[dict[str, Any]]:
    """Every Open Connect message GSPconnect logged, in order. Each gets "_logged_at", the
    time GSPconnect wrote it, when the line has its usual timestamp."""
    out = []
    for line in text.replace("\0", "").splitlines():
        start = line.find('{"DeviceID"')
        if start < 0:
            continue
        try:
            msg = json.loads(line[start:].strip())
        except json.JSONDecodeError:
            continue  # a line caught mid-write; the next read has it whole
        if m := STAMP.match(line.strip()):
            msg["_logged_at"] = datetime.strptime(f"{m.group(1)}.{m.group(2)}", "%Y-%m-%d %H:%M:%S.%f").timestamp()
        out.append(msg)
    return out


class ConnectorLogSource(ShotSource):
    """Follows GSPconnect's debug log, which records every message from the launch monitor.

    Uneekor talks to GSPro through GSPconnect's built-in support (not the Open Connect port),
    so this is the shot feed for that setup: no packet capture, no admin rights. GSPconnect
    rewrites the same file in place and keeps only the last few seconds, so it's read whole
    several times a second. Messages carry an ever-increasing ShotNumber (on every message,
    heartbeats included), which is how new ones are told apart.
    """

    def __init__(self, path: str = DEFAULT_LOG, poll_seconds: float = 0.1) -> None:
        self.path = Path(path)
        self.poll = poll_seconds
        self.last_number: int | None = None
        # ShotNumbers already emitted. Dedup is a set, not the heartbeat high-water mark: GSPconnect
        # spreads messages across rotating files and can log a real shot to a lagging file with a
        # ShotNumber the (far more frequent) heartbeats have already pushed last_number past. Gating
        # shots on `> last_number` dropped exactly those shots. This also covers one shot logged
        # across several messages under the same number, and a file-cycle re-read replaying history.
        self._seen_shots: set[int] = set()
        # GSPconnect does not write one growing file: it round-robins the active log through
        # ConnectDebug.txt and ConnectDebug.txt.1..N (~10 MB each), so a shot can be written to any
        # of them. We tail every one of them by byte offset, which never misses a shot regardless of
        # which is currently being written. A per-file offset and partial-line buffer track progress.
        self._offsets: dict[str, int] = {}
        self._bufs: dict[str, str] = {}
        self._primed = False
        # When (monotonic) GSPconnect last wrote a message, heartbeats included: it writes one about
        # every second while it's running, so a quiet log means it isn't the feed GSPro is using.
        self.heard_at: float | None = None

    _ROLLOVER = re.compile(r".*\.txt(\.\d+)?$")

    def _live_files(self) -> list[Path]:
        """The active log set: ConnectDebug.txt and its numeric rollovers, not the dated archives."""
        try:
            return sorted(p for p in self.path.parent.glob(self.path.name + "*")
                          if p.is_file() and self._ROLLOVER.match(p.name))
        except OSError:
            return [self.path]

    def _read_new_lines(self) -> list[str]:
        """Complete lines newly written to any of the live log files since the last read."""
        files = self._live_files()
        if not self._primed:  # skip the history present at startup; remember where each file ends
            self._primed = True
            newest, newest_mtime = None, -1.0
            for f in files:
                try:
                    st = f.stat()
                    self._offsets[f.name] = st.st_size
                    if st.st_mtime > newest_mtime:
                        newest, newest_mtime = f, st.st_mtime
                except OSError:
                    continue
            if newest:  # seed last_number from the active file's tail so new shots aren't skipped
                try:
                    with open(newest, "rb") as fh:
                        fh.seek(max(0, self._offsets.get(newest.name, 0) - 8192))
                        nums = re.findall(r'"ShotNumber":\s*(\d+)', fh.read().decode("utf-8", "ignore"))
                    if nums:
                        self.last_number = int(nums[-1])
                except OSError:
                    pass
            return []
        lines: list[str] = []
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                continue
            off = self._offsets.get(f.name, 0)
            if size < off:  # the file was cycled/overwritten in place: start over on it
                off, self._bufs[f.name] = 0, ""
            if size == off:
                self._offsets[f.name] = off
                continue
            try:
                with open(f, "rb") as fh:  # shared read access while GSPconnect holds it open
                    fh.seek(off)
                    data = fh.read()
                    self._offsets[f.name] = fh.tell()
            except OSError:
                continue
            buf = self._bufs.get(f.name, "") + data.decode("utf-8", errors="ignore")
            parts = buf.split("\n")
            self._bufs[f.name] = parts[-1]  # keep the trailing partial line for this file
            lines.extend(parts[:-1])
        return lines

    def new_messages(self, text: str) -> list[dict[str, Any]]:
        msgs = [m for m in parse_log(text) if isinstance(m.get("ShotNumber"), int)]
        if not msgs:
            return []
        highest = max(m["ShotNumber"] for m in msgs)
        if self.last_number is None or highest < self.last_number - 1000:
            # First read, or GSPconnect restarted its numbering: start from now, not from history.
            self.last_number = highest
            self._seen_shots.clear()
            return []
        self.last_number = max(self.last_number, highest)
        # Return every newly read message, regardless of the heartbeat high-water mark. Byte offsets
        # already ensure each line is read once (bar a file-cycle re-read, which the emitted-set in
        # events() absorbs), so there's no need to also filter on ShotNumber here, and doing so
        # silently dropped shots that landed in a lagging rollover file with a number the far more
        # frequent heartbeats had already pushed last_number past.
        return sorted(msgs, key=lambda m: m["ShotNumber"])

    async def events(self) -> AsyncIterator[ShotEvent | HoleChangeEvent]:
        log.info("Following %s", self.path)
        reads = 0
        while True:
            try:
                lines = await asyncio.to_thread(self._read_new_lines)
            except OSError as e:
                log.warning("Can't read %s: %s", self.path, e)
                await asyncio.sleep(0.5)
                continue
            if any('"ShotNumber"' in ln for ln in lines):
                self.heard_at = time.monotonic()
            try:
                fresh = self.new_messages("\n".join(lines)) if lines else []
            except Exception:
                log.exception("Failed to parse the connector log")
                fresh = []
            for msg in fresh:
                if not is_shot_message(msg):
                    continue
                num = msg.get("ShotNumber")
                if num in self._seen_shots:
                    continue  # same shot logged again under this number, or a cycled-file re-read
                self._seen_shots.add(num)
                if len(self._seen_shots) > 512:  # ShotNumber is monotonic; keep the recent tail
                    for old in sorted(self._seen_shots)[:256]:
                        self._seen_shots.discard(old)
                # Only ball speed tells a real shot from a non-shot here: putts legitimately carry
                # sentinel VLA/carry (GSPconnect can't measure a roll), and those are cleaned in
                # Shot.from_gspro, so don't reject on them or every putt is lost.
                speed = (msg.get("BallData") or {}).get("Speed") or 0
                if not 0 < speed < 250:
                    log.info("Skipping implausible shot %s: ball speed %s", num, speed)
                    continue
                logged = msg.get("_logged_at")
                age = time.time() - logged if logged else 0.0
                if logged and age > STALE_SHOT_SECONDS:
                    # Read long after it happened (we fell behind): reacting now would land on the
                    # wrong hole or the next player. Drop it.
                    log.info("Skipping stale shot %s: read %.1fs after GSPconnect logged it", num, age)
                    continue
                try:
                    event = ShotEvent(shot=Shot.from_gspro(msg), logged_at=logged)
                except Exception:
                    log.exception("Bad shot message, skipping: %s", msg)
                    continue
                log.info("Connector shot: ShotNumber %s, ball speed %s, read %.2fs after GSPconnect logged it",
                         num, speed, age)
                yield event
            reads += 1
            if reads % 100 == 0:  # ~every 10s; shows the feed is alive and the number is advancing
                log.info("Connector feed alive: last ShotNumber %s, following %s files",
                         self.last_number, len(self._offsets))
            await asyncio.sleep(self.poll)
