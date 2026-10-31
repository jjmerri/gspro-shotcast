"""Phone pushes for booth health issues, via ntfy (https://ntfy.sh): an HTTP POST to a topic, which the
ntfy phone app subscribes to. No account needed; the topic name is the only secret, so it lives in
.env (NTFY_TOPIC), never in config.yaml or git.

A health check often raises several issues at once (a GSPro update trips the version, the log format and
the tiles together), so issues are gathered for a few seconds and sent as one push. The same issue isn't
pushed again for `repeat_hours` unless its level or message changes, so a check that flaps between
rounds doesn't buzz the phone every time. Sends run on their own thread and never block the booth.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.request

from . import tls
from .config import NotifyConfig
from .health import Health, Issue

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: NotifyConfig, topic: str | None = None, send=None) -> None:
        self.cfg = cfg
        self.topic = topic if topic is not None else os.environ.get("NTFY_TOPIC", "").strip()
        self._send = send or self._post
        self._lock = threading.Lock()
        self._pending: dict[str, Issue | None] = {}
        self._timer: threading.Timer | None = None
        self._sent: dict[str, tuple[float, str, str]] = {}   # key -> (when, level, message) last pushed
        self._open: set[str] = set()   # pushed and not yet cleared: what a "resolved" push covers

    @property
    def enabled(self) -> bool:
        return self.cfg.provider == "ntfy" and bool(self.topic)

    def attach(self, health: Health) -> None:
        if self.cfg.provider == "ntfy" and not self.topic:
            log.info("No NTFY_TOPIC in .env: health issues show on the badge only, no phone pushes")
            return
        if self.enabled:
            health.add_listener(self.on_change)
            log.info("Health pushes go to ntfy topic %s…", self.topic[:4])

    # ---- gathering --------------------------------------------------------------------------

    def on_change(self, key: str, issue: Issue | None) -> None:
        if issue is not None and issue.level not in self.cfg.levels:
            return
        if issue is None and (not self.cfg.cleared or key not in self._open):
            return   # only say "resolved" for something the phone was told about
        with self._lock:
            self._pending[key] = issue
            if self._timer is None:
                self._timer = threading.Timer(self.cfg.gather_seconds, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        with self._lock:
            pending, self._pending, self._timer = self._pending, {}, None
        now = time.time()
        raised, cleared = [], []
        for key, issue in pending.items():
            if issue is None:
                if key in self._open:
                    cleared.append(key)
                    self._open.discard(key)
                continue
            # The record outlives a clear, so an issue that flaps (cleared one round, back the next)
            # buzzes once, not every time.
            last = self._sent.get(key)
            if last and (last[1], last[2]) == (issue.level, issue.message) and now - last[0] < self.cfg.repeat_hours * 3600:
                continue
            raised.append(issue)
            self._open.add(key)
            self._sent[key] = (now, issue.level, issue.message)
        if not raised and not cleared:
            return
        fail = any(i.level == "fail" for i in raised)
        if raised:
            title = f"Golf booth: {len(raised)} {'problem' if fail else 'warning'}{'s' if len(raised) > 1 else ''}"
            body = "\n".join(f"{'FAIL' if i.level == 'fail' else 'WARN'}: {i.message}" for i in raised)
            if cleared:
                body += f"\n(and {len(cleared)} resolved)"
        else:
            title = "Golf booth: resolved"
            body = f"{len(cleared)} issue{'s' if len(cleared) > 1 else ''} cleared."
        priority = "high" if fail else "default" if raised else "low"
        tags = "rotating_light" if fail else "warning" if raised else "white_check_mark"
        threading.Thread(target=self._safe_send, args=(title, body, priority, tags), daemon=True).start()

    def _safe_send(self, title: str, body: str, priority: str, tags: str) -> None:
        try:
            self._send(title, body, priority, tags)
            log.info("Health push sent: %s", title)
        except Exception as e:
            log.warning("Health push failed: %r", e)

    # ---- sending ----------------------------------------------------------------------------

    def _post(self, title: str, body: str, priority: str, tags: str) -> None:
        req = urllib.request.Request(
            f"{self.cfg.server.rstrip('/')}/{self.topic}", data=body.encode("utf-8"), method="POST",
            headers={"Title": title, "Priority": priority, "Tags": tags})
        with tls.urlopen(req, timeout=10) as resp:
            resp.read()

    def test(self) -> None:
        """A push now, bypassing the gathering (for `notify-test`)."""
        self._send("Golf booth: test", "Phone pushes are working. You'll hear from the booth when a "
                   "health check finds something off.", "default", "golf")
