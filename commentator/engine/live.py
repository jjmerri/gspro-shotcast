"""The live stream.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import threading
import time
from pathlib import Path

from .common import log


class LiveMixin:
    """The live stream."""

    def _live_on(self) -> bool:
        lv = self.cfg.live
        return lv.enabled and bool(self.cfg.publish.bucket or lv.local_keep_days) and self.cfg.source.type != "replay"

    def _live_mirrors(self) -> list:
        return [m for m in (self.live_mirror, self.live_local) if m is not None]

    def _obs(self):
        from ..live_stream import Obs

        return Obs(self.cfg.live.obs_host, self.cfg.live.obs_port)

    def _start_live(self) -> None:
        """Mirror OBS's stream folder to the bucket, and keep it on the PC, for as long as the booth runs."""
        from .. import live_stream

        lv, p = self.cfg.live, self.cfg.publish
        if not self._live_on():
            if p.enabled and p.bucket and self.cfg.source.type != "replay":
                # Turned off since it last ran: take the Live tab off the page.
                def remove() -> None:
                    try:
                        from .. import backend

                        live_stream.remove(p.bucket, p.prefix, backend.s3(p.region))
                    except Exception as e:
                        log.info("Live stream: couldn't remove the Live tab: %s", e)
                threading.Thread(target=remove, name="live-remove", daemon=True).start()
            return
        if lv.launch_obs:
            try:
                if live_stream.launch_obs(lv.obs_path):
                    log.info("Started OBS for the live stream")
            except Exception:
                log.exception("Starting OBS failed")
        from ..local_video import LocalRecorder

        status = Path(self.cfg.log_dir) / "live.json"
        if p.bucket:
            try:
                from .. import backend

                client = backend.s3(p.region)
                self.live_mirror = live_stream.Mirror(lv.dir, p.bucket, p.prefix, lv.playlist, client,
                                                      status_file=status, keep_days=lv.keep_days)
            except Exception:
                log.exception("The live stream can't upload")
        try:
            if lv.local_keep_days:
                # Its own pass over OBS's folder, so the bucket (the internet down, its start-up tidy
                # retrying) never holds up the copy here.
                self.live_local = live_stream.Mirror(lv.dir, None, playlist=lv.playlist,
                                                     status_file=None if self.live_mirror else status,
                                                     local=LocalRecorder(lv.local_dir, lv.local_keep_days))
            mirrors = self._live_mirrors()
            for m in mirrors:
                m.peers = [o for o in mirrors if o is not m]
                m.start()
        except Exception:
            log.exception("The live stream's mirror didn't start")
            return
        asyncio.get_running_loop().create_task(self._live_idle_watch(), name="live-idle")

    def _live_round_start(self) -> None:
        self._live_last_shot, self._live_resume_on_shot = time.monotonic(), False
        self._live_prerolled = False
        if not (self._live_on() and self.cfg.live.with_rounds):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return   # a round started outside the booth's loop (tests)
        if self._live_stop_task and not self._live_stop_task.done():
            self._live_stop_task.cancel()   # back-to-back rounds: stay on air
        loop.create_task(self._live_call("start"), name="live-start")

    def _live_preroll(self) -> None:
        """On the setup screen for a course: on air now, so a tee shot struck the moment the course
        loads (before the round's own start, a few seconds on) is on the recording. Straight from the
        last round, its stream stays on. No round following, the idle stop takes it off again (and a
        recording with no shots isn't kept)."""
        if not (self._live_on() and self.cfg.live.with_rounds) or self._round_on():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._live_stop_task and not self._live_stop_task.done():
            self._live_stop_task.cancel()
        self._live_last_shot, self._live_prerolled = time.monotonic(), True
        loop.create_task(self._live_call("start"), name="live-preroll")

    def _live_label(self) -> str | None:
        """The round on the stream, for the Round replays tab: course, then who's playing."""
        r = self.round
        if r is None:
            return None
        names = list(dict.fromkeys(p for side in r.sides for p in (side.players or side.gspro)))
        kind = "" if r.is_league else "tournament" if r.is_tournament else "practice"
        return " · ".join(x for x in (r.course or "Round", ", ".join(names), kind) if x)

    def _live_mark(self, replace: bool = False) -> None:
        if label := self._live_label():
            for m in self._live_mirrors():
                m.mark(label, replace=replace)

    async def _live_call(self, what: str) -> None:
        if what == "start" and self._round_on():
            self._live_mark()
        obs = self._obs()

        def call() -> bool:
            # One at a time, in order: a stop already under way when the next round starts (its task
            # cancelled, its thread not) must finish before that round's start looks at OBS.
            with self._obs_lock:
                if what == "start" and (device := self.cfg.live.game_sound_device):
                    try:
                        from ..obs_setup import game_sound

                        game_sound(obs, device, self.cfg.live.game_sound_db)
                    except Exception as e:
                        log.warning("Live stream: couldn't check OBS's game sound: %s", e)
                return getattr(obs, what)()
        try:
            changed = await asyncio.to_thread(call)
            if changed:
                log.info("Live stream: OBS output %s", "started" if what == "start" else "stopped")
        except Exception as e:
            log.warning("Live stream: couldn't %s OBS's output: %s", what, e)

    def _round_on(self) -> bool:
        return self.round is not None and not self.round.ended and not self._round_end_queued

    def _live_shot(self) -> None:
        self._live_last_shot = time.monotonic()
        if not self._live_resume_on_shot or not self._round_on():
            return
        self._live_resume_on_shot = False
        log.info("Live stream: a shot after the idle stop, back on")
        asyncio.get_running_loop().create_task(self._live_call("start"), name="live-resume")

    async def _live_idle_watch(self, every: float = 30.0) -> None:
        """A stream left on, a round abandoned on a hole: off after live.idle_minutes with no shots."""
        while True:
            await asyncio.sleep(every)
            idle = self.cfg.live.idle_minutes * 60
            m = self.live_local or self.live_mirror   # the PC's copy: the bucket's can lag (or be stuck)
            session = m.session if m is not None else None
            if not idle or session is None or session == self._live_idle_session:
                continue
            # From its last shot, or from when it went on air (by hand, long after the last shot).
            quiet = min(time.monotonic() - self._live_last_shot, m.clock() - m.since)
            if quiet < idle:
                continue
            log.info("Live stream: no shots for %.0f minutes, stopping it", quiet / 60)
            self._live_idle_session = session
            self._live_resume_on_shot = self.cfg.live.with_rounds and self._round_on()
            await self._live_call("stop")

    def _schedule_live_stop(self) -> None:
        """Off air once the sign-off has aired, and a little after."""
        self._live_mark(replace=True)   # the course and players as the round ended, read in full by now
        if not (self._live_on() and self.cfg.live.with_rounds) or self._live_prerolled:
            return   # (the next round's setup is on air already: its round, or the idle stop, ends it)
        if self._live_stop_task and not self._live_stop_task.done():
            self._live_stop_task.cancel()

        async def stop() -> None:
            await self._drain_speech(120)
            await asyncio.sleep(self.cfg.live.stop_after_seconds)
            await self._live_call("stop")

        self._live_stop_task = asyncio.create_task(stop(), name="live-stop")
