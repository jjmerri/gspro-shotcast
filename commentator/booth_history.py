"""The booth's history reads during play (Store.season_stats, player_profile, putting_make_rate and the
others), kept quick however long the season file gets (docs/dev/stats-longevity.md).

Each read went over every shot the player ever hit: 150-470 ms each on 10 years of a busy league, and
a tee shot makes several. Now a player's nights before today are read once (the first time the booth
asks about them) into sorted lists, and today's rows are read each time (a few, by session). SQLite
still decides which rows count (every filter is the old query's, worked out in the read), so the
figures are the old queries' (a tie, which SQLite left to its plan, goes to the earliest row).

What's kept is dropped when anything it read may have changed: a write from another connection (the
stats page's edits: `PRAGMA data_version`), the booth's own write to a shot of an earlier night or to a
session (noted by temp triggers, a rolled-back one too), or a new day."""
from __future__ import annotations

import math
import sqlite3
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter
from functools import cache
from typing import Any, Callable, Iterable

KEYS = ("", "league", "practice")   # a row's round context (as Store._ctx_sql slices them)

_OK = "mulligan = 0 AND left_out = 0 AND excluded = 0"
_TODAY = "session_id IN (SELECT id FROM main.sessions WHERE date = (SELECT date FROM main.sessions WHERE id = ?))"


@cache
def _sql(table: str = "main.shots") -> tuple[str, str]:
    """The reads: a player's rows (in `table`: a paired booth's past is a view, History.past_shots)
    with what each read checks of them, and every drive."""
    from .store import _DRIVE as DRIVE, DRIVE_SQL, UNKNOWN_CLUB_DRIVE_SQL
    rows = f"""SELECT id, session_id, ts, course, hole, club, to_pin_yards, finish_yards, total_yards, start_direction,
    CASE WHEN is_league = 1 THEN 'league' WHEN is_league = 0 THEN 'practice' ELSE '' END,
    attributed = 1,
    {DRIVE_SQL}, {DRIVE},
    is_putt = 0 AND finish_yards IS NOT NULL,
    is_putt = 1 AND outcome IS NOT NULL AND outcome != 'gimme',
    outcome = 'holed',
    is_putt = 0 AND ((club = 'DR' AND shot_number = 1) OR {UNKNOWN_CLUB_DRIVE_SQL}) AND lie_after IS NOT NULL,
    lie_after = 'fairway',
    lie_before,
    hole IS NOT NULL AND (lie_before = 'green' OR (is_putt = 1 AND lie_before IS NULL)),
    is_putt = 0 AND finish_yards IS NOT NULL AND (lie_after = 'green' OR outcome IN ('holed', 'conceded')),
    is_putt = 0 AND total_yards IS NOT NULL AND total_yards > 0 AND lie_before = 'tee' AND to_pin_yards >= 250,
    is_putt = 0 AND total_yards IS NOT NULL AND total_yards > 0 AND club IS NOT NULL AND to_pin_yards >= 40
        AND lie_before IN ('tee', 'fairway'),
    is_putt = 0 AND start_direction IS NOT NULL,
    club IS NOT NULL AND club NOT IN ('PT')
FROM {table} WHERE player = ? AND {_OK} AND """
    drives = f"""SELECT id, player, {DRIVE},
    CASE WHEN is_league = 1 THEN 'league' WHEN is_league = 0 THEN 'practice' ELSE '' END
FROM main.shots WHERE {DRIVE_SQL} AND player IS NOT NULL AND player != 'unknown' AND {_OK} AND NOT """ + _TODAY
    return rows, drives

