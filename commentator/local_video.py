"""Round videos kept on the PC (live.local_keep_days): while OBS records, each of its HLS segments is
appended to one MPEG-TS file per stream in live.local_dir (it plays in Windows Media Player or VLC),
with a sidecar listing where each segment is in it, and the rounds on it. The local stats page plays
them (stats_web.py) through the same live/ paths the published page uses for the bucket's:

    <dir>/<session>.ts     the stream, its segments one after another
    <dir>/<session>.json   {"session", "since", "ended", "duration", "shots", "rounds": [{"at", "label"}],
                            "segments": [[offset, length, seconds], ...]}

A stream with no shots played on it, or under a minute, isn't kept. Files older than keep_days go
when a stream starts or ends, and when the booth starts. A stream the booth was killed in the middle of
is closed (or dropped, by the same rules) on the next start.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

MIN_SECONDS = 60
SESSION = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")


class LocalRecorder:
    def __init__(self, folder: str | Path, keep_days: float, clock=time.time) -> None:
        self.folder, self.keep, self.clock = Path(folder), keep_days * 86400, clock
        self.rec: dict | None = None   # the stream being recorded, as its sidecar has it

    def _paths(self, session: str) -> tuple[Path, Path]:
        return self.folder / f"{session}.ts", self.folder / f"{session}.json"

    def _save(self) -> None:
        _, side = self._paths(self.rec["session"])
        tmp = side.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.rec), encoding="utf-8")
        tmp.replace(side)

    def begin(self, session: str, since: float) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.rec = {"session": session, "since": round(since), "ended": None, "duration": 0, "shots": False,
                    "rounds": [], "segments": []}
        self._paths(session)[0].write_bytes(b"")
        self._save()
        log.info("Recording the stream to %s", self._paths(session)[0])
        self.prune()

    def add(self, data: bytes, seconds: float) -> None:
        if self.rec is None:
            return
        video, _ = self._paths(self.rec["session"])
        with open(video, "ab") as f:
            offset = f.tell()
            f.write(data)
        self.rec["segments"].append([offset, len(data), round(seconds, 3)])
        self.rec["duration"] = round(sum(s[2] for s in self.rec["segments"]))
        self._save()

    def changed(self) -> None:
        """The rounds or shots on `rec` changed (filed by the mirror)."""
        if self.rec is not None:
            self._save()

    def end(self) -> None:
        if self.rec is None:
            return
        rec, self.rec = self.rec, None
        self._close(rec)
        self.prune()

    def _close(self, rec: dict) -> None:
        video, side = self._paths(rec["session"])
        seconds = sum(s[2] for s in rec.get("segments") or [])
        if seconds < MIN_SECONDS or rec.get("shots") is False:
            log.info("Recording %s not kept: %s", rec["session"],
                     "no shots played on it" if seconds >= MIN_SECONDS else "under a minute")
            _delete(side, video)
            return
        rec.update(ended=round(rec["since"] + seconds), duration=round(seconds))
        tmp = side.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        tmp.replace(side)
        log.info("Kept %.0f minutes of video in %s", seconds / 60, video)

    def recover(self) -> None:
        """Close what a booth killed mid-stream left open, and drop what's past keep_days."""
        for rec in recordings(self.folder):
            if rec["ended"] is None and (self.rec is None or rec["session"] != self.rec["session"]):
                self._close(rec)
        self.prune()

    def prune(self) -> None:
        now, gone = self.clock(), 0
        current = self.rec and self.rec["session"]
        for rec in recordings(self.folder):
            if rec["session"] != current and now - rec["since"] > self.keep:
                gone += _delete(*reversed(self._paths(rec["session"])))
        # A video of ours whose sidecar is gone (deleted by hand) goes by its file's age; anything else
        # in the folder is left alone.
        for video in self.folder.glob("*.ts"):
            try:
                if SESSION.fullmatch(video.stem) and not video.with_suffix(".json").exists() and now - video.stat().st_mtime > self.keep:
                    video.unlink()
                    gone += 1
            except OSError:
                pass
        if gone:
            log.info("Deleted %d round video(s) older than %g days", gone, self.keep / 86400)


def _delete(side: Path, video: Path) -> bool:
    """The video, then its sidecar; one that's open (Windows: being watched) stays, listed, till the next
    prune. False if it's still there."""
    try:
        video.unlink(missing_ok=True)
        side.unlink(missing_ok=True)
        return True
    except OSError as e:
        log.info("Couldn't delete %s yet: %s", video, e)
        return False


def delete(folder: str | Path, session: str) -> dict | None:
    """Delete a kept recording (from the local stats page). Its record, or None if there's no such one;
    ValueError for one still being recorded, OSError if it can't go yet (open in a player)."""
    if not SESSION.fullmatch(session):
        return None
    rec = next((r for r in recordings(folder) if r["session"] == session), None)
    if rec is None:
        return None
    if rec["ended"] is None:
        raise ValueError("it's still being recorded")
    video = Path(folder) / f"{session}.ts"
    if not _delete(video.with_suffix(".json"), video):
        raise OSError(f"couldn't delete {video.name} (is it open in a player?)")
    log.info("Deleted the round video %s from the stats page", video)
    return rec


def folder(live, home: str | Path = ".") -> Path | None:
    """Where the booth keeps the round videos (config live.local_dir, from the Shotcast folder), or None
    when they aren't kept."""
    return Path(home, live.local_dir) if live.enabled and live.local_keep_days else None


def recordings(folder: str | Path) -> list[dict]:
    """The recordings in `folder`, newest first, each with its video's segments."""
    out = []
    for side in Path(folder).glob("*.json"):
        if not SESSION.fullmatch(side.stem) or not side.with_suffix(".ts").exists():
            continue
        try:
            rec = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("session") == side.stem and isinstance(rec.get("since"), (int, float)):
            out.append({"ended": None, "duration": 0, "shots": False, "rounds": [], "segments": [], **rec})
    return sorted(out, key=lambda r: r["since"], reverse=True)


def playlist(rec: dict) -> bytes:
    """An HLS playlist of the recording's segments, as seg<n>.ts (served from their place in the file)."""
    from .live_stream import vod_playlist

    return vod_playlist([(f"seg{i}.ts", s[2]) for i, s in enumerate(rec["segments"])], ended=rec["ended"] is not None)


def segment(folder: str | Path, rec: dict, n: int) -> bytes | None:
    if not 0 <= n < len(rec["segments"]):
        return None
    offset, length, _ = rec["segments"][n]
    with open(Path(folder) / f"{rec['session']}.ts", "rb") as f:
        f.seek(offset)
        return f.read(length)
