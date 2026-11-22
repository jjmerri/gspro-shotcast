"""Publishing the stats page, backing up the season file, and health checks.

Part of the Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import json
import time
from pathlib import Path

from .common import log
from .. import perf_log, report as problem_report
from ..health import check_changed, check_files, check_screen, evaluate_round, report_line
from ..hud import Hud
from ..round import Round
from ..sources.gspro_log import live_log


class UpkeepMixin:
    """Publishing the stats page, backing up the season file, and health checks."""

    def _schedule_publish(self, after_speech: bool = False) -> None:
        """Publish the stats page after a round, once the sign-off has aired (its lines are stored
        as they're queued, so they're in it). Without `after_speech`, it's the backup timer
        (publish.delay_seconds) in case the sign-off never finishes. A publish that's still waiting
        is replaced by the newer one."""
        p = self.cfg.publish
        if not (p.enabled and p.after_round and p.bucket) or self.cfg.source.type == "replay":
            return
        if self._publish_task and not self._publish_task.done() and not self._publish_lock.locked():
            self._publish_task.cancel()   # still waiting: this one covers it
        self._publish_task = asyncio.create_task(self._publish_after(0 if after_speech else p.delay_seconds,
                                                                     drain=after_speech))

    async def _publish_after(self, delay: float, drain: bool = False) -> None:
        await asyncio.sleep(delay)
        if drain:
            # Not while the sign-off is still being voiced: building the site takes CPU.
            await self._drain_speech(120)
        async with self._publish_lock:
            try:
                await self._publish()
            except Exception:
                log.exception("Stats page publish failed")
        self._recap_soon()   # a league round's reel is up now

    @perf_log.timed("publish")
    async def _publish(self) -> None:
        from ..stats_site import publish_command, site_dir

        cmd = publish_command(self.cfg)
        if cmd is None:
            return
        # Its own process, so building the stats never holds up the booth during the next round.
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            out, code = b"timed out after 10 minutes", -1
        else:
            code = proc.returncode
        text = out.decode("utf-8", "replace").strip()
        if code == 0:
            log.info("Stats page: %s", text.splitlines()[-1] if text else "published")
            self.health.clear("publish.failed", why="published")
            if self.alerts is not None:
                await asyncio.to_thread(self.alerts.after_publish, site_dir(self.cfg))
        else:
            last = text.splitlines()[-1] if text else f"exit {code}"
            log.warning("Stats page publish failed: %s", text[-2000:])
            if self.cfg.health.enabled:
                self.health.raise_issue("publish.failed", "warn", f"Couldn't publish the stats page: {last}")


    def _schedule_backup(self, why: str) -> None:
        """Back up the season file in the background (commentator/backup.py): at startup, and after a
        round once its sign-off lines are stored. Skipped when nothing changed since the last copy."""
        if not self.cfg.backup.enabled or self.cfg.source.type == "replay":
            return
        task = asyncio.create_task(self._backup(why), name="backup")
        self._backup_tasks.add(task)
        task.add_done_callback(self._backup_tasks.discard)

    async def _backup(self, why: str) -> None:
        from .. import backup

        async with self._backup_lock:
            try:
                with perf_log.work("backup"):
                    path = await asyncio.to_thread(backup.run, self.cfg)
            except Exception as e:
                log.exception("Season backup (%s) failed", why)
                if self.cfg.health.enabled:
                    self.health.raise_issue("backup.failed", "warn", f"Couldn't back up the season file: {e}")
                return
        log.info("Season backup (%s): %s", why, path.name if path else "unchanged since the last copy")
        self.health.clear("backup.failed", why="backed up")


    @staticmethod
    async def _say_ready(path: str, health: asyncio.Task | None) -> None:
        """Tell the tray (which started this booth) it's up: its startup checks done, so the tray's
        warnings are this run's."""
        if health is not None:
            await asyncio.wait([health])
        try:
            Path(path).write_text(f"{time.time():.0f}\n", encoding="utf-8")
        except OSError as e:
            log.warning("Couldn't tell the tray the booth is ready (%s): %s", path, e)

    async def _startup_health(self) -> None:
        # The version comes from the log's backlog, so wait (bounded) for the follower to read it.
        for _ in range(240):
            if self._follower is None or self._follower.primed:
                break
            await asyncio.sleep(0.25)
        try:
            await asyncio.to_thread(self._health_checks, "startup")
        except Exception:
            log.exception("Startup health check failed")

    def _health_checks(self, when: str) -> None:
        """The files the booth follows exist; GSPro's version and the screen size are the ones the booth
        last ran clean against (a change raises a warning a clean round clears)."""
        h = self.health
        files = {}
        feed = type(self.source).__name__
        if self._log_truth or feed in ("GsproLogShotSource", "ConnectorWithLogFallback"):
            files["GSPro log"] = self._follower.path if self._follower else live_log(self.cfg.source.gspro_log)
        if feed in ("ConnectorLogSource", "ConnectorWithLogFallback"):   # not GSPro's log alone
            files["connector log"] = self.cfg.source.connector_log
        check_files(h, files)
        if self._log_truth:
            check_changed(h, "gspro_version", self.gspro.version, "GSPro's version")
        if self.screen and self.cfg.screen.ocr:
            check_screen(h, self._view_size())
        else:
            h.note_screen(None)   # settle the proven sizes first, so clearing this doesn't count as proof
            h.clear("changed.screen", "the screen isn't read")
        issues = h.issues()
        log.info("Health check (%s): GSPro %s; %s", when, self.gspro.version or "version unknown",
                 "; ".join(f"{i.level} {i.message}" for i in issues) if issues else "all clear")

    def _view_size(self) -> str | None:
        """GSPro's view size as `WxH`, or None without screen reading (no screen, or OCR off: nothing's
        read off it, so its size can't break or prove anything) or while GSPro's view isn't up."""
        if not self.screen or not self.cfg.screen.ocr:
            return None
        try:
            size = self.screen.view_size()
        except Exception:
            log.exception("Couldn't read the screen size")
            return None
        return f"{size[0]}x{size[1]}" if size else None

    def _health_shot(self, before: Hud, after: Hud, is_putt: bool, ocr: bool) -> set[str]:
        """Count this shot's reads, and flag at once (not at round end) a data tile that never reads or
        a GSPro log that isn't moving: either breaks the rest of the round. The required tiles it counted
        missing on this shot (a read at the next setup takes them back: _apply_late_tiles)."""
        missed: set[str] = set()
        if not self.cfg.health.enabled or not self.round or self.round.ended:
            return missed
        hc, h = self._hc, self.health
        hc.shots += 1
        # Not gated on the ball being seen to land: that's read off the carry tile, the very thing checked.
        if ocr and not is_putt and (before.lie or "").lower() != "green":
            hc.full_shots += 1
            if hc.full_shots == 1 and self.screen is not None and self.cfg.source.type != "replay":
                # A full frame with the tiles holding real values, for `fixture save/check` over ssh.
                asyncio.create_task(asyncio.to_thread(self._save_fixture_frame), name="fixture-frame")
            seen = set(before.tiles) | set(after.tiles)
            hc.tiles_seen |= seen
            for t in self.cfg.health.required_tiles:
                if t not in seen:
                    hc.tiles_missing[t] = hc.tiles_missing.get(t, 0) + 1
                    missed.add(t)
            if hc.full_shots >= 3:
                if absent := [t for t in self.cfg.health.required_tiles if t not in hc.tiles_seen]:
                    # The tile panel as it is: whether the tile is on it at all (GSPro's tiles are the player's choice).
                    self._keep_report_frame("tiles-missing", crops=after.crops or before.crops, absent=absent)
                    h.raise_issue("round.tiles_absent", "fail",
                                  f"no {', '.join(repr(t) for t in absent)} tile read in {hc.full_shots} shots: "
                                  f"a data tile changed or the HUD moved?")
                else:
                    h.clear("round.tiles_absent", "tiles read")
        if self._log_truth and self.cfg.source.type != "replay" and hc.shots >= 3:
            if self.gspro.fires == hc.fires_at_start:
                h.raise_issue("round.log_silent", "fail",
                              f"GSPro's log logged no shots while the booth saw {hc.shots}: is it the right file?")
            else:
                h.clear("round.log_silent", "log moving")
        return missed

    def _keep_fixture_frame(self, kind: str, frame=None) -> None:
        """Save a frame of this kind for `fixture save` (scorecard, setup tabs), at most every ten
        minutes per kind, off the event loop."""
        if self.screen is None or self.cfg.source.type == "replay":
            return
        now = time.monotonic()
        if now - self._fixture_saved_at.get(kind, -1e9) < 600:
            return
        self._fixture_saved_at[kind] = now
        asyncio.create_task(asyncio.to_thread(self._save_fixture_frame, kind, frame), name=f"fixture-{kind}")

    def _keep_report_frame(self, reason: str, crops: dict | None = None, **meta) -> None:
        """Leave the screen behind for the next problem report when a read came up empty (no player
        on a shot, no pin on the minimap): what the booth was looking at shows whether it's reading
        the wrong monitor or GSPro's screen changed. At most every ten minutes per reason."""
        if self.screen is None or self.cfg.source.type == "replay":
            return
        now = time.monotonic()
        key = f"report-{reason}"
        if now - self._fixture_saved_at.get(key, -1e9) < 600:
            return
        self._fixture_saved_at[key] = now
        meta = {"gspro_version": self.gspro.version, "course": self.round.course if self.round else None,
                "hole": self.hole_number, "monitor": self.screen.monitor,
                "capture": self.cfg.screen.capture, **meta}

        def save() -> None:
            try:
                problem_report.keep_frame(self.cfg.log_dir, reason, self.screen.grab(), meta, crops=crops)
            except Exception:
                log.exception("Couldn't keep the %s screen for a problem report", reason)

        asyncio.create_task(asyncio.to_thread(save), name=key)

    def _save_fixture_frame(self, kind: str = "shot", frame=None) -> None:
        try:
            frame = frame if frame is not None else self.screen.grab()
            d = Path(self.cfg.log_dir)
            d.mkdir(parents=True, exist_ok=True)
            stem = "fixture-frame" if kind == "shot" else f"fixture-frame-{kind}"
            frame.save(d / f"{stem}.png", compress_level=1)
            (d / f"{stem}.json").write_text(json.dumps({
                "captured": time.strftime("%Y-%m-%d %H:%M:%S"), "gspro_version": self.gspro.version,
                "course": self.round.course if self.round else None}), encoding="utf-8")
        except Exception:
            log.exception("Couldn't save the fixture frame")

    def _health_round_end(self, r: Round) -> None:
        """The post-round checkup: judge the round's reads, raise or clear the round's findings, and
        keep the report card. A round whose checks all pass also proves a pending change (a GSPro
        update, a new screen) didn't break anything."""
        h = self.health
        found, report = evaluate_round(self._hc, self.gspro.fires, dict(self.gspro.kinds), list(self.gspro.unparsed),
                                       self.cfg.health.required_tiles, self._log_truth,
                                       bool(self.screen and self.cfg.screen.ocr))
        report = {"round": r.id, "course": r.course, "ended": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "gspro_version": self.gspro.version, **report}
        for key, level, msg in found:
            h.raise_issue(key, level, msg)
        if report["checked"]:
            h.clear_prefix("round.", keep={k for k, _, _ in found}, why="passed this round")
            if not found:
                h.clear_verified(screen=self._view_size())
        h.record_round(report)
        log.info("Health checkup, round %s: %s", r.id, report_line(report))