# This connection's writes, noted in a temp table: `past` changes with a write to a shot of an earlier day
# than the newest session's, or to a session; `shots` with any write to a shot. Each is set to a random
# number, not counted up, so a rolled-back write leaves it at a value no later write brings back.
_TRIGGERS = """
CREATE TEMP TABLE IF NOT EXISTS booth_history_writes (past INTEGER NOT NULL, shots INTEGER NOT NULL);
INSERT INTO booth_history_writes SELECT 0, 0 WHERE NOT EXISTS (SELECT 1 FROM booth_history_writes);
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_shot_replaced BEFORE INSERT ON main.shots
WHEN EXISTS (SELECT 1 FROM main.shots WHERE id = NEW.id OR uid = NEW.uid)
BEGIN UPDATE booth_history_writes SET past = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_shot_replaced_by BEFORE UPDATE ON main.shots
WHEN (NEW.id IS NOT OLD.id AND EXISTS (SELECT 1 FROM main.shots WHERE id = NEW.id))
  OR (NEW.uid IS NOT OLD.uid AND EXISTS (SELECT 1 FROM main.shots WHERE uid = NEW.uid))
BEGIN UPDATE booth_history_writes SET past = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_any_in AFTER INSERT ON main.shots
BEGIN UPDATE booth_history_writes SET shots = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_any_out AFTER DELETE ON main.shots
BEGIN UPDATE booth_history_writes SET shots = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_any_set AFTER UPDATE ON main.shots
BEGIN UPDATE booth_history_writes SET shots = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_shot_in AFTER INSERT ON main.shots
WHEN (SELECT date FROM main.sessions WHERE id = NEW.session_id)
     IS NOT (SELECT date FROM main.sessions WHERE id = (SELECT MAX(id) FROM main.sessions))
BEGIN UPDATE booth_history_writes SET past = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_shot_out AFTER DELETE ON main.shots
WHEN (SELECT date FROM main.sessions WHERE id = OLD.session_id)
     IS NOT (SELECT date FROM main.sessions WHERE id = (SELECT MAX(id) FROM main.sessions))
BEGIN UPDATE booth_history_writes SET past = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_shot_set AFTER UPDATE ON main.shots
WHEN (SELECT date FROM main.sessions WHERE id = OLD.session_id)
     IS NOT (SELECT date FROM main.sessions WHERE id = (SELECT MAX(id) FROM main.sessions))
  OR (SELECT date FROM main.sessions WHERE id = NEW.session_id)
     IS NOT (SELECT date FROM main.sessions WHERE id = (SELECT MAX(id) FROM main.sessions))
BEGIN UPDATE booth_history_writes SET past = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_session_in AFTER INSERT ON main.sessions
BEGIN UPDATE booth_history_writes SET past = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_session_out AFTER DELETE ON main.sessions
BEGIN UPDATE booth_history_writes SET past = random(); END;
CREATE TEMP TRIGGER IF NOT EXISTS booth_history_session_set AFTER UPDATE ON main.sessions
BEGIN UPDATE booth_history_writes SET past = random(); END;
"""


class Band:
    """Values sorted by distance to the pin: a range of distances is two bisects."""
    __slots__ = ("at", "val")

    def __init__(self, pairs: list[tuple[float, float]]) -> None:
        pairs.sort()
        self.at = array("d", (p[0] for p in pairs))
        self.val = array("d", (p[1] for p in pairs))

    def between(self, lo: float, hi: float) -> Iterable[float]:
        """lo <= distance <= hi."""
        return self.val[bisect_left(self.at, lo):bisect_right(self.at, hi)]

    def over_upto(self, lo: float, hi: float) -> Iterable[float]:
        """lo < distance <= hi."""
        return self.val[bisect_right(self.at, lo):bisect_right(self.at, hi)]

    def from_below(self, lo: float, hi: float) -> Iterable[float]:
        """lo <= distance < hi."""
        return self.val[bisect_left(self.at, lo):bisect_left(self.at, hi)]

    def above(self, lo: float) -> Iterable[float]:
        """distance > lo."""
        return self.val[bisect_right(self.at, lo):]


class Ctx:
    """One round context's part of a player's rows."""

    def __init__(self) -> None:
        self.shots = 0                  # season_stats
        self.sessions: set[int] = set()
        self.drives: list[float] = []
        self.attributed = 0             # player_profile
        self.attributed_sessions: set[int] = set()
        self.aims: list[float] = []
        self.clubs: Counter[str] = Counter()
        self.approach: list | Band = []     # (to the pin, finish)
        self.putts: list | Band = []        # (to the pin, holed)
        self.fairway_tries = self.fairways = 0
        self.recovery: dict[str, list | Band] = {}      # lie -> (to the pin, finish)
        self.recovery_no_pin: dict[str, list[float]] = {}


