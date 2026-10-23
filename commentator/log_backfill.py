"""Fill shots' logged positions and aim (shots.start_*, aim_map_*) and hole_layouts from saved GSPro
logs, for shots recorded before the booth read them live.

GSPro's output_log.txt has no timestamps, so a log's shots are matched to the season's by sequence:
the log's fires and the stored struck shots are aligned in order (a weighted longest common
subsequence), a pair matching on course, hole and the logged distance to the pin. A tee shot has no
setup block in the log and so no distance; it pairs by being its hole's first stroke. Only shots that
have no position yet are filled, so running it again, or over overlapping logs, changes nothing more.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .sources.gspro_log import M_TO_YD, GsproState, parse_events
from .store import Store

_HOLE = re.compile(r"Hole (\d+)")


@dataclass
class Fire:
    course: str | None
    to_pin_yd: float | None   # the setup's distance before this shot, when one was logged since the last fire
    start: tuple | None
    aim: tuple | None
    hole: int | None = None   # 1-based, from the next state line (which credits the shot)
    segment: int = 0          # which course load (or round restart) in the log it was played in


@dataclass
class LogRead:
    fires: list[Fire] = field(default_factory=list)
    # (course, hole list, index of the fires logged before it)
    layouts: list[tuple[str | None, list[dict], int]] = field(default_factory=list)


def read_log(text: str) -> LogRead:
    st, out = GsproState(), LogRead()
    tdist, pending, segment = None, None, 0
    for ev in parse_events(text):
        st.apply(ev)
        if ev.kind == "tdist":
            tdist = ev.to_pin
        elif ev.kind == "shot_fired":
            info = st.fire_info(st.fires)
            pending = Fire(st.course_display, tdist * M_TO_YD if tdist is not None else None,
                           info.get("start"), info.get("aim"), segment=segment)
            out.fires.append(pending)
            tdist = None
        elif ev.kind == "state" and pending is not None:
            pending.hole = ev.hole + 1 if ev.hole is not None else None
            pending = None
        elif ev.kind == "layout" and ev.holes:
            out.layouts.append((st.course_display, ev.holes, len(out.fires)))
        elif ev.kind in ("course", "end_round", "range"):
            tdist = pending = None
            segment += 1
    return out


def _score(f: Fire, row: sqlite3.Row, hole: int | None) -> float:
    if not f.course or (row["course"] or "").casefold() != f.course.casefold() or f.hole != hole:
        return 0.0
    if f.to_pin_yd is None:
        return 1.5 if (row["shot_number"] or 0) <= 1 else 0.0
    d = row["to_pin_yards"]
    if d is None:
        return 0.5
    return 2.0 if abs(d - f.to_pin_yd) <= max(0.3, 0.02 * f.to_pin_yd) else 0.0


MIN_SEGMENT_MATCH = 0.5   # of a course load's fires with a distance, the share that must match


def align(fires: list[Fire], rows: list[sqlite3.Row]) -> list[tuple[int, int]]:
    """(fire index, row index) pairs of the best in-order matching. A course load in the log is kept only
    when most of its shots match: a round the season never recorded can still pair a few shots with a
    later round's on the same holes by chance."""
    holes = [int(m.group(1)) if (m := _HOLE.match(r["hole"] or "")) else None for r in rows]
    n, m = len(fires), len(rows)
    # Rows of courses the log never played can't match; leave them out to keep the table small.
    courses = {f.course.casefold() for f in fires if f.course}
    keep = [j for j in range(m) if (rows[j]["course"] or "").casefold() in courses]
    k = len(keep)
    best = [[0.0] * (k + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        bi, bn = best[i], best[i + 1]
        for jj in range(k - 1, -1, -1):
            j = keep[jj]
            s = _score(fires[i], rows[j], holes[j])
            bi[jj] = max(bn[jj], bi[jj + 1], (bn[jj + 1] + s) if s else 0.0)
    pairs, i, jj = [], 0, 0
    while i < n and jj < k:
        j = keep[jj]
        s = _score(fires[i], rows[j], holes[j])
        if s and best[i][jj] == best[i + 1][jj + 1] + s:
            pairs.append((i, j))
            i, jj = i + 1, jj + 1
        elif best[i][jj] == best[i + 1][jj]:
            i += 1
        else:
            jj += 1
    seg_total, seg_hit = {}, {}
    for f in fires:
        if f.to_pin_yd is not None:
            seg_total[f.segment] = seg_total.get(f.segment, 0) + 1
    for i, _ in pairs:
        if fires[i].to_pin_yd is not None:
            seg_hit[fires[i].segment] = seg_hit.get(fires[i].segment, 0) + 1
    return [(i, j) for i, j in pairs
            if seg_hit.get(fires[i].segment, 0) >= MIN_SEGMENT_MATCH * seg_total.get(fires[i].segment, 0) > 0]


def backfill(db_path: str, logs: list[str], dry_run: bool = False) -> dict[str, int]:
    """Fill the season file from these logs; a dry run works on a temporary copy (opening the file
    upgrades its schema) and only counts."""
    if dry_run:
        with tempfile.TemporaryDirectory() as tmp:
            copy = shutil.copy(db_path, Path(tmp) / "season.db")
            return _backfill(str(copy), logs)
    return _backfill(db_path, logs)


def _backfill(db_path: str, logs: list[str]) -> dict[str, int]:
    store = Store(db_path, new_session=False)
    db = store.db
    stats = {"fires": 0, "matched": 0, "starts": 0, "aims": 0, "layouts": 0}
    for path in logs:
        rows = db.execute("SELECT id, session_id, ts, course, hole, shot_number, to_pin_yards, start_x, aim_map_x"
                          " FROM shots WHERE outcome IS NOT 'gimme' AND course != 'Practice' ORDER BY id").fetchall()
        with open(path, encoding="utf-8", errors="replace") as fh:
            got = read_log(fh.read())
        stats["fires"] += len(got.fires)
        pairs = align(got.fires, rows)
        stats["matched"] += len(pairs)
        row_of = {}
        for i, j in pairs:
            f, r = got.fires[i], rows[j]
            row_of[i] = r
            if f.start and r["start_x"] is None:
                stats["starts"] += 1
                db.execute("UPDATE shots SET start_x = ?, start_y = ?, start_z = ? WHERE id = ?", (*f.start, r["id"]))
            if f.aim and r["aim_map_x"] is None:
                stats["aims"] += 1
                db.execute("UPDATE shots SET aim_map_x = ?, aim_map_y = ? WHERE id = ?", (*f.aim, r["id"]))
        for course, holes, before in got.layouts:
            # The round's session is the one its last matched shot was stored in; a layout from a log
            # with no matching shots is kept without one (its tees still place tee shots).
            last = next((row_of[i] for i in range(before - 1, -1, -1) if i in row_of), None)
            if last is not None and (last["course"] or "").casefold() != (course or "").casefold():
                last = None
            stats["layouts"] += store.record_hole_layout(
                last["course"] if last is not None else course, holes,
                session_id=last["session_id"] if last is not None else None,
                ts=last["ts"] + 1 if last is not None else None)
    db.commit()
    db.close()
    return stats
