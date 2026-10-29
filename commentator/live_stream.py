"""The live stream on the stats page: OBS on the GSPro PC writes the screen and sound as HLS (a
playlist and a few seconds of video per file) into a folder, and this mirrors that folder to the
stats bucket, behind the same league password as the stats. Each stream is kept for keep_days (the
page's Round replays tab plays it from the start), then deleted; with keep_days 0 nothing is kept: segments
are deleted from the bucket as they leave the playlist, and the whole session when the stream stops.

    live/status.json               {"live": true, "session": ..., "since": ..., "at": ...} or {"live": false}
                                   ("at" is refreshed every HEARTBEAT_SECONDS while live, so the page can
                                   tell a booth that was killed mid-stream)
    live/recordings.json           {"recordings": [{"session", "since", "ended", "duration", "shots",
                                   "rounds": [{"at": seconds in, "label"}]}]}, newest first ("shots":
                                   whether a shot was played on it; one with none isn't kept)
    live/s/<session>/stream.m3u8   the live playlist, re-uploaded as OBS rewrites it
    live/s/<session>/full.m3u8     every segment so far (kept streams only), ENDLIST once it's over
    live/s/<session>/stream12.ts   its segments

The booth starts and stops OBS's output with each round over obs-websocket (built into OBS 28+);
the tray can do the same by hand. See docs/tracking.md#live-stream.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PASSWORD_ENV = "OBS_WEBSOCKET_PASSWORD"
HEARTBEAT_SECONDS = 30
MIN_RECORDING_SECONDS = 60   # a stream shorter than this (OBS started and stopped) isn't kept
SHOT_LEAD_SECONDS = 60       # a shot this long before a stream is first seen counts as on it
OBS_EXE = Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe")
PORTABLE_OBS = Path("obs", "bin", "64bit", "obs64.exe")   # the tray's setup (obs_setup.py), in the home folder
TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t", ".m4s": "video/iso.segment",
         ".mp4": "video/mp4", ".json": "application/json"}


class ObsError(RuntimeError):
    pass


class Obs:
    """Just enough of obs-websocket 5 to start, stop and ask about OBS's recording output, which is
    what writes the HLS stream (docs/tracking.md#live-stream)."""

    def __init__(self, host: str = "localhost", port: int = 4455, password: str | None = None,
                 timeout: float = 5.0) -> None:
        self.url, self.timeout = f"ws://{host}:{port}", timeout
        self.password = password if password is not None else os.environ.get(PASSWORD_ENV, "")

    def call(self, request: str, data: dict | None = None) -> dict:
        import websocket   # websocket-client

        try:
            ws = websocket.create_connection(self.url, timeout=self.timeout)
        except OSError as e:
            raise ObsError(f"OBS isn't answering on {self.url} (is it running, with its WebSocket server on?)") from e
        try:
            hello = json.loads(ws.recv())["d"]
            ident: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
            if auth := hello.get("authentication"):
                ident["authentication"] = auth_response(self.password, auth["salt"], auth["challenge"])
            ws.send(json.dumps({"op": 1, "d": ident}))
            try:
                raw = ws.recv()
            except websocket.WebSocketConnectionClosedException:
                raw = ""
            if not raw:   # OBS closes the connection on a wrong password
                raise ObsError(f"OBS refused the WebSocket password (set {PASSWORD_ENV} in .env)")
            reply = json.loads(raw)
            if reply.get("op") != 2:
                raise ObsError(f"OBS didn't accept the connection: {reply}")
            rid = secrets.token_hex(4)
            ws.send(json.dumps({"op": 6, "d": {"requestType": request, "requestId": rid, "requestData": data or {}}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("op") == 7 and msg["d"].get("requestId") == rid:
                    break
            status = msg["d"]["requestStatus"]
            if not status.get("result"):
                raise ObsError(f"OBS {request}: {status.get('comment') or status.get('code')}")
            return msg["d"].get("responseData") or {}
        finally:
            ws.close()

    def live(self) -> bool:
        return bool(self.call("GetRecordStatus").get("outputActive"))

    def start(self) -> bool:
        """Start the stream; False if it was already going."""
        if self.live():
            return False
        self.call("StartRecord")
        return True

    def stop(self) -> bool:
        """Stop the stream; False if it wasn't going."""
        if not self.live():
            return False
        self.call("StopRecord")
        return True


def auth_response(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def obs_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"], capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return "obs64.exe" in out.lower()


def clear_crash_flag(exe: Path) -> None:
    """OBS 31+ offers Safe Mode on its next start after it ended without closing (a crash, a forced
    end, the power going), and waits on that dialog, which nobody's watching here. Only while OBS isn't
    running: it marks each run with a file in .sentinel and deletes it when it closes."""
    root = exe.parents[2]
    portable = any((root / n).exists() for n in ("portable_mode", "portable_mode.txt",
                                                 "obs_portable_mode", "obs_portable_mode.txt"))
    base = root / "config" if portable else Path(os.environ.get("APPDATA", ""))
    for f in (base / "obs-studio" / ".sentinel").glob("run_*"):
        f.unlink(missing_ok=True)


def launch_obs(exe: str | Path | None = None) -> bool:
    """Start OBS minimized to the tray if it isn't running (Windows). True if it was started."""
    exe = Path(exe) if exe else (PORTABLE_OBS.resolve() if PORTABLE_OBS.exists() else OBS_EXE)
    if os.name != "nt" or not exe.exists() or obs_running():
        return False
    clear_crash_flag(exe)
    # OBS looks for its data relative to the working folder, so it must start in its own.
    subprocess.Popen([str(exe), "--minimize-to-tray", "--disable-shutdown-check", "--disable-updater"],
                     cwd=exe.parent, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return True


def parse_playlist(text: str) -> tuple[int, list[str], bool] | None:
    """(media sequence, segment file names, ended) from an HLS media playlist; None if it's partly
    written or not a playlist."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0] != "#EXTM3U":
        return None
    seq, segments, ended, pending = 0, [], False, False
    for ln in lines[1:]:
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(ln.split(":", 1)[1])
        elif ln.startswith("#EXTINF:"):
            pending = True
        elif ln == "#EXT-X-ENDLIST":
            ended = True
        elif not ln.startswith("#"):
            if "/" in ln or "\\" in ln or ln.startswith("."):
                return None   # only plain file names next to the playlist
            segments.append(ln)
            pending = False
    return None if pending else (seq, segments, ended)


def segment_durations(text: str) -> dict[str, float]:
    """Each segment's #EXTINF duration in an HLS media playlist."""
    out, dur = {}, None
    for ln in (ln.strip() for ln in text.splitlines()):
        if ln.startswith("#EXTINF:"):
            try:
                dur = float(ln[8:].split(",", 1)[0])
            except ValueError:
                dur = None
        elif ln and not ln.startswith("#"):
            if dur is not None:
                out[ln] = dur
            dur = None
    return out


def vod_playlist(track: list[tuple[str, float]], ended: bool) -> bytes:
    """A playlist of every segment from the start: EVENT while it grows, closed with ENDLIST."""
    target = max([6] + [int(d + 0.999) for _, d in track])
    body = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:EVENT", f"#EXT-X-TARGETDURATION:{target}",
            "#EXT-X-MEDIA-SEQUENCE:0"]
    for name, dur in track:
        body += [f"#EXTINF:{dur:.3f},", name]
    if ended:
        body.append("#EXT-X-ENDLIST")
    return ("\n".join(body) + "\n").encode()


def off_air(cfg) -> None:
    """Take the page's Live tab off air from outside the booth (the tray, when it quits: that ends the
    booth without letting it clean up). Stops OBS's output too, so nothing keeps writing."""
    import boto3

    try:
        Obs(cfg.live.obs_host, cfg.live.obs_port, timeout=2).stop()
    except Exception as e:
        log.info("Live stream: OBS not stopped: %s", e)
    p = cfg.publish
    client = boto3.client("s3", region_name=p.region) if p.region else boto3.client("s3")
    Mirror(cfg.live.dir, p.bucket, p.prefix, cfg.live.playlist, client, keep_days=cfg.live.keep_days,
           status_file=Path(cfg.log_dir) / "live.json").clear()


def remove(bucket: str, prefix: str = "", client: Any = None) -> None:
    """No Live tab: the stream is turned off, so the page shouldn't offer it."""
    if client is None:
        import boto3

        client = boto3.client("s3")
    client.delete_objects(Bucket=bucket, Delete={"Objects": [
        {"Key": (prefix.strip("/") + "/" if prefix.strip("/") else "") + "live/status.json"}]})


def _session_name(now: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(3)


class Mirror:
    """Copies OBS's HLS folder to s3://bucket/<prefix>live/ while it's being written. step() does one
    pass; run() calls it every `interval` seconds on its own thread."""

    retry_seconds = 30.0   # a failed clear() at the start, kept streams on: tried again after this

    def __init__(self, folder: str | Path, bucket: str, prefix: str = "", playlist: str = "stream.m3u8",
                 client: Any = None, status_file: str | Path | None = None, stale_seconds: float = 20.0,
                 clock=time.time, keep_days: float = 0) -> None:
        self.folder, self.bucket, self.playlist = Path(folder), bucket, playlist
        self.keep = keep_days * 86400
        self.state_file = Path(status_file).with_name("live-recordings.json") if status_file else None
        self.root = (prefix.strip("/") + "/" if prefix.strip("/") else "") + "live/"
        self.client, self.status_file = client, Path(status_file) if status_file else None
        self.stale, self.clock = stale_seconds, clock
        self.session: str | None = None
        self.since = 0.0
        self.seq = -1
        self.sent: dict[str, int] = {}   # segment -> its file's mtime (ns) when it went up
        self.seen_mtime = 0.0
        self._announced = False
        self._beat = 0.0
        self.track: list[tuple[str, float]] = []   # kept streams: every segment so far, with its length
        self.recordings: list[dict] = []           # live/recordings.json, newest first
        self._marks: list[tuple[float, str, bool]] = []   # from mark(), for the thread to file
        self._shots: list[float] = []   # from shot(), when each was played
        self._marks_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- S3
    def _put(self, key: str, body: bytes, cache: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.root + key, Body=body, CacheControl=cache,
                               ContentType=TYPES.get(Path(key).suffix, "application/octet-stream"))

    def _delete(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})

    def _status(self, live: bool) -> None:
        body = {"live": live, "session": self.session, "since": self.since, "at": round(self.clock())} \
            if live else {"live": False}
        self._beat = self.clock()
        self._put("status.json", json.dumps(body).encode(), "no-cache")
        if self.status_file:
            try:
                self.status_file.write_text(json.dumps(body), encoding="utf-8")
            except OSError:
                pass

    def _save_index(self) -> None:
        """recordings.json to the bucket, and (with the stream so far) to the PC, so a booth killed
        mid-stream closes it from this on its next start."""
        self._put("recordings.json", json.dumps({"recordings": self.recordings}).encode(), "no-cache")
        self._save_state()

    def _save_state(self) -> None:
        if self.state_file is None:
            return
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"recordings": self.recordings, "session": self.session,
                                       "track": self.track}), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as e:
            log.info("Live stream: couldn't save %s: %s", self.state_file, e)

    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file else {}
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def mark(self, label: str, replace: bool = False) -> None:
        """A round starts (or, with replace, has its final name): label the stream from here, for the
        Round replays tab. Called from any thread; the mirror files it on its next pass."""
        if self.keep:
            with self._marks_lock:
                self._marks.append((self.clock(), label, replace))

    def shot(self) -> None:
        """A shot was played: the stream it's on is kept (one with no shots, a round that never got
        going, isn't). Called from any thread."""
        if self.keep:
            now = self.clock()
            with self._marks_lock:   # off air, only the latest counts (for a stream about to start)
                self._shots = [t for t in self._shots if t > now - SHOT_LEAD_SECONDS] + [now]

    def _file_marks(self) -> bool:
        with self._marks_lock:
            marks, self._marks = self._marks, []
            shots, self._shots = self._shots, []
        rec = self._recording()
        if rec is None:
            with self._marks_lock:
                # Off air: the next stream starts with the latest of them (a round started before OBS),
                # and a shot just before it with the shot.
                self._marks = [m for m in marks if not m[2]][-1:] + self._marks
                self._shots = [t for t in shots if t > self.clock() - SHOT_LEAD_SECONDS] + self._shots
            return False
        changed = False
        if rec.get("shots") is False and any(t >= self.since - SHOT_LEAD_SECONDS for t in shots):
            rec["shots"], changed = True, True
        for at, label, replace in marks:
            if at < self.since - 1800:
                continue   # a round that started long before this stream: not what's on it
            rounds = rec["rounds"]
            if replace:
                if rounds and rounds[-1]["label"] != label:
                    rounds[-1]["label"], changed = label, True
            elif not rounds or rounds[-1]["label"] != label:
                rounds.append({"at": round(max(0.0, at - self.since)), "label": label})
                changed = True
        return changed

    def _recording(self) -> dict | None:
        return next((r for r in self.recordings if r["session"] == self.session), None) if self.session else None

    def clear(self) -> None:
        """Off air. With nothing kept, nothing left in the bucket from a stream that never stopped
        cleanly. Kept: a stream that never stopped cleanly is closed and listed, and any past
        keep_days, too short to keep, with no shots played on it, or not listed (its record lost) deleted."""
        keys: dict[str, list[str]] = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.root + "s/"):
            for o in page.get("Contents", []):
                keys.setdefault(o["Key"][len(self.root) + 2:].split("/", 1)[0], []).append(o["Key"])
        self.session, self.track = None, []
        if not self.keep:
            self._delete([k for ks in keys.values() for k in ks] + [self.root + "recordings.json"])
            self._status(False)
            return
        state = self._load_state()
        known = {r["session"]: r for r in state.get("recordings") or [] if isinstance(r, dict) and r.get("session")}
        now, kept, gone = self.clock(), [], []
        for session, ks in keys.items():
            rec = known.get(session)
            if rec is not None and rec.get("ended") is None:
                # Cut off: the booth was killed or crashed mid-stream. Close what got uploaded.
                track = [(n, d) for n, d in (state.get("track") or []) if state.get("session") == session
                         and f"{self.root}s/{session}/{n}" in ks]
                duration = sum(d for _, d in track)
                rec.update(ended=round(rec["since"] + duration), duration=round(duration))
                if duration >= MIN_RECORDING_SECONDS:
                    self._put(f"s/{session}/full.m3u8", vod_playlist(track, ended=True), "max-age=300")
                    log.info("Live stream: closed session %s, cut off after %.0f minutes", session, duration / 60)
            if (rec is None or rec["duration"] < MIN_RECORDING_SECONDS or now - rec["since"] > self.keep
                    or rec.get("shots") is False):
                gone += ks
                continue
            if (stale := f"{self.root}s/{session}/stream.m3u8") in ks:
                gone.append(stale)
            kept.append(rec)
        self._delete(gone)
        self.recordings = sorted(kept, key=lambda r: r["since"], reverse=True)
        self._save_index()
        self._status(False)

    def _prune(self) -> None:
        """Delete the kept streams past keep_days."""
        now = self.clock()
        old = [r for r in self.recordings if now - r["since"] > self.keep and r["session"] != self.session]
        if not old:
            return
        keys: list[str] = []
        try:
            for r in old:
                for page in self.client.get_paginator("list_objects_v2").paginate(
                        Bucket=self.bucket, Prefix=f"{self.root}s/{r['session']}/"):
                    keys += [o["Key"] for o in page.get("Contents", [])]
            self._delete(keys)
        except Exception as e:   # tried again as the next stream starts or stops; the stream goes on
            log.warning("Live stream: deleting streams past %g days failed: %s", self.keep / 86400, e)
            return
        self.recordings = [r for r in self.recordings if r not in old]
        log.info("Live stream: deleted %d kept stream(s) older than %g days", len(old), self.keep / 86400)

    # -- one pass
    def step(self) -> None:
        pl = self.folder / self.playlist
        try:
            mtime = pl.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        now = self.clock()
        if self.session and (mtime is None or now - mtime > self.stale):
            self._end()   # OBS stopped without saying so (or crashed)
            return
        if mtime is None or mtime == self.seen_mtime:
            return
        if self.session is None and now - mtime > self.stale:
            return   # left over from an earlier stream
        try:
            raw = pl.read_bytes()   # once: what's uploaded is exactly the playlist read here
        except OSError:
            return
        parsed = parse_playlist(raw.decode("utf-8", "replace"))
        if parsed is None:
            return   # mid-write: the next pass gets it whole
        seq, segments, ended = parsed
        try:
            stamps = {name: (self.folder / name).stat().st_mtime_ns for name in segments}
        except OSError:
            return   # a segment went before we got to it: wait for the next playlist
        # OBS restarted the stream between passes: its numbering starts again, so the same names are new
        # video, and must not be served from the old session's (cached) files.
        if self.session and (seq < self.seq or any(n in self.sent and self.sent[n] != t for n, t in stamps.items())):
            self._end(clean_local=False)
        new_session = self.session is None
        if new_session:
            self.session, self.since, self.sent = _session_name(now), now, {}
            self.track = []
            log.info("Live stream on: mirroring %s to s3://%s/%ss/%s/", self.folder, self.bucket, self.root, self.session)
            if self.keep:
                self._prune()   # a booth left running for days: a week-old stream goes as the next starts
                self.recordings.insert(0, {"session": self.session, "since": round(now), "ended": None,
                                           "duration": 0, "shots": False, "rounds": []})
        durations = segment_durations(raw.decode("utf-8", "replace")) if self.keep else {}
        added = False
        for name in segments:
            if name not in self.sent:
                try:
                    data = (self.folder / name).read_bytes()
                except FileNotFoundError:
                    return
                self._put(f"s/{self.session}/{name}", data, "public, max-age=86400, immutable")
                self.sent[name] = stamps[name]
                if self.keep:
                    self.track.append((name, durations.get(name, 4.0)))
                    added = True
        if added:
            self._put(f"s/{self.session}/full.m3u8", vod_playlist(self.track, ended=False), "max-age=2")
            self._save_state()
        self._put(f"s/{self.session}/stream.m3u8", raw, "max-age=1")  # the page always asks for this name
        beat = not self._announced or self.clock() - self._beat >= HEARTBEAT_SECONDS
        if self.keep and (self._file_marks() or new_session or beat):
            if rec := self._recording():
                rec["duration"] = round(sum(d for _, d in self.track))
            self._save_index()
        if beat:   # once its playlist's there
            self._status(True)
            self._announced = True
        gone = sorted(set(self.sent) - set(segments))
        if gone and not self.keep:
            self._delete([f"{self.root}s/{self.session}/{n}" for n in gone])
            for n in gone:
                del self.sent[n]
        self.seq, self.seen_mtime = seq, mtime
        if ended:
            self._end()

    def _end(self, clean_local: bool = True) -> None:
        log.info("Live stream off (session %s)", self.session)
        prefix = f"{self.root}s/{self.session}/"
        self._status(False)
        duration = sum(d for _, d in self.track)
        rec = self._recording() if self.keep and duration >= MIN_RECORDING_SECONDS else None
        if rec is not None:
            self._file_marks()
        if rec is not None and rec.get("shots") is not False:
            self._put(f"s/{self.session}/full.m3u8", vod_playlist(self.track, ended=True), "max-age=300")
            self._delete([prefix + "stream.m3u8"])
            rec.update(ended=round(self.since + duration), duration=round(duration))
            self.session = None
            self._prune()
            self._save_index()
            log.info("Live stream: kept %.0f minutes for the Round replays tab", duration / 60)
        else:
            if rec is not None:
                log.info("Live stream: no shots played on it, so not kept")
            self._delete([prefix + n for n in self.sent] + [prefix + "stream.m3u8"]
                         + ([prefix + "full.m3u8"] if self.track else []))
            if self.keep:
                self.recordings = [r for r in self.recordings if r["session"] != self.session]
                self.session = None
                self._prune()
                self._save_index()
        self.session, self.sent, self.seq, self._announced, self.track = None, {}, -1, False, []
        if clean_local:
            for p in self.folder.glob("*"):
                if p.suffix in (".ts", ".m4s", ".m3u8", ".mp4", ".tmp"):
                    try:
                        p.unlink()
                    except OSError:
                        pass   # OBS still holds it: the next stream overwrites it

    # -- the thread
    def start(self, interval: float = 1.0) -> None:
        if self.client is None:
            import boto3

            self.client = boto3.client("s3")
        self.folder.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self.run, args=(interval,), name="live-mirror", daemon=True)
        self._thread.start()

    def run(self, interval: float = 1.0) -> None:
        cleared = False
        while not cleared:
            try:
                self.clear()
                cleared = True
            except Exception:
                log.exception("Live stream: clearing the bucket's live/ folder failed")
                if not self.keep:
                    break   # nothing to lose: stream anyway
                # Kept streams: not until it works, or the first new stream would overwrite the record
                # of the week's (and of one a crash cut off) and the next start would delete them.
                if self._stop.wait(self.retry_seconds):
                    return
        while not self._stop.wait(interval):
            try:
                self.step()
            except Exception:
                log.exception("Live stream upload failed (trying again)")
                self._stop.wait(5)
        if self.session:   # here, not in close(): only this thread touches the session
            try:
                self._end(clean_local=False)
            except Exception:
                log.exception("Live stream: going off air failed")

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
