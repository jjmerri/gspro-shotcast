"""The live stream to Shotcast Cloud (shotcast-cloud's docs/dev/video-stream.md, step 2a): paired, the
booth sends OBS's HLS segments to the cloud as OBS finishes them, and the cloud writes the playlists
and tells the league's Live tab. The self-hosted booth's bucket mirror (live_stream.Mirror) is never
used paired, and this is never used unpaired.

OBS writes a playlist and a few seconds of video per file into live.dir, as for the mirror. Each pass
(`step`) reads the playlist the way the mirror does (a new stream when it's fresh and none is on, off
when it stops changing or ends, a new one when OBS starts its numbering again) and, for the cloud:

    POST /api/streams {session}               when a stream is first seen (again with the same session
                                              to resume it after a failed answer): its id
    POST /api/streams/{id}/segments           a finished segment's size, SHA-256, seconds (from its
                                              #EXTINF) and recorded_at (its file's time less its
                                              seconds), up to MAX_PER_ASK at a time: an upload link each
    PUT <link>                                exactly those bytes, with the link's headers
    POST /api/streams/{id}/stored {seqs}      those are up ({"lost": [...]} for one given up)
    POST /api/streams/{id}/marks {marks}      each shot played: "shot" at its impact, "rest" when its
                                              ball came to rest (played()), by the PC's clock as
                                              recorded_at is, so a screen cycling between bays
                                              switches at a shot's end
    POST /api/streams/{id}/end                when it stops; asked again till it's answered

Every segment in the playlist is finished: OBS's HLS muxer lists one only once it's written (its
temp_file flag), as the bucket mirror relies on too. Its number is the playlist's media sequence plus
its place in it, so the cloud has them in OBS's order. One that can't be sent (gone from the folder,
too long or too big, or failing for GIVE_UP_S) is given up as lost rather than held up behind; the
cloud skips it. A PUT answered 412 (the link's If-None-Match: the bytes went up, its answer lost) is
up: the cloud checks it at /stored, and one it keeps saying isn't there is given up after GIVE_UP_S.

The cloud's refusals: `not_in_plan` (the account has no streaming) and `league_suspended`, at the
start or mid-stream, end this stream on the cloud and stop sending without a word in the tray; the
cloud is asked again OFF_S later (a fresh stream from then on, if OBS is still on). `allowance_used`
ends it for the rest of OBS's session (the month's streaming is used up) and says so in the tray
(cloud_booth.stream_issues), cleared once a stream starts again. The network, or any other answer,
is tried again after a wait that grows to MAX_WAIT_S (said once in the log, then only in its debug
lines); so is storage failing a PUT, the wait reset only once a batch has gone up. Losing the pairing, or an app too old for the API, stops the thread till Shotcast restarts;
the cloud ends a stream that's stopped hearing from its PC by itself (after minutes), and one it's
ended (`stream_ended`, or `no_stream`) is followed by a fresh one.

When it stops, what's been put but not confirmed is confirmed (for up to GIVE_UP_S, through the
network or the cloud's own trouble) before /end, as is what was put before a refusal stopped it; a
new stream isn't started meanwhile (the cloud ends the bay's other stream on a start, and with it any
confirmations still to come). Once OBS's playlist ends, what's left is sent (for up to GIVE_UP_S,
waiting as above), then the stream ends. A
segment whose file isn't the one seen (OBS started again and reused its name) is given up, never sent
as this stream's. The booth's live.json says on air while OBS's stream is (refused or not, started
on the cloud or not), and off once it stops or this thread does (however it stops), for the tray
and its updater. Only
the files this stream read, and the playlist it last read, are deleted from OBS's folder afterwards.

Left as they are: a booth restarted mid-stream starts another stream, so the few segments still in
OBS's playlist (about 20 s) go up, and are charged, again; a stream whose /end keeps failing holds the
next start behind the same wait; segments listed in the moment between the last pass and the booth
closing aren't sent.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import json
from collections.abc import Callable
from datetime import UTC, datetime
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
MAX_WAIT_S = 60.0            # the longest wait between tries after the network or an unknown answer
REFUSED = ("not_in_plan", "league_suspended")
MAX_MARKS_PER_CALL = 16      # the API's
MARK_KEEP_S = 120.0          # a mark not sent this long after its moment is dropped (no stream took it)
# The API takes recording times and marks between 2000 and 2100: a PC clock outside that sends none.
EARLIEST, LATEST = 946684800.0, 4102444800.0


def _when(t: float) -> str | None:
    """A time by this PC's clock (seconds since the epoch) as the API takes it, or None out of range."""
    if not EARLIEST <= t < LATEST:
        return None
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds")


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
                 issues: Callable[[list[tuple[str, str]]], None] | None = None,
                 status_file: str | Path | None = None) -> None:
        self.folder, self.playlist, self.cloud_of = Path(folder), playlist, cloud_of
        self.status_file = Path(status_file) if status_file else None
        self.stale, self.clock, self.put = stale_seconds, clock, put
        self.issues = issues or (lambda found: None)
        self.peers: list[Any] = []   # others reading the same folder: it's emptied once they're done too
        self.session: str | None = None   # the stream as this PC names it (live_stream._session_name)
        self.since = 0.0
        self.stream_id: str | None = None   # the cloud's id for it, once started
        self.seq = -1                # the playlist's media sequence last read
        self.seen_mtime = 0.0
        self.playlist_ns = 0               # the playlist's mtime (ns) as last read
        self.stamps: dict[str, int] = {}   # this stream's segments seen: file name -> its mtime (ns)
        self.queued: dict[int, dict] = {}  # finished, not yet up: seq -> {name, seconds, first}
        self.done: set[int] = set()        # up or given up
        self.lost: set[int] = set()        # given up, the cloud not told yet
        self.stored: dict[int, float] = {}  # put to their links, the cloud not told yet: seq -> when first put
        self.high = -1                     # the highest seq seen so far
        self.over = 0.0                    # OBS's playlist ended (when): what's left goes, then it ends
        self.halted: str | None = None     # this stream isn't sent: "refused" (asked again at off_until) or "allowance"
        self.off_until = 0.0               # streaming refused: no stream asked for before this
        # Streams to end: {id, first, stored, lost}, their confirmations said, then /end, till answered.
        self.ending: list[dict] = []
        self.wait_until = 0.0              # the network or an unknown answer: nothing asked before this
        self._wait = 0.0
        self._noisy = False
        self._marks: list[tuple[str, float]] = []   # (kind, when) played, not yet sent
        self._marks_lock = threading.Lock()
        self.marks_off: str | None = None   # the stream the cloud takes no more marks on
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the mirror's interface (live_stream.Mirror), for the booth's calls on every copy
    def mark(self, label: str, replace: bool = False) -> None:
        pass

    def shot(self) -> None:
        pass

    def played(self, hit: float | None, rest: float | None) -> None:
        """A shot played: when it was hit and when its ball came to rest (None: not known), by this
        PC's clock. Sent as the stream's marks on the next pass. Called from any thread."""
        with self._marks_lock:
            self._marks += [(kind, at) for kind, at in (("shot", hit), ("rest", rest)) if at is not None]
            del self._marks[:-4 * MAX_MARKS_PER_CALL]

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

    def _trouble(self, what: str) -> None:
        """No answer, or one not understood: tried again after a wait, longer each time. Said once."""
        (log.debug if self._noisy else log.info)("Live stream to Shotcast Cloud: %s; trying again", what)
        self._noisy = True
        self._wait = min(max(self._wait * 2, 2.0), MAX_WAIT_S)
        self.wait_until = self.clock() + self._wait

    def _fine(self) -> None:
        self._noisy, self._wait, self.wait_until = False, 0.0, 0.0

    def _status(self, live: bool) -> None:
        """live.json, for the tray (on air or not), as the bucket mirror writes it."""
        if self.status_file is None:
            return
        body = {"live": True, "session": self.session, "since": self.since, "at": round(self.clock())} \
            if live else {"live": False}
        try:
            self.status_file.write_text(json.dumps(body), encoding="utf-8")
        except OSError:
            pass

    def _start(self) -> bool:
        """The cloud's stream for this session (the same session again resumes it). False when it
        isn't (yet): refused, or no answer."""
        from .cloud import CloudError, NotPaired, PairingChanged, TooOld

        try:
            r = self._post("/api/streams", {"session": self.session})
        except (NotPaired, PairingChanged, TooOld):
            raise
        except CloudError as e:
            self._trouble(f"not started ({e})")
            return False
        err, got = _error(r)
        if r.status_code == 200 and isinstance(got.get("id"), str):
            self.stream_id = got["id"]
            self._fine()
            self.issues([])
            log.info("Live stream on: sending %s to Shotcast Cloud (stream %s)", self.folder, self.stream_id)
            return True
        if r.status_code == 403 and err in REFUSED:
            self._refused(got, err)
            return False
        if self._ended_by_cloud(err):   # (resumed, but the cloud ended it meanwhile: a fresh one next)
            return False
        self._trouble(f"not started (HTTP {r.status_code} {err or ''})")
        return False

    def _stop_stream(self, why: str) -> None:
        """This stream isn't sent any more: ended on the cloud (so its Live tab lets it go), and
        nothing more queued for it."""
        self.halted = why
        self.queued.clear()
        if self.stream_id is not None:   # (what was put before it is still confirmed, then /end)
            self.ending.append({"id": self.stream_id, "first": self.clock(), "stored": dict(self.stored),
                                "lost": set(self.lost)})
            self.stream_id = None
            self.stored, self.lost = {}, set()
            self._end_streams()

    def _refused(self, got: dict, err: str | None) -> None:
        if self.off_until <= 0:
            log.info("Live stream: Shotcast Cloud isn't taking this PC's stream (%s); asked again later",
                     got.get("message") or err)
        self.off_until = self.clock() + OFF_S
        self._stop_stream("refused")

    def _allowance_used(self, got: dict) -> None:
        log.warning("Live stream: this month's streaming on Shotcast Cloud is used up; the stream stopped (%s)",
                    got.get("message") or "allowance_used")
        self.issues([("warn", "Shotcast Cloud: this month's streaming is used up; the live stream is off")])
        self._stop_stream("allowance")

    def _ended_by_cloud(self, err: str | None) -> bool:
        """The cloud has ended this stream (it heard nothing from this PC for minutes: the network was
        down) or doesn't know it: True, and a fresh stream is asked for, from what's still queued."""
        if err not in ("stream_ended", "no_stream"):
            return False
        from .live_stream import _session_name

        log.info("Live stream: Shotcast Cloud ended stream %s (%s); starting another", self.stream_id, err)
        self.session, self.stream_id = _session_name(self.clock()), None
        self.stored, self.lost = {}, set()
        return True

    def _send(self) -> None:
        """Up to MAX_PER_ASK finished segments, oldest first: links, the bytes, then /stored."""
        from .cloud import CloudError, NotPaired, PairingChanged, TooOld

        now = self.clock()
        for seq, q in list(self.queued.items()):   # waited too long: given up (the cloud skips it)
            if now - q["first"] > GIVE_UP_S:
                log.info("Live stream: segment %s not sent in %.0fs; given up", seq, GIVE_UP_S)
                self._give_up(seq)
        batch, items, failed = [], [], None
        for seq in sorted(self.queued)[:MAX_PER_ASK]:
            q = self.queued[seq]
            if self._stamp(q["name"]) != q["ns"]:
                self._give_up(seq)   # OBS deleted it already, or started again and reused its name
                continue
            try:
                data = (self.folder / q["name"]).read_bytes()
            except OSError:
                self._give_up(seq)
                continue
            if self._stamp(q["name"]) != q["ns"]:   # (rewritten while it was read)
                self._give_up(seq)
                continue
            if not data or len(data) > MAX_SEGMENT_BYTES or not 0.1 <= q["seconds"] <= MAX_SEGMENT_SECONDS:
                log.info("Live stream: segment %s can't go to Shotcast Cloud (%s bytes, %ss); given up",
                         seq, len(data), q["seconds"])
                self._give_up(seq)
                continue
            batch.append(data)
            item = {"seq": seq, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "seconds": round(q["seconds"], 3)}
            # (OBS writes a segment as it records it: its file's time is its end)
            if (recorded := _when(q["ns"] / 1e9 - q["seconds"])) is not None:
                item["recorded_at"] = recorded
            items.append(item)
        if items:
            try:
                r = self._post(f"/api/streams/{self.stream_id}/segments", {"segments": items})
            except (NotPaired, PairingChanged, TooOld):
                raise
            except CloudError as e:
                self._trouble(f"no upload links ({e})")
                return
            err, got = _error(r)
            if r.status_code == 403 and err == "allowance_used":
                self._allowance_used(got)
                return
            if r.status_code == 403 and err in REFUSED:
                self._refused(got, err)
                return
            if self._ended_by_cloud(err):
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
                self._trouble(f"no upload links (HTTP {r.status_code} {err or ''})")
                return
            self.issues([])
            by_seq = dict(zip((i["seq"] for i in items), batch))
            for link in got["links"]:
                seq = link.get("seq") if isinstance(link, dict) else None
                if seq not in by_seq or not isinstance(link.get("url"), str):
                    continue
                try:
                    code = self.put(link["url"], dict(link.get("headers") or {}), by_seq[seq])
                except Exception as e:   # noqa: BLE001 - the network: asked again after a wait
                    failed = str(e)
                    continue
                # 412: the link's If-None-Match found the object there, its first PUT's answer lost.
                if 200 <= code < 300 or code == 412:
                    self.stored.setdefault(seq, self.clock())
                    self.queued.pop(seq, None)
                else:
                    failed = f"HTTP {code}"
        if failed is None:
            if items:
                self._fine()   # (a batch up: the wait starts again from nothing)
            self._tell()
        else:   # storage failing: the wait grows, not asked for fresh links every second
            self._tell(fine=False)
            self._trouble(f"segments not uploaded ({failed})")

    def _send_marks(self) -> None:
        """The shots played since the last pass, as this stream's marks. Unanswered, they're tried
        again on the next pass, for up to MARK_KEEP_S; refused (a cloud without marks, too many on the
        stream, the stream gone), dropped: segments matter, marks are a nicety, never a trouble."""
        from .cloud import CloudError, NotPaired, PairingChanged, TooOld

        sid, now = self.stream_id, self.clock()
        with self._marks_lock:
            self._marks = [m for m in self._marks if now - m[1] < MARK_KEEP_S]
            batch = self._marks[:MAX_MARKS_PER_CALL]
            if sid == self.marks_off:
                self._marks, batch = [], []
        marks = [{"kind": kind, "at": at} for kind, t in batch if (at := _when(t)) is not None]
        if marks:
            try:
                r = self._post(f"/api/streams/{sid}/marks", {"marks": marks})
            except (NotPaired, PairingChanged, TooOld):
                raise
            except CloudError as e:
                log.debug("Live stream: marks not sent (%s); tried again", e)
                return
            if r.status_code >= 500 or r.status_code in (408, 429):
                return
            if r.status_code != 200:
                err, _ = _error(r)
                log.info("Live stream: Shotcast Cloud took no marks (HTTP %s %s); none more on this stream",
                         r.status_code, err or "")
                self.marks_off = sid
        with self._marks_lock:
            self._marks = [m for m in self._marks if m not in batch]

    def _give_up(self, seq: int) -> None:
        self.queued.pop(seq, None)
        self.lost.add(seq)

    def _confirm(self, sid: str, stored: dict[int, float], lost: set[int]) -> tuple[int, str | None, dict] | None:
        """POST /stored for those, taking the confirmed ones out of `stored` and `lost`: None when
        answered, else (status, error, answer). A segment the cloud keeps saying isn't there is given
        up once it's waited GIVE_UP_S. Raises the client's errors."""
        seqs, gone = sorted(stored)[:32], sorted(lost - set(stored))[:32]
        r = self._post(f"/api/streams/{sid}/stored", {"seqs": seqs, "lost": gone})
        err, got = _error(r)
        if not (r.status_code == 200 or (r.status_code == 409 and err == "not_stored")):
            return r.status_code, err, got
        again = {s for s in got.get("seqs") or [] if isinstance(s, int)} if err == "not_stored" else set()
        now = self.clock()
        for s in seqs:
            if s not in again:
                stored.pop(s, None)
                self.done.add(s)
            elif now - stored[s] > GIVE_UP_S:
                log.info("Live stream: Shotcast Cloud still hasn't segment %s; given up", s)
                stored.pop(s, None)
                lost.add(s)
        for s in gone:
            lost.discard(s)
            self.done.add(s)
        return None

    def _tell(self, fine: bool = True) -> None:
        """Say which segments are up (and which are given up). `fine`: an answer resets the wait
        (not when storage has just failed: that wait goes on growing)."""
        from .cloud import CloudError, NotPaired, PairingChanged, TooOld

        if not (self.stored or self.lost) or self.stream_id is None:
            return
        try:
            res = self._confirm(self.stream_id, self.stored, self.lost)
        except (NotPaired, PairingChanged, TooOld):
            raise
        except CloudError as e:
            self._trouble(f"segments not confirmed ({e})")
            return
        if res is None:
            if fine:
                self._fine()
            return
        status, err, got = res
        if status == 403 and err == "allowance_used":
            self._allowance_used(got)
        elif status == 403 and err in REFUSED:
            self._refused(got, err)
        elif not self._ended_by_cloud(err):
            self._trouble(f"segments not confirmed (HTTP {status} {err or ''})")

    def _end_streams(self) -> None:
        """For each stream that's stopped: what was put but not confirmed is confirmed (for up to
        GIVE_UP_S), then the cloud's asked to end it, till it answers."""
        from .cloud import CloudError, NotPaired, PairingChanged, TooOld

        if self.clock() < self.wait_until:
            return
        left = []
        for e in self.ending:
            now = self.clock()
            if (e["stored"] or e["lost"]) and now - e["first"] < GIVE_UP_S:
                try:
                    res = self._confirm(e["id"], e["stored"], e["lost"])
                except (NotPaired, PairingChanged, TooOld):
                    raise
                except CloudError as err:
                    self._trouble(f"stream {e['id']}'s last segments not confirmed ({err})")
                    left.append(e)
                    continue
                if res is not None:
                    status = res[0]
                    if status >= 500 or status in (408, 429):   # the cloud's own trouble: asked again
                        self._trouble(f"stream {e['id']}'s last segments not confirmed (HTTP {status})")
                        left.append(e)
                        continue
                    e["stored"], e["lost"] = {}, set()   # (refused: ended without them)
                if e["stored"] or e["lost"]:
                    left.append(e)
                    continue
            try:
                r = self._post(f"/api/streams/{e['id']}/end")
            except (NotPaired, PairingChanged, TooOld):
                raise
            except CloudError as err:
                self._trouble(f"stream {e['id']} not ended yet ({err})")
                r = None
            code = _error(r)[0] if r is not None else None
            if r is not None and (r.status_code == 200 or r.status_code in (404, 409) or code == "device_not_paired"):
                log.info("Live stream off (Shotcast Cloud stream %s)", e["id"])
                continue   # (ended; or ended, gone or not this PC's already: nothing more to ask)
            if r is not None:
                self._trouble(f"stream {e['id']} not ended yet (HTTP {r.status_code} {code or ''})")
            if now - e["first"] < END_TRIES_S:
                left.append(e)
            else:
                log.warning("Live stream: Shotcast Cloud didn't answer ending stream %s; left to it", e["id"])
        self.ending = left

    def _confirming(self) -> bool:
        """A stopped stream still has segments to confirm (within GIVE_UP_S): no new stream started
        till they are, since the cloud ends the bay's other stream on a start."""
        now = self.clock()
        return any((e["stored"] or e["lost"]) and now - e["first"] < GIVE_UP_S for e in self.ending)

    def _push(self) -> None:
        """The stream started on the cloud if it isn't, and what's queued sent: unless waiting."""
        if self.clock() < self.wait_until or self.halted:
            return
        if self.stream_id is None and (self._confirming() or not self._start()):
            return
        if not self.halted:
            self._send()
        if not self.halted and self.stream_id is not None and self.clock() >= self.wait_until:
            self._send_marks()   # (not while the network's waited for: a hung one isn't timed out twice)

    # -- one pass
    def step(self) -> None:
        if self.ending:
            self._end_streams()
        if self.over:
            # OBS's playlist has ended: what's left goes (waiting as ever), then the stream ends.
            if self.queued and not self.halted and self.clock() - self.over < GIVE_UP_S:
                self._push()
            if not (self.queued and not self.halted) or self.clock() - self.over >= GIVE_UP_S:
                self._end()
            return
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
        retry = self.halted == "refused" and self.session is not None and now >= self.off_until
        if mtime is None or (mtime == self.seen_mtime and not self.queued and not self.stored and not self.lost
                             and not retry):
            return
        if self.session is None and now - mtime > self.stale:
            return   # left over from an earlier stream
        if retry:
            # Refused a while ago and OBS still on: asked again, a fresh stream from here on.
            from .live_stream import _session_name

            self.session, self.halted = _session_name(now), None
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
                self.stamps, self.queued, self.done, self.lost, self.stored = {}, {}, set(), set(), {}
                self.high, self.stream_id = -1, None
                # Streaming refused a moment ago: on air here all the same, the cloud asked at off_until.
                self.halted = "refused" if now < self.off_until else None
                self._status(True)
            self.playlist_ns = mtime_ns
            self.stamps.update(stamps)
            durations = segment_durations(raw)
            for i, name in enumerate(segments):
                n = seq + i
                if n > self.high:
                    if not self.halted:
                        self.queued[n] = {"name": name, "seconds": durations.get(name, DEFAULT_SECONDS),
                                          "first": now, "ns": stamps[name]}
                    self.high = n
            self.seq, self.seen_mtime = seq, mtime
        else:
            ended = False
        self._push()
        if ended:
            self.over = now
            if not (self.queued and not self.halted):
                self._end()

    def _end(self, clean: bool = True) -> None:
        """The stream's stopped: what's left of it goes (its last segment is finished now), then the
        cloud is told; OBS's folder is emptied once every copy reading it is done."""
        if self.session is None:
            return
        if not self.halted and self.stream_id is not None:
            for _ in range(4):   # (a few passes: each sends up to MAX_PER_ASK)
                # (or the cloud ended it meanwhile, or it's waiting: what's left is given up)
                if not self.queued or self.stream_id is None or self.clock() < self.wait_until:
                    break
                self._send()
            for seq_ in list(self.queued):
                self._give_up(seq_)
            self._tell()
        if self.stream_id is not None:
            self.ending.append({"id": self.stream_id, "first": self.clock(), "stored": dict(self.stored),
                                "lost": set(self.lost)})
            self._end_streams()
        read, self.playlist_ns = self.playlist_ns, 0
        seen = self.stamps
        self.session, self.stream_id, self.seq, self.seen_mtime = None, None, -1, 0.0
        self.queued, self.stored, self.lost, self.stamps, self.halted = {}, {}, set(), {}, None
        self.over = 0.0
        self._status(False)
        if clean and all(p.session is None for p in self.peers):
            # Only what this stream read (and unchanged since): never anything else in the folder.
            for name, ns in seen.items():
                try:
                    if self._stamp(name) == ns:
                        (self.folder / name).unlink()
                except OSError:
                    pass   # OBS still holds it: the next stream overwrites it
            pl = self.folder / self.playlist
            try:
                if pl.stat().st_mtime_ns == read:   # (not one OBS has written since)
                    pl.unlink()
            except OSError:
                pass

    def _stamp(self, name: str) -> int | None:
        try:
            return (self.folder / name).stat().st_mtime_ns
        except OSError:
            return None

    # -- the thread
    def start(self, interval: float = 1.0) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._status(False)   # (a booth killed on air left it saying so)
        self._thread = threading.Thread(target=self.run, args=(interval,), name="live-cloud", daemon=True)
        self._thread.start()

    def run(self, interval: float = 1.0) -> None:
        from .cloud import NotPaired, PairingChanged, TooOld

        try:
            while not self._stop.wait(interval):
                try:
                    self.step()
                except (NotPaired, PairingChanged, TooOld) as e:
                    log.warning("Live stream: nothing more goes to Shotcast Cloud till Shotcast restarts: %s", e)
                    self.session = None   # (the idle watch and the folder's other readers see it's over)
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
        finally:   # however the thread stops: nothing on air (an update that cures it isn't held back)
            self._status(False)

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
