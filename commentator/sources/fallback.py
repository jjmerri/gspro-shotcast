"""The GSPconnect feed, with GSPro's own log standing in while GSPconnect is silent.

GSPconnect writes a heartbeat to ConnectDebug.txt about every second while it runs. A log that has
gone quiet is a leftover from a monitor no longer connected that way (or GSPconnect isn't running),
so the shots GSPro is playing are taken from its own log instead (ball data only). When GSPconnect
speaks again, its shots (with club data) win and the log's copies are dropped as duplicates.

GSPconnect can also skip a single shot while it keeps heartbeating (GSPro played it; ConnectDebug.txt
never logged it). So each log shot is held for two seconds: one GSPconnect sends too (the same
ball speed) is dropped, and one it never sends is taken from the log. Copies pair one to one, and a
GSPconnect copy that turns up after its shot was taken from the log is dropped too.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from ..models import HoleChangeEvent, ShotEvent
from .base import ShotSource

log = logging.getLogger(__name__)

QUIET_SECONDS = 30.0
MISSED_AFTER = 2.0      # a log shot GSPconnect hasn't sent by now, it skipped (both feeds see a shot within a second)
SAME_SPEED = 0.05       # mph: the log rounds ball speed to two decimals
CAPTURE_AFTER = 0.5     # a held shot's impact state is taken this soon: the ball is still in the air
MATCH_WINDOW = 30.0     # how far apart the two copies of one shot can arrive


def _speed(ev) -> float | None:
    return ev.shot.ball_speed if isinstance(ev, ShotEvent) else None


class ConnectorWithLogFallback(ShotSource):
    def __init__(self, connector, gspro_log, quiet_seconds: float = QUIET_SECONDS,
                 missed_after: float = MISSED_AFTER) -> None:
        self.connector = connector
        self.gspro_log = gspro_log
        self.quiet_seconds = quiet_seconds
        self.missed_after = missed_after
        self.using_log = False
        # Called with a held log shot while its ball is still in the air, so the booth can snapshot
        # its impact-time state (counters, hole, the screen) for when it airs late.
        self.on_impact = None

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
        # Each physical shot reaches both feeds; copies are paired one to one, oldest first.
        sent: list[tuple[float, float]] = []       # GSPconnect shots whose log copy hasn't come yet
        filled: list[tuple[float, float]] = []     # log shots aired as skipped, in case GSPconnect was just late
        held: list[list] = []                      # [arrived, event, captured]: log shots GSPconnect should send

        def take(pool: list, speed: float | None, now: float) -> bool:
            pool[:] = [x for x in pool if now - x[0] < MATCH_WINDOW]
            if speed is not None:
                for x in pool:
                    if abs(x[1] - speed) <= SAME_SPEED:
                        pool.remove(x)
                        return True
            return False

        def next_wake() -> float | None:
            times = [h[0] + (self.missed_after if h[2] else min(CAPTURE_AFTER, self.missed_after)) for h in held]
            return min(times) if times else None
        try:
            while done < len(tasks) or held:
                now = time.monotonic()
                for h in held:
                    if not h[2] and now - h[0] >= min(CAPTURE_AFTER, self.missed_after):
                        h[2] = True
                        if self.on_impact is not None:
                            self.on_impact(h[1])   # the booth's state while this shot is in the air
                for h in [h for h in held if now - h[0] >= self.missed_after]:
                    held.remove(h)
                    log.warning("GSPconnect skipped a shot GSPro played (ball speed %s): taking it from "
                                "GSPro's log (ball data only)", _speed(h[1]))
                    if (speed := _speed(h[1])) is not None:
                        filled.append((h[0], speed))
                    yield h[1]
                if done >= len(tasks) and not held:
                    break
                try:
                    wake = next_wake()
                    timeout = max(0.0, wake - now) if wake is not None else None
                    name, ev = await asyncio.wait_for(queue.get(), timeout)
                except asyncio.TimeoutError:
                    continue
                if ev is None:
                    done += 1
                    continue
                now = time.monotonic()
                if name == "connector":
                    if self.using_log:
                        self.using_log = False
                        log.info("GSPconnect is back: taking shots from it again")
                    if (speed := _speed(ev)) is not None:
                        # Shots already aired from the log are older than any still held.
                        if take(filled, speed, now):
                            log.warning("GSPconnect sent a shot late (ball speed %s), after it was taken from "
                                        "GSPro's log: dropped", speed)
                            continue
                        twin = next((h for h in held if abs((_speed(h[1]) or -1) - speed) <= SAME_SPEED), None)
                        if twin is not None:
                            held.remove(twin)
                            if twin[2] and isinstance(ev, ShotEvent):
                                # Late enough that the booth already took the impact state: keep it.
                                ev.impact_at = twin[1].impact_at
                                ev.impact_state = twin[1].impact_state
                                ev.impact_read = twin[1].impact_read
                                ev.setup_hud = twin[1].setup_hud
                        else:
                            sent.append((now, speed))
                    yield ev
                elif self.connector_live():
                    # GSPconnect normally has this shot too, with club data; hold it in case it doesn't.
                    if isinstance(ev, ShotEvent) and not take(sent, _speed(ev), now):
                        held.append([now, ev, False])
                    continue
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
