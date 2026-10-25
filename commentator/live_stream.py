"""The live stream on the stats page: OBS on the GSPro PC writes the screen and sound as HLS (a
playlist and a few seconds of video per file) into a folder, and this mirrors that folder to the
stats bucket, behind the same league password as the stats. Nothing is kept: segments are deleted
from the bucket as they leave the playlist, and the whole session when the stream stops.

    live/status.json               {"live": true, "session": ..., "since": ..., "at": ...} or {"live": false}
                                   ("at" is refreshed every HEARTBEAT_SECONDS while live, so the page can
                                   tell a booth that was killed mid-stream)
    live/s/<session>/stream.m3u8   the playlist, re-uploaded as OBS rewrites it
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
    Mirror(cfg.live.dir, p.bucket, p.prefix, cfg.live.playlist, client).clear()


def remove(bucket: str, prefix: str = "", client: Any = None) -> None:
    """No Live tab: the stream is turned off, so the page shouldn't offer it."""
    if client is None:
        import boto3

        client = boto3.client("s3")
    client.delete_objects(Bucket=bucket, Delete={"Objects": [
        {"Key": (prefix.strip("/") + "/" if prefix.strip("/") else "") + "live/status.json"}]})


class Mirror:
    """Copies OBS's HLS folder to s3://bucket/<prefix>live/ while it's being written. step() does one
    pass; run() calls it every `interval` seconds on its own thread."""

    def __init__(self, folder: str | Path, bucket: str, prefix: str = "", playlist: str = "stream.m3u8",
                 client: Any = None, status_file: str | Path | None = None, stale_seconds: float = 20.0,
                 clock=time.time) -> None:
        self.folder, self.bucket, self.playlist = Path(folder), bucket, playlist
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

    def clear(self) -> None:
        """Off air, and nothing left in the bucket from a stream that never stopped cleanly."""
        keys: list[str] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.root + "s/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        self._delete(keys)
        self.session = None
        self._status(False)

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
        if self.session is None:
            self.session, self.since, self.sent = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3), now, {}
            log.info("Live stream on: mirroring %s to s3://%s/%ss/%s/", self.folder, self.bucket, self.root, self.session)
        for name in segments:
            if name not in self.sent:
                try:
                    data = (self.folder / name).read_bytes()
                except FileNotFoundError:
                    return
                self._put(f"s/{self.session}/{name}", data, "public, max-age=86400, immutable")
                self.sent[name] = stamps[name]
        self._put(f"s/{self.session}/stream.m3u8", raw, "max-age=1")  # the page always asks for this name
        if not self._announced or self.clock() - self._beat >= HEARTBEAT_SECONDS:   # once its playlist's there
            self._status(True)
            self._announced = True
        gone = sorted(set(self.sent) - set(segments))
        if gone:
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
        self._delete([prefix + n for n in self.sent] + [prefix + "stream.m3u8"])
        self.session, self.sent, self.seq, self._announced = None, {}, -1, False
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
        try:
            self.clear()
        except Exception:
            log.exception("Live stream: clearing the bucket's live/ folder failed")
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
