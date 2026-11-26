"""The live chat on Shotcast Cloud, booth side: what members watching this bay's stream say on their
leagues' Live tabs (the cloud's docs/dev/chat.md), offered to the booth through the same calls it
makes of the site stack's chat table (chat.ChatTable), so engine/chat.py reads and answers it the
same way. The cloud keeps the chat open while the stream is live, so there's no on-air heartbeat.

Blocking (HTTP): call it off the event loop, as the table's.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from .chat import ChatMessage

log = logging.getLogger(__name__)

PAGE = 50          # the cloud's page of messages
MAX_PAGES = 10     # read this many at most at a time
KEEP = 500         # messages remembered (the booth looks back minutes, not hours)


def message_id(at: float, n: int) -> str:
    """A cloud message as the table's sort keys run ("<ms, 13 digits>-<hex>"), so the booth's
    cursors (chat.cursor_at) order it the same way."""
    return f"{int(at * 1000):013d}-{n:012x}"


def number_of(message_id_: str) -> int | None:
    try:
        return int(message_id_.split("-", 1)[1], 16)
    except (IndexError, ValueError):
        return None


class CloudChat:
    """The chat of the stream `stream` (cloud_stream.CloudStream) sends: its `stream_id`, and the
    paired cloud.Cloud (`stream.cloud_of()`) to ask it."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self._sid: str | None = None
        self._last = 0
        self._seen: list[ChatMessage] = []
        self._lock = threading.Lock()

    def set_on_air(self, on: bool, since: str | None = None) -> None:
        """Nothing: the cloud's chat is open while the stream is live."""

    def _read(self, sid: str) -> None:
        cloud = self.stream.cloud_of()
        for _ in range(MAX_PAGES):
            r = cloud.request("GET", f"/api/streams/{sid}/chat", params={"after": self._last})
            if r.status_code != 200:
                raise RuntimeError(f"Shotcast Cloud's chat answered {r.status_code}")
            rows = (r.json() or {}).get("messages") or []
            for row in rows:
                try:
                    n, at = int(row["id"]), datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00")).timestamp()
                    self._seen.append(ChatMessage(message_id(at, n), str(row["name"]), str(row["text"]), at))
                    self._last = max(self._last, n)
                except (KeyError, TypeError, ValueError):
                    log.debug("Chat: skipped a malformed cloud message %r", row)
            if len(rows) < PAGE:
                break
        del self._seen[:-KEEP]

    def since(self, after: str, limit: int = 50) -> list[ChatMessage]:
        """What's been said about the stream after the cursor `after`, oldest first; the newest
        `limit` of them if more. None before the stream's started."""
        sid = self.stream.stream_id
        if sid is None:
            return []
        with self._lock:
            if sid != self._sid:   # (a new stream: its own chat)
                self._sid, self._last, self._seen = sid, 0, []
            self._read(sid)
            return [m for m in self._seen if m.id > after][-limit:]

    def mark_aired(self, message_id_: str) -> None:
        """The booth said it on air: the Live tab shows it so."""
        sid, n = self.stream.stream_id, number_of(message_id_)
        if sid is None or n is None:
            return
        r = self.stream.cloud_of().request("POST", f"/api/streams/{sid}/chat/aired", body={"ids": [n]})
        if r.status_code != 204:
            raise RuntimeError(f"Shotcast Cloud didn't take the aired mark: {r.status_code}")
