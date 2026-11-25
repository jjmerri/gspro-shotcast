"""The booth's side of cloud mode (shotcast-cloud's docs/dev/thin-pc.md): in a cloud profile
(cloud.startup, as cloud.locate found it), the running booth sends its settled rounds up
(cloud_rounds). Read once, as the booth starts: self-hosted, nothing runs here. A pairing made or changed while it runs takes effect at the
next start; meanwhile the loop stops for good once the stored pairing isn't the one the booth started
with, and nothing else about the booth changes.

How uploads stand goes to logs/cloud-health.json (`HEALTH`) for the tray: a pairing the API no longer
accepts, uploads stopped, the API needing a newer Shotcast (426), and, the one rule for everything
else (a round the API refuses too), finished rounds that haven't all been on Shotcast Cloud for
BEHIND_S (`Uploads._behind`: by its outcome, whatever the cause). Each turns the
tray red. It's rewritten after every try, so it clears once that's resolved.

Every wait here is counted in elapsed seconds while the booth runs (cloud_rounds.Pace), never by the
time of day: a booth that starts counts afresh.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import cloud

log = logging.getLogger(__name__)

DRAIN_S = 15.0         # how often the upload loop looks for done rounds to send (a look at the season file:
                       # the API is only called when one is ready). A round goes up to this long after it's done.
BLOCKED_S = 900.0      # how long it waits when the cloud refuses every upload from this PC
BEHIND_S = 900.0       # finished rounds not all on Shotcast Cloud for this long: the tray goes red, whatever the cause
HEALTH = "cloud-health.json"

# The live stream's word for the tray (cloud_stream.CloudStream: the month's streaming used up), shown
# with how uploads stand each time they're written. Set from the stream's thread; read here.
stream_issues: list[tuple[str, str]] = []


def set_stream_issues(issues: list[tuple[str, str]]) -> None:
    global stream_issues
    stream_issues = list(issues)


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


def start(cfg: Any, prof: cloud.Profile | None = None,
          where: Callable[[], tuple[int, int, int, int]] | None = None) -> asyncio.Task | None:
    """As the booth runs (not a replay), in the cloud profile it started in (`prof`, else
    cloud.startup): the upload loop, and beside it a venue bay's claim cards (cloud_cards; `where`
    gives GSPro's monitor, for where they're drawn). None otherwise, and when the API no longer
    accepts the pairing (the tray is told why: rounds are kept in the profile)."""
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
    return asyncio.create_task(_run(Uploads(cfg, prof), prof, where), name="cloud-upload")


async def _run(uploads: "Uploads", prof: cloud.Profile, where: Callable[[], tuple[int, int, int, int]] | None) -> None:
    """The upload loop, with the claim cards' loop for as long as it runs."""
    from . import cloud_cards

    cards = asyncio.create_task(
        cloud_cards.Cards(uploads.cfg.db_path, prof.rec["api"], uploads._cloud, tuple(uploads.identity[:3]),
                          where).run(), name="cloud-cards")
    try:
        await uploads.run()
    finally:
        cards.cancel()
        await asyncio.gather(cards, return_exceptions=True)


class Uploads:
    def __init__(self, cfg: Any, prof: cloud.Profile) -> None:
        self.cfg, self.bound, self.identity = cfg, prof.bound, cloud.identity(prof.rec)
        self.stopped = False   # the stored pairing isn't the booth's any more: nothing more goes out
        from . import cloud_rounds

        self.pace = cloud_rounds.Pace()   # what this run remembers between drains, and its clock
        # When every finished round was last on Shotcast Cloud, on that clock: counted from the
        # booth's start (a queue that was stuck before a restart shows again BEHIND_S after it).
        self.clear_at = self.pace.clock()

    def _cloud(self) -> cloud.Cloud:
        """This PC's pairing, if it's still the one the booth started with (else PairingChanged)."""
        cl = cloud.Cloud(cloud.store(), expect=self.bound)
        if cl.identity != self.identity:
            raise cloud.PairingChanged("this PC's Shotcast Cloud pairing changed; restart Shotcast to use it")
        return cl

    def _behind(self) -> list[tuple[str, str]]:
        """The one rule of upload health, by its outcome and whatever the cause (the network, a busy
        or failing lock or season file, an answer this PC doesn't understand, a round the API
        refuses): a failure once finished rounds have gone BEHIND_S without all being on Shotcast
        Cloud. A season file that can't even be read counts as behind."""
        from . import cloud_rounds

        now = self.pace.clock()
        try:
            n: int | None = cloud_rounds.behind(self.cfg.db_path)
        except Exception:   # noqa: BLE001 - not known to be clear
            log.debug("Couldn't read how uploads stand from %s", self.cfg.db_path, exc_info=True)
            n = None
        if n == 0:
            self.clear_at = now
            return []
        if now - self.clear_at <= BEHIND_S:
            return []
        return [("fail", f"Shotcast Cloud: {n if n else 'some'} round(s) haven't gone up for "
                         f"{int((now - self.clear_at) // 60)} minutes (see cloud status)")]

    def _health(self, *issues: tuple[str, str]) -> None:
        """The tray's view, rewritten after every try: `issues` from this try, and whether uploads
        are behind."""
        set_health(self.cfg.log_dir, list(issues) + self._behind() + stream_issues)

    async def run(self) -> None:
        from . import cloud_rounds

        while True:
            wait = DRAIN_S
            try:
                out = await asyncio.to_thread(cloud_rounds.drain, self._cloud(), self.cfg.db_path, self.pace,
                                              cloud_rounds.CEILING_S, 0)
                if out.sent:
                    log.info("Shotcast Cloud: %s round(s) sent", len(out.sent))
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
                self._health()   # (the command line is uploading; if it lasts, `_behind` shows it)
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001 - the network, a refresh refused, a busy file: tried again
                log.warning("Shotcast Cloud: rounds not sent: %s", e)
                self._health()
            await asyncio.sleep(wait)
