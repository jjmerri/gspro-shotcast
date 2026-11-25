"""The live stream to Shotcast Cloud (shotcast-cloud's docs/dev/video-stream.md, step 2a): paired, the
booth sends OBS's HLS segments to the cloud as OBS finishes them, and the cloud writes the playlists
and tells the league's Live tab. The self-hosted booth's bucket mirror (live_stream.Mirror) is never
used paired, and this is never used unpaired.

OBS writes a playlist and a few seconds of video per file into live.dir, as for the mirror. Each pass
(`step`) reads the playlist the way the mirror does (a new stream when it's fresh and none is on, off
when it stops changing or ends, a new one when OBS starts its numbering again) and, for the cloud:

    POST /api/streams {session}               when a stream is first seen (again with the same session
                                              to resume it after a failed answer): its id
    POST /api/streams/{id}/segments           a finished segment's size, SHA-256 and seconds (from its
                                              #EXTINF), up to MAX_PER_ASK at a time: an upload link each
    PUT <link>                                exactly those bytes, with the link's headers
    POST /api/streams/{id}/stored {seqs}      those are up ({"lost": [...]} for one given up)
    POST /api/streams/{id}/end                when it stops; asked again till it's answered

A segment is finished once a later one is in the playlist (or the stream has stopped). Its number is
the playlist's media sequence plus its place in it, so the cloud has them in OBS's order. One that
can't be sent (gone from the folder, too long or too big, or failing for GIVE_UP_S) is given up as
lost rather than held up behind; the cloud skips it.

The cloud's refusals: `not_in_plan` (the account has no streaming) and `league_suspended` stop
asking for a while (OFF_S) without a word in the tray, `allowance_used` stops this stream (the month's
streaming is used up) and says so in the tray (cloud_booth.stream_issues). Anything else, the network
included, is tried again on the next pass.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .live_stream import parse_playlist, segment_durations

log = logging.getLogger(__name__)

MAX_PER_ASK = 8              # segments a /segments call may ask links for (the API's MAX_PER_ASK)
MAX_SEGMENT_BYTES = 4 * 1024 * 1024
MAX_SEGMENT_SECONDS = 10.0
DEFAULT_SECONDS = 4.0        # a segment whose #EXTINF can't be read
GIVE_UP_S = 45.0             # a segment that still hasn't gone up this long after it was first tried: lost
OFF_S = 600.0                # streaming refused for the account (not in its plan, its leagues suspended): asked again after this
END_TRIES_S = 600.0          # how long an unanswered /end is asked again


def _error(r: Any) -> tuple[str | None, dict]:
    """The API's error code and its whole answer (a device error is flat JSON), else (None, {})."""
    try:
        got = r.json()
    except ValueError:
        return None, {}
    if not isinstance(got, dict):
        return None, {}
    err = got.get("error")
    return (err if isinstance(err, str) else None), got


def _put(url: str, headers: dict[str, str], body: bytes) -> int:
    """The upload itself, straight to the link (storage, not the API)."""
    import httpx

    r = httpx.put(url, content=body, headers=headers, timeout=60.0)
    return r.status_code


