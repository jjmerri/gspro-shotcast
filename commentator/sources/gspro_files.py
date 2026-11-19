"""Ball data GSPro saves beside its log, for the builds whose log has none (the 3.59 public beta).

GSPro writes these itself whatever launch monitor sent the shot, once the ball comes to rest
(3-13 s after "Shot Fired!" in the log):

- currentRound.dat: JSON, the round in play, one entry per shot. Each entry has the player, hole,
  shot number and, in GhostData, the same numbers 3.1 logged as `LaunchExt Vars` (sp ball speed,
  el launch, az start direction, ts total spin, sa spin axis, cy launch-monitor carry). A new round
  starts the list over. Not every entry is a swing: a penalty (ShotResult 2 water, 3 out of bounds)
  is its own entry, a ball that drops after the stroke (a gimme, a holed putt) can add a copy of
  that stroke with HoleResult 7 (in a scramble, one for every teammate: holed_copies), and a mulligan
  marks the replaced entry ShotResult 1. A concession
  (GSPro's pickup at the max score) writes no entry at all, nor does a practice or range shot.
  GSPro rewrites the whole file (File.WriteAllText) in the frame it decides the ball is at rest,
  before it logs the shooter's score line, so it's the booth's earliest word of the rest: the lie,
  carry, total and distance to the pin (round_file.py). RoundFileWatch parses it only when it
  changed; on Windows a change notification (ReadDirectoryChangesW on GSPro's folder, ChangeNotifier)
  has it read within milliseconds of the write, with a stat every WATCH_BACKUP_S as the backup for a
  missed one (and the only check elsewhere). The engine's live follow and the shot feed share one.
- GSPro.db, table LastShot: the latest shot only (ball speed, VLA, HLA, back and side spin,
  rawCarryLM). Written for practice shots too, which currentRound.dat isn't, but it skips some
  shots (into the water, holed), so it's only the fallback.

Checked on 3.59.11 with a Uneekor EYE XO2 (mulligan, OB, water, chip-in, gimmes, two players, a
player hitting out of turn, a new round).
"""

import json
import logging
import math
import os
import sqlite3
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ROUND_FILE = "currentRound.dat"
DB_FILE = "GSPro.db"
SWING = 0          # ShotResult of a real stroke (1 mulliganed, 2 water penalty, 3 out-of-bounds penalty)
HOLED_COPY = 7     # HoleResult of the entry GSPro adds when the ball drops
DB_SETTLE = 0.2    # seconds GSPro.db must stay unchanged before it's read
# The backup look at currentRound.dat (a stat, parsed only when it changed), for a change notification
# missed (its buffer overflowed, an antivirus held the file...), and the only one off Windows.
WATCH_BACKUP_S = 1.0
# GSPro's write truncates then writes, so a read on its notification can catch the file half-written: it's
# read again every PARSE_RETRY_STEP_S for this long. A partial list never parses (it lacks its closing ]).
PARSE_RETRY_S = 0.1
PARSE_RETRY_STEP_S = 0.02
_PARTIAL = object()


def launch_of(entry: dict) -> dict[str, float] | None:
    """The `LaunchExt Vars`-style numbers in a currentRound.dat entry (None without a ball speed)."""
    g = entry.get("GhostData") or {}
    vals = {k: float(g[k]) for k in ("sp", "el", "az", "ts", "sa", "cy", "id")
            if isinstance(g.get(k), (int, float))}
    return vals if vals.get("sp") else None


def holed_copies(entries: list) -> dict[int, int]:
    """The copies GSPro adds when a hole ends on a ball (HoleResult 7): index in `entries` -> the index of
    the stroke it copies. A copy carries the ball data of the stroke that ended the hole, often one
    stroke number on. Alone it follows its own stroke; in a team format (scramble) GSPro adds one for
    every teammate, in either order, so it's a HoleResult 7 entry with the same ball data as an earlier
    entry on the hole, whoever's: the first entry with that ball is the stroke."""
    out: dict[int, int] = {}
    first: dict[tuple, int] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or (vals := launch_of(e)) is None:
            continue
        key = (e.get("Hole"), tuple(sorted(vals.items())))
        if key in first and e.get("HoleResult") == HOLED_COPY:
            out[i] = first[key]
        else:
            first.setdefault(key, i)
    return out


