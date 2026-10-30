"""Ball data GSPro saves beside its log, for the builds whose log has none (the 3.59 public beta).

GSPro writes these itself whatever launch monitor sent the shot, once the ball comes to rest
(3-13 s after "Shot Fired!" in the log):

- currentRound.dat: JSON, the round in play, one entry per shot. Each entry has the player, hole,
  shot number and, in GhostData, the same numbers 3.1 logged as `LaunchExt Vars` (sp ball speed,
  el launch, az start direction, ts total spin, sa spin axis, cy launch-monitor carry). A new round
  starts the list over. Not every entry is a swing: a penalty (ShotResult 2 water, 3 out of bounds)
  is its own entry, a ball that drops after the stroke (a gimme, a holed putt) can add a copy of
  that stroke with HoleResult 7, and a mulligan marks the replaced entry ShotResult 1.
- GSPro.db, table LastShot: the latest shot only (ball speed, VLA, HLA, back and side spin,
  rawCarryLM). Written for practice shots too, which currentRound.dat isn't, but it skips some
  shots (into the water, holed), so it's only the fallback.

Checked on 3.59.11 with a Uneekor EYE XO2 (mulligan, OB, water, chip-in, gimmes, two players, a
player hitting out of turn, a new round).
"""

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ROUND_FILE = "currentRound.dat"
DB_FILE = "GSPro.db"
SWING = 0          # ShotResult of a real stroke (1 mulliganed, 2 water penalty, 3 out-of-bounds penalty)
HOLED_COPY = 7     # HoleResult of the entry GSPro adds when the ball drops
DB_SETTLE = 0.2    # seconds GSPro.db must stay unchanged before it's read


def launch_of(entry: dict) -> dict[str, float] | None:
    """The `LaunchExt Vars`-style numbers in a currentRound.dat entry (None without a ball speed)."""
    g = entry.get("GhostData") or {}
    vals = {k: float(g[k]) for k in ("sp", "el", "az", "ts", "sa", "cy", "id")
            if isinstance(g.get(k), (int, float))}
    return vals if vals.get("sp") else None


def swings(entries: list[dict]) -> list[tuple[str, dict]]:
    """(ShotID, entry) for each entry that is a stroke the player hit: not a penalty, not the copy
    GSPro adds when the ball drops (the stroke before it again: same player, hole and ball data)."""
    out, prev = [], None
    for e in entries:
        sid, vals = e.get("ShotID"), launch_of(e)
        copy = (e.get("HoleResult") == HOLED_COPY and prev is not None and vals is not None
                and vals == launch_of(prev) and e.get("PlayerName") == prev.get("PlayerName")
                and e.get("Hole") == prev.get("Hole"))
        if sid and vals and e.get("ShotResult") in (SWING, None) and not copy:
            out.append((sid, e))
        prev = e
    return out


@dataclass
class LastShot:
    id: int
    data: dict

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

    def __init__(self, folder: str) -> None:
        self.folder = Path(folder)
        self.seen: set[str] = set()
        self._round_sig = None
        self._db_mtime = None
        self._db_moving = None
        self._db_settled = None   # a settled change, acknowledged once it's been read
        self.last_shot_id: int | None = None
        self.db_written = 0.0     # when GSPro.db was written, as of the last read (wall-clock seconds)
        self.primed = False

    def move(self, folder: str) -> None:
        if Path(folder) != self.folder:
            self.__init__(folder)

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

    def _load_round(self) -> list[dict] | None:
        try:
            data = json.loads((self.folder / ROUND_FILE).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:   # mid-write: read again on the next change
            log.debug("Can't read %s yet: %s", ROUND_FILE, e)
            self._round_sig = None
            return None
        return data if isinstance(data, list) else None

    def _entries_changed(self) -> list[tuple[str, dict]] | None:
        sig = self._mtime(self.folder / ROUND_FILE)
        if sig is None or sig == self._round_sig:
            return None
        self._round_sig = sig
        entries = self._load_round()
        return swings(entries) if entries is not None else None

    def new_swings(self) -> tuple[list[dict], float]:
        """currentRound.dat entries for strokes not seen before, oldest first, and when the file was
        written (wall-clock seconds)."""
        out = []
        for sid, e in self._entries_changed() or []:
            if sid not in self.seen:
                self.seen.add(sid)
                out.append(e)
        return out, (self._round_sig[0] / 1e9 if self._round_sig else 0.0)

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
                row = con.execute("select ID, ShotData from LastShot order by ID desc limit 1").fetchone()
            finally:
                con.close()
        except sqlite3.Error as e:
            log.debug("Can't read LastShot from %s: %s", db, e)
            return False, None
        if not row:
            return True, None
        try:
            return True, LastShot(int(row[0]), json.loads(row[1] or "{}"))
        except (TypeError, ValueError):
            return True, None


def folder_of(log_path: str) -> str:
    return os.path.dirname(os.path.abspath(log_path))