class CloudStream:
    """Sends OBS's stream folder to Shotcast Cloud while OBS writes it. step() does one pass; start()
    runs it every second on its own thread. `cloud_of` gives the paired cloud.Cloud (it raises once
    the pairing isn't the booth's). Offers the mirror's methods the booth calls on every stream copy
    (mark, shot, league, week, watched_at, on_page): the cloud keeps none of that yet."""

    def __init__(self, folder: str | Path, cloud_of: Callable[[], Any], playlist: str = "stream.m3u8",
                 stale_seconds: float = 20.0, clock: Callable[[], float] = time.time,
                 put: Callable[[str, dict[str, str], bytes], int] = _put,
                 issues: Callable[[list[tuple[str, str]]], None] | None = None) -> None:
        self.folder, self.playlist, self.cloud_of = Path(folder), playlist, cloud_of
        self.stale, self.clock, self.put = stale_seconds, clock, put
        self.issues = issues or (lambda found: None)
        self.peers: list[Any] = []   # others reading the same folder: it's emptied once they're done too
        self.session: str | None = None   # the stream as this PC names it (live_stream._session_name)
        self.since = 0.0
        self.stream_id: str | None = None   # the cloud's id for it, once started
        self.seq = -1                # the playlist's media sequence last read
        self.seen_mtime = 0.0
        self.newest_ns = 0
        self.stamps: dict[str, int] = {}   # this stream's segments seen: file name -> its mtime (ns)
        self.queued: dict[int, dict] = {}  # finished, not yet up: seq -> {name, seconds, first}
        self.done: set[int] = set()        # up or given up
        self.lost: set[int] = set()        # given up, the cloud not told yet
        self.stored: set[int] = set()      # put to their links, the cloud not told yet
        self.high = -1                     # the highest seq queued so far
        self.halted = False                # this stream won't be sent (allowance used up)
        self.off_until = 0.0               # streaming refused: no new stream before this
        self.ending: list[tuple[str, float]] = []   # streams to end: (id, first asked), asked again till answered
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the mirror's interface (live_stream.Mirror), for the booth's calls on every copy
    def mark(self, label: str, replace: bool = False) -> None:
        pass

    def shot(self) -> None:
        pass

    def league(self, deadline: str | None) -> None:
        pass

    def week(self, body: dict | None) -> None:
        pass

    def watched_at(self, session: str | None, segment: str | None, offset: float | None) -> float | None:
        return None

    def on_page(self) -> bool:
        return False

    # -- the API
    def _post(self, path: str, body: dict | None = None) -> Any:
        return self.cloud_of().request("POST", path, body=body if body is not None else {})

    def _start(self) -> bool:
        """The cloud's stream for this session (the same session again resumes it). False when it
        isn't (yet): refused, or no answer."""
        from .cloud import CloudError

        try:
            r = self._post("/api/streams", {"session": self.session})
        except CloudError as e:
            log.info("Live stream to Shotcast Cloud: not started (%s); trying again", e)
            return False
        err, got = _error(r)
        if r.status_code == 200 and isinstance(got.get("id"), str):
            self.stream_id = got["id"]
            log.info("Live stream on: sending %s to Shotcast Cloud (stream %s)", self.folder, self.stream_id)
            return True
        if r.status_code == 403 and err in ("not_in_plan", "league_suspended"):
            if self.off_until <= 0:
                log.info("Live stream: Shotcast Cloud isn't taking this PC's stream (%s); asked again later",
                         got.get("message") or err)
            self.off_until = self.clock() + OFF_S
            self.halted = True   # (this stream: the next one asks again)
            return False
        log.info("Live stream to Shotcast Cloud: not started (HTTP %s %s); trying again", r.status_code, err or "")
        return False

    def _allowance_used(self, got: dict) -> None:
        log.warning("Live stream: this month's streaming on Shotcast Cloud is used up; the stream stopped (%s)",
                    got.get("message") or "allowance_used")
        self.issues([("warn", "Shotcast Cloud: this month's streaming is used up; the live stream is off")])
        self.halted = True
        self.queued.clear()

    def _send(self) -> None:
        """Up to MAX_PER_ASK finished segments, oldest first: links, the bytes, then /stored."""
        from .cloud import CloudError

        now = self.clock()
        for seq, q in list(self.queued.items()):   # waited too long: given up (the cloud skips it)
            if now - q["first"] > GIVE_UP_S:
                log.info("Live stream: segment %s not sent in %.0fs; given up", seq, GIVE_UP_S)
                self._give_up(seq)
        batch, items = [], []
        for seq in sorted(self.queued)[:MAX_PER_ASK]:
            q = self.queued[seq]
            try:
                data = (self.folder / q["name"]).read_bytes()
            except OSError:
                self._give_up(seq)   # OBS deleted it already
                continue
            if not data or len(data) > MAX_SEGMENT_BYTES or not 0.1 <= q["seconds"] <= MAX_SEGMENT_SECONDS:
                log.info("Live stream: segment %s can't go to Shotcast Cloud (%s bytes, %ss); given up",
                         seq, len(data), q["seconds"])
                self._give_up(seq)
                continue
            batch.append(data)
            items.append({"seq": seq, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "seconds": round(q["seconds"], 3)})
        if items:
            try:
                r = self._post(f"/api/streams/{self.stream_id}/segments", {"segments": items})
            except CloudError as e:
                log.debug("Live stream: no upload links (%s)", e)
                return
            err, got = _error(r)
            if r.status_code == 403 and err == "allowance_used":
                self._allowance_used(got)
                return
            if r.status_code == 409 and err in ("segment_passed", "segment_lost", "segment_taken"):
                # The cloud has moved past them (`seqs`), or can't take this one (named in its message:
                # its time to arrive passed, or it already has other bytes or this one up).
                named = re.search(r"segment (\d+)", str(got.get("message") or ""))
                gone = [s for s in got.get("seqs") or [] if isinstance(s, int)] or (
                    [int(named.group(1))] if named else [items[0]["seq"]])
                for s in gone:
                    self.queued.pop(s, None)
                    self.done.add(s)
                return
            if r.status_code != 200 or not isinstance(got.get("links"), list):
                log.debug("Live stream: no upload links (HTTP %s %s)", r.status_code, err or "")
                return
            by_seq = dict(zip((i["seq"] for i in items), batch))
            for link in got["links"]:
                seq = link.get("seq") if isinstance(link, dict) else None
                if seq not in by_seq or not isinstance(link.get("url"), str):
                    continue
                try:
                    code = self.put(link["url"], dict(link.get("headers") or {}), by_seq[seq])
                except Exception as e:   # noqa: BLE001 - the network: the next pass asks again
                    log.debug("Live stream: segment %s not uploaded (%s)", seq, e)
                    continue
                if 200 <= code < 300:
                    self.stored.add(seq)
                    self.queued.pop(seq, None)
        self._tell()

    def _give_up(self, seq: int) -> None:
        self.queued.pop(seq, None)
        self.lost.add(seq)

    def _tell(self) -> None:
        """Say which segments are up (and which are given up)."""
        from .cloud import CloudError

        if not (self.stored or self.lost) or self.stream_id is None:
            return
        seqs, lost = sorted(self.stored)[:32], sorted(self.lost - self.stored)[:32]
        try:
            r = self._post(f"/api/streams/{self.stream_id}/stored", {"seqs": seqs, "lost": lost})
        except CloudError as e:
            log.debug("Live stream: segments not confirmed (%s)", e)
            return
        err, got = _error(r)
        if r.status_code == 200 or (r.status_code == 409 and err == "not_stored"):
            again = {s for s in got.get("seqs") or [] if isinstance(s, int)} if err == "not_stored" else set()
            for s in seqs:
                if s not in again:
                    self.stored.discard(s)
                    self.done.add(s)
            for s in lost:
                self.lost.discard(s)
                self.done.add(s)
        elif r.status_code == 403 and err == "allowance_used":
            self._allowance_used(got)
        else:
            log.debug("Live stream: segments not confirmed (HTTP %s %s)", r.status_code, err or "")

    def _end_streams(self) -> None:
        """Ask the cloud to end each stream that's stopped, till it answers."""
        from .cloud import CloudError

        left = []
        for sid, first in self.ending:
            try:
                r = self._post(f"/api/streams/{sid}/end")
            except CloudError as e:
                log.debug("Live stream: stream %s not ended yet (%s)", sid, e)
                r = None
            err = _error(r)[0] if r is not None else None
            if r is not None and (r.status_code == 200 or r.status_code in (404, 409) or err == "device_not_paired"):
                log.info("Live stream off (Shotcast Cloud stream %s)", sid)
                continue   # (ended; or ended, gone or not this PC's already: nothing more to ask)
            if self.clock() - first < END_TRIES_S:
                left.append((sid, first))
            else:
                log.warning("Live stream: Shotcast Cloud didn't answer ending stream %s; left to it", sid)
        self.ending = left

    # -- one pass
    def step(self) -> None:
        if self.ending:
            self._end_streams()
        pl = self.folder / self.playlist
        try:
            st = pl.stat()
            mtime, mtime_ns = st.st_mtime, st.st_mtime_ns
        except FileNotFoundError:
            mtime = mtime_ns = None
        now = self.clock()
        if self.session and (mtime is None or now - mtime > self.stale):
            self._end()   # OBS stopped without saying so (or crashed)
            return
        if mtime is None or (mtime == self.seen_mtime and not self.queued and not self.stored and not self.lost):
            return
        if self.session is None and now - mtime > self.stale:
            return   # left over from an earlier stream
        if self.session is None and now < self.off_until:
            return   # streaming refused a moment ago: not asked again yet
        if mtime != self.seen_mtime:
            try:
                raw = pl.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return
            parsed = parse_playlist(raw)
            if parsed is None:
                return   # mid-write: the next pass gets it whole
            seq, segments, ended = parsed
            try:
                stamps = {name: (self.folder / name).stat().st_mtime_ns for name in segments}
            except OSError:
                return   # a segment went before we got to it: wait for the next playlist
            # OBS restarted between passes: its numbering starts again, so it's another stream.
            if self.session and (seq < self.seq or any(n in self.stamps and self.stamps[n] != t
                                                       for n, t in stamps.items())):
                self._end(clean=False)
            if self.session is None:
                from .live_stream import _session_name

                self.session, self.since = _session_name(now), now
                self.stamps, self.queued, self.done, self.lost, self.stored = {}, {}, set(), set(), set()
                self.high, self.halted, self.stream_id = -1, False, None
            self.newest_ns = max(self.newest_ns, mtime_ns, *stamps.values())
            self.stamps.update(stamps)
            durations = segment_durations(raw)
            finished = segments if ended else segments[:-1]
            for i, name in enumerate(finished):
                n = seq + i
                if n > self.high:
                    self.queued[n] = {"name": name, "seconds": durations.get(name, DEFAULT_SECONDS), "first": now}
                    self.high = n
            self.seq, self.seen_mtime = seq, mtime
        else:
            ended = False
        if not self.halted and self.stream_id is None:
            if not self._start():
                return
        if not self.halted:
            self._send()
        if ended:
            self._end()

    def _end(self, clean: bool = True) -> None:
        """The stream's stopped: what's left of it goes (its last segment is finished now), then the
        cloud is told; OBS's folder is emptied once every copy reading it is done."""
        if self.session is None:
            return
        try:
            raw = (self.folder / self.playlist).read_text(encoding="utf-8", errors="replace")
            parsed = parse_playlist(raw)
        except OSError:
            parsed = None
        if parsed is not None and not self.halted and self.stream_id is not None:
            seq, segments, _ = parsed
            durations = segment_durations(raw)
            now = self.clock()
            for i, name in enumerate(segments):
                n = seq + i
                if n > self.high and self.stamps.get(name) == self._stamp(name):
                    self.queued[n] = {"name": name, "seconds": durations.get(name, DEFAULT_SECONDS), "first": now}
                    self.high = n
            for _ in range(4):   # (a few passes: each sends up to MAX_PER_ASK)
                if not self.queued:
                    break
                self._send()
            for seq_ in list(self.queued):
                self._give_up(seq_)
            self._tell()
        if self.stream_id is not None:
            self.ending.append((self.stream_id, self.clock()))
            self._end_streams()
        newest, self.newest_ns = self.newest_ns, 0
        self.session, self.stream_id, self.seq, self.seen_mtime = None, None, -1, 0.0
        self.queued, self.stored, self.lost, self.stamps = {}, set(), set(), {}
        if clean and all(p.session is None for p in self.peers):
            for p in self.folder.glob("*"):
                if p.suffix in (".ts", ".m4s", ".m3u8", ".mp4", ".tmp"):
                    try:
                        if p.stat().st_mtime_ns <= newest:
                            p.unlink()
                    except OSError:
                        pass   # OBS still holds it: the next stream overwrites it

    def _stamp(self, name: str) -> int | None:
        try:
            return (self.folder / name).stat().st_mtime_ns
        except OSError:
            return None

    # -- the thread
    def start(self, interval: float = 1.0) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self.run, args=(interval,), name="live-cloud", daemon=True)
        self._thread.start()

    def run(self, interval: float = 1.0) -> None:
        from .cloud import NotPaired, PairingChanged

        while not self._stop.wait(interval):
            try:
                self.step()
            except (NotPaired, PairingChanged) as e:
                log.warning("Live stream: nothing more goes to Shotcast Cloud till Shotcast restarts: %s", e)
                return
            except Exception:
                log.exception("Live stream to Shotcast Cloud failed (trying again)")
                self._stop.wait(5)
        try:   # here, not in close(): only this thread touches the stream
            if self.session:
                self._end(clean=False)
            elif self.ending:
                self._end_streams()
        except Exception:
            log.exception("Live stream: going off air on Shotcast Cloud failed")

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