def swings(entries: list[dict]) -> list[tuple[str, dict]]:
    """(ShotID, entry) for each entry that is a stroke the player hit: not a penalty, not a mulligan's
    taken-back stroke, not a copy GSPro adds when the ball drops (holed_copies)."""
    copies = holed_copies(entries)
    return [(sid, e) for i, e in enumerate(entries)
            if isinstance(e, dict) and i not in copies and (sid := e.get("ShotID")) and launch_of(e)
            and e.get("ShotResult") in (SWING, None)]


class ChangeNotifier:
    """Windows change notifications for one file: ReadDirectoryChangesW on its folder, on a daemon
    thread, calls `on_change` for each change to the file named `name` (and on a notification buffer
    overflow, which may have hidden one). start() is False where there are none (not Windows, no folder)."""

    def __init__(self, folder: str, name: str, on_change) -> None:
        self.folder, self.name, self.on_change = str(folder), name.lower(), on_change
        self._handle = None
        self._stopping = False
        self._k32 = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.ReadDirectoryChangesW.restype = wintypes.BOOL
        k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
                                              wintypes.LPVOID]
        k32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        file_list_directory, share_all, open_existing, backup_semantics = 0x1, 0x7, 3, 0x02000000
        h = k32.CreateFileW(self.folder, file_list_directory, share_all, None, open_existing, backup_semantics, None)
        if h is None or h == wintypes.HANDLE(-1).value:
            log.debug("Can't watch %s for changes: error %d", self.folder, ctypes.get_last_error())
            return False
        self._k32, self._handle = k32, h
        threading.Thread(target=self._run, args=(ctypes, wintypes), name="round-file-notify", daemon=True).start()
        return True

    def _run(self, ctypes, wintypes) -> None:
        # FILE_NOTIFY_CHANGE_FILE_NAME | _SIZE | _LAST_WRITE: GSPro's rewrite, or a replace by rename.
        flags = 0x1 | 0x8 | 0x10
        buf = ctypes.create_string_buffer(64 * 1024)
        got = wintypes.DWORD()
        h = self._handle
        try:
            while not self._stopping:
                if not self._k32.ReadDirectoryChangesW(h, buf, len(buf), False, flags, ctypes.byref(got), None, None):
                    if not self._stopping:
                        log.info("Lost change notifications for %s (error %d): checking it every %.0f s instead",
                                 self.name, ctypes.get_last_error(), WATCH_BACKUP_S)
                    break
                if self._stopping:
                    break
                if got.value == 0 or self._names(buf.raw[:got.value]):   # 0: the buffer overflowed
                    try:
                        self.on_change()
                    except Exception:
                        log.exception("Change notification handler failed")
        finally:
            # Closed here, by the only thread reading it: never under a read, never used once closed.
            with self._lock:
                self._stopping, self._handle = True, None
            self._k32.CloseHandle(h)

    def _names(self, data: bytes) -> bool:
        off = 0
        while off + 12 <= len(data):
            nxt, _, n = struct.unpack_from("<III", data, off)
            if data[off + 12:off + 12 + n].decode("utf-16-le", "replace").lower() == self.name:
                return True
            if not nxt:
                break
            off += nxt
        return False

    def stop(self) -> None:
        """Ends the thread: its blocked read is cancelled (one not yet begun ends at the folder's next
        change, with no call), and it closes the folder."""
        with self._lock:
            self._stopping = True
            if self._handle is not None:
                self._k32.CancelIoEx(self._handle, None)