class Rows:
    """What a player's rows (the nights before today, or today's) come to, for each read."""

    def __init__(self, rows: Iterable[tuple]) -> None:
        self.ctx = {k: Ctx() for k in KEYS}
        greens: dict[tuple, tuple] = {}
        leave, tee, club_len = [], [], {}
        for (rid, sid, ts, course, hole, club, to_pin, finish, total, start, key, attributed, drive_ok, drive,
             approach, putt, holed, fairway_try, fairway, lie, green, leaves, tee250, club_full, aimed, fav) in rows:
            c = self.ctx[key]
            c.shots += 1
            c.sessions.add(sid)
            if drive_ok and drive is not None:
                c.drives.append(drive)
            if not attributed:
                continue
            c.attributed += 1
            c.attributed_sessions.add(sid)
            if aimed:
                c.aims.append(start)
            if fav:
                c.clubs[club] += 1
            if approach:
                if to_pin is not None:
                    c.approach.append((to_pin, finish))
                if lie is not None:
                    if to_pin is None:
                        c.recovery_no_pin.setdefault(lie, []).append(finish)
                    else:
                        c.recovery.setdefault(lie, []).append((to_pin, finish))
            if putt and to_pin is not None:
                c.putts.append((to_pin, 1.0 if holed else 0.0))
            if fairway_try:
                c.fairway_tries += 1
                c.fairways += 1 if fairway else 0
            if green:
                g = greens.setdefault((sid, course, hole, key), [None, 0])
                g[1] += 1
                if g[0] is None or (ts, rid) < g[0][:2]:
                    g[0] = (ts, rid, to_pin)
            if leaves and to_pin is not None:
                leave.append((to_pin, 0.0 if holed else finish))
            if tee250:
                tee.append(total)
            if club_full:
                club_len.setdefault(club, []).append((to_pin, total))
        for c in self.ctx.values():
            c.approach = Band(c.approach)
            c.putts = Band(c.putts)
            c.recovery = {lie: Band(v) for lie, v in c.recovery.items()}
        self.leave = Band(leave)
        self.tee = sorted(tee)
        self.club_len = club_len
        # three-putts: a hole's putts by context, as the read's WHERE takes them (all contexts: every key's)
        self.first_putts: dict[str | None, Band] = {}
        for want in (None, "league", "practice"):
            holes: dict[tuple, list] = {}
            for (sid, course, hole, key), (first, n) in greens.items():
                if want is None or key == want:
                    h = holes.setdefault((sid, course, hole), [None, 0])
                    h[1] += n
                    if h[0] is None or first[:2] < h[0][:2]:
                        h[0] = first
            self.first_putts[want] = Band([(first[2], 1.0 if n >= 3 else 0.0) for first, n in holes.values()
                                           if first[2] is not None])


def _keys(context: str | None) -> tuple[str, ...]:
    return (context,) if context in ("league", "practice") else KEYS


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


