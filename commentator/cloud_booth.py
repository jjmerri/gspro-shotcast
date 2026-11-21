"""The booth's side of cloud mode (shotcast-cloud's docs/dev/thin-pc.md): in a cloud profile
(cloud.startup, as cloud.locate found it), the running booth sends its settled rounds up
(cloud_rounds). Read once, as the booth starts: self-hosted, nothing runs here. A pairing made or changed while it runs takes effect at the
next start; meanwhile the loop stops for good once the stored pairing isn't the one the booth started
with, and nothing else about the booth changes.

How uploads stand goes to logs/cloud-health.json (`HEALTH`) for the tray: a pairing the API no longer
accepts, uploads stopped, the API needing a newer Shotcast (426), and what the season file keeps
(cloud_rounds.health): rounds held as conflicts or refused, and rounds waiting to be tried again after
failing again and again; each of those turns the tray red. It's rewritten after every try, so it clears once that's resolved.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from . import cloud

log = logging.getLogger(__name__)

DRAIN_S = 60.0         # how often the upload loop looks for settled rounds to send
BLOCKED_S = 900.0      # how long it waits when the cloud refuses every upload from this PC
FAILS_SHOWN = 3        # failed tries in a row (of a drain, or of one round) before the tray goes red for it
HEALTH = "cloud-health.json"


def set_health(log_dir: str | Path, issues: list[tuple[str, str]]) -> None:
    """The tray's view of uploads: (level, message) pairs, level 'fail' or 'warn'; [] clears it."""
    path = Path(log_dir) / HEALTH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"issues": [{"level": lvl, "message": msg} for lvl, msg in issues]}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.debug("Couldn't write %s", path, exc_info=True)


def start(cfg: Any, prof: cloud.Profile | None = None) -> asyncio.Task | None:
    """As the booth runs (not a replay), in the cloud profile it started in (`prof`, else
    cloud.startup): the upload loop. None otherwise, and when the API no longer accepts the pairing
    (the tray is told why: rounds are kept in the profile)."""
    prof = prof or cloud.startup
    if prof is None or cfg.source.type == "replay":
        return None
    if prof.problem:
        log.warning("Shotcast Cloud: rounds are kept in %s and not sent: %s", cfg.log_dir, prof.problem)
        set_health(cfg.log_dir, [("fail", f"Shotcast Cloud: nothing is sent: {prof.problem}")])
        return None
    set_health(cfg.log_dir, [])
    log.info("Shotcast Cloud: paired as sim %r; rounds are kept in %s and sent up", prof.rec.get("sim_name"),
             cfg.log_dir)
    return asyncio.create_task(Uploads(cfg, prof).run(), name="cloud-upload")


class Uploads:
    def __init__(self, cfg: Any, prof: cloud.Profile) -> None:
        self.cfg, self.bound, self.identity = cfg, prof.bound, cloud.identity(prof.rec)
        self.stopped = False   # the stored pairing isn't the booth's any more: nothing more goes out
        self.fails = 0

    def _cloud(self) -> cloud.Cloud:
        """This PC's pairing, if it's still the one the booth started with (else PairingChanged)."""
        cl = cloud.Cloud(cloud.store(), expect=self.bound)
        if cl.identity != self.identity:
            raise cloud.PairingChanged("this PC's Shotcast Cloud pairing changed; restart Shotcast to use it")
        return cl

    def _health(self, *issues: tuple[str, str]) -> None:
        """The tray's view: `issues` from this try, then what the season file keeps."""
        from . import cloud_rounds

        kept: list[tuple[str, str]] = []
        try:
            kept = cloud_rounds.health(self.cfg.db_path, retries=FAILS_SHOWN)
        except Exception:   # noqa: BLE001 - a busy file: this try's view only
            log.debug("Couldn't read upload health from %s", self.cfg.db_path, exc_info=True)
        set_health(self.cfg.log_dir, list(issues) + kept)

    async def run(self) -> None:
        from . import cloud_rounds

        while True:
            wait = DRAIN_S
            try:
                out = await asyncio.to_thread(cloud_rounds.drain, self._cloud(), self.cfg.db_path, None,
                                              cloud_rounds.CLOSER_SECONDS, 0)
                if out.sent or out.parked or out.conflicts:
                    log.info("Shotcast Cloud: %s round(s) sent%s%s", len(out.sent),
                             f", {len(out.conflicts)} held as conflicts" if out.conflicts else "",
                             f", {len(out.parked)} refused" if out.parked else "")
                self.fails = 0
                self._health()
            except (cloud.NotPaired, cloud.PairingChanged, cloud_rounds.NotThisPairing) as e:
                # Not paired, paired elsewhere, a pairing file that can't be trusted, a season file
                # that isn't this pairing's: nothing more till Shotcast restarts.
                log.warning("Shotcast Cloud: nothing more sent till Shotcast restarts: %s", e)
                self._health(("fail", f"Shotcast Cloud: uploads stopped: {e}"))
                self.stopped = True
                return
            except cloud.TooOld as e:
                log.warning("Shotcast Cloud: %s", e)
                self._health(("fail", f"Shotcast Cloud needs Shotcast {e.minimum} or later: update Shotcast"))
                wait = BLOCKED_S
            except cloud_rounds.Blocked as e:
                log.warning("Shotcast Cloud: %s", e)
                self._health(("fail", f"Shotcast Cloud: {e}"))
                wait = BLOCKED_S
            except TimeoutError:
                pass   # (the command line is uploading)
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001 - the network, a refresh refused, a busy file: tried again
                log.warning("Shotcast Cloud: rounds not sent: %s", e)
                self._failed(f"rounds aren't going up: {e}")
            await asyncio.sleep(wait)

    def _failed(self, what: str) -> None:
        self.fails += 1
        self._health(*[("fail", f"Shotcast Cloud: {what}")] * (self.fails >= FAILS_SHOWN))
