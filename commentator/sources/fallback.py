"""The GSPconnect feed, with GSPro's own log standing in while GSPconnect is silent.

GSPconnect writes a heartbeat to ConnectDebug.txt about every second while it runs. A log that has
gone quiet is a leftover from a monitor no longer connected that way (or GSPconnect isn't running),
so the shots GSPro is playing are taken from its own log instead (ball data only). When GSPconnect
speaks again, its shots (with club data) win and the log's copies are dropped as duplicates.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from ..models import HoleChangeEvent, ShotEvent
from .base import ShotSource

log = logging.getLogger(__name__)

QUIET_SECONDS = 30.0


class ConnectorWithLogFallback(ShotSource):
    def __init__(self, connector, gspro_log, quiet_seconds: float = QUIET_SECONDS) -> None:
        self.connector = connector
        self.gspro_log = gspro_log
        self.quiet_seconds = quiet_seconds
        self.using_log = False

    def connector_live(self) -> bool:
        heard = self.connector.heard_at
        return heard is not None and time.monotonic() - heard < self.quiet_seconds

    async def events(self) -> AsyncIterator[ShotEvent | HoleChangeEvent]:
        queue: asyncio.Queue = asyncio.Queue()

        async def pump(name: str, source) -> None:
            try:
                async for ev in source.events():
                    await queue.put((name, ev))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("The %s shot feed failed", name)
            await queue.put((name, None))

        tasks = [asyncio.create_task(pump("connector", self.connector), name="feed-connector"),
                 asyncio.create_task(pump("gspro_log", self.gspro_log), name="feed-gspro-log")]
        done = 0
        try:
            while done < len(tasks):
                name, ev = await queue.get()
                if ev is None:
                    done += 1
                    continue
                if name == "connector":
                    if self.using_log:
                        self.using_log = False
                        log.info("GSPconnect is back: taking shots from it again")
                    yield ev
                elif self.connector_live():
                    continue   # GSPconnect has (or will have) this shot, with club data
                else:
                    if not self.using_log:
                        self.using_log = True
                        heard = self.connector.heard_at
                        log.warning("GSPconnect's log has been quiet %s: taking shots from GSPro's own log "
                                    "(ball data only). Set source.type: gspro_log if your monitor doesn't "
                                    "use GSPconnect.", "since the booth started" if heard is None
                                    else f"for {time.monotonic() - heard:.0f}s")
                    yield ev
        finally:
            for t in tasks:
                t.cancel()