class History:
    def __init__(self, db: sqlite3.Connection, session: Callable[[], int | None]) -> None:
        self.db = db
        self.session = session
        db.executescript(_TRIGGERS)
        # Where a player's nights before today are read: the season file's shots, or a paired booth's
        # view of them and its history file's (Store.attach_history), whose changes (a new answer from
        # the cloud, written by another connection) its data_version tells.
        self.past_shots = "main.shots"
        self.attached: str | None = None
        self.stamp: tuple | None = None
        self.past: dict[str, Rows] = {}
        self.drives: dict[str, tuple] | None = None
        self.today: dict[str, tuple[tuple, Rows]] = {}

    def _fresh(self) -> int | None:
        """The booth's session; what's kept is dropped if anything it read may have changed."""
        sid = self.session()
        last, past, shots = self.db.execute(
            "SELECT (SELECT MAX(id) FROM main.sessions), past, shots FROM temp.booth_history_writes").fetchone()
        stamp = (self.db.execute("PRAGMA data_version").fetchone()[0], past, sid)
        if self.attached:
            stamp += (self.db.execute(f"PRAGMA {self.attached}.data_version").fetchone()[0],)
        if sid is None or sid != last:   # (the triggers take the newest session's day as today)
            stamp += (shots,)
        if stamp != self.stamp:
            self.stamp, self.past, self.drives, self.today = stamp, {}, None, {}
        return sid

    def _parts(self, player: str, today: bool = True) -> list[Rows]:
        sid = self._fresh()
        if player not in self.past:
            self.past[player] = Rows(self.db.execute(_sql(self.past_shots)[0] + "NOT " + _TODAY, (player, sid)))
        if not today:
            return [self.past[player]]
        stamp = tuple(self.db.execute("SELECT shots FROM temp.booth_history_writes").fetchone())
        got = self.today.get(player)
        if got is None or got[0] != stamp:
            got = self.today[player] = (stamp, Rows(self.db.execute(_sql()[0] + _TODAY, (player, sid))))
        return [self.past[player], got[1]]

    def _ctxs(self, player: str, context: str | None, today: bool = True) -> list[Ctx]:
        return [p.ctx[k] for p in self._parts(player, today) for k in _keys(context)]

    def _round1(self, x: float | None) -> float | None:
        return self.db.execute("SELECT ROUND(?, 1)", (x,)).fetchone()[0]

    @staticmethod
    def _avg(vals: list[float]) -> float | None:
        return math.fsum(vals) / len(vals) if vals else None

    def season_stats(self, player: str, context: str | None) -> dict[str, Any]:
        cs = self._ctxs(player, context, today=False)
        drives = [d for c in cs for d in c.drives]
        return {"shots": sum(c.shots for c in cs), "longest_drive": max(drives, default=None),
                "avg_drive": self._avg(drives), "nights": len(set().union(*(c.sessions for c in cs)))}

    def season_longest_drive(self, context: str | None) -> tuple[str, float] | None:
        sid = self._fresh()
        if self.drives is None:
            best: dict[str, tuple] = {}
            for rid, player, drive, key in self.db.execute(_sql()[1], (sid,)):
                rank = (drive is not None, drive if drive is not None else 0.0, -rid)
                if key not in best or rank > best[key][0]:
                    best[key] = (rank, player, drive)
            self.drives = best
        got = [self.drives[k] for k in _keys(context) if k in self.drives]
        if not got:
            return None
        _, player, drive = max(got)
        return player, drive

    def player_profile(self, player: str, context: str | None) -> dict[str, Any]:
        cs = self._ctxs(player, context)
        aims = [a for c in cs for a in c.aims]
        clubs: Counter[str] = Counter()
        for c in cs:
            clubs.update(c.clubs)
        fav = min(clubs.items(), key=lambda kv: (-kv[1], kv[0])) if clubs else (None, 0)
        return {"shots": sum(c.attributed for c in cs),
                "nights": len(set().union(*(c.attributed_sessions for c in cs))),
                "avg_start_dir": self._avg(aims), "aimed": len(aims),
                "favorite_club": fav[0], "favorite_club_n": fav[1]}

    def approach_proximity(self, player: str, yards: float, band: float, context: str | None) -> dict[str, Any]:
        vals = [v for c in self._ctxs(player, context) for v in c.approach.between(yards - band, yards + band)]
        return {"n": len(vals), "avg_finish_yards": self._round1(self._avg(vals))}

    def putting_make_rate(self, player: str, lo: float, hi: float, context: str | None) -> dict[str, Any]:
        vals = [v for c in self._ctxs(player, context) for v in c.putts.between(lo, hi)]
        return {"attempts": len(vals), "made": int(sum(vals)) if vals else None}

    def three_putt_rate(self, player: str, lo: float, hi: float, context: str | None) -> dict[str, Any]:
        want = context if context in ("league", "practice") else None
        vals = [v for p in self._parts(player) for v in p.first_putts[want].between(lo, hi)]
        return {"holes": len(vals), "three_putts": int(sum(vals)) if vals else None}

    def fairways_hit(self, player: str, context: str | None) -> dict[str, Any]:
        cs = self._ctxs(player, context)
        tries = sum(c.fairway_tries for c in cs)
        return {"tries": tries, "hit": sum(c.fairways for c in cs) if tries else None}

    def recovery_proximity(self, player: str, lies: tuple[str, ...], max_to_pin: float | None,
                           context: str | None) -> dict[str, Any]:
        vals: list[float] = []
        for c in self._ctxs(player, context):
            for lie in set(lies):
                if (b := c.recovery.get(lie)) is not None:
                    vals.extend(b.val if max_to_pin is None else b.between(-math.inf, max_to_pin))
                if max_to_pin is None:
                    vals.extend(c.recovery_no_pin.get(lie, ()))
        return {"n": len(vals), "avg_finish_yards": self._round1(self._avg(vals))}

    def putt_band_rate(self, player: str, lo: float, hi: float) -> dict[str, Any]:
        vals = [v for c in self._ctxs(player, None, today=False) for v in c.putts.over_upto(lo, hi)]
        return {"attempts": len(vals), "made": int(sum(vals)) if vals else None}

    def approach_leave(self, player: str, lo: float, hi: float) -> dict[str, Any]:
        vals = list(self._parts(player, today=False)[0].leave.from_below(lo, hi))
        return {"n": len(vals), "avg_finish_yards": self._avg(vals)}

    def length_norm(self, player: str, club: str | None) -> dict[str, Any]:
        parts = self._parts(player)
        if club is None:
            totals = sorted(t for p in parts for t in p.tee) if parts[1].tee else parts[0].tee
        else:
            rows = [r for p in parts for r in p.club_len.get(club, ())]
            rough = _median(sorted(t for _, t in rows))
            totals = sorted(t for to_pin, t in rows if rough and to_pin > rough * 1.15)
        return {"n": len(totals), "avg_total": _median(totals)}