class RoundFileWatch:
    """currentRound.dat as GSPro last wrote it. poll() stats it and parses it only when its mtime or
    size changed. GSPro rewrites the whole file (truncate, then write), so a read can catch it half
    written: that fails to parse (or the file changes under it) and is never taken; poll(retry_s) reads
    again till it parses, else the next poll does. Thread-safe, so the shot feed, the engine and the
    change notifier (notify()) can share one.

    `version` counts the writes read; `seen_at` is when (monotonic) each ShotID was first read, so an
    entry can be told from one already there before a swing (-inf: in the file before the watch began); `written` is the file's mtime (wall-clock)."""

    def __init__(self, folder: str) -> None:
        self.folder = Path(folder)
        self._lock = threading.Lock()
        self.version = 0
        self._clear()
        self._on_change = None
        self._notifier: ChangeNotifier | None = None

    def notify(self, on_change) -> bool:
        """Call `on_change` (from another thread) as soon as GSPro writes the file, read by then: Windows
        change notifications on its folder. False when there are none here: poll on WATCH_BACKUP_S alone."""
        self._on_change = on_change
        return self._start_notifier()

    def unnotify(self) -> None:
        self._on_change = None
        if self._notifier is not None:
            self._notifier.stop()
            self._notifier = None

    def _start_notifier(self) -> bool:
        if self._notifier is not None:
            self._notifier.stop()
            self._notifier = None
        if self._on_change is None:
            return False
        n = ChangeNotifier(str(self.folder), ROUND_FILE, self._notified)
        try:
            ok = n.start()
        except Exception as e:   # (ctypes on an odd Windows)
            log.debug("Change notifications failed to start: %s", e)
            ok = False
        if ok:
            self._notifier = n
            log.info("Watching %s with change notifications (backup check every %.0f s)",
                     self.path, WATCH_BACKUP_S)
        else:
            log.info("No change notifications for %s here: checking it every %.0f s", self.path, WATCH_BACKUP_S)
        return ok

    def _notified(self) -> None:
        try:
            self.poll(retry_s=PARSE_RETRY_S)
        except Exception:
            log.exception("Couldn't read %s", ROUND_FILE)
        # Told even when another reader had the write first: the version says whether there's news.
        if (cb := self._on_change) is not None:
            cb()

    def _clear(self) -> None:
        self.sig = None
        self.entries: list[dict] | None = None   # None: no file read (no file, or a GSPro that writes none)
        self.written = 0.0
        self.changed_at: float | None = None
        self.seen_at: dict[str, float] = {}
        self._since = time.time()   # the file as written before this (wall-clock) is history

    @property
    def path(self) -> Path:
        return self.folder / ROUND_FILE

    def move(self, folder: str) -> None:
        with self._lock:
            if Path(folder) == self.folder:
                return
            self.folder = Path(folder)
            self._clear()
            self.version += 1
        if self._on_change is not None:
            self._start_notifier()

    def _read(self):
        """(sig, parsed) for a changed file, None for no change (or no file), _PARTIAL mid-write."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self.sig:
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            st = self.path.stat()
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.debug("Can't read %s yet: %s", ROUND_FILE, e)
            return _PARTIAL
        if (st.st_mtime_ns, st.st_size) != sig:   # written again under the read: which one was read?
            return _PARTIAL
        return sig, data

    def poll(self, now: float | None = None, retry_s: float = 0.0) -> bool:
        """Read the file if it changed since the last read, trying again for `retry_s` seconds if it's
        caught mid-write. True when a new write was read."""
        deadline = time.monotonic() + retry_s
        with self._lock:
            while (got := self._read()) is _PARTIAL and time.monotonic() < deadline:
                time.sleep(PARSE_RETRY_STEP_S)
            if got is None or got is _PARTIAL:
                return False
            sig, data = got
            self.sig = sig
            if not isinstance(data, list):
                return False
            now = time.monotonic() if now is None else now
            # The first read of a file last written before the watch began: all of it was played before, so
            # no stroke in it can be a shot struck since (however late that read comes).
            seen = -math.inf if self.entries is None and sig[0] / 1e9 < self._since else now
            self.entries, self.written, self.changed_at = data, sig[0] / 1e9, now
            self.version += 1
            for e in data:
                if isinstance(e, dict) and e.get("ShotID") is not None:
                    self.seen_at.setdefault(str(e["ShotID"]), seen)
            return True


@dataclass
class LastShot:
    id: int
    data: dict
    saved: str | None = None   # its DateCreated, as GSPro wrote it (local time, to the second)

    def launch(self) -> dict | None:
        """An Open Connect BallData dict from the row (None without a ball speed)."""
        d = self.data
        if not d.get("BallSpeed"):
            return None
        carry = d.get("rawCarryLM")
        return {"Speed": d.get("BallSpeed"), "VLA": d.get("VLA"), "HLA": d.get("HLA"),
                "BackSpin": d.get("BackSpin"), "SideSpin": d.get("SideSpin"),
                "CarryDistance": carry if carry is not None else d.get("Carry")}


class GsproFiles:
    """Watches currentRound.dat and GSPro.db in a GSPro log's folder for shots added since it started."""

    def __init__(self, folder: str, watch: RoundFileWatch | None = None) -> None:
        self.folder = Path(folder)
        self.seen: set[str] = set()
        self.watch = watch or RoundFileWatch(folder)   # the engine's follow polls the same one
        self._round_version = None
        self._db_mtime = None
        self._db_moving = None
        self._db_settled = None   # a settled change, acknowledged once it's been read
        self.last_shot_id: int | None = None
        self.db_written = 0.0     # when GSPro.db was written, as of the last read (wall-clock seconds)
        self.primed = False

    def move(self, folder: str) -> None:
        if Path(folder) != self.folder:
            watch = self.watch
            self.__init__(folder, watch)
            watch.move(folder)

    def prime(self) -> None:
        """Take what's there now as history."""
        for sid, _ in self._entries_changed() or []:
            self.seen.add(sid)
        self._db_mtime = self._mtime(self.folder / DB_FILE)
        _, row = self._query_last_shot()
        self.last_shot_id = row.id if row else None
        self.primed = True

    @staticmethod
    def _mtime(p: Path):
        try:
            st = p.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def _entries_changed(self) -> list[tuple[str, dict]] | None:
        self.watch.poll()
        if self.watch.entries is None or self.watch.version == self._round_version:
            return None
        self._round_version = self.watch.version
        return swings(self.watch.entries)

    def new_swings(self) -> tuple[list[dict], float]:
        """currentRound.dat entries for strokes not seen before, oldest first, and when the file was
        written (wall-clock seconds)."""
        out = []
        for sid, e in self._entries_changed() or []:
            if sid not in self.seen:
                self.seen.add(sid)
                out.append(e)
        return out, self.watch.written

    def db_changed(self, now: float) -> bool:
        """GSPro.db was written and has been still for DB_SETTLE seconds (read it then, not while
        GSPro is mid-write)."""
        sig = self._mtime(self.folder / DB_FILE)
        if sig is None or sig == self._db_mtime:
            self._db_moving = None
            return False
        if self._db_moving is None or self._db_moving[0] != sig:
            self._db_moving = (sig, now)
            return False
        if now - self._db_moving[1] < DB_SETTLE:
            return False
        self._db_settled, self._db_moving = sig, None
        return True

    def new_last_shot(self) -> LastShot | None:
        """The LastShot row, if it's newer than the last one seen. A failed read (GSPro holding the
        lock) leaves the change unacknowledged, so db_changed reports it again."""
        ok, row = self._query_last_shot()
        if ok and self._db_settled is not None:
            self._db_mtime = self._db_settled
            self.db_written = self._db_settled[0] / 1e9
        if row is None or (self.last_shot_id is not None and row.id <= self.last_shot_id):
            return None
        self.last_shot_id = row.id
        return row

    def _query_last_shot(self) -> tuple[bool, LastShot | None]:
        """(read it, the latest row)."""
        db = self.folder / DB_FILE
        if not db.exists():
            return True, None
        try:
            # Read-only, and only after GSPro wrote it, so the read lock is brief and rare.
            con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.2)
            try:
                row = con.execute("select ID, ShotData, DateCreated from LastShot order by ID desc limit 1").fetchone()
            finally:
                con.close()
        except sqlite3.Error as e:
            log.debug("Can't read LastShot from %s: %s", db, e)
            return False, None
        if not row:
            return True, None
        try:
            return True, LastShot(int(row[0]), json.loads(row[1] or "{}"), row[2])
        except (TypeError, ValueError):
            return True, None


def folder_of(log_path: str) -> str:
    return os.path.dirname(os.path.abspath(log_path))
